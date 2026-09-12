#!/usr/bin/env python3
"""braidweb — the optional web view for braidctl.

Loaded lazily by `braidctl web`; nothing else imports it. If this file is missing or
broken, every other subcommand behaves exactly as before — which is the point of it
being a separate file. It is stdlib-only, like the rest of the tool.

Two rules shape everything here:

1. It never mutates the pool in a request. Reads are answered from a short-lived cache;
   anything that changes the pool is handed to a transient systemd unit running the
   ordinary CLI, so the argv in the audit log is the argv a human would have typed.
2. It holds no lock and runs nothing in the background that the pool depends on. Stop
   it, kill it, never install it: the timers and udev rules that keep the pool alive do
   not involve it.

The server is root, because every btrfs read needs root. That is why it binds loopback
by default, why the token is required before anything but the request line is parsed,
and why the only privileged work reachable from a request goes through collect() or a
fixed argv table.
"""

import base64
import hashlib
import hmac
import http.server
import json
import os
import secrets
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

BIND_DEFAULT = "127.0.0.1:8765"
MAX_BODY = 8192
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")


# ---------------------------------------------------------------- token & session

class Auth:
    """A 256-bit token in a 0600 file, and a signed cookie derived from it.

    The cookie is not the token: it is HMAC(token, issued_at), so the long-lived secret
    never sits in a browser's cookie jar, and rotating the token file invalidates every
    session at once. It is stateless because the server is meant to exit when idle — a
    server-side session table would log you out every fifteen minutes."""

    def __init__(self, path, ttl):
        self.path = Path(path)
        self.ttl = ttl
        self.token = self._load_or_create()

    def _load_or_create(self):
        if self.path.exists():
            st = self.path.stat()
            if st.st_uid != 0 or st.st_mode & 0o077:
                raise RuntimeError(f"{self.path} must be owned by root and mode 0600 "
                                   f"(it is {oct(st.st_mode & 0o777)}, uid {st.st_uid})")
            return self.path.read_text().strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        return token

    def check_token(self, given):
        return bool(given) and hmac.compare_digest(given, self.token)

    def issue(self):
        issued = str(int(time.time())).encode()
        sig = hmac.new(self.token.encode(), b"braid-session|" + issued, hashlib.sha256).digest()
        b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
        return f"{b64(issued)}.{b64(sig)}"

    def check_session(self, cookie):
        try:
            issued_b64, sig_b64 = (cookie or "").split(".", 1)
            pad = lambda s: s + "=" * (-len(s) % 4)
            issued = base64.urlsafe_b64decode(pad(issued_b64))
            sig = base64.urlsafe_b64decode(pad(sig_b64))
        except Exception:
            return False
        want = hmac.new(self.token.encode(), b"braid-session|" + issued, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, want):
            return False
        try:
            return (time.time() - int(issued)) < self.ttl
        except ValueError:
            return False


class Limiter:
    """Bounded, in-memory, and it answers immediately. Never sleep in a handler: that
    pins a thread, which is a denial of service of its own."""

    def __init__(self, window=60, warn=5, lock_after=10, lockout=600, cap=1024):
        self.window, self.warn, self.lock_after, self.lockout, self.cap = window, warn, lock_after, lockout, cap
        self.hits = {}
        self.lock = threading.Lock()

    def fail(self, addr):
        with self.lock:
            now = time.time()
            hits = [t for t in self.hits.get(addr, []) if now - t < self.window]
            hits.append(now)
            if len(self.hits) > self.cap:
                self.hits.clear()
            self.hits[addr] = hits
            return len(hits)

    def blocked(self, addr):
        with self.lock:
            now = time.time()
            hits = [t for t in self.hits.get(addr, []) if now - t < self.window]
            self.hits[addr] = hits
            if len(hits) >= self.lock_after:
                return self.lockout
            if len(hits) >= self.warn:
                return 30
            return 0

    def ok(self, addr):
        with self.lock:
            self.hits.pop(addr, None)


# ---------------------------------------------------------------- pool state

