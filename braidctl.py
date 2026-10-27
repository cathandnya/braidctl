#!/usr/bin/env python3
"""
braidctl — Drobo BeyondRAID-style pool management on top of btrfs raid1.

  status [--json]      raw / usable / used capacity, protection state, per-drive health
  add DEV [--force]    add a drive (refuses a drive that carries data); moves no data
  balance              spread the data evenly over every drive (a full balance, hours)
  remove ID|DEV        remove a drive safely (only if what remains can still hold the data)
  monitor              run from a timer: detect losses and failures, re-protect automatically
  scrub                verify every block against its checksum (monthly timer)
  cancel [WHAT]        stop a running scrub, balance or device remove
  guard                unmount if too few devices remain to write (run from udev)
  mount [--wait N]     mount the pool, degraded if need be (run at boot)
  mount-degraded       mount with a drive missing, by hand

btrfs itself provides chunk-level mirroring, mixed drive sizes, and online add/remove.
This tool provides what Drobo did in firmware: reporting, detection, automatic recovery,
and the interlocks that keep it from eating the wrong disk.
"""

import argparse
import fcntl
import json
import logging
import os
import re
import signal
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
    # Grace period between noticing a drive is gone and re-laying-out without it.
    # Exists so a USB bus glitch that clears in seconds does not trigger a rebuild.
    "debounce_sec": 600,
    # Rebuild is considered possible when used <= usable_after_loss * this
    "rebuild_margin": 0.95,
    "warn_percent": 90,
    # Near-full thresholds, in bytes of unallocated space on the device a new chunk
    # still has to land on (see chunk_headroom). Not a percentage: what runs out is
    # room for a new chunk, which happens while df still reports free space.
    # fail: below one metadata chunk plus reserve. warn: below one data chunk plus headroom.
    "chunk_headroom_fail": 2 * 1024**3,
    "chunk_headroom_warn": 16 * 1024**3,
    # Services stopped only for the degraded-remount fallback
    "samba_units": ["smbd"],
    "state_file": "/var/lib/braid/state.json",
    # Machine-readable copy of what the last monitor pass saw, world-readable, for
    # dashboards that cannot run btrfs as root. Empty string disables it.
    "status_file": "/var/lib/braid/status.json",
    "remind_interval_sec": 86400,
    "smart_args": ["-d", "sat"],
    # Evict drives that are failing but still present, instead of only reporting them
    "auto_evict_on_errors": False,
    "error_threshold": 10,
    # Substrings of /dev/disk/by-id names that `add` must always refuse
    "protected_devices": [],
    # The optional web view. Nothing here starts anything: it exists only while
    # braid-web.socket is enabled or `braidctl web` is running.
    "web": {
        # Loopback by default. Changing it is one line and the tool will not argue;
        # what the pool is exposed to is the operator's call. See the README for the
        # shapes that make sense (LAN plus read_only, tailnet plus allow_from).
        "listen": "127.0.0.1:8765",
        "allowed_hosts": [],
        # "token" or "none". A token is the default because it assumes nothing about
        # what is in front. Where the network already answers the question — a tailnet
        # pinned to one machine, a proxy that authenticates, a host whose users can ssh
        # in anyway — a token adds a step and no protection, and that is the operator's
        # call to make. Cross-origin and Host checks apply either way.
        "auth": "token",
        # Source addresses allowed to connect at all, as addresses or CIDRs. Empty means
        # anything that can reach the socket. Worth setting whenever the socket is not on
        # loopback: on a tailnet an address cannot be claimed without that peer's key.
        "allow_from": [],
        # 0600, created on first start. There is deliberately no `token` key here:
        # braid.toml is world-readable and ends up quoted in bug reports.
        "token_file": "/etc/braid/web-token",
        "session_ttl_sec": 604800,
        "cookie_secure": False,
        # smartctl is slow behind a USB bridge and wakes sleeping drives, so the web
        # view refreshes it on its own clock and never inside a request.
        "smart_ttl_sec": 900,
        "btrfs_ttl_sec": 3,
        # Socket-activated: exit when nobody is looking, so nothing of braidctl is
        # resident between visits. 0 stays up; ignored when run in the foreground.
        "idle_exit_sec": 900,
        "assets": "",
        # Mutations run as transient systemd units, not inside a request: `remove` can
        # take hours, and a request that waits for it is a wedged UI.
        "jobs_dir": "/var/lib/braid/jobs",
        "job_retention": 50,
        "read_only": False,
        "allow_mount_actions": True,
        # `add --force` runs wipefs: the only irreversible operation in the tool, and the
        # last thing standing between a typo and somebody else's disk. Off by default.
        # Also what lets the page run `wipe-stale`, which is wipefs as well.
        "allow_force_add": False,
    },
    "notify": {
        # Borrow the SMTP block from an existing nas-monitor-style config
        "config_file": "/etc/nas-monitor/config.json",
        "to": "",
        "subject_prefix": "[braid]",
    },
}


class BraidError(Exception):
    pass


# ---------------------------------------------------------------- utilities

