#!/usr/bin/env python3
"""Serve the web view against canned data, for looking at the page without a pool.

    contrib/mock-web.py                      # http://127.0.0.1:8799/ , scenario "healthy"
    contrib/mock-web.py -s degraded
    contrib/mock-web.py -s full -l 0.0.0.0:8799      # to open it from a phone
    contrib/mock-web.py --list

It serves the real web/index.html, app.js and app.css — the point is to look at the files
that will be installed, not at a copy of them — and answers the API from the tables below.
The states worth designing for are the awkward ones: a drive that is missing and has no
allocation to draw, one shrunk to zero because a remove is relocating off it, a name long
enough to wrap, an error count that is not zero. They are tedious to reproduce on a real
pool (docs/testing.md does it with loop devices) and impossible to reproduce on purpose on
the pool you care about.

It touches nothing: no root, no btrfs, no /var/lib/braid. Mutations return a fake job that
runs for a few seconds so the Job card and the cancel flow can be looked at too.
"""

import argparse
import json
import http.server
import re
import threading
import time
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
GB, TB, GiB = 10**9, 10**12, 1024**3


def drive(devid, name, model, size, alloc, **kw):
    d = {"devid": devid, "name": name, "model": model, "size": size, "alloc": alloc,
         "path": f"/dev/{name}", "serial": f"MOCK{devid:04d}", "errors": {}, "errors_total": 0,
         "errors_new": 0, "smart": {"passed": True, "temp": 40}, "missing": False,
         "present": True, "detached": False, "health": "OK 40°C", "health_level": "ok"}
    d.update(kw)
    return d


# A five-drive mixed-size pool, which is the shape that makes the capacity arithmetic
# interesting: losing the 2.5 TB drive is not the same as losing a 2 TB one.
def base_drives():
    return [drive(1, "sdb", "ThinkPlus", 2 * TB, 1.54 * TB, health="OK 41°C"),
            drive(2, "sdc", "ThinkPlus", 2 * TB, 1.54 * TB, health="OK 43°C"),
            drive(3, "sda", "WDC WD25EZRX-00MMMB0", 2.5 * TB, 1.47 * TB, health="OK 43°C"),
            drive(5, "sdd", "ST2000LM007-1R8174", 2 * TB, 1.54 * TB, health="OK 35°C"),
            drive(6, "sdg", "ST2000DM001-9YN164", 2 * TB, 1.54 * TB, health="OK 39°C")]


def pool(devices, **kw):
    now = int(time.time())
    used = sum(d["alloc"] for d in devices) / 2
    snap = {
        "version": 1, "generated_at": now, "label": "braid", "mountpoint": "/srv/pool",
        "state": "PROTECTED", "profiles": {"Data": "RAID1", "Metadata": "RAID1"},
        "mounted_degraded": False, "balance_running": False,
        "usage": {"raw": sum(d["size"] for d in devices), "used": used, "data_used": used,
                  "meta_used": 12 * GB, "free": 1.44 * TB, "usable": used + 1.44 * TB,
                  "missing_bytes": 0, "pct": round(used / (used + 1.44 * TB) * 100, 1)},
        "devices": devices,
        "verdict": {"level": "ok", "text": "Any single drive can be lost and re-protected "
                    "(losing the largest, sda 2.50 TB, leaves 5.25 TB usable >= 3.81 TB used)"},
        "missing_since": {}, "rebuild": None, "errors_since": now - 3 * 86400,
        "scrub": {"started": now - 3 * 86400, "status": "finished", "duration": "4:12:03",
                  "level": "ok", "text": "no errors found", "pct": None, "error_summary": "no errors found"},
        "balance": {"running": False, "paused": False, "pct": None, "chunks_done": None,
                    "chunks_total": None, "text": ""},
        "removable": {str(d["devid"]): {"ok": True} for d in devices},
        "operations": [], "lock": {"held": False, "pid": None, "cmdline": None},
        "smart_at": now - 120, "source": "live", "age_sec": 0,
        "config": {"debounce_sec": 600, "warn_percent": 90, "rebuild_margin": 0.95,
                   "chunk_headroom_warn": 16 * GiB, "chunk_headroom_fail": 2 * GiB,
                   "read_only": False, "mountpoint": "/srv/pool"},
    }
    snap.update(kw)
    return snap


def healthy():
    """Nothing wrong: five drives, no errors, room to lose any one of them."""
    return pool(base_drives())