class PoolView:
    """Everything the page shows, from the same snapshot_dict() the status file uses.

    Two caches, for two very different costs: btrfs reads are ioctls and can be repeated
    every few seconds; SMART is seconds per drive behind a USB bridge and wakes a
    sleeping one, so it is refreshed on a thread and seeded from the status file the
    monitor already wrote."""

    def __init__(self, core, cfg, log):
        self.core, self.cfg, self.log = core, cfg, log
        self.web = cfg["web"]
        self.lock = threading.Lock()
        self.value, self.at, self.error = None, 0, None
        self.smart = {}
        self._seed_smart()

    def _seed_smart(self):
        """The monitor read SMART at most five minutes ago and wrote it down. Starting
        from that means the first page load after an idle exit spins up no drives."""
        try:
            snap = json.loads(Path(self.cfg["status_file"]).read_text())
        except (OSError, ValueError, KeyError):
            return
        for d in snap.get("devices") or []:
            if d.get("smart"):
                self.smart[d["devid"]] = d["smart"]

    def refresh_smart(self):
        try:
            pool = self.core.Pool(self.cfg)
            if not pool.is_mounted():
                return
            devs = pool.devices()
            for d in devs:
                self.smart[d.devid] = pool.smart(d)
        except Exception as e:                      # a wedged drive must not kill the thread
            self.log.warning("SMART refresh failed: %s", e)

    def _build(self):
        core, cfg = self.core, self.cfg
        pool = core.Pool(cfg)
        state = core.State(cfg["state_file"]).load()
        if not pool.is_mounted():
            present = pool.member_devices_present()
            note = f"{cfg['mountpoint']} is not mounted. Members visible: {', '.join(present) or 'none'}"
            snap = core.snapshot_dict(cfg, pool, None, state, note=note)
        else:
            report = core.collect(pool, smart=False)
            for d in report.devs:
                d.smart = self.smart.get(d.devid, {})
            snap = core.snapshot_dict(cfg, pool, report, state)
            snap["removable"] = self._removable(report, core, cfg)
        lock_path = Path(cfg["state_file"]).with_suffix(".lock")
        holder = core.lock_holder(lock_path)
        snap["lock"] = {"held": bool(holder), "pid": holder["pid"] if holder else None,
                        "cmdline": holder["cmdline"] if holder else None}
        snap["config"] = {"debounce_sec": cfg["debounce_sec"], "warn_percent": cfg["warn_percent"],
                          "rebuild_margin": cfg["rebuild_margin"],
                          "read_only": self.web["read_only"], "mountpoint": cfg["mountpoint"]}
        snap["smart_at"] = int(self.smart_at) if getattr(self, "smart_at", 0) else None
        return snap

    def _removable(self, report, core, cfg):
        """Whether each drive could be removed right now, decided by the same predicate
        the CLI uses. The page must never offer a button the CLI would refuse."""
        out = {}
        for d in report.devs:
            if d.missing or not d.present:
                continue
            ok, after, needed = core.can_rebuild(report, cfg, losing=[d])
            out[str(d.devid)] = {"ok": bool(ok)} if ok else {
                "ok": False,
                "reason": (f"usable afterwards {core.fmt_bytes(after)} < used {core.fmt_bytes(needed)}"),
            }
        return out

    def get(self):
        """live -> last good value -> the status file the monitor wrote -> nothing.

        Always the freshest thing that exists, labelled with what it is. A banner saying
        "snapshot, 4 minutes old" is useful; a blank page on a NAS reads as "the pool is
        gone" and sends people to do something rash."""
        with self.lock:
            fresh = self.value and (time.time() - self.at) < self.web["btrfs_ttl_sec"]
            if fresh:
                return dict(self.value, source="live", age_sec=int(time.time() - self.at))
            try:
                snap = self._build()
                self.value, self.at, self.error = snap, time.time(), None
                return dict(snap, source="live", age_sec=0)
            except Exception as e:
                self.error = str(e)
                self.log.warning("live collect failed: %s", e)
            if self.value:
                return dict(self.value, source="cache", age_sec=int(time.time() - self.at),
                            error=self.error)
            try:
                snap = json.loads(Path(self.cfg["status_file"]).read_text())
                age = int(time.time() - snap.get("generated_at", 0))
                return dict(snap, source="snapshot", age_sec=age, error=self.error)
            except (OSError, ValueError):
                return None


# ---------------------------------------------------------------- jobs

JOB_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{12}$")
BRAIDCTL = "/usr/local/sbin/braidctl"


def new_job_id():
    return time.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(6)