def run(cmd, check=True, ok_codes=(0,), capture=True, timeout=None):
    LOG.debug("run: %s", " ".join(cmd))
    try:
        p = subprocess.run(cmd, text=True, capture_output=capture, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A drive behind a dead USB bridge puts btrfs in D state forever. Callers
        # that have something better to do than wait pass a timeout and get an
        # error they can report instead of a process that never returns.
        raise BraidError(f"{' '.join(cmd)} timed out after {timeout}s")
    if check and p.returncode not in ok_codes:
        msg = (p.stderr or p.stdout or "").strip()
        raise BraidError(f"{' '.join(cmd)} failed ({p.returncode}): {msg}")
    return p


def fmt_bytes(n):
    """Decimal units (1 TB = 10^12), matching drive labels and Drobo"""
    n = float(n)
    for unit, div in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= div:
            return f"{n / div:.2f} {unit}"
    return f"{int(n)} B"


def require_root():
    if os.geteuid() != 0:
        raise BraidError("root privileges required (run with sudo)")


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
    """Logical capacity of a two-copy pool over these drives (ideal chunk allocation)"""
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
    """The containing disk for a partition; the device itself for a whole disk"""
    p = run(["lsblk", "-no", "PKNAME", path], check=False)
    pk = (p.stdout or "").strip().splitlines()
    return f"/dev/{pk[0]}" if pk and pk[0] else path


# ---------------------------------------------------------------- state

def lock_holder(path):
    """Which process holds the pool lock, so a refusal can name it instead of just saying no.

    flock() does not tell you the holder, so match the lock file's inode against every
    open fd in /proc. Best effort: None when it cannot tell, and the caller just says
    "something else is running"."""
    try:
        target = os.stat(path)
    except OSError:
        return None
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        fddir = f"/proc/{entry}/fd"
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue          # gone, or not ours to look at
        for fd in fds:
            try:
                st = os.stat(f"{fddir}/{fd}")
            except OSError:
                continue
            if st.st_ino == target.st_ino and st.st_dev == target.st_dev:
                try:
                    raw = Path(f"/proc/{entry}/cmdline").read_bytes()
                except OSError:
                    raw = b""
                argv = raw.decode("utf-8", "replace").split("\0")
                return {"pid": int(entry), "cmdline": " ".join(argv).strip(), "argv": [a for a in argv if a]}
    return None


def hold_lock(cfg):
    """Take the pool lock for the rest of this process.

    Only monitor used to lock, which left `braidctl remove 3` free to run while a
    monitor-driven rebuild was relocating chunks on the same filesystem — two device
    removes and a balance interleaving, with nothing to stop them. README has claimed
    "one mutation at a time" all along; this makes it true. The returned object must
    stay referenced: closing it releases the lock.

    Deliberately not taken by `scrub`, which runs for hours: blocking loss detection
    for a whole scrub would trade a real protection for a theoretical one."""
    lock = State(cfg["state_file"])
    lock.lock()
    return lock


class State:
    """/var/lib/braid/state.json — when each loss was first seen, error/SMART baselines,
    notification rate limiting, an inventory so a vanished drive can still be named, and
    the drives evicted while absent, which still carry the pool's superblock"""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {"missing": {}, "err_base": {}, "last_notify": {}, "inventory": {},
                     "rebuild": None, "smart_base": {}, "scrub_ack": None, "err_scrub_base": None,
                     "evicted": []}
        self._lock_fh = None

    def lock(self, quiet=False, wait=0):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fh = open(self.path.with_suffix(".lock"), "w")
        deadline = time.time() + max(0, wait)
        while True:
            try:
                fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.time() >= deadline:
                    break
                time.sleep(1)
        if quiet:
            # A udev-triggered pass while another is already working is not a
            # problem: the work is being done. Several fire at once when a bus
            # re-enumerates and every member device reappears.
            LOG.info("another braidctl holds the lock; nothing to do")
            return False
        holder = lock_holder(self.path.with_suffix(".lock"))
        who = f": {holder['cmdline']}" if holder and holder.get("cmdline") else ""
        raise BraidError(f"another braidctl is already running{who}. "
                         "A monitor-driven rebuild holds this for as long as it takes")

    def unlock(self):
        if self._lock_fh:
            self._lock_fh.close()
            self._lock_fh = None

    def load(self):
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except json.JSONDecodeError:
                LOG.warning("state file is corrupt, starting fresh: %s", self.path)
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
            LOG.warning("no usable notification config (%s); email disabled", e)

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
            LOG.error("failed to send mail: %s", ex)


# ---------------------------------------------------------------- btrfs

@dataclass
class Device:
    devid: int
    path: str          # '' when the device is missing
    size: int
    used: int          # raw bytes allocated to chunks on this device
    missing: bool = False
    present: bool = True   # whether the device node exists and the mounted filesystem is using it
    # The node exists and btrfs no longer calls it missing, but the mounted filesystem never
    # opened it: a drive that appeared after a degraded mount. See Pool.devices().
    detached: bool = False
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
    raw: int            # Device size, summed over all members
    used: int           # logical data + metadata in use
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
        """The filesystem's fsid. Ask btrfs, not findmnt: findmnt derives the UUID from
        the mount's source device node, and that is empty precisely when the device that
        vanished happens to be the one the mount was made through — the case where the
        fsid is needed most (matching a returned member, reading the sysfs missing flag)."""
        p = run(["btrfs", "filesystem", "show", self.mnt], check=False)
        m = re.search(r"uuid:\s*([0-9a-fA-F-]{36})", p.stdout)
        if m:
            return m[1]
        return (run(["findmnt", "-no", "UUID", self.mnt], check=False).stdout or "").strip()

    def member_devices_present(self):
        """Devices carrying this pool's label, mounted or not. A drive evicted while it was
        absent carries it too; scan_members tells the two apart."""
        out = []
        for n in flatten_lsblk(lsblk()):
            if n.get("fstype") == "btrfs" and n.get("label") == self.label:
                out.append(n["path"])
        return out

    @staticmethod
    def read_super(path):
        """The fields of a device's primary superblock that say which filesystem it belongs
        to and as which device, or None if there is no readable one"""
        try:
            p = run(["btrfs", "inspect-internal", "dump-super", path], check=False, timeout=30)
        except BraidError as e:
            LOG.warning("%s", e)
            return None
        f = dict(re.findall(r"^(fsid|generation|num_devices|dev_item\.devid|dev_item\.uuid)\s+(\S+)",
                            p.stdout, re.M))
        if len(f) < 5:
            return None
        return {"path": path, "fsid": f["fsid"], "generation": int(f["generation"]),
                "num_devices": int(f["num_devices"]), "devid": int(f["dev_item.devid"]),
                "uuid": f["dev_item.uuid"]}

    @staticmethod
    def dev_items(path):
        """{(devid, device uuid)} of every device the filesystem's chunk tree lists, read
        through `path`; None if it cannot be read. That tree is the filesystem's own answer
        to "who are my devices" — a superblock only knows which device it is itself."""
        try:
            p = run(["btrfs", "inspect-internal", "dump-tree", "-t", "chunk", path], check=False, timeout=60)
        except BraidError as e:
            LOG.warning("%s", e)
            return None
        items = set()
        for block in p.stdout.split("key (DEV_ITEMS DEV_ITEM ")[1:]:
            dv = re.search(r"^\s*devid (\d+)", block, re.M)
            uu = re.search(r"^\s*uuid (\S+)", block, re.M)
            if dv and uu:
                items.add((int(dv[1]), uu[1]))
        return items or None

    def evicted_records(self):
        try:
            return State(self.cfg["state_file"]).load().data.get("evicted") or []
        except OSError:
            return []

    def scan_members(self):
        """(newest superblock, members, stale) among the devices carrying this pool's label,
        each a read_super dict; (None, [], []) when there are none.

        `btrfs device remove` scratches the superblock of a device it can write to. One that
        is absent — `remove missing`, the monitor's re-protection — keeps it: same label, same
        fsid, its old devid. Attached again, it looks like a member to anything that goes by
        label and fsid, and it is not one. Counted, it pushed the devids seen past
        num_devices, the kernel's own ready check disagreed, and the pool mounted degraded on
        every boot with nothing missing. Worse, with a real member absent it can make the
        count look complete.

        So membership is decided by the filesystem: a device is a member when its (devid,
        device uuid) is in the chunk tree as the newest superblock's device sees it. The uuid
        matters because btrfs hands out the highest devid again once it has been evicted. If
        that tree cannot be read, what braidctl recorded at eviction time (state.json
        "evicted") is used instead; it is consulted in any case, because when every real
        member is absent the newest superblock is the stale drive's own, and its old tree
        lists itself. Devices with the label but another fsid are neither."""
        sbs = [sb for sb in (self.read_super(p) for p in self.member_devices_present()) if sb]
        if not sbs:
            return None, [], []
        newest = max(sbs, key=lambda s: s["generation"])
        same = [s for s in sbs if s["fsid"] == newest["fsid"]]
        items = self.dev_items(newest["path"])
        if items is not None and (newest["devid"], newest["uuid"]) not in items:
            # The device that was read from is not in its own list: the output was not what
            # this parses. Deciding membership from it would make every member stale.
            LOG.warning("device list read through %s does not include it; not using it", newest["path"])
            items = None
        if items is None:
            LOG.info("could not read the device list through %s; using the eviction records", newest["path"])
        evicted = self.evicted_records()
        for s in same:
            disk = next(iter(lsblk(parent_disk(s["path"]))), {})
            s["serial"] = (disk.get("serial") or "").strip()
            s["model"] = (disk.get("model") or "").strip()
            s["size"] = disk.get("size")
            s["record"] = eviction_record(evicted, s)
        # The tree is the answer whenever it can be read, except when the device it was read
        # through is itself a recorded eviction: then every real member is absent, and that
        # old tree still lists the drive it was read from. Only then do the records decide —
        # letting them override a readable tree would turn a wrong record (a restored
        # state.json, a bug) into a live member that is ignored at boot and offered to wipefs.
        trust_tree = items is not None and newest["record"] is None
        stale, members = [], []
        for s in same:
            gone = (s["devid"], s["uuid"]) not in items if trust_tree else s["record"] is not None
            (stale if gone else members).append(s)
        for s in stale:
            s["evicted_at"] = s["record"].get("at") if s["record"] else None
        if members:
            newest = max(members, key=lambda s: s["generation"])
        return newest, members, stale

    def stale_members(self, report=None):
        """The stale half of scan_members. With the pool mounted, a label device that is
        attached to it is a member by definition, so when that is all there is, nothing
        is read from the drives"""
        if report is not None:
            attached = {os.path.realpath(d.path) for d in report.devs if d.path and d.present}
            if all(os.path.realpath(p) in attached for p in self.member_devices_present()):
                return []
        return self.scan_members()[2]

    def forget(self, path):
        """Drop a stale device from the kernel's registry of btrfs devices. Registered, it
        counts towards the fsid's devices, and `btrfs device ready` stays false for good."""
        p = run(["btrfs", "device", "scan", "--forget", path], check=False)
        if p.returncode != 0:
            LOG.info("could not forget %s: %s", path, (p.stderr or p.stdout).strip())

    def members_complete(self, scan=None):
        """(complete, devids seen, devices expected) for the pool as it can be mounted now.

        A normal mount with a member absent cannot succeed, and it fails the dangerous way:
        btrfs starts reading metadata from the members it found, gives up on the missing one,
        and tears the half-built filesystem down while those reads are still completing. On
        2026-09-16 (Linux 7.0) one completed after the workqueue it reports to was destroyed
        — "cannot queue simple_end_io_work on wq btrfs-endio-meta", then a page fault in
        btrfs_simple_end_io — and the kernel panicked. The member had been re-enumerated
        0.4 s earlier and its disk node did not exist yet. So a normal mount is attempted only
        when every member is attached: each superblock records how many devices the
        filesystem has, and the kernel's own device-ready check has to agree. The kernel check
        alone is not enough — it still counts a path registered before the node vanished.

        A drive evicted while it was absent still carries the label and fsid. Only members
        are counted (see scan_members), and stale drives are dropped from the kernel's
        registry before it is asked, or they would make its answer wrong in either direction.

        scan is a scan_members() result the caller already has: each one reads every drive."""
        newest, members, stale = scan or self.scan_members()
        if newest is None:
            return False, set(), 0
        for s in stale:
            LOG.info("ignoring %s: devid %d was evicted from the pool (generation %d)",
                     s["path"], s["devid"], s["generation"])
            self.forget(s["path"])
        devids = {s["devid"] for s in members}
        expected = newest["num_devices"]
        if not members or len(devids) < expected:
            return False, devids, expected
        ready = run(["btrfs", "device", "ready", newest["path"]], check=False).returncode == 0
        return ready, devids, expected

    def returned_members(self, report):
        """(returned, stale) among devices carrying this pool's label that are not attached
        to the mount. A member whose devid the filesystem lists as gone has come back ->
        returned, as (path, devid); a drive the filesystem no longer has was already
        evicted -> stale, as scan_members dicts."""
        gone_ids = {d.devid for d in report.gone}
        fs_paths = {os.path.realpath(d.path) for d in report.devs if d.present}
        if all(os.path.realpath(p) in fs_paths for p in self.member_devices_present()):
            return [], []
        uuid = self.uuid()
        _, members, stale = self.scan_members()
        returned = [(s["path"], s["devid"]) for s in members
                    if s["fsid"] == uuid and s["devid"] in gone_ids
                    and os.path.realpath(s["path"]) not in fs_paths]
        return returned, [s for s in stale if s["fsid"] == uuid]

    # -- queries (mounted)
    def leftover_devinfo(self):
        """devids the kernel still has a /sys/fs/btrfs/<fsid>/devinfo entry for although the
        filesystem no longer has the device. Only the names are listed: nothing under them
        is read, because reading one is what crashes (see cmd_add)."""
        d = Path(f"/sys/fs/btrfs/{self.uuid()}/devinfo")
        try:
            ids = {int(e.name) for e in d.iterdir() if e.name.isdigit()}
        except OSError:
            return set()
        return ids - {dev.devid for dev in self.devices()}

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
        # Trust sysfs over the progs output for the missing flag
        uuid = self.uuid()
        ro = "ro" in ((self.mount_entry() or {}).get("options") or [])
        for d in devs:
            f = Path(f"/sys/fs/btrfs/{uuid}/devinfo/{d.devid}/missing")
            if f.exists() and f.read_text().strip() == "1":
                d.missing = True
            d.present = bool(d.path) and os.path.exists(d.path) and not d.missing
            # A drive that turns up after the pool was mounted degraded gets scanned: btrfs
            # records its path and clears "missing", and `filesystem show` lists it like any
            # other member. But the mounted filesystem never opens it, so nothing is written
            # to it — every write that pairs with it lands on one drive. sysfs says so
            # (writeable 0 on a read-write mount); nothing else does. Counted as present,
            # it read as PROTECTED for as long as the pool stayed mounted. It is not
            # present: a remount is what brings it in, which is what the monitor's re-attach does.
            w = Path(f"/sys/fs/btrfs/{uuid}/devinfo/{d.devid}/writeable")
            if d.present and not ro and w.exists() and w.read_text().strip() == "0":
                d.present, d.detached = False, True
            if d.present or d.detached:
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
                # Join them if a block type somehow has more than one profile
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

    def balance_status(self):
        """btrfs prints "N out of about M chunks balanced (K considered), P% left".

        balance_running() used to throw all of that away and keep the boolean. A
        balance is the slowest thing this tool starts — hours on a full pool — and
        the percentage is the only progress it has, so keep it."""
        # -v adds the filters, which is the only place btrfs says it is converting profiles
        p = run(["btrfs", "balance", "status", "-v", self.mnt], check=False)
        out = p.stdout
        lines = out.strip().splitlines()
        info = {"running": "running" in out, "paused": "paused" in out,
                "converting": "converting" in out,
                "pct": None, "chunks_done": None, "chunks_total": None, "considered": None,
                "text": lines[0] if lines else ""}
        m = re.search(r"(\d+) out of about (\d+) chunks balanced \((\d+) considered\)", out)
        if m:
            info["chunks_done"], info["chunks_total"], info["considered"] = int(m[1]), int(m[2]), int(m[3])
            info["text"] = next(l for l in lines if "chunks balanced" in l).strip()
        m = re.search(r"(\d+)% left", out)
        if m:
            info["pct"] = 100 - int(m[1])
        elif info["chunks_total"]:
            info["pct"] = round(info["chunks_done"] / info["chunks_total"] * 100, 1)
        return info

    def balance_running(self):
        st = self.balance_status()
        return st["running"] or st["paused"]

    def alloc_by_devid(self):
        """{devid: (size, alloc)} from one `filesystem show --raw`.

        devices() costs three subprocesses per device (lsblk, SMART, parent_disk), so it
        cannot be polled. This can: watching a departing device's allocation drain is the
        only progress a `device remove` has, because btrfs reports none."""
        p = run(["btrfs", "filesystem", "show", "--raw", self.mnt], check=False)
        out = {}
        for line in p.stdout.splitlines():
            m = re.search(r"devid\s+(\d+)\s+size\s+(\d+)\s+used\s+(\d+)", line)
            if m:
                out[int(m[1])] = (int(m[2]), int(m[3]))
        return out

    def stale_superblocks(self, devids):
        """{devid: (oldest copy's generation, filesystem generation)} for the given devices
        whose superblock copies are behind right now; {} when all are current; None when
        it cannot tell.

        A reference device is read first and the ones in question after it. A commit
        between the reads can only move the later reads ahead, never behind, so a copy
        that reads lower than the reference really is stale."""
        paths = {}
        for line in run(["btrfs", "filesystem", "show", "--raw", self.mnt], check=False).stdout.splitlines():
            m = re.search(r"devid\s+(\d+)\s+size\s+\d+\s+used\s+\d+\s+path\s+(/\S+)", line)
            if m:
                paths[int(m[1])] = m[2]

        def generations(path):
            out = run(["btrfs", "inspect-internal", "dump-super", "-a", path], check=False).stdout
            return [int(g) for g in re.findall(r"^generation\s+(\d+)", out, re.M)]
        others = [d for d in paths if d not in devids]
        ref = generations(paths[others[0]]) if others else []
        if not ref or any(d not in paths for d in devids):
            return None
        stale = {}
        for d in devids:
            gens = generations(paths[d])
            if not gens:
                return None
            if min(gens) < max(ref):
                stale[d] = (min(gens), max(ref))
        return stale

    def scrub_status(self, raw=False, per_device=False):
        argv = ["btrfs", "scrub", "status"]
        if raw:
            argv.append("-R")          # exact counters per class instead of a summary line
        if per_device:
            argv.append("-d")
        p = run(argv + [self.mnt], check=False)
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

    def enrich(self, devs, smart=True):
        stats = self.dev_stats()
        for d in devs:
            d.errors = stats.get(d.path) or stats.get(f"devid:{d.devid}") or {}
            # smartctl takes seconds per drive behind a USB bridge and spins up a
            # sleeping one. Callers that refresh often read it on their own clock.
            d.smart = self.smart(d) if smart else {}
        return devs

    # -- mutations
    def in_fstab(self):
        return run(["findmnt", "--fstab", "-n", self.mnt], check=False).returncode == 0

    def mount(self, degraded=False, check=True, scan=None):
        """Mount via fstab if there is an entry, otherwise through the member with the newest
        superblock. Not LABEL=: a drive evicted while absent carries the label too, and
        LABEL= is free to resolve to it. With no member visible there is nothing to mount
        through, and falling back to LABEL= would mount whatever stale drive has it: an old
        copy of the pool, writable, diverging from the real one."""
        cmd = ["mount"]
        if self.in_fstab():
            if degraded:
                cmd += ["-o", "degraded"]
            cmd.append(self.mnt)
        else:
            newest, members, _ = scan or self.scan_members()
            if not members:
                msg = f"no member of the pool with LABEL={self.label} is visible"
                if check:
                    raise BraidError(msg)
                return subprocess.CompletedProcess(cmd, 32, "", msg)
            cmd += ["-o", "degraded,noatime" if degraded else "noatime", newest["path"], self.mnt]
        return run(cmd, check=check)

    def umount(self, retries=3):
        for i in range(retries):
            p = run(["umount", self.mnt], check=False)
            if p.returncode == 0:
                return
            LOG.warning("umount failed (%d/%d): %s", i + 1, retries, p.stderr.strip())
            time.sleep(5)
        raise BraidError(f"cannot unmount {self.mnt} (busy)")

    def remove_device(self, target, stream=False):
        cmd = ["btrfs", "device", "remove", str(target), self.mnt]
        return run(cmd, check=False, capture=not stream)

    def balance_soft_convert(self):
        """Bring chunks written while degraded back up to raid1"""
        run(["btrfs", "balance", "start", "--bg", "-dconvert=raid1,soft", "-mconvert=raid1,soft", self.mnt],
            check=False)

    def compact(self, usage=50):
        """Consolidate half-empty chunks to free unallocated space. Doing this before a
        `device remove` is what keeps it from dying of ENOSPC halfway. Runs synchronously."""
        LOG.info("compaction balance (usage<%d%%) running", usage)
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


def collect(pool, smart=True):
    devs = pool.enrich(pool.devices(), smart=smart)
    m = pool.mount_entry() or {"options": []}
    return Report(devs=devs, usage=pool.usage(),
                  mounted_degraded="degraded" in m["options"],
                  balance_running=pool.balance_running())


def copies_required(profiles):
    """How many devices a write needs: raid1 places two copies on two distinct devices,
    raid1c3 three, raid1c4 four. Below this the filesystem cannot allocate a new chunk."""
    need = 2
    for p in profiles.values():
        m = re.match(r"RAID1C(\d)", p)
        if m:
            need = max(need, int(m[1]))
    return need


def chunk_headroom(report):
    """Unallocated bytes on the least-free device a new chunk still has to land on.

    btrfs allocates a chunk as one extent on each of N distinct devices (N = copies_required),
    choosing the N with the most unallocated space. Allocation therefore stops when the Nth-most-
    free device runs out, and that happens while `df` still shows space free: that free space sits
    inside chunks already allocated and cannot hold a new one. A metadata chunk wants up to 1 GiB,
    a data chunk up to 10 GiB. This is the number that decides how full the pool can get -- the
    used percentage is only a proxy for it, and a poor one once allocation is uneven.
    """
    need = copies_required(report.usage.profiles)
    free = sorted((d.size - d.used for d in report.devs if d.present), reverse=True)
    return free[need - 1] if len(free) >= need else 0


def can_rebuild(report, cfg, losing=None):
    """Could the pool be re-protected after losing these devices?
    Returns (ok, usable_after, needed)."""
    losing = losing or report.gone
    exclude = {d.devid for d in report.gone} | {d.devid for d in losing}
    sizes = [d.size for d in report.devs if d.devid not in exclude]
    usable_after = raid1_usable(sizes)
    needed = report.usage.used
    return needed <= usable_after * cfg["rebuild_margin"], usable_after, needed


# ---------------------------------------------------------------- status

def device_health(d):
    """One device's health as (level, text). level: ok / info / warn / fail / missing / unknown.
    'info' is a defect that is present but not growing (a stable reallocated count)."""
    if d.detached:
        return "missing", "NOT IN USE (connected; remount to use it)"
    if d.missing or not d.present:
        return "missing", "MISSING"
    s = d.smart
    if not s or s.get("passed") is None:
        return "unknown", "SMART n/a"
    if s.get("passed") is False:
        return "fail", "SMART FAIL"
    level, text = "ok", "OK"
    bad = [k for k in ("reallocated", "pending", "offline_uncorr") if (s.get(k) or 0) > 0]
    if bad:
        level = "warn" if any(k in ("pending", "offline_uncorr") for k in bad) else "info"
        text = "WARN " + " ".join(f"{k}={s[k]}" for k in bad)
    if s.get("temp") is not None:
        text += f" {s['temp']}°C"
    return level, text


def verdict(report, cfg):
    """The last line of `status` — Drobo's green / amber / red light — as (level, text)."""
    u = report.usage
    if report.gone:
        ok, after, needed = can_rebuild(report, cfg)
        if ok:
            return "warn", (f"! {len(report.gone)} drive(s) missing. Can re-protect with what remains "
                            f"(usable {fmt_bytes(after)} >= used {fmt_bytes(needed)}); the monitor will do it")
        short = needed / cfg["rebuild_margin"] - after
        return "fail", (f"!! {len(report.gone)} drive(s) missing. Cannot re-protect with what remains "
                        f"(usable {fmt_bytes(after)} < used {fmt_bytes(needed)}). "
                        f"Add at least {fmt_bytes(short)} more")
    healthy = [d for d in report.devs if d.present]
    if len(healthy) < 2:
        return "fail", "Only one drive present; nothing is mirrored"
    # Running out of room for new chunks is the one capacity problem with a deadline: past it
    # writes and metadata fail, and balance has nowhere to relocate to, so it cannot be fixed
    # from inside. It outranks the re-protection arithmetic below, which has no deadline.
    head = chunk_headroom(report)
    if head < cfg["chunk_headroom_fail"]:
        return "fail", (f"Out of unallocated space ({fmt_bytes(head)} left on the drive a new chunk "
                        f"must land on). Writes will fail with ENOSPC and balance cannot run. "
                        f"Delete data or add a drive")
    if head < cfg["chunk_headroom_warn"]:
        return "warn", (f"Running out of unallocated space ({fmt_bytes(head)} left on the drive a new "
                        f"chunk must land on; a data chunk wants 10 GB). Delete data or add a drive")
    largest = max(healthy, key=lambda d: d.size)
    after = report.usable_after_losing(largest)
    if u.used <= after * cfg["rebuild_margin"]:
        return "ok", (f"Any single drive can be lost and re-protected "
                      f"(losing the largest, {largest.name} {fmt_bytes(largest.size)}, "
                      f"leaves {fmt_bytes(after)} usable >= {fmt_bytes(u.used)} used)")
    # Losing the largest drive would leave too little to re-mirror everything. Nothing is wrong
    # now and nothing breaks on a schedule -- the data stays two-copy at any fill level; what is
    # missing is the room to get back to two copies after a failure, and the only fix is hardware.
    # So this is reported, not warned about: it turns true around 70% full on a mixed-size pool
    # and never clears on its own, and a standing amber is how a status line stops being read.
    tol = [d.name for d in healthy if u.used <= report.usable_after_losing(d) * cfg["rebuild_margin"]]
    if tol:
        short = u.used - after * cfg["rebuild_margin"]
        return "info", (f"Losing {largest.name} ({fmt_bytes(largest.size)}) would leave "
                        f"{fmt_bytes(short)} too little to re-protect automatically; losing "
                        f"{', '.join(tol)} is still covered. Consider adding a drive")
    return "warn", (f"No single drive can be lost and re-protected automatically: {fmt_bytes(u.used)} "
                    f"used, and losing {largest.name} would leave {fmt_bytes(after)} usable. "
                    f"A failure now would leave part of the pool on one copy until a drive is added")


def render_status(report, cfg, state=None, scrub=None, balance=None, stale=()):
    u = report.usage
    lines = []
    label = report.state_label()
    lines.append(f"braid  {cfg['mountpoint']}   {label} ({'/'.join(sorted(set(u.profiles.values())) or ['-'])})"
                 + ("   [mounted degraded]" if report.mounted_degraded else ""))
    pct = (u.used / u.usable * 100) if u.usable else 0
    lines.append(f"Raw {fmt_bytes(u.raw):>10}   Usable {fmt_bytes(u.usable):>10}   "
                 f"Used {fmt_bytes(u.used):>10} ({pct:.0f}%)   Free {fmt_bytes(u.free):>10}")
    lines.append("")
    lines.append(f"{'ID':>2}  {'device':8} {'model':22} {'size':>9} {'alloc':>9}  {'errors':>6}  health")
    inv = (state.data.get("inventory", {}) if state else {})
    older = []
    for d in report.devs:
        errs = errors_since_scrub(d, state)
        if d.errors and sum(d.errors.values()) != errs:
            older.append(f"devid {d.devid} {sum(d.errors.values())}")
        _, health = device_health(d)
        if (d.missing or not d.present) and not d.detached:
            known = inv.get(str(d.devid), {})
            model = known.get("model", "?")
            name = f"(was {os.path.basename(known.get('path', '') or d.path or '?')})"
        else:
            model = d.model
            name = d.name
        lines.append(f"{d.devid:>2}  {name:8} {model[:22]:22} {fmt_bytes(d.size):>9} {fmt_bytes(d.used):>9}  "
                     f"{errs:>6}  {health}")
    since = ((state.data.get("err_scrub_base") or {}).get("scrub_started") if state else None)
    if since and older:
        lines.append(f"errors: since the scrub of {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}"
                     f" (lifetime: {', '.join(older)})")
    lines.append("")
    lines.append(verdict(report, cfg)[1])
    if pct >= cfg["warn_percent"]:
        lines.append(f"Warning: {pct:.0f}% full. Rebuilds (balance) fail when free space runs short")
    if report.balance_running:
        b = balance if balance is not None else {}
        pct = f" ({b['pct']:.0f}%)" if b.get("pct") is not None else ""
        chunks = (f", {b['chunks_done']} of about {b['chunks_total']} chunks"
                  if b.get("chunks_total") else "")
        lines.append(f"Balance in progress{pct}{chunks}; performance is reduced until it finishes")
    if scrub and scrub.get("started"):
        when = time.strftime("%Y-%m-%d", time.localtime(scrub["started"]))
        lines.append(f"Last scrub {when} ({scrub.get('status') or '?'}): {scrub['text']}")
    lines.extend(stale_line(s) for s in stale)
    return "\n".join(lines)


def cmd_status(cfg, args):
    require_root()
    pool = Pool(cfg)
    if not pool.is_mounted():
        _, members, stale = pool.scan_members()
        present = [m["path"] for m in members]
        raise BraidError(f"{cfg['mountpoint']} is not mounted. "
                         f"Members visible: {', '.join(present) or 'none'}"
                         + (". If one drive is missing, try `braidctl mount-degraded`" if present else "")
                         + "".join("\n" + stale_line(s) for s in stale))
    report = collect(pool)
    stale = pool.stale_members(report)
    state = State(cfg["state_file"]).load()
    update_inventory(state, report.devs)   # so a later loss can still name the drive
    try:
        state.save()
    except OSError:
        pass
    scrub = scrub_info(pool, state, report)
    if args.json:
        out = {"state": report.state_label(), "usage": asdict(report.usage),
               "devices": [asdict(d) for d in report.devs],
               "mounted_degraded": report.mounted_degraded, "balance_running": report.balance_running,
               "scrub": scrub, "stale_members": stale}
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(render_status(report, cfg, state, scrub, stale=stale))


# ---------------------------------------------------------------- add / remove

def check_addable(cfg, pool, dev, force):
    dev = os.path.realpath(dev)
    try:
        st = os.stat(dev)
    except FileNotFoundError:
        raise BraidError(f"{dev} does not exist")
    if not stat.S_ISBLK(st.st_mode):
        raise BraidError(f"{dev} is not a block device")
    # A card reader or an empty bay is a block device with no medium. Refuse it before
    # anything else looks at it: udev creates no /dev/disk/by-id link for a device with
    # no medium, so protected_devices — which matches those links — cannot see it.
    try:
        size = int(Path(f"/sys/class/block/{os.path.basename(dev)}/size").read_text())
    except (OSError, ValueError):
        size = None
    if size == 0:
        raise BraidError(f"refusing {dev}: it has no medium (0 bytes) — an empty card reader or bay")

    # The system disk, anything mounted, and anything with a holder are refused even with --force
    root_src = run(["findmnt", "-no", "SOURCE", "/"]).stdout.strip()
    root_disk = parent_disk(os.path.realpath(root_src))
    if parent_disk(dev) == root_disk:
        raise BraidError(f"refusing {dev}: it is the system disk ({root_disk})")
    nodes = list(flatten_lsblk(lsblk(dev)))
    for n in nodes:
        if any(n.get("mountpoints") or []):
            raise BraidError(f"refusing {dev}: {n['path']} is mounted at {n['mountpoints']}")
        holders = Path(f"/sys/class/block/{os.path.basename(n['path'])}/holders")
        if holders.exists() and any(holders.iterdir()):
            raise BraidError(f"refusing {dev}: {n['path']} is in use by another device (LUKS/LVM/MD)")
    byid = Path("/dev/disk/by-id")
    for link in byid.iterdir() if byid.exists() else []:
        if os.path.realpath(link) == dev:
            for pat in cfg["protected_devices"]:
                if pat in link.name:
                    raise BraidError(f"refusing {dev} ({link.name}): matches protected_devices")
    if dev in [d.path for d in pool.devices()]:
        raise BraidError(f"{dev} is already a member of this pool")

    # Signatures or partitions mean --force is required
    sigs = run(["wipefs", "-n", dev], check=False).stdout.strip()
    children = [n["path"] for n in nodes if n["path"] != dev]
    if (sigs or children) and not force:
        raise BraidError(f"{dev} already holds data ({'partitions: ' + ', '.join(children) if children else ''}"
                         f"{' ' if children and sigs else ''}{'signature: ' + sigs.splitlines()[-1] if sigs else ''}). "
                         f"Pass --force to erase it")
    return dev, children


def cmd_add(cfg, args):
    require_root()
    lock = hold_lock(cfg)                      # noqa: F841 — released when this returns
    pool = Pool(cfg)
    if not pool.is_mounted():
        raise BraidError(f"{cfg['mountpoint']} is not mounted")
    if pool.balance_running():
        raise BraidError("a balance is running; try again once it finishes")
    # Linux 7.0: `device remove missing` leaves the evicted devid's sysfs devinfo entry
    # behind. btrfs gives a new device the highest devid + 1, so when the evicted one was
    # the highest, the add reuses it, cannot create devinfo/<devid> ("duplicate filename"),
    # and the next read of that entry is a NULL dereference: on 2026-10-27 a loop-device
    # test panicked the host this way. A remount (or reboot) clears the entry.
    devids = [d.devid for d in pool.devices()]
    next_devid = max(devids, default=0) + 1
    if next_devid in pool.leftover_devinfo():
        raise BraidError(f"devid {next_devid} was removed from {cfg['mountpoint']} during this mount, and "
                         "adding now would reuse it, which crashes this kernel. Remount the pool "
                         "(or reboot) first")
    dev, children = check_addable(cfg, pool, args.device, args.force)
    before = pool.usage()
    if args.force:
        for c in children:
            run(["wipefs", "-a", c])
        run(["wipefs", "-a", dev])
    print(f"== adding {dev}")
    run(["btrfs", "device", "add", "-f", dev, cfg["mountpoint"]], capture=False)
    after = pool.usage()
    print(f"usable capacity: {fmt_bytes(before.usable)} -> {fmt_bytes(after.usable)}")
    # No balance. It used to start one in the background, which from the web view was
    # killed with the job's cgroup seconds later, so a page add never balanced and a
    # terminal add always did. New chunks go to the drives with the most unallocated
    # space, so the new drive fills first without one; spreading what is already there
    # is `braidctl balance`, started when somebody decides the hours of I/O are worth it.
    #
    # Except that the capacity above is arithmetic on drive sizes. A raid1 chunk needs
    # unallocated space on two drives, so when the old ones are fully allocated the new
    # drive alone cannot take one, and writes keep failing until a balance has run.
    head = chunk_headroom(collect(pool, smart=False))
    if head < cfg["chunk_headroom_warn"]:
        print(f"!! only {fmt_bytes(head)} unallocated on the drive a new chunk must land on besides "
              "the new one: a raid1 chunk needs room on two drives, so most of the new space cannot be "
              "written to yet. Run `braidctl balance` to make it usable.")
    else:
        print("existing data stays where it is; new writes go to the emptiest drives first.\n"
              "`braidctl balance` spreads the existing data over every drive (hours, the pool stays usable)")
    st = State(cfg["state_file"]).load()
    update_inventory(st, pool.devices())
    st.save()


def cmd_balance(cfg, args):
    """A full balance, in the foreground: the job the web view runs it as lasts as long as
    the balance does, so its progress and its Cancel are the job's.

    The pool lock is not taken, for the reason scrub does not take it: a balance runs for
    hours, and holding the lock that long would hold off loss detection. add and remove
    refuse while it runs, and the monitor cancels it when it has to re-protect the pool
    (cancel_balance_for_rebuild)."""
    require_root()
    pool = Pool(cfg)
    if not pool.is_mounted():
        raise BraidError(f"{cfg['mountpoint']} is not mounted")
    if pool.balance_running():
        raise BraidError("a balance is already running")
    if "running" in pool.scrub_status():
        raise BraidError("a scrub is running; balance once it finishes")
    holder = lock_holder(Path(cfg["state_file"]).with_suffix(".lock"))
    if holder:
        raise BraidError(f"another braidctl is running: {holder['cmdline']}")
    LOG.info("full balance starting")
    print(f"== balancing {cfg['mountpoint']}: every chunk is rewritten, so this takes hours. "
          "`braidctl cancel balance` stops it after the chunk it is moving", flush=True)
    # Captured, not passed through: a foreground balance prints nothing until it ends, and
    # a cancelled one still exits 0, so its last line is the only way to tell.
    p = run(["btrfs", "balance", "start", "--full-balance", cfg["mountpoint"]], check=False)
    out = (p.stdout + p.stderr).strip()
    if out:
        print(out, flush=True)
    if p.returncode != 0:
        raise BraidError(f"btrfs balance exited {p.returncode}")
    if "canceled" in out or "cancelled" in out:
        LOG.warning("full balance cancelled; the chunks already moved stay where they are")
        return
    if "paused" in out:
        # Also what an unmount does to it. A paused balance still counts as running, so
        # scrub, add, remove and the monitor's rebuild all keep waiting for it.
        raise BraidError(f"the balance was paused, not finished: `btrfs balance resume {cfg['mountpoint']}` "
                         "continues it, `braidctl cancel balance` drops it")
    LOG.info("full balance finished")


def resolve_target(pool, target):
    devs = pool.devices()
    if target.isdigit():
        for d in devs:
            if d.devid == int(target):
                return d
        raise BraidError(f"no devid {target} in this pool")
    real = os.path.realpath(target)
    for d in devs:
        if d.path and os.path.realpath(d.path) == real:
            return d
    raise BraidError(f"{target} is not a member of this pool")


def cmd_remove(cfg, args):
    require_root()
    lock = hold_lock(cfg)                      # noqa: F841 — released when this returns
    pool = Pool(cfg)
    if not pool.is_mounted():
        raise BraidError(f"{cfg['mountpoint']} is not mounted")
    if pool.balance_running():
        raise BraidError("a balance is running; try again once it finishes")
    report = collect(pool)
    if args.target == "missing":
        if not report.gone:
            raise BraidError("no missing devices")
        target, losing = "missing", report.gone
    else:
        d = resolve_target(pool, args.target)
        target, losing = d.devid, [d]
    ok, after, needed = can_rebuild(report, cfg, losing=losing)
    names = ", ".join(f"devid {d.devid} ({d.name}, {fmt_bytes(d.size)})" for d in losing)
    if not ok:
        short = needed / cfg["rebuild_margin"] - after
        raise BraidError(f"removing {names} would not leave room for the data "
                         f"(usable afterwards {fmt_bytes(after)} < used {fmt_bytes(needed)}). "
                         f"Add at least {fmt_bytes(short)} first")
    print(f"== removing {names} (usable afterwards {fmt_bytes(after)}, used {fmt_bytes(needed)})")
    print("   data is relocated first, so this takes a while. Ctrl-C (or `braidctl cancel remove`)\n"
          "   stops it; the drive then stays in the pool")
    try:
        pool.compact()
        p = pool.remove_device(target, stream=True)
    except KeyboardInterrupt:
        # subprocess.run has already killed the btrfs child, and a fatal signal is what the
        # kernel checks between extents: relocation stops, the device gets its size back
        # and stays a member. What was already moved stays moved, still mirrored.
        raise BraidError(f"cancelled: {names} stays in the pool. Data already relocated off it "
                         "stays on the other drives, mirrored as before")
    if p.returncode != 0:
        raise BraidError(f"btrfs device remove failed (exit {p.returncode}). "
                         "If it was ENOSPC, the remaining drives lack unallocated space: run "
                         f"`btrfs balance start --full-balance {cfg['mountpoint']}` and retry, or add a drive")
    print("== done")
    state = State(cfg["state_file"]).load()
    # `remove missing` takes out one missing device however many there are, so look at
    # which devids are gone rather than assuming. One that was absent could not have its
    # superblock scratched; see record_evicted.
    left = {d.devid for d in pool.devices()}
    removed = [d for d in losing if d.devid not in left]
    record_evicted(state, pool, [d for d in removed if not d.present])
    for d in removed:
        state.data["missing"].pop(str(d.devid), None)
        state.data["inventory"].pop(str(d.devid), None)
    state.save()
    print(render_status(collect(pool), cfg, state))


def cmd_wipe_stale(cfg, args):
    """Erase a drive that was evicted from the pool while it was absent. Only such a drive:
    one that scan_members finds carrying this pool's fsid under a device the filesystem
    no longer has. Anything else — a member, another filesystem, a blank disk — is refused,
    and wipefs by hand is the tool for those."""
    require_root()
    lock = hold_lock(cfg)                      # noqa: F841 — released when this returns
    pool = Pool(cfg)
    dev = os.path.realpath(args.device)
    _, members, stale = pool.scan_members()
    s = next((s for s in stale if os.path.realpath(s["path"]) == dev), None)
    if s is None:
        what = ("a member of this pool" if any(os.path.realpath(m["path"]) == dev for m in members)
                else "not a drive evicted from this pool")
        raise BraidError(f"refusing {dev}: it is {what}")
    if pool.is_mounted() and dev in {os.path.realpath(d.path) for d in pool.devices() if d.path}:
        raise BraidError(f"refusing {dev}: the mounted pool is using it")
    for n in flatten_lsblk(lsblk(dev)):
        if any(n.get("mountpoints") or []):
            raise BraidError(f"refusing {dev}: {n['path']} is mounted at {n['mountpoints']}")
    print(f"== erasing {dev}: was devid {s['devid']} (generation {s['generation']})")
    pool.forget(dev)
    run(["wipefs", "-a", dev], capture=False)
    state = State(cfg["state_file"]).load()
    # Only this drive's record. Another drive evicted later under the same (reused) devid
    # has its own, and still needs it.
    rec = s.get("record")
    state.data["evicted"] = [e for e in state.data.get("evicted") or [] if e != rec]
    state.clear_notify(f"stale-{s['path']}")
    state.save()
    print("== done")


# ---------------------------------------------------------------- snapshot

# btrfs counts scrub errors by class, and the classes are not equally serious:
#
#   read / csum         : a block could not be read, or did not match its checksum.
#                         Real damage. btrfs also charges these to the device stats.
#   super               : a superblock copy was unreadable or behind the filesystem's
#                         generation. Scrub does not repair these because it need not:
#                         every transaction commit rewrites every copy on every
#                         device. A drive that was away comes back with all of them
#                         behind, so what matters is whether they are current NOW.
#   verify              : a tree block's header did not say what the extent tree
#                         expected. Genuine metadata damage looks like this — and so
#                         does a block that was freed and reused while the scrub was
#                         still reading it, which is ordinary on a filesystem under
#                         heavy write. From the summary line alone the two are
#                         indistinguishable.
#
# Reporting both as one red light makes a busy pool cry wolf, and a pool that cries
# wolf is one nobody reads. So a verify-only result with clean device counters is a
# question ("look at this") rather than a verdict ("your data is damaged"), and once
# a human has looked, `braidctl scrub --ack` records that instead of forcing a choice
# between a five-hour re-scrub and deleting btrfs's own record.
SCRUB_HARD = ("read_errors", "csum_errors")
SCRUB_SUPER = ("super_errors",)
SCRUB_SOFT = ("verify_errors",)
SCRUB_ALL = SCRUB_HARD + SCRUB_SUPER + SCRUB_SOFT


def parse_scrub_raw(text):
    """`btrfs scrub status -R` prints one `name: value` per counter, tab-indented"""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s+([a-z_]+):\s+(-?\d+)\s*$", line)
        if m:
            out[m[1]] = int(m[2])
    return out


def parse_scrub_devices(text):
    """`btrfs scrub status -R -d` repeats those counters under a per-device header"""
    devs, cur = [], None
    for line in text.splitlines():
        m = re.match(r"Scrub device (\S+) \(id (\d+)\)", line)
        if m:
            cur = {"path": m[1], "devid": int(m[2]), "counters": {}}
            devs.append(cur)
            continue
        m = re.match(r"\s+([a-z_]+):\s+(-?\d+)\s*$", line)
        if m and cur is not None:
            cur["counters"][m[1]] = int(m[2])
    return devs


def scrub_errors(counters):
    """Non-zero error classes only, in the short form btrfs uses on its summary line"""
    return {k.replace("_errors", ""): counters[k]
            for k in SCRUB_ALL + ("uncorrectable_errors", "corrected_errors")
            if counters.get(k)}


def scrub_signature(info):
    """What an acknowledgement is bound to: this scrub run and this result.
    A later scrub, or the same scrub reporting more, does not match and rings again."""
    c = info.get("counters") or {}
    parts = ",".join(f"{k}={c[k]}" for k in sorted(SCRUB_ALL + ("uncorrectable_errors",)) if c.get(k))
    return f"{info.get('started')}|{parts}"


def scrub_judgement(info, report=None, ack=None, state=None):
    """(level, text) for the last scrub — ok / info (reviewed, or nothing left to fix) /
    warn (look) / fail (damage)"""
    counters = info.get("counters") or {}
    if not counters:
        return "ok", "no scrub has run yet"
    hard = {k: counters[k] for k in SCRUB_HARD if counters.get(k)}
    supers = {k: counters[k] for k in SCRUB_SUPER if counters.get(k)}
    soft = {k: counters[k] for k in SCRUB_SOFT if counters.get(k)}
    corrected = counters.get("corrected_errors", 0)
    uncorrectable = counters.get("uncorrectable_errors", 0)

    def where_for(classes):
        for d in info.get("devices") or []:
            if any(d["errors"].get(k.replace("_errors", "")) for k in classes):
                return f" on {os.path.basename(d['path'])} (devid {d['devid']})"
        return ""
    show = lambda d: " ".join(f"{k.replace('_errors', '')}={v}" for k, v in d.items())

    # Device I/O errors are what tell a drive that was disconnected (it missed writes, and
    # the scrub finds them stale) from one that silently hands back wrong data. Once this
    # scrub is itself the error baseline, the errors that led up to it are no longer
    # "since", but they still explain its result.
    dev_errs = sum(errors_since_scrub(d, state) for d in report.devs) if report else 0
    base = (state.data.get("err_scrub_base") or {}) if state else {}
    if base.get("scrub_started") and base.get("scrub_started") == info.get("started"):
        dev_errs += base.get("errors_before", 0)

    def reviewed(classes):
        when = time.strftime("%Y-%m-%d", time.localtime(ack.get("at", 0)))
        return f"scrub reported {show(classes)}{where_for(SCRUB_ALL)}; reviewed {when}"

    repaired = corrected and not uncorrectable
    if hard:
        if not repaired:
            return "fail", f"scrub found damaged data: {show(hard)}{where_for(SCRUB_HARD)}, {uncorrectable} uncorrectable"
        if ack:
            return "info", reviewed(hard)
        cause = (f"the devices report {dev_errs} I/O error(s), as a drive that was disconnected does"
                 if dev_errs else "the drive returned bad data")
        return "warn", (f"scrub repaired {corrected} block(s) from the mirror ({show(hard)}{where_for(SCRUB_HARD)}) "
                        f"— {cause}, but nothing was lost. `braidctl scrub --ack` once reviewed")

    if supers:
        swhere = where_for(SCRUB_SUPER)
        stale = info.get("supers_stale")
        if stale is None:
            return "warn", (f"scrub found {show(supers)}{swhere}: superblock copies that did not match, "
                            "and whether they have been rewritten since could not be checked")
        if stale:
            behind = ", ".join(f"devid {k} at generation {old} < {cur}" for k, (old, cur) in sorted(stale.items()))
            return "warn", (f"superblock copies are still behind the filesystem ({behind}); every commit "
                            "should rewrite them, so writes to that drive may be failing")
        if not soft:
            return "info", (f"scrub found {show(supers)}{swhere}: superblock copies that were behind, as a "
                            "drive that was away comes back. All copies have been rewritten since")

    if soft:
        where = where_for(SCRUB_SOFT)
        if dev_errs and repaired:
            # Missed writes read as stale tree blocks, and the mirror rewrote every one.
            # Real, and already repaired: the same verdict as repaired csum errors.
            if ack:
                return "info", reviewed(soft)
            return "warn", (f"scrub repaired {corrected} block(s) from the mirror ({show(soft)}{where}) — the "
                            f"devices report {dev_errs} I/O error(s), as a drive that was disconnected does, "
                            "but nothing was lost. `braidctl scrub --ack` once reviewed")
        if dev_errs:
            return "fail", (f"scrub found {show(soft)}{where} and the devices report "
                            f"{dev_errs} I/O error(s) — treat this as real")
        if ack:
            return "info", reviewed(soft)
        return "warn", (f"scrub reported {show(soft)}{where}, but no data failed its checksum "
                        f"(csum=0 read=0) and the device error counters are clean. "
                        f"A block freed and reused mid-scrub looks exactly like this. "
                        f"Re-scrub while the pool is idle, or `braidctl scrub --ack` once reviewed")
    return "ok", "no errors found"


def scrub_info(pool, state=None, report=None):
    """Parse `btrfs scrub status` into a small dict, with braidctl's own reading of it"""
    out = {}
    for line in pool.scrub_status().splitlines():
        m = re.match(r"\s*(Status|Scrub started|Scrub resumed|Duration|Total to scrub|Rate|Error summary"
                     r"|Time left|ETA|Bytes scrubbed):\s+(.*)", line)
        if m:
            out[m[1]] = m[2].strip()
    info = {"status": out.get("Status"), "started": None, "duration": out.get("Duration"),
            "total": out.get("Total to scrub"), "rate": out.get("Rate"),
            "error_summary": out.get("Error summary"),
            # only present while one is running
            "scrubbed": out.get("Bytes scrubbed"), "time_left": out.get("Time left"),
            "eta": out.get("ETA"), "pct": None}
    m = re.search(r"\(([\d.]+)%\)", out.get("Bytes scrubbed") or "")
    if m:
        info["pct"] = float(m[1])
    if out.get("Scrub started"):
        try:
            info["started"] = int(time.mktime(time.strptime(out["Scrub started"], "%a %b %d %H:%M:%S %Y")))
        except ValueError:
            pass
    info["counters"] = parse_scrub_raw(pool.scrub_status(raw=True))
    info["errors"] = scrub_errors(info["counters"])
    # The per-device breakdown costs another btrfs call, so only pay for it when
    # there is something to attribute.
    info["devices"] = [{"path": d["path"], "devid": d["devid"], "errors": scrub_errors(d["counters"])}
                       for d in parse_scrub_devices(pool.scrub_status(raw=True, per_device=True))
                       if scrub_errors(d["counters"])] if info["errors"] else []
    saved = (state.data.get("scrub_ack") if state else None) or None
    ack = saved if saved and saved.get("signature") == scrub_signature(info) else None
    info["ack"] = ack
    if info["counters"].get("super_errors"):
        devids = [d["devid"] for d in info["devices"] if d["errors"].get("super")]
        info["supers_stale"] = pool.stale_superblocks(devids) if devids else None
    info["level"], info["text"] = scrub_judgement(info, report=report, ack=ack, state=state)
    return info


def errors_since_scrub(d, state):
    """This device's btrfs error count since the last scrub that left every block good.

    btrfs keeps device error counters forever — across reboots, remounts and a drive
    being unplugged — and braidctl never resets them. So a drive that was away for two
    minutes shows its failed reads and writes for the rest of its life, and a real error
    tomorrow is one more on top of 152,213. A scrub that finished with nothing
    uncorrectable is the point where every copy was verified or rewritten from a good one;
    counting from there keeps btrfs's record intact (`btrfs device stats` still shows it)
    and makes the number mean "since the data was last known good"."""
    errors = d.errors or {}
    total = sum(errors.values())
    base = (((state.data.get("err_scrub_base") or {}).get("devices") or {}).get(str(d.devid))
            if state else None)
    if base is None:
        return total
    if any(v < base.get(k, 0) for k, v in errors.items()):
        return total            # reset by hand (`btrfs device stats -z`): all of it is new
    return sum(max(0, v - base.get(k, 0)) for k, v in errors.items())


def record_scrub_baseline(state, info, report):
    """Take the error-counter baseline from a scrub that left every block good. Once per run.

    That means finished (not cancelled or interrupted), nothing uncorrectable, and no member
    missing. Blocks repaired from the mirror do not disqualify it: the resync scrub after a
    drive comes back repairs exactly the writes it missed, and demanding a repair-free scrub
    would keep that drive's absence on the counter until a second multi-hour scrub. The
    repairs are not forgotten — the scrub's own result still says so until the next one.

    Recorded by the monitor pass, so errors in the minutes between the scrub ending and that
    pass fold into the baseline; the pass notifies about any increase before it does."""
    if info.get("status") != "finished" or not info.get("started") or report.gone:
        return False
    if (info.get("counters") or {}).get("uncorrectable_errors"):
        return False
    if (state.data.get("err_scrub_base") or {}).get("scrub_started") == info["started"]:
        return False
    before = sum(errors_since_scrub(d, state) for d in report.devs)
    state.data["err_scrub_base"] = {"scrub_started": info["started"], "at": int(time.time()),
                                    "errors_before": before,
                                    "devices": {str(d.devid): dict(d.errors or {}) for d in report.devs if d.present}}
    LOG.info("error counters counted from the scrub of %s",
             time.strftime("%Y-%m-%d %H:%M", time.localtime(info["started"])))
    return True


def snapshot_dict(cfg, pool, report, state, note=None):
    """The whole picture as one JSON-able dict: capacity, protection, per-drive health,
    the verdict line, rebuild and scrub state.

    Split out of write_snapshot so that anything serving this — the status file a
    dashboard reads, an HTTP handler — builds it from one place. Two producers of the
    same shape drift, and a dashboard that disagrees with `braidctl status` is worse
    than no dashboard."""
    snap = {"version": 1, "generated_at": int(time.time()),
            "mountpoint": cfg["mountpoint"], "label": cfg["label"]}
    inv = state.data.get("inventory", {})
    if report is None:
        _, members, stale = pool.scan_members()
        snap.update({"state": "UNMOUNTED", "note": note, "usage": None, "devices": [],
                     "members_visible": [m["path"] for m in members],
                     "stale_members": stale,
                     "verdict": {"level": "fail", "text": note or "not mounted"}})
    else:
        u = report.usage
        level, text = verdict(report, cfg)
        devices = []
        for d in report.devs:
            known = inv.get(str(d.devid), {})
            hlevel, htext = device_health(d)
            devices.append({
                "devid": d.devid,
                "name": d.name if d.present or d.detached else f"was {os.path.basename(known.get('path') or d.path or '?')}",
                "path": d.path or known.get("path"),
                "model": d.model or known.get("model"),
                "serial": d.serial or known.get("serial"),
                "size": d.size or known.get("size"),
                "alloc": d.used,
                "missing": d.missing or not d.present,
                "present": d.present,
                "detached": d.detached,
                "errors": d.errors,
                "errors_total": sum(d.errors.values()) if d.errors else 0,
                # what the page shows: since the last scrub that left every block good (errors_total is btrfs's lifetime count)
                "errors_new": errors_since_scrub(d, state),
                "smart": d.smart,
                "health": htext,
                "health_level": hlevel,
            })
        snap.update({
            "state": report.state_label(),
            "profiles": u.profiles,
            "mounted_degraded": report.mounted_degraded,
            "balance_running": report.balance_running,
            "usage": {"raw": u.raw, "used": u.used, "data_used": u.data_used, "meta_used": u.meta_used,
                      "free": u.free, "usable": u.usable, "missing_bytes": u.missing_bytes,
                      "pct": round(u.used / u.usable * 100, 1) if u.usable else None},
            "devices": devices,
            "verdict": {"level": level, "text": text},
            "missing_since": {k: int(v) for k, v in state.data.get("missing", {}).items()},
            "rebuild": state.data.get("rebuild"),
            "errors_since": (state.data.get("err_scrub_base") or {}).get("scrub_started"),
            "scrub": scrub_info(pool, state, report),
            "balance": pool.balance_status(),
            "stale_members": pool.stale_members(report),
        })
    return snap


def write_snapshot(cfg, pool, report, state, note=None):
    """World-readable copy of the above, so a dashboard running without root can show
    the pool. Everything btrfs reports about devices and usage needs root."""
    path = cfg.get("status_file")
    if not path:
        return
    snap = snapshot_dict(cfg, pool, report, state, note)
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap, indent=1, ensure_ascii=False))
        os.chmod(tmp, 0o644)
        os.replace(tmp, p)
    except OSError as e:
        LOG.warning("could not write status snapshot %s: %s", path, e)


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