def errors():
    """Error counts that are not zero, and the health states that go with them."""
    devs = base_drives()
    devs[2].update(errors_new=28, errors_total=152242, health="WARN reallocated=1 47°C",
                   health_level="warn", smart={"passed": True, "temp": 47, "reallocated": 1})
    devs[3].update(errors_new=3, errors_total=18852077, health="OK 35°C")
    devs[4].update(health="SMART FAIL", health_level="fail", smart={"passed": False, "temp": 39})
    return pool(devs, scrub={
        "started": int(time.time()) - 86400, "status": "finished", "duration": "4:40:11",
        "level": "warn", "error_summary": "csum=28",
        "text": "scrub repaired 28 block(s) from the mirror (csum=28 on sda (devid 3)) — the drive "
                "returned bad data, but nothing was lost. `braidctl scrub --ack` once reviewed"})


def degraded():
    """A drive gone, on the debounce countdown before the pool re-lays-out without it."""
    devs = base_drives()
    devs[2].update(name="was sda", size=2.5 * TB, alloc=0, missing=True, present=False,
                   health="MISSING", health_level="missing", smart={})
    now = int(time.time())
    return pool(devs, state="DEGRADED", mounted_degraded=True,
                missing_since={"3": now - 240},
                removable={"1": {"ok": True}, "2": {"ok": True},
                           "3": {"ok": False, "reason": "usable afterwards 3.50 TB < used 3.81 TB"},
                           "5": {"ok": True}, "6": {"ok": True}},
                verdict={"level": "warn", "text": "! 1 drive(s) missing. Can re-protect with what "
                         "remains (usable 5.25 TB >= used 3.81 TB); the monitor will do it"})


def full():
    """Unallocated space running out per drive, which is what really stops a write."""
    devs = base_drives()
    devs[0].update(alloc=2 * TB - 1 * GiB, health="OK 44°C")          # below chunk_headroom_fail
    devs[1].update(alloc=2 * TB - 9 * GiB, health="OK 43°C")          # below chunk_headroom_warn
    devs[2].update(alloc=900 * GB)
    devs[4].update(name="sdg", size=0, alloc=0,                        # a remove is draining it
                   health="OK 39°C")
    return pool(devs, verdict={
        "level": "fail", "text": "Out of unallocated space (1.00 GiB left on the drive a new chunk "
        "must land on). Writes will fail with ENOSPC and balance cannot run. Delete data or add a drive"},
        removable={"1": {"ok": False, "reason": "usable afterwards 3.00 TB < used 3.81 TB"},
                   "6": {"ok": False, "removing": True,
                         "reason": "being removed: its data is being relocated to the other drives"}})


def busy():
    """Something running: the Running card, a progress bar, and a held lock."""
    devs = base_drives()
    now = int(time.time())
    return pool(devs, state="REBUILDING", balance_running=True,
                balance={"running": True, "paused": False, "pct": 42.0, "chunks_done": 1180,
                         "chunks_total": 2810, "text": "1180 out of about 2810 chunks balanced"},
                scrub={"started": now - 900, "status": "running", "duration": "0:15:00",
                       "level": "ok", "text": "scrub in progress", "pct": 12.4,
                       "time_left": "3:20:00", "error_summary": None},
                lock={"held": True, "pid": 4242, "cmdline": "braidctl monitor"},
                operations=[
                    {"kind": "scrub", "label": "scrub", "pct": 12.4, "cancellable": True,
                     "note": "Progress is kept: `btrfs scrub resume /srv/pool` continues from where it stopped."},
                    {"kind": "balance", "label": "balance", "pct": 42.0, "cancellable": False,
                     "reason": "braidctl monitor is re-protecting the pool; stopping it would only "
                               "postpone that to the next pass"}],
                removable={str(d["devid"]): {"ok": False,
                                             "reason": "another operation holds the pool: braidctl monitor"}
                           for d in devs})


SCENARIOS = {"healthy": healthy, "errors": errors, "degraded": degraded, "full": full, "busy": busy}

