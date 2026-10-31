#!/usr/bin/env python3
"""Run the scraping engine without Docker: downloads the engine's own release build, checks its
SHA-256, and starts it on this machine behind the same guard proxy the Docker setup uses.

    python3 scripts/engine.py start     # first run downloads ~60 MB, the first scrape ~170 MB of Chromium
    python3 scripts/engine.py status
    python3 scripts/engine.py restart
    python3 scripts/engine.py stop

Standard library only. The engine listens on a private port; this script's proxy serves
http://localhost:8080 (SCRAPER_PORT in .env) with the Host and Origin checks, page fixes and
Content-Security-Policy read from ui-branding.conf, so scripts, slash commands and the web UI work
exactly as with Docker. Everything lives in .engine/ in the kit folder; delete it to remove the engine.
"""
import hashlib, http.client, json, os, platform, re, signal, socket, subprocess, sys, time
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.join(KIT, ".engine")
STATE = os.path.join(HOME, "state.json")
LOG = os.path.join(HOME, "engine.log")
CONF = os.path.join(KIT, "ui-branding.conf")
VERSION = "1.18.1"  # same engine release as the Docker image in docker-compose.yml
RELEASE = f"https://github.com/gosom/google-maps-scraper/releases/download/v{VERSION}/"
# Release assets and their SHA-256 as GitHub publishes them. A download that does not match is
# deleted, never run. Apple Silicon runs the Intel build through Rosetta; there is no arm64 build.
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


def public_port():
    """SCRAPER_PORT from the environment or the kit's .env, like docker-compose.yml."""
    port = os.environ.get("SCRAPER_PORT")
    try:
        with open(os.path.join(KIT, ".env"), encoding="utf-8") as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and k == "SCRAPER_PORT" and not port:
                    port = v.strip().strip("\"'")
    except OSError:
        pass
    return int(port or 8080)


def build():
    """(asset name, sha256) for this machine, or exit with what to do instead."""
    system, machine = platform.system().lower(), platform.machine().lower()
    if system not in BUILDS or machine not in ("x86_64", "amd64", "arm64", "aarch64"):
        sys.exit(f"✗ No engine build for {system}/{machine}. Use Docker instead (see SETUP.md).")
    if machine in ("arm64", "aarch64") and system == "linux":
        sys.exit("✗ The engine has no Linux arm64 build. Use Docker instead (see SETUP.md).")
    if machine == "arm64" and system == "darwin" and \
            subprocess.run(["arch", "-x86_64", "/usr/bin/true"], capture_output=True).returncode:
        sys.exit("✗ Apple Silicon runs the engine through Rosetta, which is not installed. Install it:\n"
                 "    softwareupdate --install-rosetta --agree-to-license")
    return BUILDS[system]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def binary():
    """Path to a verified engine build, downloading it once."""
    name, digest = build()
    path = os.path.join(HOME, name)
    if os.path.exists(path) and sha256(path) == digest:
        return path
    os.makedirs(HOME, exist_ok=True)
    print(f"▶ Downloading the engine ({name}, about 60 MB) from its GitHub release…")
    tmp = path + ".part"
    with urllib.request.urlopen(RELEASE + name, timeout=60) as resp, open(tmp, "wb") as f:
        for chunk in iter(lambda: resp.read(1 << 20), b""):
            f.write(chunk)
    if sha256(tmp) != digest:
        os.remove(tmp)
        sys.exit("✗ The download does not match the engine's published SHA-256, so it was deleted "
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


def answers(port, timeout=2):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/jobs", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def port_free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def spawn(cmd, log):
    """Start a process that outlives this script, its output appended to `log`."""
    out = open(log, "ab")
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                                creationflags=flags, cwd=HOME).pid
    return subprocess.Popen(cmd, stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                            start_new_session=True, cwd=HOME).pid