EVICTED_KEEP = 32


def eviction_record(evicted, sb):
    """The record of the eviction that left this superblock behind, or None. A record is
    written at the pool's generation right after the eviction, so the drive it is about is
    older than it; btrfs reuses devids, so of the records for this devid the matching one
    is the earliest that is newer than the drive. A serial on both sides must agree."""
    cands = [e for e in evicted
             if e.get("fsid") == sb["fsid"] and e.get("devid") == sb["devid"]
             and sb["generation"] < (e.get("generation") or 0)
             and (not e.get("serial") or not sb.get("serial") or e["serial"] == sb["serial"])]
    return min(cands, key=lambda e: e["generation"]) if cands else None


def record_evicted(state, pool, gone):
    """Remember drives evicted while absent. btrfs could not scratch their superblock, so
    if one is attached again it still claims to be devid N of this pool; the record is
    what names it then, and what tells it apart from a member if the device list cannot
    be read. The generation is the pool's right after the eviction: the drive's own is
    older, a drive added later under the same devid is newer."""
    fsid = pool.uuid()
    newest, members, _ = pool.scan_members()
    gen = newest["generation"] if members else None
    inv = state.data.setdefault("inventory", {})
    evicted = state.data.setdefault("evicted", [])
    for d in gone:
        known = inv.pop(str(d.devid), {})
        state.data["missing"].pop(str(d.devid), None)
        evicted.append({"fsid": fsid, "devid": d.devid, "serial": known.get("serial") or "",
                        "model": known.get("model") or "", "size": known.get("size") or d.size,
                        "path": known.get("path") or "", "at": int(time.time()), "generation": gen})
    del evicted[:-EVICTED_KEEP]


