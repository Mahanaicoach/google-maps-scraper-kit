#!/usr/bin/env python3
"""Run the kit's own engine on this machine, without Docker.

    python3 scripts/engine.py start      # first run on a machine without Chrome downloads one (~100 MB)
    python3 scripts/engine.py status
    python3 scripts/engine.py restart
    python3 scripts/engine.py stop

Standard library only. The engine (engine/serve.py) serves http://localhost:8080 (SCRAPER_PORT in
.env), the same engine and API that `docker compose up -d` runs in a container. It drives an
installed Chrome, Chromium, Edge or Brave; on a machine with none, `start` downloads Chrome for
Testing's headless shell, pinned by version and SHA-256. Everything lives in .engine/ in the kit
folder; delete it to remove the engine and its results.
"""
import json, os, signal, socket, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

for _stream in (sys.stdout, sys.stderr):  # ▶ ✓ ✗ on a Windows cp1252 console
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KIT, "engine"))
import browser  # noqa: E402

HOME = browser.HOME
SERVE = os.path.join(KIT, "engine", "serve.py")
STATE = os.path.join(HOME, "state.json")
LOCK = os.path.join(HOME, "start.lock")
LOG = os.path.join(HOME, "engine.log")
LOG_KEEP = 2_000_000  # bytes of engine.log kept across starts
MARK = "gmskit-engine/"
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # HTTP_PROXY must not catch localhost
PY = "py" if os.name == "nt" else "python3"  # what the user types; the README says `py` on Windows