class Jobs:
    """One mutation at a time, started as a transient systemd unit, its output tee'd to a
    file so the page can tail it with a seek instead of a journal cursor.

    The record is written by the job itself, not by the web process: a five-hour remove
    has to keep reporting progress while nobody is looking at the page."""

    def __init__(self, cfg, core, log):
        self.cfg, self.core, self.log = cfg, core, log
        self.web = cfg["web"]
        self.dir = Path(self.web["jobs_dir"])
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.lock = threading.Lock()

    # -- record io
    def path(self, job_id):
        if not JOB_ID_RE.match(job_id or ""):
            raise ValueError("bad job id")
        return self.dir / f"{job_id}.json"

    def read(self, job_id):
        try:
            return json.loads(self.path(job_id).read_text())
        except (OSError, ValueError):
            return None

    def write(self, job):
        p = self.path(job["id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)          # a poll must never read half a record

    def all(self):
        out = []
        for f in sorted(self.dir.glob("*.json"), reverse=True):
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
        return out

    def prune(self):
        keep = self.web["job_retention"]
        done = [j for j in self.all() if j.get("state") in ("done", "failed", "cancelled", "orphaned")]
        for j in done[keep:]:
            for suffix in (".json", ".log"):
                try:
                    (self.dir / (j["id"] + suffix)).unlink()
                except OSError:
                    pass

    # -- lifecycle
    def unfinished(self):
        return [j for j in self.all() if j.get("state") in ("queued", "running")]

    def reconcile(self, job):
        """A record that says running while its unit is gone is not running. Say so
        rather than showing a spinner forever; the log usually says what happened."""
        if job.get("state") != "running":
            return job
        unit = job.get("unit")
        if unit and shutil.which("systemctl"):
            p = subprocess.run(["systemctl", "show", unit, "-p", "LoadState", "--value"],
                               capture_output=True, text=True)
            if p.stdout.strip() in ("not-found", ""):
                job["state"] = "orphaned"
                self.write(job)
        return job

    def start(self, action, argv, params, requested_by):
        """Refuse rather than queue: two mutations at once is the thing the pool lock
        exists to prevent, and a queue would just hide the wait."""
        with self.lock:
            busy = self.unfinished()
            if busy:
                raise Busy(f"job {busy[0]['id']} ({busy[0]['action']}) is still running", busy[0]["id"])
            job_id = new_job_id()
            job = {"version": 1, "id": job_id, "action": action, "params": params,
                   "argv": [BRAIDCTL, "-c", self.cfg.get("__path__", "/etc/braid/braid.toml")] + argv,
                   "unit": f"braid-job-{job_id}", "runner": "systemd",
                   "created": int(time.time()), "started": None, "finished": None,
                   "state": "queued", "exit": None, "error": None,
                   "cancellable": action == "scrub",
                   "requested_by": requested_by, "progress": None,
                   "log": str(self.dir / f"{job_id}.log")}
            self.write(job)
            self.prune()
            cmd = ["systemd-run", f"--unit={job['unit']}", "--collect", "--no-block",
                   f"--description=braidctl {action} (web)",
                   "-p", "TimeoutStartSec=infinity", "-p", "Nice=10",
                   "-p", "IOSchedulingClass=best-effort", "-p", "IOSchedulingPriority=7",
                   BRAIDCTL, "-c", self.cfg.get("__path__", "/etc/braid/braid.toml"),
                   "web-job", "--id", job_id, "--"] + argv
            self.log.warning("AUDIT job=%s addr=%s auth=%s action=%s argv=%s unit=%s",
                             job_id, requested_by.get("addr"), requested_by.get("auth"),
                             action, job["argv"], job["unit"])
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode != 0:
                job.update(state="failed", finished=int(time.time()),
                           error=(p.stderr or p.stdout).strip()[:500])
                self.write(job)
                raise Busy(f"could not start the job: {job['error']}", job_id)
            return job

    def cancel(self, job):
        mnt = self.cfg["mountpoint"]
        if job["action"] == "scrub":
            subprocess.run(["btrfs", "scrub", "cancel", mnt], capture_output=True, text=True)
            return True
        return False


class Busy(Exception):
    def __init__(self, message, job_id=None):
        super().__init__(message)
        self.job_id = job_id


def run_job(cfg, args, core):
    """The wrapper that actually runs inside the transient unit (`braidctl web-job`)."""
    jobs = Jobs(cfg, core, core.LOG)
    argv = [a for a in (args.argv or []) if a != "--"]
    job = jobs.read(args.id) or {"id": args.id, "action": argv[0] if argv else "?", "version": 1}
    job.update(state="running", started=int(time.time()), pid=os.getpid())
    # The baseline has to be taken before the command starts. Sampling it later means
    # measuring against a device that has already been partly drained, and a bar that
    # starts at zero when the work is half done is worse than no bar.
    baseline_alloc = None
    devid = (job.get("params") or {}).get("devid")
    if devid:
        try:
            alloc = core.Pool(cfg).alloc_by_devid().get(int(devid))
            if alloc:
                baseline_alloc = {"alloc": alloc[1]}
        except Exception:
            pass
    jobs.write(job)
    logfile = Path(job.get("log") or (jobs.dir / f"{args.id}.log"))
    stop = threading.Event()

    def sample():
        pool = core.Pool(cfg)
        baseline = baseline_alloc
        first = True
        while first or not stop.wait(5):
            first = False
            try:
                if stop.is_set():
                    return
                prog = job_progress(core, cfg, pool, job, baseline)
                if prog and prog.get("baseline"):
                    baseline = prog["baseline"]
                if prog:
                    cur = jobs.read(args.id) or job
                    if cur.get("state") not in ("queued", "running"):
                        return              # finished while we were sampling
                    cur["progress"] = prog
                    jobs.write(cur)
            except Exception:
                pass                        # progress is a nicety; never fail the job for it
    t = threading.Thread(target=sample, daemon=True)
    t.start()

    cmd = [BRAIDCTL, "-c", cfg.get("__path__", "/etc/braid/braid.toml")] + argv
    code, err = 1, None
    try:
        with logfile.open("w") as f:
            os.chmod(logfile, 0o600)
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                f.write(line)
                f.flush()
                print(line, end="", flush=True)     # and into the journal
            code = p.wait()
    except Exception as e:
        err = str(e)
    finally:
        stop.set()
        cur = jobs.read(args.id) or job
        ok = code == 0 and not err
        # Leaving the last sample on a finished job shows a bar stuck at whatever it
        # happened to read a few seconds before the end.
        cur.update(state=("done" if ok else "failed"),
                   finished=int(time.time()), exit=code, error=err,
                   progress={"phase": "finished" if ok else "failed",
                             "pct": 100 if ok else None, "detail": "",
                             "updated": int(time.time())})
        jobs.write(cur)
    return None if code == 0 else sys.exit(code)


def job_progress(core, cfg, pool, job, baseline):
    """What the page shows while a job runs. btrfs reports no progress for a device
    remove at all, so derive it: `remove` relocates by balancing first (a percentage)
    and then draining the departing device's allocation (bytes we can watch)."""
    action = job.get("action")
    if action == "scrub":
        info = {}
        for line in pool.scrub_status().splitlines():
            m = re.match(r"\s*(Status|Bytes scrubbed|Time left|ETA|Rate):\s+(.*)", line)
            if m:
                info[m[1]] = m[2].strip()
        m = re.search(r"\(([\d.]+)%\)", info.get("Bytes scrubbed", ""))
        return {"phase": info.get("Status") or "scrubbing",
                "pct": float(m[1]) if m else None,
                "detail": " ".join(x for x in (info.get("Bytes scrubbed"), info.get("Rate")) if x),
                "time_left": info.get("Time left"), "updated": int(time.time())}
    bal = pool.balance_status()
    if action in ("remove", "monitor") :
        devid = (job.get("params") or {}).get("devid")
        if bal["running"]:
            return {"phase": "compacting", "pct": bal["pct"],
                    "detail": bal.get("text") or "", "baseline": baseline,
                    "updated": int(time.time())}
        if devid:
            alloc = pool.alloc_by_devid().get(int(devid))
            if alloc:
                base = baseline or {"alloc": alloc[1]}
                left, start = alloc[1], max(1, base["alloc"])
                pct = max(0.0, min(100.0, (1 - left / start) * 100))
                prev = ((job.get("progress") or {}).get("pct")) or 0
                return {"phase": "relocating", "pct": round(max(pct, prev), 1),
                        "detail": f"{core.fmt_bytes(left)} still allocated on devid {devid} "
                                  f"(of {core.fmt_bytes(start)}) — approximate",
                        "baseline": base, "updated": int(time.time())}
    if bal["running"]:
        return {"phase": "balancing", "pct": bal["pct"], "detail": bal.get("text") or "",
                "updated": int(time.time())}
    return {"phase": action or "working", "pct": None, "detail": "", "updated": int(time.time())}


def candidates(core, cfg, allow_force):
    """Every whole disk on the machine, each carrying what `braidctl add` would say about it.

    Refused drives are listed with their reason rather than hidden: hiding them sends the
    operator to a terminal with `lsblk` to find out why, and a terminal with lsblk and an
    impatient human is where the wrong /dev/sdX gets typed."""
    pool = core.Pool(cfg)
    members = {d.path for d in pool.devices()} if pool.is_mounted() else set()
    out = []
    for node in core.lsblk():
        # Whole disks, plus loop devices so the loop testbed can exercise this path.
        # Partitions are left out on purpose: the CLI will take one, but listing every
        # partition on the machine turns the picker into noise, which is its own hazard.
        if node.get("type") not in ("disk", "loop"):
            continue
        path = node["path"]
        entry = {"path": path, "model": node.get("model") or "", "serial": node.get("serial") or "",
                 "size": node.get("size"), "tran": node.get("tran") or "",
                 "fstype": node.get("fstype") or "",
                 "children": [c["path"] for c in core.flatten_lsblk(node.get("children", []))],
                 "member": path in members}
        if entry["member"]:
            entry["status"], entry["reason"] = "member", "already a member of this pool"
            out.append(entry)
            continue
        try:
            core.check_addable(cfg, pool, path, False)
            entry["status"], entry["reason"] = "ok", ""
        except core.BraidError as e:
            reason = str(e)
            # "already holds data … Pass --force to erase it" is the only refusal --force
            # lifts; everything else (system disk, mounted, holders, protected_devices)
            # stands whatever the flags say, and the UI must not suggest otherwise.
            if "Pass --force" in reason:
                entry["status"] = "needs_force" if allow_force else "has_data"
            else:
                entry["status"] = "refused"
            entry["reason"] = reason
        except Exception as e:
            entry["status"], entry["reason"] = "unknown", str(e)
        # A refused real disk is worth showing — the operator may be hunting for it. A
        # refused loop device is a snap mount, and every machine has a pile of them.
        if node.get("type") == "loop" and entry["status"] == "refused":
            continue
        out.append(entry)
    return out


# ---------------------------------------------------------------- http

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "braidctl"
    sys_version = ""

    # -- plumbing
    def log_message(self, fmt, *args):
        self.server.ctx["log"].debug("%s %s", self.address_string(), fmt % args)

    @property
    def ctx(self):
        return self.server.ctx

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.respond(code, body, "application/json; charset=utf-8")

    def respond(self, code, body, ctype, extra=None):
        self.server.ctx["last"] = time.time()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}):
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def deny(self, code, reason, message):
        """Every refusal is logged by class, never by value, and says the same thing to
        a wrong token as to no token."""
        self.ctx["log"].warning("refused %s from %s: %s", self.path, self.client_address[0], reason)
        self.send_json(code, {"error": message})

    # -- guards
    def host_ok(self):
        """DNS rebinding is the attack a loopback service actually faces: a page whose
        hostname resolves to 127.0.0.1 talks to us from the attacker's origin. The Host
        header is the tell, so allow only the names we are actually served under."""
        host = (self.headers.get("Host") or "").lower()
        return host in self.ctx["hosts"]

    def authorised(self):
        """Credentials are checked before the rate limiter is consulted: a valid token
        must never be refused because something else from this address guessed wrong.
        The limiter exists to keep the journal readable, not to defend 256 bits."""
        auth = self.ctx["auth"]
        header = self.headers.get("X-Braid-Token") or ""
        bearer = (self.headers.get("Authorization") or "")
        if bearer.startswith("Bearer "):
            header = header or bearer[7:].strip()
        if header and auth.check_token(header):
            self.ctx["limiter"].ok(self.client_address[0])
            return True
        cookie = ""
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "braid_session":
                cookie = v
        if cookie and auth.check_session(cookie):
            self.ctx["limiter"].ok(self.client_address[0])
            return True
        self.ctx["limiter"].fail(self.client_address[0])
        wait = self.ctx["limiter"].blocked(self.client_address[0])
        if wait:
            self.send_response(429)
            self.send_header("Retry-After", str(wait))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        self.deny(401, "no-credential" if not (header or cookie) else "bad-credential",
                  "unauthorised: open the URL printed by `braidctl web --url`")
        return False

    # -- routes
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = {}
        for part in query.split("&"):
            k, _, v = part.partition("=")
            if k:
                from urllib.parse import unquote_plus
                params[k] = unquote_plus(v)

        if path == "/api/health":                      # for a reverse proxy; leaks nothing
            return self.send_json(200, {"ok": True})
        if not self.host_ok():
            return self.deny(421, "bad-host", "unexpected Host header")

        # A token in the query string is exchanged once for a cookie, so the secret
        # leaves the address bar, the history and any future Referer.
        if path == "/" and params.get("token"):
            if not self.ctx["auth"].check_token(params["token"]):
                self.ctx["limiter"].fail(self.client_address[0])
                return self.deny(401, "bad-token", "unauthorised")
            self.ctx["limiter"].ok(self.client_address[0])
            cookie = (f"braid_session={self.ctx['auth'].issue()}; Path=/; HttpOnly; SameSite=Strict; "
                      f"Max-Age={self.ctx['cfg']['web']['session_ttl_sec']}")
            if self.ctx["cfg"]["web"]["cookie_secure"]:
                cookie += "; Secure"
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        if not self.authorised():
            return None

        if path == "/api/status":
            snap = self.ctx["view"].get()
            if snap is None:
                return self.send_json(503, {"error": "no pool data yet", "hint": "sudo braidctl status"})
            return self.send_json(200, snap)

        if path == "/api/devices/candidates":
            core, cfg = self.ctx["core"], self.ctx["cfg"]
            allow = bool(cfg["web"].get("allow_force_add"))
            return self.send_json(200, {"candidates": candidates(core, cfg, allow),
                                        "allow_force_add": allow})

        if path == "/api/jobs":
            jobs = self.ctx["jobs"]
            return self.send_json(200, {"jobs": [jobs.reconcile(j) for j in jobs.all()[:20]],
                                        "read_only": self.ctx["cfg"]["web"]["read_only"],
                                        "actions": sorted(self.ACTIONS)})
        m = re.match(r"^/api/jobs/([^/]+)(/log)?$", path)
        if m:
            jobs = self.ctx["jobs"]
            try:
                job = jobs.read(m[1])
            except ValueError:
                return self.deny(400, "bad-job-id", "bad job id")
            if not job:
                return self.send_json(404, {"error": "no such job"})
            if not m[2]:
                return self.send_json(200, jobs.reconcile(job))
            # Incremental tail by offset: a seek and a read, no journal cursors.
            try:
                offset = max(0, int(params.get("offset") or 0))
            except ValueError:
                offset = 0
            try:
                with open(job["log"], "r", errors="replace") as f:
                    f.seek(offset)
                    text = f.read(MAX_BODY * 8)
                    return self.send_json(200, {"offset": offset, "next": f.tell(), "text": text,
                                                "eof": job.get("state") not in ("queued", "running")})
            except OSError:
                return self.send_json(200, {"offset": offset, "next": offset, "text": "", "eof": False})

        if path in ASSETS:
            # The page in text, for curl and for a browser with no JS: the same output as
            # `braidctl status`, which is a useful answer rather than an apology.
            if path == "/" and (params.get("plain") or "text/plain" in (self.headers.get("Accept") or "")):
                return self.respond(200, self.plain_status().encode(), "text/plain; charset=utf-8")
            name, ctype = ASSETS[path]
            body = self.ctx["assets"].get(name)
            if body is None:
                return self.send_json(500, {"error": f"asset {name} not found in {self.ctx['assetdir']}"})
            return self.respond(200, body, ctype)

        return self.send_json(404, {"error": "no such endpoint"})

    # The whole mutation vocabulary. Validation is membership, not sanitising: a value
    # that is not an element of a set we enumerated ourselves never reaches argv.
    ACTIONS = {
        "scrub":          {"label": "scrub"},
        "monitor":        {"label": "monitor pass"},
        "add":            {"label": "add a drive"},
        "remove":         {"label": "remove a drive"},
        "mount":          {"label": "mount", "gate": "allow_mount_actions"},
        "mount-degraded": {"label": "mount degraded", "gate": "allow_mount_actions"},
    }

    def build_argv(self, action, body):
        """Validation is membership in a set we enumerated ourselves, never sanitising.
        `/dev/sda; rm -rf /` is not an element of any of these sets, so it cannot reach
        argv — a stronger property than any amount of quoting, and easier to review."""
        core, cfg = self.ctx["core"], self.ctx["cfg"]
        if action in ("scrub", "monitor", "mount", "mount-degraded"):
            return [action], {}
        pool = core.Pool(cfg)
        if action == "remove":
            target = str(body.get("target") or "")
            if target == "missing":
                return ["remove", "missing"], {"target": "missing"}
            devs = {str(d.devid): d for d in pool.devices()}
            d = devs.get(target)
            if d is None:
                raise ValueError(f"devid {target!r} is not a member of this pool right now")
            # str(int) we produced, not the client's string
            return ["remove", str(d.devid)], {"devid": d.devid, "name": d.name,
                                              "model": d.model, "size": d.size}
        if action == "add":
            want = os.path.realpath(str(body.get("device") or ""))
            allow_force = bool(cfg["web"].get("allow_force_add"))
            for c in candidates(core, cfg, allow_force):
                if os.path.realpath(c["path"]) != want:
                    continue
                if c["status"] == "ok":
                    return ["add", c["path"]], {"device": c["path"], "model": c["model"],
                                                "serial": c["serial"], "size": c["size"]}
                if c["status"] == "needs_force":
                    self.check_wipe_confirmation(c, body)
                    return ["add", "--force", c["path"]], {"device": c["path"], "model": c["model"],
                                                           "serial": c["serial"], "size": c["size"],
                                                           "force": True}
                raise ValueError(c["reason"])
            raise ValueError("no such addable device")
        raise ValueError(f"unknown action {action!r}")

    def check_wipe_confirmation(self, cand, body):
        """`add --force` runs wipefs. It is the only irreversible thing in the tool, so it
        is off unless braid.toml says otherwise, and even then the operator has to type
        back what the server just read from the drive."""
        # A drive behind a USB bridge often reports no serial, and a loop device reports
        # neither, so fall back to something the operator can actually read off the page.
        want = cand["serial"] or f"WIPE {cand['model'] or os.path.basename(cand['path'])} {cand['size']}"
        given = str(body.get("confirm") or "")
        if not hmac.compare_digest(given, want):
            raise ValueError(f"to erase {cand['path']} you must confirm with exactly: {want}")
        if str(body.get("confirm_wipe") or "") != "ERASE":
            raise ValueError('also send confirm_wipe: "ERASE"')

    def mutation_allowed(self):
        if self.ctx["cfg"]["web"]["read_only"]:
            self.deny(403, "read-only", "this braidctl web is configured read_only")
            return False
        # SameSite=Strict is most of the defence, but cookies ignore port: anything else
        # on localhost is same-site with us. A custom header cannot be set cross-origin
        # without a preflight, and we answer none.
        if self.headers.get("X-Braid-CSRF") != "1":
            self.deny(403, "missing-csrf", "mutations require the X-Braid-CSRF: 1 header")
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            host = origin.split("//", 1)[-1].lower()
            if host not in self.ctx["hosts"]:
                self.deny(403, "bad-origin", "unexpected Origin")
                return False
        return True

    def body_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY or self.headers.get("Transfer-Encoding"):
            return None
        try:
            return json.loads(self.rfile.read(length).decode())
        except (ValueError, UnicodeDecodeError):
            return None

    def do_POST(self):
        path, _, _ = self.path.partition("?")
        if not self.host_ok():
            return self.deny(421, "bad-host", "unexpected Host header")
        if not self.authorised():
            return None
        if not self.mutation_allowed():
            return None

        if path == "/api/jobs":
            return self.start_job()
        m = re.match(r"^/api/jobs/([^/]+)/cancel$", path)
        if m:
            return self.cancel_job(m[1])
        return self.send_json(404, {"error": "no such endpoint"})

    def start_job(self):
        body = self.body_json() or {}
        action = body.get("action")
        spec = self.ACTIONS.get(action)
        if not spec:
            return self.deny(400, "bad-action", f"unknown action {action!r}")
        gate = spec.get("gate")
        if gate and not self.ctx["cfg"]["web"].get(gate):
            return self.deny(403, "gated", f"{action} is disabled in braid.toml ([web] {gate})")
        try:
            argv, params = self.build_argv(action, body)
        except ValueError as e:
            return self.deny(400, "bad-params", str(e))
        except Exception as e:
            return self.deny(400, "bad-params", str(e))
        # Pre-flight with the same predicates the CLI uses, so the common refusal arrives
        # as an error with the real message instead of a job that dies two seconds later.
        try:
            core, cfg = self.ctx["core"], self.ctx["cfg"]
            pool = core.Pool(cfg)
            if action == "scrub":
                if not pool.is_mounted():
                    raise core.BraidError(f"{cfg['mountpoint']} is not mounted")
                if "running" in pool.scrub_status():
                    raise core.BraidError("a scrub is already running")
                if pool.balance_running():
                    raise core.BraidError("a balance is running (rebuild or rebalance); "
                                          "scrub once it finishes")
            if action in ("add", "remove"):
                if not pool.is_mounted():
                    raise core.BraidError(f"{cfg['mountpoint']} is not mounted")
                if pool.balance_running():
                    raise core.BraidError("a balance is running; try again once it finishes")
            if action == "remove":
                report = core.collect(pool, smart=False)
                target = str(body.get("target") or "")
                losing = report.gone if target == "missing" else [d for d in report.devs
                                                                 if str(d.devid) == target]
                if losing:
                    ok, after, needed = core.can_rebuild(report, cfg, losing=losing)
                    if not ok:
                        raise core.BraidError(
                            f"removing that would not leave room for the data (usable afterwards "
                            f"{core.fmt_bytes(after)} < used {core.fmt_bytes(needed)})")
            holder = core.lock_holder(Path(cfg["state_file"]).with_suffix(".lock"))
            if holder and action != "monitor":
                raise core.BraidError(f"another braidctl is running: {holder['cmdline']}")
        except Exception as e:
            return self.deny(400, "preflight", str(e))
        try:
            job = self.ctx["jobs"].start(action, argv, params,
                                         {"addr": self.client_address[0],
                                          "auth": "cookie" if self.headers.get("Cookie") else "token",
                                          "ua": (self.headers.get("User-Agent") or "")[:120]})
        except Busy as e:
            return self.send_json(409, {"error": str(e), "job": e.job_id})
        return self.send_json(202, job)

    def cancel_job(self, job_id):
        jobs = self.ctx["jobs"]
        try:
            job = jobs.read(job_id)
        except ValueError:
            return self.deny(400, "bad-job-id", "bad job id")
        if not job:
            return self.send_json(404, {"error": "no such job"})
        if not job.get("cancellable"):
            return self.send_json(409, {"error": "this job cannot be cancelled: btrfs continues "
                                                 "the work in the kernel even if the command stops"})
        jobs.cancel(job)
        self.ctx["log"].warning("AUDIT cancel job=%s addr=%s", job_id, self.client_address[0])
        return self.send_json(200, {"ok": True})

    def do_OPTIONS(self):
        # No Access-Control-Allow-* header, ever. That is what makes a cross-origin
        # fetch with a custom header fail its preflight instead of reaching a route.
        self.send_json(405, {"error": "no"})

    def plain_status(self):
        core, cfg = self.ctx["core"], self.ctx["cfg"]
        try:
            pool = core.Pool(cfg)
            if not pool.is_mounted():
                return f"{cfg['mountpoint']} is not mounted\n"
            state = core.State(cfg["state_file"]).load()
            report = core.collect(pool, smart=False)
            for d in report.devs:
                d.smart = self.ctx["view"].smart.get(d.devid, {})
            return core.render_status(report, cfg, state,
                                      core.scrub_info(pool, state, report),
                                      pool.balance_status()) + "\n"
        except Exception as e:
            return f"error: {e}\n"


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16