def stale_line(s):
    return (f"Stale member: {s['path']} (devid {s['devid']}, generation {s['generation']}) — "
            f"not part of the pool; `braidctl wipe-stale {s['path']}` before reuse")


def ensure_mounted(cfg, pool, notifier, state, degraded=True):
    """Mount normally, else degraded. Returns whether the pool ended up mounted.

    degraded=False is for the udev-triggered pass. At boot it fires on the FIRST member to
    appear, while the rest are still enumerating: a normal mount fails for want of devices,
    and a degraded fallback would then succeed with drives that are seconds away. Deciding
    that a drive is really absent is braid-mount's job (it waits for enumeration) and the
    timer's. After a guard unmount the udev pass still recovers the pool: the normal mount
    succeeds as the last member returns."""
    if pool.is_mounted():
        return True
    scan = pool.scan_members()
    _, members, stale = scan
    present = [m["path"] for m in members]
    if not present:
        for s in stale:
            # Registered, it would skew `btrfs device ready` once the real members arrive
            pool.forget(s["path"])
        if state.should_notify("no-members", cfg["remind_interval_sec"]):
            notifier.send("No pool drives are visible",
                          f"{cfg['mountpoint']} is not mounted and no member of the pool "
                          f"(LABEL={cfg['label']}) can be found.\nCheck the cables and power."
                          + "".join("\n" + stale_line(s) for s in stale))
        return False
    complete, devids, expected = pool.members_complete(scan)
    if complete:
        p = pool.mount(check=False, scan=scan)
        if p.returncode == 0:
            LOG.info("mounted normally")
            state.clear_notify("no-members")
            return True
        if pool.is_mounted():
            # Another braidctl that does not take the lock (or an admin) got there first.
            LOG.info("mount failed because the pool was mounted meanwhile: %s", p.stderr.strip())
            return True
        why = f"normal mount failed: {p.stderr.strip()}"
    else:
        # See Pool.members_complete: a normal mount that is bound to fail is not harmless.
        why = f"{len(devids)} of {expected} members attached, so no normal mount was attempted"
    if not degraded:
        LOG.info("%s; not trying degraded from this pass", why)
        return False
    LOG.warning("%s — trying degraded", why)
    p = pool.mount(degraded=True, check=False, scan=scan)
    if p.returncode != 0:
        if state.should_notify("mount-failed", cfg["remind_interval_sec"]):
            notifier.send("Cannot mount the pool",
                          f"Both a normal and a degraded mount failed.\n"
                          f"Members visible: {', '.join(present)}\n\n{p.stderr}")
        return False
    notifier.send("Mounted degraded",
                  f"{cfg['mountpoint']} was mounted with a drive missing (degraded).\n"
                  f"Members visible: {', '.join(present)}\n"
                  f"If the drive does not return within {cfg['debounce_sec']} seconds, the pool will be "
                  "re-protected automatically provided the remaining capacity is sufficient.")
    return True


