#!/usr/bin/env python3
"""
braidctl — btrfs raid1 の上に Drobo BeyondRAID 風の運用層を載せる。

  status [--json]      実容量 / 使える容量 / 使用量 / 保護状態 / ドライブ一覧
  add DEV [--force]    ドライブを追加して再配置（署名のあるドライブは拒否）
  remove ID|DEV        ドライブを安全に外す（外した後も収まるときだけ）
  monitor              timer から定期実行。脱落・故障の検知と自動再冗長化
  scrub                全データの検証（月次 timer）
  mount-degraded       1 台欠けた状態で縮退マウントする（手動用）

btrfs 本体がやること: チャンク単位の 2 重化、容量混在、オンライン増減。
このツールがやること: Drobo がファームウェアでやっている表示・検知・自動復旧・安全装置。
"""

import argparse
import fcntl
import json
import logging
import os
import re
import smtplib
import socket
import stat
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field, asdict
from email.message import EmailMessage
from pathlib import Path

LOG = logging.getLogger("braid")
CONFIG_PATH = "/etc/braid/braid.toml"

DEFAULT_CONFIG = {
    "mountpoint": "/srv/pool",
    "label": "braid",
    # 脱落を検知してから自動再冗長化を始めるまでの待ち時間。USB 瞬断で走らないための猶予
    "debounce_sec": 600,
    # 「外した後の使える容量 × margin >= 使用量」なら再構築可能と判定する
    "rebuild_margin": 0.95,
    "warn_percent": 90,
    # 縮退再マウントの間だけ止めるサービス
    "samba_units": ["smbd"],
    "state_file": "/var/lib/braid/state.json",
    "remind_interval_sec": 86400,
    "smart_args": ["-d", "sat"],
    # エラーカウンタが増え続ける「生きているが壊れかけ」のドライブを自動で外すか
    "auto_evict_on_errors": False,
    "error_threshold": 10,
    # add で絶対に触らないデバイス (/dev/disk/by-id の部分文字列)
    "protected_devices": [],
    "notify": {
        # nas-monitor と同じ Gmail SMTP 設定を流用する
        "config_file": "/etc/nas-monitor/config.json",
        "to": "",
        "subject_prefix": "[braid]",
    },
}


class BraidError(Exception):
    pass


# ---------------------------------------------------------------- utilities

def run(cmd, check=True, ok_codes=(0,), capture=True):
    LOG.debug("run: %s", " ".join(cmd))
    p = subprocess.run(cmd, text=True, capture_output=capture)
    if check and p.returncode not in ok_codes:
        msg = (p.stderr or p.stdout or "").strip()
        raise BraidError(f"{' '.join(cmd)} failed ({p.returncode}): {msg}")
    return p


def fmt_bytes(n):
    """Drobo 風に 10 進 (1 TB = 10^12) で表示する"""
    n = float(n)
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{int(n)} B"