def kill(pid):
    """Stop a process and its children (the engine starts Chromium)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(50):
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def start():
    port = public_port()
    st = read_state()
    if st and answers(port):
        print(f"✓ The engine is already running: http://localhost:{port}")
        return
    if st:
        stop(quiet=True)
    if not port_free(port):
        sys.exit(f"✗ Port {port} is in use. Is the Docker engine running? Stop it with "
                 f"`docker compose stop`, or set SCRAPER_PORT in .env to a free port.")
    path = binary()
    with socket.socket() as s:  # a free private port for the engine itself
        s.bind(("127.0.0.1", 0))
        engine_port = s.getsockname()[1]
    os.makedirs(os.path.join(HOME, "data"), exist_ok=True)
    engine = spawn([path, "-web", "-addr", f"127.0.0.1:{engine_port}",
                    "-data-folder", os.path.join(HOME, "data"), "-c", "4"], LOG)
    proxy = spawn([sys.executable, os.path.abspath(__file__), "_proxy", str(port), str(engine_port)],
                  os.path.join(HOME, "proxy.log"))
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"engine_pid": engine, "proxy_pid": proxy, "port": port,
                   "engine_port": engine_port, "log": LOG, "version": VERSION}, f)
    for _ in range(60):
        if answers(port):
            print(f"✓ Engine running without Docker: http://localhost:{port}\n"
                  f"  The first scrape downloads Chromium (about 170 MB) once.\n"
                  f"  Stop it with: python3 scripts/engine.py stop")
            return
        time.sleep(1)
    stop(quiet=True)
    sys.exit(f"✗ The engine did not start. Its log: {LOG}")


def stop(quiet=False):
    st = read_state()
    if not st:
        if not quiet:
            print("The engine is not running.")
        return
    for key in ("proxy_pid", "engine_pid"):
        if st.get(key):
            kill(st[key])
    try:
        os.remove(STATE)
    except OSError:
        pass
    if not quiet:
        print("✓ Engine stopped. Its jobs and results stay in .engine/data.")


def status():
    st = read_state()
    port = public_port()
    if st and answers(port):
        print(f"✓ Running without Docker on http://localhost:{port} (engine {st.get('version')})")
    else:
        print("✗ Not running. Start it with: python3 scripts/engine.py start")
        sys.exit(1)


# ── The guard proxy: the nginx `ui` service of docker-compose.yml, in Python ──
def conf_rules(text):
    """(sub_filter pairs, Content-Security-Policy) from ui-branding.conf, so Docker and native
    mode share one set of page fixes."""
    q = r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")"""
    unq = lambda s: re.sub(r"\\(.)", r"\1", s[1:-1])
    pairs = [(unq(a), unq(b)) for a, b in re.findall(rf"sub_filter\s+{q}\s+{q}\s*;", text, re.S)]
    csp = re.search(rf"add_header\s+Content-Security-Policy\s+{q}", text)
    return pairs, unq(csp.group(1)) if csp else None


def make_handler(engine_port, pairs, csp):
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, headers, body):
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
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
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            conn = http.client.HTTPConnection("127.0.0.1", engine_port, timeout=600)
            try:
                conn.request(self.command, self.path, body, headers)
                resp = conn.getresponse()
                data = resp.read()
            except OSError:
                return self._send(502, [], b"engine not reachable")
            finally:
                conn.close()
            out = [(k, v) for k, v in resp.getheaders() if k.lower() not in HOP]
            if "text/html" in (resp.getheader("Content-Type") or ""):
                html = data.decode("utf-8", "replace")
                for a, b in pairs:
                    html = html.replace(a, b)
                data = html.encode("utf-8")
                if csp:
                    out = [(k, v) for k, v in out if k.lower() != "content-security-policy"]
                    out.append(("Content-Security-Policy", csp))
            self._send(resp.status, out, data)

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
               "restart": lambda: (stop(quiet=True), start())}
    if cmd not in actions:
        sys.exit("Usage: python3 scripts/engine.py start | stop | restart | status")
    actions[cmd]()


if __name__ == "__main__":
    main()