def cancel_scrub_for_rebuild(pool):
    """Re-protecting the pool outranks verifying it. A scrub holds the disks for hours;
    a rebuild that waits for one is a rebuild that has not started."""
    if "running" not in pool.scrub_status():
        return False
    LOG.warning("cancelling the running scrub: a rebuild needs the disks")
    run(["btrfs", "scrub", "cancel", pool.mnt], check=False)
    return True


def cancel_balance_for_rebuild(pool):
    """Re-protecting the pool outranks spreading it evenly. Before `braidctl balance` a
    full balance only ran briefly after an add; now it is something a person starts and
    leaves running for hours, and a rebuild or an eviction that waits for it leaves the
    pool on single copies, or a failing drive in it, for that long. A device remove cannot
    run beside a balance at all (it is an exclusive operation).

    Returns whether the way is clear. A convert is not cancelled: it is putting data that
    was written single-copy back onto two drives, the thing a rebuild is for."""
    bal = pool.balance_status()
    if not (bal["running"] or bal["paused"]):
        return True
    if bal["converting"]:
        LOG.info("a convert balance is running; re-protection waits for it")
        return False
    LOG.warning("cancelling the running balance (%s): re-protection needs the pool", bal.get("text") or "")
    try:
        run(["btrfs", "balance", "cancel", pool.mnt], check=False, timeout=900)
    except BraidError as e:
        LOG.warning("%s", e)
        return False
    return not pool.balance_running()