def public_port():
    """The port scrape.py talks to (SCRAPER_PORT / SCRAPER_BASE_URL, environment or .env), read by
    scrape.py's own loader so both always agree."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scrape
    u = urllib.parse.urlparse(scrape.BASE)
    if u.hostname not in ("localhost", "127.0.0.1", "::1"):
        sys.exit(f"✗ SCRAPER_BASE_URL points at {scrape.BASE}; engine.py runs the engine on this machine only.")
    return u.port or 80


def probe(port, timeout=2):
    """"ours" (this kit's engine answers, here or in Docker), "other" (something else, such as the
    kit's 1.x Docker engine) or None."""
    try:
        with _NO_PROXY.open(f"http://127.0.0.1:{port}/api/v1/jobs", timeout=timeout) as r:
            return "ours" if (r.headers.get("X-Engine") or "").startswith(MARK) else "other"
    except urllib.error.HTTPError as e:  # 401 without the key is still ours
        e.close()
        return "ours" if (e.headers.get("X-Engine") or "").startswith(MARK) else "other"
    except Exception:  # noqa: BLE001 - nothing listening, or not HTTP
        return None


def port_free(port):
    """Nothing listens on the port and it can be bound. The connect test matters on macOS, where
    SO_REUSEADDR lets 127.0.0.1:port bind while another server listens on *:port."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return False
    except OSError:
        pass
    with socket.socket() as s:
        if os.name != "nt":  # like the engine's own server: connections in TIME_WAIT do not block
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def read_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)  # a crash mid-write never leaves half a state file


def command_line(pid):
    """The command line of `pid`; "" if the process is gone; None if it cannot be read (PowerShell
    blocked or too slow), which stop must not mistake for "gone"."""
    try:
        if os.name == "nt":
            ps = ["powershell", "-NoProfile", "-Command",
                  f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"]
            out = subprocess.run(ps, capture_output=True, text=True, errors="replace", timeout=20)
            return None if out.returncode else out.stdout
        if os.path.exists(f"/proc/{int(pid)}/cmdline"):
            with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        return subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="], capture_output=True,
                              text=True, errors="replace", timeout=10).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def kill(pid):
    """Stop the engine and its Chrome. False if it could not be told whether `pid` is the engine:
    after a reboot or crash the pid in state.json may belong to an unrelated process."""
    cmd = command_line(pid)
    if cmd is None:
        return False
    if os.path.normcase(SERVE) not in os.path.normcase(cmd):  # Windows: c:\ and C:\ are one path
        return True  # gone, or someone else's process now: nothing of ours to stop
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return True
    try:
        # The engine alone: it pauses its job (which resumes on the next start) and closes its own
        # Chrome. A Chrome that got the signal too would end the job before the engine could pause it.
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return True
    for _ in range(150):  # the engine leads its own process group, Chrome included
        try:
            os.waitpid(pid, os.WNOHANG)  # our own child (start that timed out): reap it, or it lingers as a zombie
        except ChildProcessError:
            pass
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
    sys.exit(f"✗ Port {port} is in use. Is the kit's older Docker engine running? Stop it with `docker compose stop`,\n"
             f"  or set SCRAPER_PORT in .env to a free port. To see what holds it: {hint}")


def chrome(download):
    """A browser for the engine; downloads the pinned one only when allowed (start, never restart:
    scrape.py may restart the engine unattended, and fetching code is for an explicit start)."""
    path = browser.find_chrome()
    if path:
        return path
    if not download:
        sys.exit(f"✗ No Chrome found. Run: {PY} scripts/engine.py start   (it downloads one)")
    try:
        return browser.download()
    except browser.BrowserError as e:
        sys.exit(f"✗ {e}")


def start(download=True):
    port = public_port()
    seen = probe(port)
    if seen == "ours":
        print(f"✓ The engine is already running: http://localhost:{port}")
        return
    if seen == "other":
        busy(port)
    if read_state():
        stop(quiet=True)  # stale state from a crash or reboot: clean up whatever is still ours
    if not port_free(port):
        busy(port)
    browser.make_home()
    try:  # one start at a time
        lock = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if time.time() - os.path.getmtime(LOCK) < 600:
            sys.exit("✗ Another `engine.py start` is running. Wait for it, or delete .engine/start.lock.")
        os.remove(LOCK)  # left by a start that crashed
        lock = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        _start(port, chrome(download))
    finally:
        os.close(lock)
        os.remove(LOCK)


def _start(port, chrome_path):
    trim_log()
    env = dict(os.environ, CHROME=chrome_path, SCRAPER_PORT=str(port))
    with open(LOG, "ab") as out:  # the engine keeps its own handle
        kw = {"stdout": out, "stderr": out, "stdin": subprocess.DEVNULL, "cwd": HOME, "env": env}
        if os.name == "nt":
            # A hidden console, not DETACHED_PROCESS: Chrome started from a process with no console
            # at all closed at its first page on Windows.
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        try:
            proc = subprocess.Popen([sys.executable, SERVE, "--port", str(port)], **kw)
        except OSError as e:
            sys.exit(f"✗ Could not start the engine: {e}")
    write_state({"pid": proc.pid, "port": port, "chrome": chrome_path, "log": LOG})
    deadline = time.time() + 30
    while time.time() < deadline:
        if probe(port) == "ours":
            print(f"✓ Engine running: http://localhost:{port}  (web UI and API)\n"
                  f"  Browser: {chrome_path}\n"
                  f"  Stop it with: {PY} scripts/engine.py stop")
            return
        if proc.poll() is not None:
            stop(quiet=True)
            sys.exit(f"✗ The engine exited at once (code {proc.returncode}). The end of {LOG}:\n{tail(LOG)}")
        time.sleep(0.5)
    stop(quiet=True)
    sys.exit(f"✗ The engine did not answer within 30 seconds. The end of {LOG}:\n{tail(LOG)}")


def stop(quiet=False):
    st = read_state()
    if not st:
        if not quiet:
            port = public_port()
            if probe(port) == "ours":  # started by docker compose, not by this script
                print(f"ℹ The engine on port {port} was not started by engine.py; it runs in Docker, probably.\n"
                      "  Stop that one with: docker compose stop")
            else:
                print("The engine is not running.")
        return
    if st.get("pid") and not kill(st["pid"]):
        how = "taskkill /PID {} /T /F" if os.name == "nt" else "kill {}"
        sys.exit(f"✗ Could not check whether process {st['pid']} is the engine, so it was left running.\n"
                 f"  Stop it yourself: {how.format(st['pid'])}")
    try:
        os.remove(STATE)
    except OSError:
        pass
    if not quiet:
        print("✓ Engine stopped. Its jobs and results stay in .engine/data.")


def restart():
    if not read_state() and probe(public_port()) == "ours":  # docker compose started it, not this script
        sys.exit("ℹ This engine runs in Docker, not under engine.py. Restart it with: docker compose restart\n"
                 "  (after an update: docker compose up -d --build)")
    stop(quiet=True)
    start(download=False)


def status():
    port = public_port()
    seen = probe(port)
    if seen == "ours":
        st = read_state() or {}
        print(f"✓ Running on http://localhost:{port}" + (f"  (browser: {st['chrome']})" if st.get("chrome") else ""))
    elif seen == "other":
        print(f"ℹ Port {port} answers, but not as this kit's engine (the kit's older Docker engine, probably).")
        sys.exit(1)
    else:
        found = browser.find_chrome()
        print(f"✗ Not running. Start it with: {PY} scripts/engine.py start\n  Browser: " + (found or
              "none found, so start would download Chrome for Testing (about 100 MB, pinned by SHA-256)"))
        sys.exit(1)


def main():
    cmds = {"start": start, "stop": stop, "status": status,
            "restart": restart}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(f"usage: {PY} scripts/engine.py start|stop|status|restart")
    if sys.version_info < (3, 9):
        sys.exit(f"✗ The engine needs Python 3.9 or newer; this is {sys.version.split()[0]}.")
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