# ---------------------------------------------------------------- entry point

def load_assets(dirs):
    for d in dirs:
        p = Path(d)
        if (p / "index.html").exists():
            return p, {name: (p / name).read_bytes() for name, _ in ASSETS.values()}
    return None, {}


def parse_bind(text):
    host, _, port = text.rpartition(":")
    return host.strip("[]") or "127.0.0.1", int(port)


def serve(cfg, args, core):
    log = core.LOG
    web = cfg["web"]
    bind = getattr(args, "listen", None) or web["listen"]
    host, port = parse_bind(bind)
    auth = Auth(web["token_file"], web["session_ttl_sec"])

    if getattr(args, "url", False):
        print(f"http://{host if ':' not in host else '[' + host + ']'}:{port}/?token={auth.token}")
        return None

    loopback = host in ("127.0.0.1", "::1", "localhost")
    if not loopback and not web["allow_nonloopback"]:
        raise core.BraidError(
            f"refusing to bind {bind}: the token travels in cleartext and what it opens can "
            "erase disks. Put it behind ssh -L, `tailscale serve` or a TLS proxy, or set "
            "[web] allow_nonloopback = true if you meant it")
    if not loopback:
        log.warning("listening on %s — anyone who can reach this port and guess nothing "
                    "is one stolen token away from your disks", bind)

    assetdir, assets = load_assets([a for a in (web["assets"], str(Path(core.__file__).resolve().parent / "web"),
                                                "/usr/local/share/braidctl/web") if a])
    if not assets:
        raise core.BraidError("web assets not found (index.html); set [web] assets in braid.toml")

    view = PoolView(core, cfg, log)
    hosts = {f"{host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"}
    hosts |= {h.lower() for h in web["allowed_hosts"]}

    httpd = Server((host, port), Handler, bind_and_activate=False)
    httpd.ctx = {"cfg": cfg, "core": core, "log": log, "auth": auth, "view": view,
                 "assets": assets, "assetdir": str(assetdir), "hosts": hosts,
                 "limiter": Limiter(), "last": time.time(),
                 "jobs": Jobs(cfg, core, log)}
    fd = systemd_socket()
    if fd is not None:
        httpd.socket = socket.socket(fileno=fd)
        log.info("serving on the socket systemd handed us")
    else:
        httpd.server_bind()
        httpd.server_activate()
        log.info("braidctl web on http://%s:%d/ (token in %s)", host, port, web["token_file"])

    # Socket-activated, the steady state of the machine is meant to be the state it had
    # before this was installed: pid 1 holding a socket and nothing of braidctl running.
    # A tab left open for a month should not keep root's HTTP stack loaded.
    idle = web["idle_exit_sec"]
    if fd is not None and idle > 0:
        def idle_loop():
            while True:
                time.sleep(30)
                if time.time() - httpd.ctx["last"] > idle:
                    log.info("idle for %ds; exiting until the next request", idle)
                    threading.Thread(target=httpd.shutdown, daemon=True).start()
                    return
        threading.Thread(target=idle_loop, daemon=True).start()

    def smart_loop():
        while True:
            view.refresh_smart()
            view.smart_at = time.time()
            time.sleep(max(60, web["smart_ttl_sec"]))
    t = threading.Thread(target=smart_loop, daemon=True)
    t.start()

    try:
        httpd.serve_forever(poll_interval=1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return None


def systemd_socket():
    """Socket activation: pid 1 holds the listening socket, so nothing of braidctl is
    resident between visits. The pool's own machinery is timers and udev rules, and this
    keeps the web view from being the one daemon in the design."""
    try:
        if int(os.environ.get("LISTEN_PID", 0)) != os.getpid():
            return None
        if int(os.environ.get("LISTEN_FDS", 0)) < 1:
            return None
    except ValueError:
        return None
    return 3