RESYNC_UNIT = "braid-resync-scrub"


def start_scrub_background(cfg):
    """Start a scrub that outlives this process, and return at once. Returns (started, why not).

    It gets a transient unit of its own, running `braidctl scrub`, because both ways of
    starting it from here went wrong on the real pool:

    - `btrfs scrub start` through run(): without -B, btrfs forks the scrub into a child
      that keeps the stdout and stderr it was given (unlike `balance start --bg`, which
      points them at /dev/null). The child held run()'s pipes for the whole scrub and
      run() waited for EOF, so the monitor pass sat holding the pool lock for hours:
      every timer pass skipped, and a drive lost meanwhile was neither noticed nor
      re-protected.
    - The same child with its output sent elsewhere: the pass returned at once, and the
      scrub still died a second later with "not finished ... status: -4". The monitor
      runs as a oneshot service, and when a service ends systemd kills everything left
      in its cgroup — the scrub included.

    `braidctl scrub` waits for the scrub (-B), notifies if it finds damage, and reports
    when it is done, so the resync that the re-attach notice promises is seen through."""
    p = run(["systemd-run", f"--unit={RESYNC_UNIT}", "--collect", "--no-block",
             "--description=braid pool resync scrub (a member came back)",
             "-p", "TimeoutStartSec=infinity", "-p", "Nice=10",
             "-p", "IOSchedulingClass=best-effort", "-p", "IOSchedulingPriority=7",
             sys.executable, os.path.realpath(__file__), "-c", cfg.get("__path__", CONFIG_PATH), "scrub"],
            check=False)
    return p.returncode == 0, (p.stderr or p.stdout).strip()


def rebuild_missing(cfg, pool, notifier, state, report):
    """Evict the missing devices and restore raid1 across the survivors (Drobo's re-layout)"""
    gone = report.gone
    names = "\n".join("  - " + describe(d, state) for d in gone)
    ok, after, needed = can_rebuild(report, cfg)
    state.data["rebuild"] = {"started": time.time(), "devids": [d.devid for d in gone]}
    state.save()
    notifier.send("Starting automatic re-protection",
                  f"Missing drive(s):\n{names}\n\n"
                  f"Re-protecting across the remaining {len(report.devs) - len(gone)} drive(s).\n"
                  f"Usable afterwards {fmt_bytes(after)} / used {fmt_bytes(needed)}\n"
                  "This takes a while; you will get another message when it finishes.")
    try:
        cancel_scrub_for_rebuild(pool)
        pool.compact()
        # 1) Try removing by devid on the live filesystem (works when the kernel tracks the loss)
        live_ok = True
        for d in gone:
            p = pool.remove_device(d.devid)
            if p.returncode != 0:
                LOG.info("live remove failed (devid %d): %s", d.devid, (p.stderr or p.stdout).strip())
                live_ok = False
                break
        # 2) Otherwise take the brief downtime: remount degraded and remove missing
        if not live_ok:
            LOG.info("falling back to degraded remount; stopping %s", cfg["samba_units"])
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
        # 3) Anything written single-copy while degraded goes back to raid1
        if pool.usage().multiple_profiles:
            LOG.info("multiple profiles present -> soft convert balance")
            pool.balance_soft_convert()
    except BraidError as e:
        state.data["rebuild"] = None
        state.save()
        notifier.send("Automatic re-protection failed",
                      f"{e}\n\nThe pool is still running, degraded. It needs attention:\n"
                      f"  sudo braidctl status\n  sudo btrfs device remove missing {cfg['mountpoint']}")
        return
    left = {d.devid for d in pool.devices()}
    record_evicted(state, pool, [d for d in gone if d.devid not in left])
    state.data["rebuild"] = None
    state.save()
    new = collect(pool)
    notifier.send("Automatic re-protection complete",
                  f"Removed:\n{names}\n\n" + render_status(new, cfg, state))


def reattach(cfg, pool, notifier, state, returned):
    """A member that was only temporarily absent came back: remount to pick it up,
    then scrub so the copies it missed get rewritten."""
    names = ", ".join(f"devid {devid} ({os.path.basename(p)})" for p, devid in returned)
    LOG.info("re-attaching returned member(s): %s", names)
    for u in cfg["samba_units"]:
        run(["systemctl", "stop", u], check=False)
    try:
        pool.umount()
        for p, _ in returned:
            run(["btrfs", "device", "scan", p], check=False)
        if not (pool.members_complete()[0] and pool.mount(check=False).returncode == 0):
            LOG.warning("members incomplete or normal mount failed -> degraded")
            pool.mount(degraded=True)
    except BraidError as e:
        notifier.send("Failed to re-attach a returned drive", f"{names}\n{e}")
        return False
    finally:
        for u in cfg["samba_units"]:
            run(["systemctl", "start", u], check=False)
    new = collect(pool)
    still = {d.devid for d in new.gone} & {devid for _, devid in returned}
    if still:
        notifier.send("Could not re-attach a returned drive",
                      f"Of {names}, devid {sorted(still)} is still missing.\n\n"
                      + render_status(new, cfg, state))
        return False
    for _, devid in returned:
        state.data["missing"].pop(str(devid), None)
    state.clear_notify("cannot-rebuild")
    # Whatever was written while it was away is not on it; scrub rewrites it from the mirror
    started, said = start_scrub_background(cfg)
    if started:
        resync = (f"A scrub is running to resync what it missed ({RESYNC_UNIT}.service); "
                  "you will hear when it finishes.")
    else:
        LOG.warning("could not start the resync scrub: %s", said)
        resync = (f"The scrub that resyncs what it missed did not start ({said}). "
                  "Run `sudo braidctl scrub` once the pool is idle.")
    notifier.send("Drive returned and was re-attached",
                  f"{names} is back in the pool. {resync}\n\n"
                  + render_status(new, cfg, state))
    return True