def require_root():
    if os.geteuid() != 0:
        raise BraidError("root 権限が必要です (sudo で実行してください)")


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    p = Path(path)
    if p.exists():
        with p.open("rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def raid1_usable(sizes):
    """raid1 で 2 重化したときに格納できる論理容量 (チャンク割当の理想値)"""
    sizes = [s for s in sizes if s > 0]
    if len(sizes) < 2:
        return 0
    total = sum(sizes)
    return min(total // 2, total - max(sizes))


def lsblk(dev=None):
    cmd = ["lsblk", "-J", "-b", "-o",
           "PATH,PKNAME,MODEL,SERIAL,TRAN,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS"]
    if dev:
        cmd.append(dev)
    p = run(cmd, check=False)
    if p.returncode != 0:
        return []
    return json.loads(p.stdout).get("blockdevices", [])


def flatten_lsblk(nodes):
    for n in nodes:
        yield n
        yield from flatten_lsblk(n.get("children", []))


def parent_disk(path):
    """パーティションなら親ディスク、ディスクならそのまま"""
    p = run(["lsblk", "-no", "PKNAME", path], check=False)
    pk = (p.stdout or "").strip().splitlines()
    return f"/dev/{pk[0]}" if pk and pk[0] else path


# ---------------------------------------------------------------- state

class State:
    """/var/lib/braid/state.json — 脱落の初回検知時刻、エラーカウンタ基準値、通知抑制、在庫"""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {"missing": {}, "err_base": {}, "last_notify": {}, "inventory": {},
                     "rebuild": None, "smart_alerted": {}}
        self._lock_fh = None

    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fh = open(self.path.with_suffix(".lock"), "w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BraidError("別の braidctl が実行中です (monitor/rebuild)")

    def load(self):
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except json.JSONDecodeError:
                LOG.warning("state file が壊れています。初期化します: %s", self.path)
        return self

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)

    def should_notify(self, key, interval):
        last = self.data["last_notify"].get(key, 0)
        if time.time() - last >= interval:
            self.data["last_notify"][key] = time.time()
            return True
        return False

    def clear_notify(self, key):
        self.data["last_notify"].pop(key, None)


# ---------------------------------------------------------------- notifier

class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg["notify"]
        self.host = socket.gethostname()
        self.email = None
        try:
            src = json.loads(Path(self.cfg["config_file"]).read_text())
            self.email = src["notification"]["email"]
        except (OSError, KeyError, json.JSONDecodeError) as e:
            LOG.warning("通知設定を読めません (%s)。メール通知は無効", e)

    def send(self, subject, body):
        LOG.info("NOTIFY %s\n%s", subject, body)
        if not self.email:
            return
        e = self.email
        msg = EmailMessage()
        msg["Subject"] = f"{self.cfg['subject_prefix']} {self.host}: {subject}"
        msg["From"] = e["from"]
        msg["To"] = self.cfg["to"] or e["to"]
        msg.set_content(body + f"\n\n-- braidctl on {self.host} {time.strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            with smtplib.SMTP(e["smtp_server"], e["smtp_port"], timeout=30) as s:
                if e.get("use_tls", True):
                    s.starttls()
                s.login(e["username"], e["password"])
                s.send_message(msg)
        except (OSError, smtplib.SMTPException) as ex:
            LOG.error("メール送信に失敗: %s", ex)


# ---------------------------------------------------------------- btrfs

@dataclass
class Device:
    devid: int
    path: str          # 欠けている場合は ''
    size: int
    used: int          # このデバイス上に割り当て済みの生バイト数
    missing: bool = False
    present: bool = True   # デバイスノードが存在するか
    model: str = ""
    serial: str = ""
    tran: str = ""
    errors: dict = field(default_factory=dict)
    smart: dict = field(default_factory=dict)

    @property
    def name(self):
        return os.path.basename(self.path) if self.path else f"devid:{self.devid}"


@dataclass
class Usage:
    raw: int            # Device size (全ドライブ合計)
    used: int           # データ+メタデータの論理使用量
    data_used: int
    meta_used: int
    free: int           # Free (estimated)
    missing_bytes: int
    profiles: dict      # {"Data": "RAID1", "Metadata": "RAID1", "System": "RAID1"}
    multiple_profiles: bool

    @property
    def usable(self):
        return self.used + self.free


class Pool:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mnt = cfg["mountpoint"]
        self.label = cfg["label"]

    # -- mount state
    def mount_entry(self):
        with open("/proc/self/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[1] == self.mnt:
                    return {"source": parts[0], "fstype": parts[2], "options": parts[3].split(",")}
        return None

    def is_mounted(self):
        m = self.mount_entry()
        return bool(m and m["fstype"] == "btrfs")

    def uuid(self):
        p = run(["findmnt", "-no", "UUID", self.mnt], check=False)
        return (p.stdout or "").strip()

    def member_devices_present(self):
        """マウントの有無に関わらず、ラベル一致の btrfs メンバーとして見えているデバイス"""
        out = []
        for n in flatten_lsblk(lsblk()):
            if n.get("fstype") == "btrfs" and n.get("label") == self.label:
                out.append(n["path"])
        return out

    @staticmethod
    def superblock(path):
        """デバイスのスーパーブロックから (fsid, devid) を読む"""
        p = run(["btrfs", "inspect-internal", "dump-super", path], check=False)
        fs = re.search(r"^fsid\s+(\S+)", p.stdout, re.M)
        dv = re.search(r"dev_item\.devid\s+(\d+)", p.stdout)
        return (fs[1] if fs else None, int(dv[1]) if dv else None)

    def returned_members(self, report):
        """欠けている devid のうち、デバイスとして再び見えているもの → [(path, devid)]。
        既に外し終えた古いメンバー (devid が fs に無い) は stale として別に返す"""
        gone_ids = {d.devid for d in report.gone}
        fs_paths = {os.path.realpath(d.path) for d in report.devs if d.present}
        uuid = self.uuid()
        returned, stale = [], []
        for path in self.member_devices_present():
            if os.path.realpath(path) in fs_paths:
                continue
            fsid, devid = self.superblock(path)
            if fsid != uuid:
                continue
            (returned if devid in gone_ids else stale).append((path, devid))
        return returned, stale

    # -- queries (mounted)
    def devices(self):
        p = run(["btrfs", "filesystem", "show", "--raw", self.mnt])
        devs = []
        for line in p.stdout.splitlines():
            m = re.search(r"devid\s+(\d+)\s+size\s+(\d+)\s+used\s+(\d+)\s+path\s*(.*?)\s*(MISSING)?\s*$", line)
            if not m:
                continue
            path = m[4].strip()
            if path.startswith("<") or path == "MISSING":   # "<missing disk>"
                path = ""
            d = Device(devid=int(m[1]), size=int(m[2]), used=int(m[3]), path=path,
                       missing=bool(m[5]) or not path)
            devs.append(d)
        # sysfs の missing フラグも見る（progs の表示より確実）
        uuid = self.uuid()
        for d in devs:
            f = Path(f"/sys/fs/btrfs/{uuid}/devinfo/{d.devid}/missing")
            if f.exists() and f.read_text().strip() == "1":
                d.missing = True
            d.present = bool(d.path) and os.path.exists(d.path) and not d.missing
            if d.present:
                info = next(iter(lsblk(d.path)), {})
                disk = next(iter(lsblk(parent_disk(d.path))), {})
                d.model = (disk.get("model") or info.get("model") or "").strip()
                d.serial = (disk.get("serial") or "").strip()
                d.tran = disk.get("tran") or ""
        devs.sort(key=lambda d: d.devid)
        return devs

    def usage(self):
        p = run(["btrfs", "filesystem", "usage", "-b", self.mnt])
        overall = {}
        profiles = {}
        data_used = meta_used = 0
        for line in p.stdout.splitlines():
            m = re.match(r"\s+([A-Za-z ,()]+?):\s+(-?\d+)", line)
            if m:
                overall[m[1].strip()] = int(m[2])
                continue
            m = re.match(r"(Data|Metadata|System),(\S+): Size:(\d+), Used:(\d+)", line)
            if m:
                kind, prof, used = m[1], m[2], int(m[4])
                # 同種で複数プロファイルがある場合は連結して表示
                profiles[kind] = prof if kind not in profiles else f"{profiles[kind]}+{prof}"
                if kind == "Data":
                    data_used += used
                else:
                    meta_used += used
        multi = bool(re.search(r"Multiple profiles:\s+yes", p.stdout))
        return Usage(
            raw=overall.get("Device size", 0),
            used=data_used + meta_used,
            data_used=data_used, meta_used=meta_used,
            free=overall.get("Free (estimated)", 0),
            missing_bytes=overall.get("Device missing", 0),
            profiles=profiles, multiple_profiles=multi,
        )

    def dev_stats(self):
        p = run(["btrfs", "device", "stats", self.mnt], check=False)
        stats = {}
        for line in p.stdout.splitlines():
            m = re.match(r"\[(.+?)\]\.(\w+)\s+(\d+)", line)
            if m:
                stats.setdefault(m[1], {})[m[2]] = int(m[3])
        return stats

    def balance_running(self):
        p = run(["btrfs", "balance", "status", self.mnt], check=False)
        return "running" in p.stdout or "paused" in p.stdout

    def scrub_status(self):
        p = run(["btrfs", "scrub", "status", self.mnt], check=False)
        return p.stdout

    def smart(self, dev):
        if not dev.present:
            return {}
        disk = parent_disk(dev.path)
        p = run(["smartctl", "-j", "-H", "-A"] + list(self.cfg["smart_args"]) + [disk], check=False)
        try:
            j = json.loads(p.stdout)
        except json.JSONDecodeError:
            return {}
        attrs = {a["id"]: a.get("raw", {}).get("value") for a in
                 j.get("ata_smart_attributes", {}).get("table", [])}
        return {
            "passed": j.get("smart_status", {}).get("passed"),
            "temp": j.get("temperature", {}).get("current"),
            "reallocated": attrs.get(5),
            "pending": attrs.get(197),
            "offline_uncorr": attrs.get(198),
            "power_on_hours": j.get("power_on_time", {}).get("hours"),
        }

    def enrich(self, devs):
        stats = self.dev_stats()
        for d in devs:
            d.errors = stats.get(d.path) or stats.get(f"devid:{d.devid}") or {}
            d.smart = self.smart(d)
        return devs

    # -- mutations
    def in_fstab(self):
        return run(["findmnt", "--fstab", "-n", self.mnt], check=False).returncode == 0

    def mount(self, degraded=False, check=True):
        """fstab にあればその設定で、無ければ LABEL 指定でマウントする"""
        cmd = ["mount"]
        if self.in_fstab():
            if degraded:
                cmd += ["-o", "degraded"]
            cmd.append(self.mnt)
        else:
            cmd += ["-o", "degraded,noatime" if degraded else "noatime", f"LABEL={self.label}", self.mnt]
        return run(cmd, check=check)

    def umount(self, retries=3):
        for i in range(retries):
            p = run(["umount", self.mnt], check=False)
            if p.returncode == 0:
                return
            LOG.warning("umount 失敗 (%d/%d): %s", i + 1, retries, p.stderr.strip())
            time.sleep(5)
        raise BraidError(f"{self.mnt} を umount できません (使用中)")

    def remove_device(self, target, stream=False):
        cmd = ["btrfs", "device", "remove", str(target), self.mnt]
        return run(cmd, check=False, capture=not stream)

    def balance_soft_convert(self):
        """縮退中に作られた不完全なチャンクを raid1 に揃える"""
        run(["btrfs", "balance", "start", "--bg", "-dconvert=raid1,soft", "-mconvert=raid1,soft", self.mnt],
            check=False)

    def compact(self, usage=50):
        """スカスカのチャンクを詰めて未割当領域を作る (remove の前に行うと ENOSPC を避けやすい)。同期実行"""
        LOG.info("compaction balance (usage<%d%%) 実行中", usage)
        run(["btrfs", "balance", "start", f"-dusage={usage}", f"-musage={usage}", self.mnt], check=False)


# ---------------------------------------------------------------- analysis

@dataclass
class Report:
    devs: list
    usage: Usage
    mounted_degraded: bool
    balance_running: bool

    @property
    def gone(self):
        return [d for d in self.devs if d.missing or not d.present]

    @property
    def healthy_sizes(self):
        return [d.size for d in self.devs if d not in self.gone]

    def usable_after_losing(self, dev):
        return raid1_usable([d.size for d in self.devs if d is not dev and d not in self.gone])

    def state_label(self):
        u = self.usage
        if self.gone:
            return "DEGRADED"
        if self.balance_running or u.multiple_profiles:
            return "REBUILDING"
        profs = set(u.profiles.values())
        if profs and all(p.startswith("RAID1") for p in profs):
            return "PROTECTED"
        return "UNPROTECTED"


def collect(pool):
    devs = pool.enrich(pool.devices())
    m = pool.mount_entry() or {"options": []}
    return Report(devs=devs, usage=pool.usage(),
                  mounted_degraded="degraded" in m["options"],
                  balance_running=pool.balance_running())


def can_rebuild(report, cfg, losing=None):
    """losing (list[Device]) を失った状態で再冗長化できるか。戻り値 (ok, usable_after, needed)"""
    losing = losing or report.gone
    exclude = {d.devid for d in report.gone} | {d.devid for d in losing}
    sizes = [d.size for d in report.devs if d.devid not in exclude]
    usable_after = raid1_usable(sizes)
    needed = report.usage.used
    return needed <= usable_after * cfg["rebuild_margin"], usable_after, needed


# ---------------------------------------------------------------- status

def render_status(report, cfg, state=None):
    u = report.usage
    lines = []
    label = report.state_label()
    lines.append(f"braid  {cfg['mountpoint']}   {label} ({'/'.join(sorted(set(u.profiles.values())) or ['-'])})"
                 + ("   [mount option: degraded]" if report.mounted_degraded else ""))
    pct = (u.used / u.usable * 100) if u.usable else 0
    lines.append(f"実容量 {fmt_bytes(u.raw):>10}   使える容量 {fmt_bytes(u.usable):>10}   "
                 f"使用 {fmt_bytes(u.used):>10} ({pct:.0f}%)   空き {fmt_bytes(u.free):>10}")
    lines.append("")
    lines.append(f"{'ID':>2}  {'device':8} {'model':22} {'size':>9} {'alloc':>9}  {'errors':>6}  health")
    inv = (state.data.get("inventory", {}) if state else {})
    for d in report.devs:
        errs = sum(d.errors.values()) if d.errors else 0
        if d.missing or not d.present:
            known = inv.get(str(d.devid), {})
            health = "MISSING"
            model = known.get("model", "?")
            name = f"(was {os.path.basename(known.get('path', '') or d.path or '?')})"
        else:
            model = d.model
            name = d.name
            s = d.smart
            if not s or s.get("passed") is None:
                health = "SMART n/a"
            elif s.get("passed") is False:
                health = "SMART FAIL"
            else:
                health = "OK"
                bad = [k for k in ("reallocated", "pending", "offline_uncorr") if (s.get(k) or 0) > 0]
                if bad:
                    health = "WARN " + " ".join(f"{k}={s[k]}" for k in bad)
                if s.get("temp") is not None:
                    health += f" {s['temp']}°C"
        lines.append(f"{d.devid:>2}  {name:8} {model[:22]:22} {fmt_bytes(d.size):>9} {fmt_bytes(d.used):>9}  "
                     f"{errs:>6}  {health}")
    lines.append("")
    if report.gone:
        ok, after, needed = can_rebuild(report, cfg)
        if ok:
            lines.append(f"! {len(report.gone)} 台が欠けています。残りで再冗長化可能 "
                         f"(使える容量 {fmt_bytes(after)} >= 使用 {fmt_bytes(needed)})。monitor が自動で実行します")
        else:
            short = needed / cfg["rebuild_margin"] - after
            lines.append(f"!! {len(report.gone)} 台が欠けています。残りの容量では再冗長化できません "
                         f"(使える容量 {fmt_bytes(after)} < 使用 {fmt_bytes(needed)})。"
                         f"あと {fmt_bytes(short)} 分のドライブを追加してください")
    else:
        healthy = [d for d in report.devs if d.present]
        if len(healthy) >= 2:
            largest = max(healthy, key=lambda d: d.size)
            after = report.usable_after_losing(largest)
            if u.used <= after * cfg["rebuild_margin"]:
                lines.append(f"どの 1 台を失っても再冗長化できます "
                             f"(最大の {largest.name} {fmt_bytes(largest.size)} を失った場合の使える容量 {fmt_bytes(after)} "
                             f">= 使用 {fmt_bytes(u.used)})")
            else:
                tol = [d.name for d in healthy if u.used <= report.usable_after_losing(d) * cfg["rebuild_margin"]]
                lines.append(f"注意: {largest.name} ({fmt_bytes(largest.size)}) を失うと再冗長化できません "
                             f"(使える容量 {fmt_bytes(after)} < 使用 {fmt_bytes(u.used)})。"
                             + (f"自動復旧できるのは {', '.join(tol)} の脱落のみ" if tol else "どの 1 台の脱落も自動復旧できません")
                             + "。ドライブの追加を検討してください")
    if pct >= cfg["warn_percent"]:
        lines.append(f"警告: 使用率 {pct:.0f}% です。空きが少ないと再構築 (balance) が失敗します")
    if report.balance_running:
        lines.append("balance 実行中 (再配置中)。完了まで性能が落ちます")
    return "\n".join(lines)


def cmd_status(cfg, args):
    require_root()
    pool = Pool(cfg)
    if not pool.is_mounted():
        present = pool.member_devices_present()
        raise BraidError(f"{cfg['mountpoint']} はマウントされていません。"
                         f"見えているメンバー: {', '.join(present) or 'なし'}"
                         + ("。1 台欠けているなら `braidctl mount-degraded`" if present else ""))
    report = collect(pool)
    state = State(cfg["state_file"]).load()
    update_inventory(state, report.devs)   # 脱落後に「何が欠けたか」を出せるよう在庫を更新
    try:
        state.save()
    except OSError:
        pass
    if args.json:
        out = {"state": report.state_label(), "usage": asdict(report.usage),
               "devices": [asdict(d) for d in report.devs],
               "mounted_degraded": report.mounted_degraded, "balance_running": report.balance_running}
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(render_status(report, cfg, state))


# ---------------------------------------------------------------- add / remove

def check_addable(cfg, pool, dev, force):
    dev = os.path.realpath(dev)
    try:
        st = os.stat(dev)
    except FileNotFoundError:
        raise BraidError(f"{dev} が存在しません")
    if not stat.S_ISBLK(st.st_mode):
        raise BraidError(f"{dev} はブロックデバイスではありません")

    # ルート FS / 既存マウント / holders (LUKS 等で使用中) は force でも拒否
    root_src = run(["findmnt", "-no", "SOURCE", "/"]).stdout.strip()
    root_disk = parent_disk(os.path.realpath(root_src))
    if parent_disk(dev) == root_disk:
        raise BraidError(f"{dev} はシステムディスク ({root_disk}) です。拒否")
    nodes = list(flatten_lsblk(lsblk(dev)))
    for n in nodes:
        if any(n.get("mountpoints") or []):
            raise BraidError(f"{n['path']} はマウント中 ({n['mountpoints']}) です。拒否")
        holders = Path(f"/sys/class/block/{os.path.basename(n['path'])}/holders")
        if holders.exists() and any(holders.iterdir()):
            raise BraidError(f"{n['path']} は他のデバイス (LUKS/LVM 等) に使われています。拒否")
    byid = Path("/dev/disk/by-id")
    for link in byid.iterdir() if byid.exists() else []:
        if os.path.realpath(link) == dev:
            for pat in cfg["protected_devices"]:
                if pat in link.name:
                    raise BraidError(f"{dev} ({link.name}) は protected_devices に一致します。拒否")
    if dev in [d.path for d in pool.devices()]:
        raise BraidError(f"{dev} は既にプールのメンバーです")

    # 署名・パーティションがあれば --force が必要
    sigs = run(["wipefs", "-n", dev], check=False).stdout.strip()
    children = [n["path"] for n in nodes if n["path"] != dev]
    if (sigs or children) and not force:
        raise BraidError(f"{dev} には既存のデータがあります ({'パーティション: ' + ', '.join(children) if children else ''}"
                         f"{' ' if children and sigs else ''}{'署名: ' + sigs.splitlines()[-1] if sigs else ''}). "
                         f"消してよければ --force を付けてください")
    return dev, children


def cmd_add(cfg, args):
    require_root()
    pool = Pool(cfg)
    if not pool.is_mounted():
        raise BraidError(f"{cfg['mountpoint']} がマウントされていません")
    if pool.balance_running():
        raise BraidError("balance 実行中です。完了後にやり直してください")
    dev, children = check_addable(cfg, pool, args.device, args.force)
    before = pool.usage()
    if args.force:
        for c in children:
            run(["wipefs", "-a", c])
        run(["wipefs", "-a", dev])
    print(f"== {dev} を追加")
    run(["btrfs", "device", "add", "-f", dev, cfg["mountpoint"]], capture=False)
    print("== 再配置 (balance) をバックグラウンドで開始")
    run(["btrfs", "balance", "start", "--bg", "--full-balance", cfg["mountpoint"]], check=False, capture=False)
    time.sleep(2)
    after = pool.usage()
    print(f"使える容量: {fmt_bytes(before.usable)} -> {fmt_bytes(after.usable)} (balance 完了後に確定)")
    st = State(cfg["state_file"]).load()
    update_inventory(st, pool.devices())
    st.save()


def resolve_target(pool, target):
    devs = pool.devices()
    if target.isdigit():
        for d in devs:
            if d.devid == int(target):
                return d
        raise BraidError(f"devid {target} はありません")
    real = os.path.realpath(target)
    for d in devs:
        if d.path and os.path.realpath(d.path) == real:
            return d
    raise BraidError(f"{target} はプールのメンバーではありません")


def cmd_remove(cfg, args):
    require_root()
    pool = Pool(cfg)
    if not pool.is_mounted():
        raise BraidError(f"{cfg['mountpoint']} がマウントされていません")
    if pool.balance_running():
        raise BraidError("balance 実行中です。完了後にやり直してください")
    report = collect(pool)
    if args.target == "missing":
        if not report.gone:
            raise BraidError("欠けているデバイスはありません")
        target, losing = "missing", report.gone
    else:
        d = resolve_target(pool, args.target)
        target, losing = d.devid, [d]
    ok, after, needed = can_rebuild(report, cfg, losing=losing)
    names = ", ".join(f"devid {d.devid} ({d.name}, {fmt_bytes(d.size)})" for d in losing)
    if not ok:
        short = needed / cfg["rebuild_margin"] - after
        raise BraidError(f"{names} を外すと残りに収まりません "
                         f"(外した後の使える容量 {fmt_bytes(after)} < 使用 {fmt_bytes(needed)})。"
                         f"先に {fmt_bytes(short)} 以上のドライブを追加してください")
    print(f"== {names} を外します (外した後の使える容量 {fmt_bytes(after)}、使用 {fmt_bytes(needed)})")
    print("   データを他のドライブへ移すため時間がかかります (Ctrl-C しても btrfs 側の処理は続きます)")
    pool.compact()
    p = pool.remove_device(target, stream=True)
    if p.returncode != 0:
        raise BraidError(f"btrfs device remove が失敗しました (exit {p.returncode})。"
                         "ENOSPC の場合は残りドライブの未割当領域が足りません: "
                         f"`btrfs balance start --full-balance {cfg['mountpoint']}` 後に再試行するか、ドライブを追加してください")
    print("== 完了")
    print(render_status(collect(pool), cfg))


# ---------------------------------------------------------------- monitor

def update_inventory(state, devs):
    inv = state.data.setdefault("inventory", {})
    for d in devs:
        if d.present:
            inv[str(d.devid)] = {"path": d.path, "model": d.model, "serial": d.serial, "size": d.size}


def describe(dev, state):
    known = state.data.get("inventory", {}).get(str(dev.devid), {})
    return (f"devid {dev.devid} {known.get('model') or dev.model or '?'} "
            f"{fmt_bytes(known.get('size') or dev.size)} ({os.path.basename(known.get('path') or dev.path or '?')})")


def ensure_mounted(cfg, pool, notifier, state):
    """未マウントなら通常マウント → 駄目なら縮退マウント。戻り値: マウントできたか"""
    if pool.is_mounted():
        return True
    present = pool.member_devices_present()
    if not present:
        if state.should_notify("no-members", cfg["remind_interval_sec"]):
            notifier.send("プールのドライブが 1 台も見えません",
                          f"{cfg['mountpoint']} は未マウントで、LABEL={cfg['label']} の btrfs メンバーが見つかりません。\n"
                          "USB ハブ・電源を確認してください。")
        return False
    p = pool.mount(check=False)
    if p.returncode == 0:
        LOG.info("通常マウントに成功")
        state.clear_notify("no-members")
        return True
    LOG.warning("通常マウント失敗: %s — 縮退マウントを試みます", p.stderr.strip())
    p = pool.mount(degraded=True, check=False)
    if p.returncode != 0:
        if state.should_notify("mount-failed", cfg["remind_interval_sec"]):
            notifier.send("プールをマウントできません",
                          f"通常マウントも縮退マウントも失敗しました。\n見えているメンバー: {', '.join(present)}\n\n{p.stderr}")
        return False
    notifier.send("縮退状態でマウントしました",
                  f"{cfg['mountpoint']} を 1 台欠けた状態 (degraded) でマウントしました。\n"
                  f"見えているメンバー: {', '.join(present)}\n"
                  f"{cfg['debounce_sec']} 秒以内にドライブが戻らなければ、容量が足りる場合は自動で再冗長化します。")
    return True


def rebuild_missing(cfg, pool, notifier, state, report):
    """欠けたデバイスを外して残りで raid1 を組み直す（Drobo の再レイアウト相当）"""
    gone = report.gone
    names = "\n".join("  - " + describe(d, state) for d in gone)
    ok, after, needed = can_rebuild(report, cfg)
    state.data["rebuild"] = {"started": time.time(), "devids": [d.devid for d in gone]}
    state.save()
    notifier.send("自動再冗長化を開始します",
                  f"欠けたドライブ:\n{names}\n\n残り {len(report.devs) - len(gone)} 台で再冗長化します。\n"
                  f"外した後の使える容量 {fmt_bytes(after)} / 使用 {fmt_bytes(needed)}\n"
                  "完了まで時間がかかります。完了時に再度通知します。")
    try:
        pool.compact()
        # 1) ライブのまま devid 指定で外せるか試す（カーネルが missing を把握している場合）
        live_ok = True
        for d in gone:
            p = pool.remove_device(d.devid)
            if p.returncode != 0:
                LOG.info("ライブでの remove 失敗 (devid %d): %s", d.devid, (p.stderr or p.stdout).strip())
                live_ok = False
                break
        # 2) 駄目なら短時間止めて縮退再マウント → remove missing
        if not live_ok:
            LOG.info("縮退再マウントに切り替え: %s 停止", cfg["samba_units"])
            for u in cfg["samba_units"]:
                run(["systemctl", "stop", u], check=False)
            try:
                pool.umount()
                pool.mount(degraded=True)
            finally:
                for u in cfg["samba_units"]:
                    run(["systemctl", "start", u], check=False)
            for _ in range(len(gone)):
                p = pool.remove_device("missing")
                if p.returncode != 0:
                    raise BraidError(f"btrfs device remove missing: {(p.stderr or p.stdout).strip()}")
        # 3) 縮退中に作られた不完全チャンクがあれば raid1 に揃える
        if pool.usage().multiple_profiles:
            LOG.info("複数プロファイル検出 → soft convert balance")
            pool.balance_soft_convert()
    except BraidError as e:
        state.data["rebuild"] = None
        state.save()
        notifier.send("自動再冗長化に失敗しました", f"{e}\n\nプールは縮退状態のまま動作しています。手動確認が必要です:\n"
                      f"  sudo braidctl status\n  sudo btrfs device remove missing {cfg['mountpoint']}")
        return
    for d in gone:
        state.data["missing"].pop(str(d.devid), None)
        state.data["inventory"].pop(str(d.devid), None)
    state.data["rebuild"] = None
    state.save()
    new = collect(pool)
    notifier.send("自動再冗長化が完了しました",
                  f"外したドライブ:\n{names}\n\n" + render_status(new, cfg, state))


def reattach(cfg, pool, notifier, state, returned):
    """一時的に外れていたメンバーが戻ってきた: 再マウントで組み戻し、古くなったコピーを scrub で同期する"""
    names = ", ".join(f"devid {devid} ({os.path.basename(p)})" for p, devid in returned)
    LOG.info("復帰したメンバーを組み戻します: %s", names)
    for u in cfg["samba_units"]:
        run(["systemctl", "stop", u], check=False)
    try:
        pool.umount()
        for p, _ in returned:
            run(["btrfs", "device", "scan", p], check=False)
        if pool.mount(check=False).returncode != 0:
            LOG.warning("通常マウント失敗 → 縮退マウント")
            pool.mount(degraded=True)
    except BraidError as e:
        notifier.send("復帰したドライブの組み戻しに失敗しました", f"{names}\n{e}")
        return False
    finally:
        for u in cfg["samba_units"]:
            run(["systemctl", "start", u], check=False)
    new = collect(pool)
    still = {d.devid for d in new.gone} & {devid for _, devid in returned}
    if still:
        notifier.send("復帰したドライブを組み戻せませんでした",
                      f"{names} のうち devid {sorted(still)} はまだ欠けたままです。\n\n" + render_status(new, cfg, state))
        return False
    for _, devid in returned:
        state.data["missing"].pop(str(devid), None)
    state.clear_notify("cannot-rebuild")
    # 外れていた間に書かれたデータはそのドライブに無いので、scrub でミラーから補完する
    run(["btrfs", "scrub", "start", cfg["mountpoint"]], check=False)
    notifier.send("ドライブが復帰しました (組み戻し完了)",
                  f"{names} をプールに戻しました。外れていた間の差分を scrub で同期しています。\n\n"
                  + render_status(new, cfg, state))
    return True


def cmd_monitor(cfg, args):
    require_root()
    pool = Pool(cfg)
    notifier = Notifier(cfg)
    state = State(cfg["state_file"])
    state.lock()
    state.load()
    now = time.time()
    debounce = args.debounce if args.debounce is not None else cfg["debounce_sec"]

    if not ensure_mounted(cfg, pool, notifier, state):
        state.save()
        return
    report = collect(pool)
    update_inventory(state, report.devs)
    missing = state.data["missing"]

    # --- 一時的に外れたメンバーが戻ってきた → 再構築せず組み戻す ---
    returned, stale = pool.returned_members(report)
    if returned and not report.balance_running and state.data.get("rebuild") is None:
        if reattach(cfg, pool, notifier, state, returned):
            report = collect(pool)
    for p, devid in stale:
        if state.should_notify(f"stale-{p}", cfg["remind_interval_sec"]):
            notifier.send("古いメンバーのドライブが接続されています",
                          f"{p} (旧 devid {devid}) は既にプールから外されたドライブです。\n"
                          f"再利用するなら: sudo braidctl add --force {p}")

    # --- 脱落 (デバイスが消えた) ---
    gone_ids = {str(d.devid) for d in report.gone}
    for d in report.gone:
        key = str(d.devid)
        if key not in missing:
            missing[key] = now
            notifier.send("ドライブの脱落を検知しました",
                          f"{describe(d, state)} が見えなくなりました。\n"
                          f"{int(debounce)} 秒待って戻らなければ、容量が足りる場合は自動で再冗長化します。\n\n"
                          + render_status(report, cfg, state))
    for key in list(missing):
        if key not in gone_ids:
            LOG.info("devid %s が復帰", key)
            del missing[key]
            notifier.send("ドライブが復帰しました", f"devid {key} が再び見えています。再構築は行いません。\n\n"
                          + render_status(report, cfg, state))
            state.clear_notify(f"cannot-rebuild")

    # --- エラーカウンタ / SMART ---
    base = state.data["err_base"]
    failing = []
    for d in report.devs:
        if not d.present:
            continue
        key = str(d.devid)
        prev = base.get(key, {})
        delta = {k: v - prev.get(k, 0) for k, v in d.errors.items() if v - prev.get(k, 0) > 0}
        base[key] = d.errors
        if delta and state.should_notify(f"errors-{key}", cfg["remind_interval_sec"]):
            notifier.send("ドライブの I/O エラーが増えています",
                          f"{describe(d, state)}: " + ", ".join(f"{k}+{v}" for k, v in delta.items())
                          + f"\n累計: {d.errors}\nSMART: {d.smart}")
        if sum(delta.values()) >= cfg["error_threshold"]:
            failing.append(d)
        # SMART: 健康判定 FAIL、保留セクタあり、再割当セクタが前回より増えた、のいずれかで警告。
        # 古いドライブの「再割当 1 個で安定」を毎日警告しないよう、再割当は増分で見る
        s = d.smart
        sbase = state.data.setdefault("smart_base", {})
        prev_realloc = sbase.get(key, {}).get("reallocated")
        realloc = s.get("reallocated") if s else None
        grew = realloc is not None and prev_realloc is not None and realloc > prev_realloc
        if s:
            sbase[key] = {"reallocated": realloc}
        smart_bad = s and (s.get("passed") is False or (s.get("pending") or 0) > 0
                           or (s.get("offline_uncorr") or 0) > 0 or grew)
        if smart_bad and state.should_notify(f"smart-{key}", cfg["remind_interval_sec"]):
            notifier.send("SMART が劣化を示しています",
                          f"{describe(d, state)}: {s}\n"
                          + (f"再割当セクタが {prev_realloc} → {realloc} に増えました。\n" if grew else "")
                          + "早めに `braidctl remove` で外して交換することを勧めます。")

    # --- 自動再冗長化 ---
    expired = [d for d in report.gone if now - missing.get(str(d.devid), now) >= debounce]
    if cfg["auto_evict_on_errors"] and failing and not report.gone:
        # 生きているが壊れかけ: ライブで外す（読めない部分はミラーから読まれる）
        d = failing[0]
        ok, after, needed = can_rebuild(report, cfg, losing=[d])
        if ok:
            notifier.send("故障しかけのドライブを自動で外します", f"{describe(d, state)}\n外した後の使える容量 {fmt_bytes(after)}")
            p = pool.remove_device(d.devid)
            notifier.send("自動退避 " + ("完了" if p.returncode == 0 else "失敗"),
                          (p.stderr or p.stdout or "") + "\n\n" + render_status(collect(pool), cfg, state))
    elif expired and not report.balance_running:
        ok, after, needed = can_rebuild(report, cfg)
        if ok:
            rebuild_missing(cfg, pool, notifier, state, report)
            report = collect(pool)
        elif state.should_notify("cannot-rebuild", cfg["remind_interval_sec"]):
            short = needed / cfg["rebuild_margin"] - after
            notifier.send("ドライブの追加が必要です (再冗長化できません)",
                          f"欠けたドライブ:\n" + "\n".join("  - " + describe(d, state) for d in expired)
                          + f"\n\n残りの使える容量 {fmt_bytes(after)} < 使用 {fmt_bytes(needed)} のため、"
                          f"このままでは冗長性を回復できません。\n"
                          f"あと {fmt_bytes(short)} 分のドライブを追加してください: sudo braidctl add /dev/sdX\n"
                          "データは現在も読み書きできますが、もう 1 台失うと消えます。\n\n"
                          + render_status(report, cfg, state))

    # --- 容量警告 ---
    u = report.usage
    pct = (u.used / u.usable * 100) if u.usable else 0
    if pct >= cfg["warn_percent"] and state.should_notify("capacity", cfg["remind_interval_sec"]):
        notifier.send(f"プールの使用率が {pct:.0f}% です", render_status(report, cfg, state))
    elif pct < cfg["warn_percent"]:
        state.clear_notify("capacity")

    state.save()
    LOG.info("monitor: %s used=%s usable=%s gone=%d", report.state_label(),
             fmt_bytes(u.used), fmt_bytes(u.usable), len(report.gone))


# ---------------------------------------------------------------- scrub / mount

def cmd_scrub(cfg, args):
    require_root()
    pool = Pool(cfg)
    notifier = Notifier(cfg)
    if not pool.is_mounted():
        raise BraidError(f"{cfg['mountpoint']} がマウントされていません")
    if "running" in pool.scrub_status():
        raise BraidError("scrub は既に実行中です")
    LOG.info("scrub 開始")
    p = run(["btrfs", "scrub", "start", "-B", "-d", cfg["mountpoint"]], check=False)
    status = pool.scrub_status()
    errs = re.search(r"Error summary:\s+(.*)", status)
    summary = errs[1].strip() if errs else "unknown"
    ok = p.returncode == 0 and (summary.startswith("no errors") or summary == "")
    body = f"exit={p.returncode}\n{p.stdout}\n{status}"
    if ok:
        LOG.info("scrub 完了: %s", summary)
        if not args.quiet:
            notifier.send("scrub 完了 (エラーなし)", body)
    else:
        notifier.send("scrub でエラーが見つかりました", body)
        sys.exit(1)


def cmd_mount_degraded(cfg, args):
    require_root()
    pool = Pool(cfg)
    if pool.is_mounted():
        print("既にマウントされています")
        return
    present = pool.member_devices_present()
    if not present:
        raise BraidError(f"LABEL={cfg['label']} のメンバーが 1 台も見えません")
    pool.mount(degraded=True)
    print(f"縮退マウントしました (メンバー: {', '.join(present)})")
    print(render_status(collect(pool), cfg))


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="braidctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=CONFIG_PATH)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="プールの状態")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("add", help="ドライブを追加")
    s.add_argument("device")
    s.add_argument("--force", action="store_true", help="既存の署名・パーティションを消して追加")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("remove", help="ドライブを外す (devid / デバイスパス / missing)")
    s.add_argument("target")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("monitor", help="検知と自動再冗長化 (timer 用)")
    s.add_argument("--debounce", type=int, default=None, help="秒 (設定値を上書き。検証用)")
    s.set_defaults(fn=cmd_monitor)

    s = sub.add_parser("scrub", help="全データ検証")
    s.add_argument("--quiet", action="store_true", help="成功時は通知しない")
    s.set_defaults(fn=cmd_scrub)

    s = sub.add_parser("mount-degraded", help="1 台欠けた状態で縮退マウント")
    s.set_defaults(fn=cmd_mount_degraded)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = load_config(args.config)
    try:
        args.fn(cfg, args)
    except BraidError as e:
        print(f"braidctl: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