CANDIDATES = {"busy": None, "allow_force_add": False, "candidates": [
    {"path": "/dev/sdh", "model": "Samsung SSD 860", "serial": "S3Z8NB0K", "size": 1 * TB,
     "status": "ok", "reason": "", "children": [], "member": False},
    {"path": "/dev/sdi", "model": "Seagate Expansion", "serial": "NA7X1234", "size": 4 * TB,
     "status": "has_data", "children": ["/dev/sdi1"], "member": False,
     "reason": "/dev/sdi already holds data (partitions: /dev/sdi1 signature: ntfs). Pass --force to erase it"},
    {"path": "/dev/nvme0n1", "model": "SAMSUNG MZVLB512", "serial": "S4EN", "size": 512 * GB,
     "status": "refused", "children": [], "member": False,
     "reason": "refusing /dev/nvme0n1: it is the system disk (/dev/nvme0n1)"}]}


class Mock:
    """The moving parts: a fake job that finishes on its own, so the Job card is not frozen."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.jobs = []
        self.lock = threading.Lock()

    def status(self):
        return SCENARIOS[self.scenario]()

    def start(self, action):
        with self.lock:
            now = int(time.time())
            job = {"version": 1, "id": time.strftime("%Y%m%dT%H%M%S") + "-0123456789ab",
                   "action": action, "params": {}, "state": "running", "created": now,
                   "started": now, "finished": None, "exit": None, "error": None,
                   "cancellable": action in ("scrub", "balance", "remove"), "cancel_requested": False,
                   "progress": {"phase": "starting", "pct": 0, "detail": "", "updated": now},
                   "log": "", "requested_by": {"addr": "mock"}}
            self.jobs.insert(0, job)
            threading.Thread(target=self._run, args=(job,), daemon=True).start()
            return job

    def _run(self, job):
        for pct in range(0, 101, 10):
            time.sleep(1.5)
            if job["state"] != "running":
                return
            job["progress"] = {"phase": "working", "pct": pct, "detail": f"step {pct // 10} of 10",
                               "time_left": f"{(100 - pct) * 2}s", "updated": int(time.time())}
        job.update(state="done", finished=int(time.time()), exit=0,
                   progress={"phase": "finished", "pct": 100, "detail": "", "updated": int(time.time())})

    def cancel(self, kind):
        with self.lock:
            for j in self.jobs:
                if j["state"] == "running":
                    j.update(state="cancelled", finished=int(time.time()), exit=1,
                             cancel_requested=True)
                    return {"ok": True, "message": f"{kind} cancelled (mock)"}
        return {"ok": True, "message": f"no {kind} was running (mock)"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(WEB), **k)

    def log_message(self, *a):
        pass

    def send_obj(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        mock = self.server.mock
        path = self.path.split("?")[0]
        if path == "/api/status":
            return self.send_obj(mock.status())
        if path == "/api/jobs":
            return self.send_obj({"jobs": mock.jobs[:20], "read_only": False,
                                  "actions": ["add", "balance", "monitor", "mount", "remove", "scrub"]})
        if re.match(r"^/api/jobs/[^/]+/log$", path):
            return self.send_obj({"offset": 0, "next": 0, "text": "", "eof": True})
        m = re.match(r"^/api/jobs/([^/]+)$", path)
        if m:
            job = next((j for j in mock.jobs if j["id"] == m[1]), None)
            return self.send_obj(job or {"error": "no such job"}, 200 if job else 404)
        if path == "/api/devices/candidates":
            return self.send_obj(CANDIDATES)
        if path == "/api/health":
            return self.send_obj({"ok": True})
        return super().do_GET()

    def do_POST(self):
        mock = self.server.mock
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        path = self.path.split("?")[0]
        if path == "/api/cancel" or re.match(r"^/api/jobs/[^/]+/cancel$", path):
            return self.send_obj(mock.cancel(body.get("kind") or "job"))
        if path == "/api/jobs":
            return self.send_obj(mock.start(body.get("action") or "scrub"), 202)
        return self.send_obj({"error": "no such endpoint"}, 404)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-l", "--listen", default="127.0.0.1:8799")
    ap.add_argument("-s", "--scenario", default="healthy", choices=sorted(SCENARIOS))
    ap.add_argument("--list", action="store_true", help="print the scenarios and what they show")
    args = ap.parse_args()
    if args.list:
        for name, fn in sorted(SCENARIOS.items()):
            first = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
            print(f"{name:9} {first}")
        return
    if not (WEB / "index.html").exists():
        raise SystemExit(f"web assets not found at {WEB}")
    host, _, port = args.listen.rpartition(":")
    httpd = Server((host.strip("[]") or "127.0.0.1", int(port)), Handler)
    httpd.mock = Mock(args.scenario)
    print(f"mock braid web ({args.scenario}) on http://{args.listen}/ — serving {WEB}")
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