def cmd_monitor(cfg, args):
    require_root()
    pool = Pool(cfg)
    notifier = Notifier(cfg)
    state = State(cfg["state_file"])
    if not state.lock(quiet=args.if_idle):
        return
    state.load()
    now = time.time()
    debounce = args.debounce if args.debounce is not None else cfg["debounce_sec"]

    if not ensure_mounted(cfg, pool, notifier, state, degraded=not args.if_idle):
        state.save()
        write_snapshot(cfg, pool, None, state, note=f"{cfg['mountpoint']} is not mounted")
        return
    report = collect(pool)
    update_inventory(state, report.devs)
    missing = state.data["missing"]

    # --- a member that was only temporarily absent came back: re-attach, do not rebuild ---
    #
    # This is also what rescues the pool after a USB bus re-enumeration, where every drive
    # comes back under a NEW device node. btrfs will not swap new nodes into a mounted
    # filesystem — the kernel just logs "duplicate device ... scanned by udev-worker" — so
    # the mount is left holding dead block devices. Writes to it then land in page cache and
    # are lost, while reporting success. Only a remount fixes it, and until it happens the
    # damage is silent, which is why a udev rule triggers this pass within a second or two
    # rather than waiting for the timer.
    returned, stale = pool.returned_members(report)
    if returned and len(returned) == len(report.devs):
        LOG.warning("every member returned under a new device node; the mount is stale "
                    "and writes to it are being lost. Remounting now.")
    if returned and not report.balance_running and state.data.get("rebuild") is None:
        if reattach(cfg, pool, notifier, state, returned):
            report = collect(pool)
    for s in stale:
        p = s["path"]
        if state.should_notify(f"stale-{p}", cfg["remind_interval_sec"]):
            what = " ".join(x for x in (s.get("model"), s.get("serial") and f"serial {s['serial']}") if x)
            when = (f", evicted {time.strftime('%Y-%m-%d', time.localtime(s['evicted_at']))}"
                    if s.get("evicted_at") else "")
            notifier.send("A previously removed drive is attached",
                          f"{p}{f' ({what})' if what else ''} was devid {s['devid']} of this pool{when}.\n"
                          "It was evicted while it was absent, so it still carries the pool's "
                          f"superblock (generation {s['generation']}). It is not a member and is "
                          "not counted as one.\n\n"
                          f"To erase it: sudo braidctl wipe-stale {p}\n"
                          f"To put it back in the pool: sudo braidctl add --force {p}")

    # --- losses ---
    gone_ids = {str(d.devid) for d in report.gone}
    for d in report.gone:
        key = str(d.devid)
        if key not in missing:
            missing[key] = now
            if d.detached:
                # Only reached when the re-attach above did not happen or did not work
                # (a balance or rebuild running, or the unmount refused while something is busy).
                notifier.send("A drive is connected but the pool is not using it",
                              f"{describe(d, state)} is attached, but the pool was mounted without it and "
                              "nothing is being written to it. A remount brings it back in:\n"
                              "  sudo systemctl stop smbd && sudo umount "
                              f"{cfg['mountpoint']} && sudo braidctl mount && sudo systemctl start smbd\n"
                              f"Otherwise it is treated as lost: after {int(debounce)} seconds the pool is "
                              "re-protected without it.\n\n" + render_status(report, cfg, state))
                continue
            notifier.send("Drive lost",
                          f"{describe(d, state)} disappeared.\n"
                          f"If it does not return within {int(debounce)} seconds, the pool will be "
                          "re-protected automatically provided the remaining capacity is sufficient.\n\n"
                          + render_status(report, cfg, state))
    for key in list(missing):
        if key not in gone_ids:
            LOG.info("devid %s is back", key)
            del missing[key]
            notifier.send("Drive is back",
                          f"devid {key} is visible again. No rebuild will be performed.\n\n"
                          + render_status(report, cfg, state))
            state.clear_notify("cannot-rebuild")

    # --- error counters / SMART ---
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
            notifier.send("I/O errors are increasing on a drive",
                          f"{describe(d, state)}: " + ", ".join(f"{k}+{v}" for k, v in delta.items())
                          + f"\nTotals: {d.errors}\nSMART: {d.smart}")
        if sum(delta.values()) >= cfg["error_threshold"]:
            failing.append(d)
        # Warn on a SMART failure, pending/uncorrectable sectors, or a reallocation count that grew.
        # Reallocations are compared against the last reading so an old drive sitting at a stable
        # count is not an alarm every single day.
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
            notifier.send("SMART reports a degrading drive",
                          f"{describe(d, state)}: {s}\n"
                          + (f"Reallocated sectors went from {prev_realloc} to {realloc}.\n" if grew else "")
                          + "Consider removing it with `braidctl remove` and replacing it soon.")

    # --- automatic re-protection ---
    expired = [d for d in report.gone if now - missing.get(str(d.devid), now) >= debounce]
    if cfg["auto_evict_on_errors"] and failing and not report.gone:
        # Failing but present: remove it live; unreadable parts come from the mirror
        d = failing[0]
        ok, after, needed = can_rebuild(report, cfg, losing=[d])
        if ok and cancel_balance_for_rebuild(pool):
            notifier.send("Evicting a failing drive automatically",
                          f"{describe(d, state)}\nUsable afterwards {fmt_bytes(after)}")
            p = pool.remove_device(d.devid)
            notifier.send("Automatic eviction " + ("complete" if p.returncode == 0 else "failed"),
                          (p.stderr or p.stdout or "") + "\n\n" + render_status(collect(pool), cfg, state))
    elif expired:
        ok, after, needed = can_rebuild(report, cfg)
        if ok:
            if cancel_balance_for_rebuild(pool):
                rebuild_missing(cfg, pool, notifier, state, report)
                report = collect(pool)
        elif state.should_notify("cannot-rebuild", cfg["remind_interval_sec"]):
            short = needed / cfg["rebuild_margin"] - after
            notifier.send("Drive addition required (cannot re-protect)",
                          "Missing drive(s):\n"
                          + "\n".join("  - " + describe(d, state) for d in expired)
                          + f"\n\nRemaining usable capacity {fmt_bytes(after)} is less than the "
                          f"{fmt_bytes(needed)} in use, so redundancy cannot be restored as things stand.\n"
                          f"Add at least {fmt_bytes(short)} more: sudo braidctl add /dev/sdX\n"
                          "The data is still readable and writable, but losing one more drive loses it.\n\n"
                          + render_status(report, cfg, state))

    # --- capacity ---
    u = report.usage
    pct = (u.used / u.usable * 100) if u.usable else 0
    if pct >= cfg["warn_percent"] and state.should_notify("capacity", cfg["remind_interval_sec"]):
        notifier.send(f"Pool is {pct:.0f}% full", render_status(report, cfg, state))
    elif pct < cfg["warn_percent"]:
        state.clear_notify("capacity")

    record_scrub_baseline(state, scrub_info(pool, state, report), report)
    state.save()
    write_snapshot(cfg, pool, report, state)
    LOG.info("monitor: %s used=%s usable=%s gone=%d", report.state_label(),
             fmt_bytes(u.used), fmt_bytes(u.usable), len(report.gone))


# ---------------------------------------------------------------- scrub / mount

def scrub_ack(cfg, pool):
    """Record that a human looked at the last scrub result and found nothing wrong.

    Without this the only ways out of a stale verify-only result are a five-hour
    re-scrub or deleting btrfs's own status file — one costs a night of I/O, the
    other destroys the record. Damage is deliberately not acknowledgeable."""
    state = State(cfg["state_file"]).load()
    info = scrub_info(pool, state, collect(pool))
    if not info.get("counters"):
        raise BraidError("no scrub result to acknowledge")
    if not info["errors"]:
        raise BraidError("the last scrub reported no errors; nothing to acknowledge")
    if info["level"] == "fail":
        raise BraidError(f"{info['text']}\n"
                         "This is damage, not a false alarm, and cannot be acknowledged away")
    if info["level"] == "info":
        print(info["text"])
        return
    state.data["scrub_ack"] = {"signature": scrub_signature(info), "at": int(time.time()),
                               "summary": info["error_summary"], "started": info["started"]}
    state.save()
    print(f"acknowledged: {info['error_summary']} (scrub of "
          f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(info['started'] or 0))}). "
          "The next scrub that reports anything different will raise it again")


def cmd_scrub(cfg, args):
    require_root()
    pool = Pool(cfg)
    notifier = Notifier(cfg)
    if not pool.is_mounted():
        raise BraidError(f"{cfg['mountpoint']} is not mounted")
    if args.ack:
        lock = hold_lock(cfg)                  # noqa: F841 — a state.json write
        return scrub_ack(cfg, pool)
    if "running" in pool.scrub_status():
        raise BraidError("a scrub is already running")
    # What `Conflicts=braid-monitor.service` was reaching for, in the one place that can
    # actually express it. The unit-level version stopped the scrub every time the
    # five-minute monitor timer fired, so a monthly scrub never finished.
    if pool.balance_running():
        raise BraidError("a balance is running (rebuild or rebalance); scrub once it finishes")
    LOG.info("scrub starting")
    p = run(["btrfs", "scrub", "start", "-B", "-d", cfg["mountpoint"]], check=False)
    state = State(cfg["state_file"]).load()
    info = scrub_info(pool, state, collect(pool))
    body = f"exit={p.returncode}\n{p.stdout}\n{pool.scrub_status(per_device=True)}"
    if info["level"] == "fail":
        notifier.send("Scrub found damaged data", f"{info['text']}\n\n{body}")
        sys.exit(1)
    if info["level"] == "warn":
        # Neither silence nor an alarm: somebody has to look, once.
        LOG.warning("scrub needs review: %s", info["text"])
        notifier.send("Scrub needs review", f"{info['text']}\n\n{body}")
        return
    if info["status"] in ("aborted", "interrupted"):
        # Cancelled — by `braidctl cancel`, or by a rebuild that needed the disks. What it
        # found before that was reported above; "finished, no errors" would be a lie.
        LOG.warning("scrub %s after %s; `btrfs scrub resume %s` continues it",
                    info["status"], info["duration"], cfg["mountpoint"])
        print(f"scrub {info['status']} after {info['duration']}")
        return
    LOG.info("scrub finished: %s", info["text"])
    if not args.quiet:
        notifier.send("Scrub finished, no errors" if info["level"] == "ok" else "Scrub finished", f"{info['text']}\n\n{body}")


# ---------------------------------------------------------------- cancel

def braidctl_subcommand(argv):
    """'remove' out of [python3, /usr/local/sbin/braidctl, -c, x.toml, remove, 3]"""
    it = iter(argv[1:])
    for a in it:
        if a in ("-c", "--config"):
            next(it, None)
        elif not (a.startswith("-") or a.endswith(("braidctl", ".py"))):
            return a
    return None


