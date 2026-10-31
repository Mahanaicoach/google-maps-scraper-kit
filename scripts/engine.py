#!/usr/bin/env python3
"""Run the scraping engine without Docker: downloads the engine's own release build, checks its
SHA-256, and starts it on this machine behind the same guard proxy the Docker setup uses.

    python3 scripts/engine.py start     # first run downloads ~60 MB, the first scrape ~170 MB of Chromium
    python3 scripts/engine.py status
    python3 scripts/engine.py restart
    python3 scripts/engine.py stop

Standard library only. The engine listens on a private port; this script's proxy serves
http://localhost:8080 (SCRAPER_PORT in .env) with the Host and Origin checks, page fixes and
Content-Security-Policy read from ui-branding.conf, so scripts, slash commands and the web UI use it
as they use Docker. Everything lives in .engine/ in the kit folder; delete it to remove the engine.
"""
import hashlib, http.client, json, os, platform, re, signal, socket, subprocess, sys, time
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _stream in (sys.stdout, sys.stderr):  # ▶ ✓ ✗ on a Windows cp1252 console
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.join(KIT, ".engine")
STATE = os.path.join(HOME, "state.json")
LOCK = os.path.join(HOME, "start.lock")
LOG = os.path.join(HOME, "engine.log")
PROXY_LOG = os.path.join(HOME, "proxy.log")
CONF = os.path.join(KIT, "ui-branding.conf")
VERSION = "1.18.1"  # same engine release as the Docker image in docker-compose.yml
MARK = "gmskit-native/" + VERSION  # X-Engine header on every proxy reply: tells native from Docker
LOG_KEEP = 2_000_000  # bytes of engine.log kept across starts
RELEASE = f"https://github.com/gosom/google-maps-scraper/releases/download/v{VERSION}/"
# Release assets and their SHA-256, pinned here from GitHub's release page. A download that does not
# match is deleted, never run. Apple Silicon runs the Intel build through Rosetta (no arm64 build).
BUILDS = {
    "darwin": ("google_maps_scraper-1.18.1-darwin-amd64",
               "9793889f88df54944c8faaf8ed90363c60b324d15444dd7bb692662f9aa50a75"),
    "linux": ("google_maps_scraper-1.18.1-linux-amd64",
              "af573eeab19b174e8a70a5c804b80f191681b2c9d01c96706691af6567926062"),
    "windows": ("google_maps_scraper-1.18.1-windows-amd64.exe",
                "c124fab30f12e4aae25ef52f38eb2bea5422ece708b3171ca0f34be56cf7848f"),
}
LOCAL = r"(localhost|127\.0\.0\.1|\[::1\])"
HOST_OK = re.compile(rf"^{LOCAL}(:\d+)?$", re.I)
ORIGIN_OK = re.compile(rf"^(https?://{LOCAL}(:\d+)?)?$", re.I)
HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-connection", "te",
       "trailer", "content-length", "content-encoding", "accept-encoding"}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # HTTP_PROXY must not catch localhost
PY = "py" if os.name == "nt" else "python3"  # what the user types; the README says `py` on Windows


