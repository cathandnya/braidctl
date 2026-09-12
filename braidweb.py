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
import socket
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
                          "read_only": True, "mountpoint": cfg["mountpoint"]}
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

    def do_POST(self):
        # Read-only for now: mutations arrive with the job runner.
        self.send_json(405, {"error": "this build serves GET only"})

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
                 "limiter": Limiter(), "last": time.time()}
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