def btrfs_processes(mnt):
    """btrfs commands acting on this mountpoint, as {pid, ppid, argv}.

    A device remove, like a foreground balance, does its work inside the ioctl of the
    process that asked for it, and relocation checks for a fatal signal between extents.
    That is what makes signalling the process a way to stop the work."""
    want = os.path.realpath(mnt)
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            argv = [a for a in Path(f"/proc/{entry}/cmdline").read_bytes().decode("utf-8", "replace").split("\0") if a]
            if len(argv) < 3 or os.path.basename(argv[0]) != "btrfs" or os.path.realpath(argv[-1]) != want:
                continue
            ppid = int(Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        out.append({"pid": int(entry), "ppid": ppid, "argv": argv})
    return out


def is_remover(proc):
    return "device" in proc["argv"] and bool({"remove", "delete"} & set(proc["argv"]))


def pid_alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False


def running_operations(cfg, pool, holder=None, balance=None, scrub_running=None):
    """What is running on the pool that someone might want to stop, and whether stopping it
    is safe. `braidctl cancel` acts on this and the web page shows it, so they cannot
    disagree about what may be stopped.

    The three long operations all stop cleanly (docs/testing.md has the loop-device runs):
    a scrub is cancelled and resumable; a balance stops after the chunk it is moving; a
    device remove stops when its process gets a fatal signal, and the device gets its size
    back and stays a member. What is refused is what braidctl would undo or suffer from:
    the monitor re-protecting the pool, and a convert putting data written while degraded
    back onto two drives — nothing restarts that one.

    holder, balance and scrub_running are for callers that already have them."""
    if not pool.is_mounted():
        return []
    if holder is None:
        holder = lock_holder(Path(cfg["state_file"]).with_suffix(".lock"))
    held_by = braidctl_subcommand(holder.get("argv") or []) if holder else None
    procs = btrfs_processes(pool.mnt)
    removers = [p for p in procs if is_remover(p)]
    monitor = ("braidctl monitor is re-protecting the pool; stopping it would only postpone "
               "that to the next pass")
    ops = []

    if scrub_running is None:
        scrub_running = "running" in pool.scrub_status()
    if scrub_running:
        ops.append({"kind": "scrub", "label": "scrub", "cancellable": True,
                    "note": f"Progress is kept: `btrfs scrub resume {pool.mnt}` continues from where it stopped."})

    bal = balance if balance is not None else pool.balance_status()
    if held_by == "remove" or removers:
        target = holder["argv"][-1] if held_by == "remove" else removers[0]["argv"][-2]
        op = {"kind": "remove", "target": target,
              "label": f"device remove ({'devid ' + target if target.isdigit() else target})",
              "phase": "compacting" if bal["running"] else "relocating"}
        if held_by == "monitor":
            op.update(cancellable=False, reason=monitor)
        elif target == "missing":
            op.update(cancellable=True, note=(
                "The pool stays degraded until a removal of the missing drive completes. The monitor "
                f"starts one itself once a drive has been gone for {cfg['debounce_sec']} seconds."))
        else:
            op.update(cancellable=True, note=(
                "The drive stays in the pool. Data already moved off it stays on the other drives, "
                "still mirrored; nothing is lost."))
        ops.append(op)

    if bal["running"] or bal["paused"]:
        op = {"kind": "balance", "label": "balance" + ("" if bal["running"] else " (paused)"),
              "pct": bal["pct"]}
        if held_by == "monitor":
            op.update(cancellable=False, reason=monitor)
        elif held_by == "remove":
            op.update(cancellable=False, reason="this is the compaction step of the device remove; cancel that instead")
        elif bal["converting"]:
            op.update(cancellable=False, reason=(
                "it is converting chunks back to raid1. Stopping it leaves data written while a "
                "drive was missing with a single copy, and nothing starts it again"))
        else:
            op.update(cancellable=True, note=(
                "Chunks already balanced stay balanced. The pool stays fully protected; its free "
                "space is just spread less evenly until a balance runs to the end."))
        ops.append(op)
    return ops


def cancel_operation(cfg, kind, timeout=120):
    """Stop one entry of running_operations(). Returns what happened, raises if refused."""
    pool = Pool(cfg)
    op = next((o for o in running_operations(cfg, pool) if o["kind"] == kind), None)
    if op is None:
        raise BraidError(f"no {kind} is running on {pool.mnt}")
    if not op["cancellable"]:
        raise BraidError(f"not cancelling the {kind}: {op['reason']}")
    if kind == "scrub":
        run(["btrfs", "scrub", "cancel", pool.mnt], timeout=timeout)
        return f"scrub cancelled; `btrfs scrub resume {pool.mnt}` continues it"
    if kind == "balance":
        # Returns once the kernel has stopped, which is after the chunk it is moving.
        run(["btrfs", "balance", "cancel", pool.mnt], timeout=timeout)
        return "balance cancelled"

    LOG.warning("cancelling device remove (%s)", op["target"])
    holder = lock_holder(Path(cfg["state_file"]).with_suffix(".lock"))
    waiting = []
    if holder and braidctl_subcommand(holder["argv"]) == "remove":
        # The braidctl first, so it cannot move on to its next step. Its KeyboardInterrupt
        # kills the btrfs child it is waiting on, compaction balance or device remove.
        os.kill(holder["pid"], signal.SIGINT)
        waiting.append(holder["pid"])
        time.sleep(1)
    for p in btrfs_processes(pool.mnt):
        # A remove run as plain `btrfs device remove`, or a child the braidctl has not
        # killed yet. Any fatal signal will do; btrfs installs no handler.
        if is_remover(p) or (holder and p["ppid"] == holder["pid"]):
            os.kill(p["pid"], signal.SIGINT)
            waiting.append(p["pid"])
    deadline = time.time() + timeout
    while any(pid_alive(pid) for pid in waiting):
        if time.time() > deadline:
            raise BraidError(f"asked the device remove to stop, but it is still running after {timeout}s")
        time.sleep(0.5)
    return f"device remove of {op['target']} cancelled; the drive stays in the pool"


def cmd_cancel(cfg, args):
    require_root()
    if not args.what:
        ops = running_operations(cfg, Pool(cfg))
        for o in ops:
            print(f"{o['kind']:8} {o['label']}: " + ("cancellable" if o["cancellable"] else f"not cancellable — {o['reason']}"))
        if not ops:
            print("nothing is running")
        return
    print(cancel_operation(cfg, args.what))


def cmd_web(cfg, args):
    """Hand over to braidweb, which is a separate file on purpose.

    braidctl.py is what removes devices from a live filesystem; it should stay short
    enough to read in one sitting, and a broken or missing web module must not be able
    to take the monitor with it. This is the only subcommand that can fail to import."""
    require_root()
    here = Path(__file__).resolve().parent
    for d in (here, Path("/usr/local/lib/braidctl")):
        if (d / "braidweb.py").exists():
            sys.path.insert(0, str(d))
            break
    else:
        raise BraidError("braidweb.py not found next to braidctl or in /usr/local/lib/braidctl")
    import braidweb
    return braidweb.serve(cfg, args, core=sys.modules[__name__])


def cmd_web_job(cfg, args):
    """Run one braidctl command on behalf of the web view, from inside a transient unit.

    Started by `systemd-run`, so it is a child of pid 1: it survives the web process
    being stopped, killed or idle-exited, it lands in the host mount namespace (a mount
    made in a private one would be invisible to Samba), and it gets the same nice and
    I/O priority as the timer units. What it runs is the plain CLI, so the argv in the
    audit log is the argv a human would have typed."""
    require_root()
    here = Path(__file__).resolve().parent
    for d in (here, Path("/usr/local/lib/braidctl")):
        if (d / "braidweb.py").exists():
            sys.path.insert(0, str(d))
            break
    import braidweb
    return braidweb.run_job(cfg, args, core=sys.modules[__name__])


def cmd_guard(cfg, args):
    """Unmount a pool that can no longer write, so writes fail loudly instead of vanishing.

    When enough devices disappear that btrfs cannot place a full set of copies, the mount
    becomes a zombie: writes land in page cache, `write`, `fsync` and `sync` all return
    success, and the data is discarded at the next remount with every error counter still
    at zero. Measured, not theorised — 128 MB written that way was simply gone.

    A lazy unmount makes the path stop resolving immediately, so the next open fails and
    whatever is writing (Samba, Time Machine) reports an error and retries, which is the
    correct outcome. The monitor remounts as soon as the devices return.

    The threshold matters enormously in the other direction: losing ONE drive out of a
    healthy raid1 must NOT unmount anything — surviving that is the entire point of the
    pool. So this only fires below `copies_required`."""
    require_root()
    pool = Pool(cfg)
    if not pool.is_mounted():
        LOG.info("guard: not mounted, nothing to do")
        return
    devs = pool.devices()
    live = [d for d in devs if d.present]
    need = copies_required(pool.usage().profiles)
    if len(live) >= need:
        LOG.info("guard: %d/%d devices present, a %d-copy pool can still write",
                 len(live), len(devs), need)
        return

    LOG.error("guard: only %d of %d devices present; this pool needs %d to write. "
              "Unmounting so writes fail instead of being silently discarded.",
              len(live), len(devs), need)
    p = run(["umount", "-l", cfg["mountpoint"]], check=False)
    ok = p.returncode == 0
    if not ok:
        LOG.error("guard: lazy unmount failed: %s", (p.stderr or p.stdout or "").strip())
    notifier = Notifier(cfg)
    state = State(cfg["state_file"]).load()
    notifier.send("Pool unmounted: too few devices to write" if ok
                  else "Pool cannot write and could not be unmounted",
                  f"Only {len(live)} of {len(devs)} devices are present and this pool needs "
                  f"{need} to place a write.\n"
                  + ("The mount has been detached so writes fail visibly rather than being "
                     "accepted and thrown away. It will be remounted automatically as soon as "
                     "the devices return.\n"
                     if ok else
                     "The lazy unmount failed, so writes may still be silently discarded.\n")
                  + "\nNote that anything holding a file open across this keeps writing into the "
                    "detached filesystem; only new opens fail.")
    try:
        write_snapshot(cfg, pool, None, state,
                       note=f"unmounted by guard: {len(live)}/{len(devs)} devices present")
    except Exception:  # a snapshot is a nicety; never let it mask the unmount
        pass


def cmd_mount(cfg, args):
    """Mount the pool at boot. This exists because the pool must NOT be in /etc/fstab:
    systemd ties an fstab mount to one backing device and unmounts the filesystem when
    that device disappears — which for a redundant multi-device pool is precisely the
    wrong response to losing a drive. See docs/design.md."""
    require_root()
    pool = Pool(cfg)
    deadline = time.time() + max(0, args.wait)
    # At boot the udev rule starts a monitor pass as the members appear, at the same moment
    # as this. Unlocked, the two raced: one mount lost with EBUSY and fell back to degraded.
    # Holding the lock makes that pass exit as idle; waiting for it covers the reverse order.
    state = State(cfg["state_file"])
    state.lock(wait=args.wait)
    # Quiet retry loop: right after boot the USB bus is still enumerating, and
    # "not yet" is not a failure worth an email.
    while not pool.is_mounted() and time.time() < deadline:
        if pool.members_complete()[0] and pool.mount(check=False).returncode == 0:
            break
        time.sleep(5)
    notifier = Notifier(cfg)
    state.load()
    if not ensure_mounted(cfg, pool, notifier, state):   # degraded fallback, and notify
        state.save()
        raise BraidError(f"could not mount {cfg['mountpoint']}")
    state.save()
    print(render_status(collect(pool), cfg, state))


def cmd_mount_degraded(cfg, args):
    require_root()
    lock = hold_lock(cfg)                      # noqa: F841 — released when this returns
    pool = Pool(cfg)
    if pool.is_mounted():
        print("already mounted")
        return
    present = [m["path"] for m in pool.scan_members()[1]]
    if not present:
        raise BraidError(f"no members with LABEL={cfg['label']} are visible")
    pool.mount(degraded=True)
    print(f"mounted degraded (members: {', '.join(present)})")
    print(render_status(collect(pool), cfg))


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="braidctl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default=CONFIG_PATH)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="pool capacity, protection state and drive health")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("add", help="add a drive to the pool")
    s.add_argument("device")
    s.add_argument("--force", action="store_true", help="erase existing signatures and partitions first")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("balance", help="spread the data evenly over every drive (hours)")
    s.set_defaults(fn=cmd_balance)

    s = sub.add_parser("remove", help="remove a drive (devid, device path, or 'missing')")
    s.add_argument("target")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("wipe-stale", help="erase a drive that was evicted while absent and still carries the pool's superblock")
    s.add_argument("device")
    s.set_defaults(fn=cmd_wipe_stale)

    s = sub.add_parser("monitor", help="detect losses and re-protect automatically (for the timer)")
    s.add_argument("--debounce", type=int, default=None,
                   help="seconds; overrides the configured value (for testing)")
    s.add_argument("--if-idle", action="store_true",
                   help="exit quietly if another braidctl holds the lock (for udev)")
    s.set_defaults(fn=cmd_monitor)

    s = sub.add_parser("scrub", help="verify every block against its checksum")
    s.add_argument("--ack", action="store_true",
                   help="do not scrub: record the last result as reviewed (refused for real damage)")
    s.add_argument("--quiet", action="store_true", help="do not notify on success")
    s.set_defaults(fn=cmd_scrub)

    s = sub.add_parser("cancel", help="stop a running scrub, balance or device remove (no argument: list them)")
    s.add_argument("what", nargs="?", choices=["scrub", "balance", "remove"])
    s.set_defaults(fn=cmd_cancel)

    s = sub.add_parser("guard", help="unmount if too few devices remain to write (for udev)")
    s.set_defaults(fn=cmd_guard)

    s = sub.add_parser("mount", help="mount the pool, degraded if need be (run at boot)")
    s.add_argument("--wait", type=int, default=0,
                   help="seconds to keep retrying while the drives appear (boot)")
    s.set_defaults(fn=cmd_mount)

    s = sub.add_parser("web", help="serve the optional read-only web view (loopback + token)")
    s.add_argument("--listen", help="host:port to bind (default from braid.toml)")
    s.add_argument("--url", action="store_true", help="print the URL with its token and exit")
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("web-job", help=argparse.SUPPRESS)
    s.add_argument("--id", required=True)
    s.add_argument("argv", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_web_job)

    s = sub.add_parser("mount-degraded", help="mount with a drive missing")
    s.set_defaults(fn=cmd_mount_degraded)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = load_config(args.config)
    # Which config this run used, so a job started on behalf of the web view is
    # given the same one rather than guessing at the default.
    cfg["__path__"] = args.config
    try:
        args.fn(cfg, args)
    except BraidError as e:
        print(f"braidctl: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