def public_port():
    """The port scrape.py talks to: SCRAPER_PORT / SCRAPER_BASE_URL from the environment or .env,
    read by scrape.py's own loader so both always agree."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scrape
    u = urllib.parse.urlparse(scrape.BASE)
    if u.hostname not in ("localhost", "127.0.0.1", "::1"):
        sys.exit(f"✗ SCRAPER_BASE_URL points at {scrape.BASE}; engine.py runs the engine on this machine only.")
    return u.port or 80


def build():
    """(asset name, sha256) for this machine, or exit with what to do instead."""
    system, machine = platform.system().lower(), platform.machine().lower()
    if system not in BUILDS or machine not in ("x86_64", "amd64", "arm64", "aarch64"):
        sys.exit(f"✗ No engine build for {system}/{machine}. Use Docker instead (see SETUP.md).")
    if machine in ("arm64", "aarch64") and system == "linux":
        sys.exit("✗ The engine has no Linux arm64 build. Use Docker instead (see SETUP.md).")
    if machine == "arm64" and system == "darwin" and \
            subprocess.run(["arch", "-x86_64", "/usr/bin/true"], capture_output=True).returncode:
        sys.exit("✗ Apple Silicon runs the engine through Rosetta, which is not installed. Install it\n"
                 "  yourself (macOS asks you to accept Apple's licence), then run start again:\n"
                 "    softwareupdate --install-rosetta")
    return BUILDS[system]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def binary(download=True):
    """Path to a verified engine build, downloading it once. restart never downloads: scrape.py may
    call it unattended, and fetching code is for an explicit `start` only."""
    name, digest = build()
    path = os.path.join(HOME, name)
    if os.path.exists(path) and sha256(path) == digest:
        return path
    if not download:
        sys.exit(f"✗ The engine build is missing or changed. Run: {PY} scripts/engine.py start")
    os.makedirs(HOME, exist_ok=True)
    print(f"▶ Downloading the engine ({name}, about 60 MB) from its GitHub release…")
    tmp = path + ".part"
    with urllib.request.urlopen(RELEASE + name, timeout=60) as resp, open(tmp, "wb") as f:
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            f.write(chunk)
            if os.path.exists(LOCK):  # a slow download must not look like a crashed start
                os.utime(LOCK)
    if sha256(tmp) != digest:
        os.remove(tmp)
        sys.exit("✗ The download does not match the SHA-256 pinned in engine.py, so it was deleted "
                 "and not run. Try again, or use Docker.")
    os.replace(tmp, path)
    os.chmod(path, 0o755)
    print("  ✓ SHA-256 verified")
    return path


def read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def probe(port, timeout=2):
    """"native" (this script's proxy and engine answer), "native-down" (the proxy answers but its
    engine is gone, e.g. it crashed), "other" (something else, e.g. the Docker engine) or None."""
    try:
        with _NO_PROXY.open(f"http://127.0.0.1:{port}/api/v1/jobs", timeout=timeout) as r:
            return "native" if (r.headers.get("X-Engine") or "").startswith("gmskit-native/") else "other"
    except urllib.error.HTTPError as e:
        e.close()
        return "native-down" if (e.headers.get("X-Engine") or "").startswith("gmskit-native/") else "other"
    except Exception:
        return None


def port_free(port):
    with socket.socket() as s:
        if os.name != "nt":  # like the proxy's own server: closed connections in TIME_WAIT do not block
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def spawn(cmd, log):
    """Start a process that outlives this script, its output appended to `log`."""
    with open(log, "ab") as out:  # the child keeps its own handle
        if os.name == "nt":
            # A hidden console, not DETACHED_PROCESS: with no console at all, the Chromium the
            # engine starts closed at its first page ("target closed") on Windows.
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            return subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                    creationflags=flags, cwd=HOME)
        return subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=HOME)


def command_line(pid):
    """The command line of `pid`; "" if the process is gone; None if it cannot be read (PowerShell
    blocked or too slow), which stop must not mistake for "gone"."""
    try:
        if os.name == "nt":
            ps = ["powershell", "-NoProfile", "-Command",
                  f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"]
            out = subprocess.run(ps, capture_output=True, text=True, errors="replace", timeout=20)
            if out.returncode:
                return None
            return out.stdout
        if os.path.exists(f"/proc/{int(pid)}/cmdline"):
            with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        return subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="], capture_output=True,
                              text=True, errors="replace", timeout=10).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def ours(pid):
    """True if `pid` is still this kit's engine or proxy, None if that cannot be told. After a reboot
    or crash the pid in state.json may belong to an unrelated process, which stop must never kill."""
    cmd = command_line(pid)
    if cmd is None:
        return None
    return HOME in cmd or (os.path.basename(__file__) in cmd and "_proxy" in cmd)


def kill(pid):
    """Stop a process and its children (the engine starts Chromium). False if it could not be told
    whether `pid` is ours, so the caller can say so instead of claiming it stopped."""
    mine = ours(pid)
    if mine is None:
        return False
    if not mine:
        return True
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return True
    for _ in range(50):
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return True


def tail(path, lines=15):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - 8000))
            return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-lines:])
    except OSError:
        return ""


def trim_log():
    """Keep the last LOG_KEEP bytes of engine.log, so it cannot grow without bound across starts."""
    try:
        if os.path.getsize(LOG) > LOG_KEEP:
            with open(LOG, "rb") as f:
                f.seek(-LOG_KEEP, os.SEEK_END)
                keep = f.read()
            with open(LOG, "wb") as f:
                f.write(keep)
    except OSError:
        pass


def busy(port):
    hint = f"netstat -ano | findstr :{port}" if os.name == "nt" else f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
    sys.exit(f"✗ Port {port} is in use. Is the Docker engine running? Stop it with `docker compose stop`,\n"
             f"  or set SCRAPER_PORT in .env to a free port. To see what holds it: {hint}")


def start(download=True):
    port = public_port()
    seen = probe(port)
    if seen == "native":
        print(f"✓ The engine is already running without Docker: http://localhost:{port}")
        return
    if seen == "other":
        busy(port)
    if seen == "native-down":
        print("ℹ The engine had stopped behind its proxy; starting it again.")
    if os.name == "nt":
        print("ℹ Windows without Docker is not verified yet: if scrapes return 0 places, use Docker.")
    if read_state():
        stop(quiet=True)  # stale state from a crash or reboot: clean up whatever is still ours
    if not port_free(port):
        busy(port)
    os.makedirs(HOME, exist_ok=True)
    try:  # one start at a time: a second one waits for nothing and stops here
        lock = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - os.path.getmtime(LOCK) < 300:
            sys.exit("✗ Another `engine.py start` is running. Wait for it, or delete .engine/start.lock.")
        os.remove(LOCK)  # left by a start that crashed
        lock = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        _start(port, binary(download))
    finally:
        os.close(lock)
        os.remove(LOCK)


def _start(port, path):
    with socket.socket() as s:  # a free private port for the engine itself
        s.bind(("127.0.0.1", 0))
        engine_port = s.getsockname()[1]
    os.makedirs(os.path.join(HOME, "data"), exist_ok=True)
    trim_log()
    try:
        engine = spawn([path, "-web", "-addr", f"127.0.0.1:{engine_port}",
                        "-data-folder", os.path.join(HOME, "data"), "-c", "4"], LOG)
        proxy = spawn([sys.executable, os.path.abspath(__file__), "_proxy", str(port), str(engine_port)],
                      PROXY_LOG)
    except OSError as e:  # e.g. antivirus blocking the downloaded program
        sys.exit(f"✗ Could not start the engine: {e}")
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"engine_pid": engine.pid, "proxy_pid": proxy.pid, "port": port,
                   "engine_port": engine_port, "log": LOG, "version": VERSION}, f)
    deadline = time.time() + 90
    while time.time() < deadline:
        if probe(port) == "native":
            print(f"✓ Engine running without Docker: http://localhost:{port}\n"
                  f"  The first scrape downloads Chromium (about 170 MB) once.\n"
                  f"  Stop it with: {PY} scripts/engine.py stop")
            return
        for proc, log, what in ((engine, LOG, "engine"), (proxy, PROXY_LOG, "proxy")):
            if proc.poll() is not None:
                stop(quiet=True)
                sys.exit(f"✗ The {what} exited at once (code {proc.returncode}). The end of {log}:\n{tail(log)}")
        time.sleep(1)
    stop(quiet=True)
    sys.exit(f"✗ The engine did not answer within 90 seconds. The end of {LOG}:\n{tail(LOG)}")


def stop(quiet=False):
    st = read_state()
    if not st:
        if not quiet:
            print("The engine is not running.")
        return
    unknown = [st[k] for k in ("proxy_pid", "engine_pid") if st.get(k) and not kill(st[k])]
    if unknown:
        how = "taskkill /PID {} /T /F" if os.name == "nt" else "kill {}"
        sys.exit("✗ Could not check whether these processes are the engine, so they were left running: "
                 + ", ".join(map(str, unknown)) + "\n  Stop them yourself: "
                 + "; ".join(how.format(p) for p in unknown))
    try:
        os.remove(STATE)
    except OSError:
        pass
    if not quiet:
        print("✓ Engine stopped. Its jobs and results stay in .engine/data.")


def status():
    port = public_port()
    seen = probe(port)
    if seen == "native":
        print(f"✓ Running without Docker on http://localhost:{port} (engine {VERSION})")
    elif seen == "native-down":
        print(f"✗ The proxy answers but its engine has stopped. Run: {PY} scripts/engine.py restart")
        sys.exit(1)
    elif seen == "other":
        print(f"ℹ Port {port} answers, but not as this script's engine (the Docker engine, probably).")
        sys.exit(1)
    else:
        print(f"✗ Not running. Start it with: {PY} scripts/engine.py start")
        sys.exit(1)


# ── The guard proxy: the nginx `ui` service of docker-compose.yml, in Python ──
def conf_rules(text):
    """(sub_filter pairs, Content-Security-Policy) from ui-branding.conf, so Docker and native
    mode share one set of page fixes. Unescapes only what nginx does (\\" \\' \\\\), so a regex
    such as \\s in the injected script survives."""
    q = r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
    unq = lambda s: re.sub(r"""\\(["'\\])""", r"\1", s[1:-1])
    pairs = [(unq(a), unq(b)) for a, b in re.findall(rf"sub_filter\s+{q}\s+{q}\s*;", text, re.S)]
    csp = re.search(rf"add_header\s+Content-Security-Policy\s+{q}", text)
    return pairs, unq(csp.group(1)) if csp else None


def make_handler(engine_port, pairs, csp):
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, headers, body, length=None):
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("X-Engine", MARK)
            self.send_header("Content-Length", str(len(body) if length is None else length))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _forward(self):
            # Same rules as ui-branding.conf: only local Host headers (DNS rebinding) and no
            # Origin from another site (cross-site posts).
            if not HOST_OK.match(self.headers.get("Host", "")):
                return self._send(421, [], b"")
            if not ORIGIN_OK.match(self.headers.get("Origin", "")):
                return self._send(403, [], b"")
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self.close_connection = True
                return self._send(411, [], b"send a Content-Length")
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
                # Short connect timeout: Windows retries a refused loopback connect for ~2 s, which
                # made a dead engine look like no proxy at all. Long scrapes still get 600 s to reply.
                conn = http.client.HTTPConnection("127.0.0.1", engine_port, timeout=1)
                try:
                    conn.connect()
                    conn.sock.settimeout(600)
                    conn.request(self.command, self.path, body, headers)
                    resp = conn.getresponse()
                    data = resp.read()
                finally:
                    conn.close()
            except (ValueError, UnicodeError):
                self.close_connection = True
                return self._send(400, [], b"bad request")
            except (OSError, http.client.HTTPException):
                return self._send(502, [], b"engine not reachable")
            out = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP]
            if "text/html" in (resp.getheader("Content-Type") or ""):
                html = data.decode("utf-8", "replace")
                for a, b in pairs:
                    html = html.replace(a, b)
                data = html.encode("utf-8")
                if csp:
                    out = [(k, v) for k, v in out if k.lower() != "content-security-policy"]
                    out.append(("Content-Security-Policy", csp))
            length = resp.getheader("Content-Length") if self.command == "HEAD" else None
            self._send(resp.status, out, data, length)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _forward
    return Proxy


def serve_proxy(port, engine_port):
    try:
        with open(CONF, encoding="utf-8") as f:
            pairs, csp = conf_rules(f.read())
    except OSError:
        pairs, csp = [], None
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine_port, pairs, csp)).serve_forever()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "_proxy":
        return serve_proxy(int(sys.argv[2]), int(sys.argv[3]))
    actions = {"start": start, "stop": stop, "status": status,
               "restart": lambda: (stop(quiet=True), start(download=False))}
    if cmd not in actions:
        sys.exit("Usage: python3 scripts/engine.py start | stop | restart | status")
    actions[cmd]()


if __name__ == "__main__":
    main()
