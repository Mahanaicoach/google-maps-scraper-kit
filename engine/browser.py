"""Find, fetch and run the Chrome the engine drives: headless, loopback DevTools port, private profile.

    with Browser() as b:
        page = cdp.Page(b.cdp)

An installed Google Chrome, Chromium, Edge or Brave is used first (set CHROME=/path/to/binary to
pick one). On a machine with none, `python3 scripts/engine.py start` downloads Chrome for Testing's
headless shell into .engine/, pinned below by version and SHA-256. Standard library only.
"""
import hashlib, os, platform, queue, shutil, subprocess, tempfile, threading, time, urllib.parse, urllib.request
import zipfile

from cdp import CDP, CDPError

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.join(KIT, ".engine")  # everything the engine keeps: jobs, results, logs, its Chrome

CANDIDATES = {
    "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
               "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
    "Linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
              "microsoft-edge", "brave-browser"],
    "Windows": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles%\Chromium\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"],
}

# Chrome for Testing's headless shell (https://googlechromelabs.github.io/chrome-for-testing/),
# Stable channel. Each zip's SHA-256 was computed from the download on 2026-10-31. A download that
# does not match is deleted and never run. Windows on Arm runs the x64 build under emulation.
CFT_VERSION = "156.0.8078.80"
CFT_URL = "https://storage.googleapis.com/chrome-for-testing-public/{v}/{p}/chrome-headless-shell-{p}.zip"
CFT_SHA256 = {
    "mac-arm64": "ea615baa2e9caf0d02d1898a6562b5f8fa9c35239707dae0f592178df5b986ac",
    "mac-x64": "535d417d9034cbd11e7dacd1a71aa3d6e1b525e6d5dcd2671eb2532f01481228",
    "linux64": "547dcd374e02961bf998f6046a267644f8502225c36bb78ca307dab32a0b3eb4",
    "linux-arm64": "5bb6a00a4285c13303d3f72c5afdb70f144ea7a93eed462365a52b69631c9607",
    "win64": "10a77466005e7f64e00f1a75fd1ea81f4a38a60093c65fea6895843b26e60a9a",
}

# What Chrome needs on a bare Debian or Ubuntu (a server, a container). apt picks the t64 variants
# (libasound2t64 and friends) on Ubuntu 24.04 by itself.
APT_LINE = ("sudo apt-get install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 "
            "libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 "
            "libcairo2 libasound2")


PY = "py" if os.name == "nt" else "python3"  # what the user types in the messages below


class BrowserError(RuntimeError):
    pass


def make_home():
    """.engine/, private to this user on macOS and Linux (results hold scraped phone numbers)."""
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    if os.name != "nt":
        os.chmod(HOME, 0o700)


def cft_platform(system=None, machine=None):
    """Chrome for Testing's name for this machine (mac-arm64, linux64, ...), or None."""
    system = system or platform.system()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        (machine or platform.machine()).lower())
    return {("Darwin", "arm64"): "mac-arm64", ("Darwin", "x64"): "mac-x64",
            ("Linux", "x64"): "linux64", ("Linux", "arm64"): "linux-arm64",
            ("Windows", "x64"): "win64", ("Windows", "arm64"): "win64"}.get((system, arch))


def shell_path(plat=None):
    """Where the downloaded headless shell lives (it may not exist yet)."""
    plat = plat or cft_platform() or "none"
    exe = "chrome-headless-shell.exe" if plat.startswith("win") else "chrome-headless-shell"
    return os.path.join(HOME, "chrome", CFT_VERSION, f"chrome-headless-shell-{plat}", exe)


def find_chrome():
    """Path of a usable Chromium-based browser: CHROME, an installed one, then the downloaded shell."""
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    for c in CANDIDATES.get(platform.system(), []):
        c = os.path.expandvars(c)
        if os.sep not in c:
            c = shutil.which(c) or ""
            if is_snap(c):  # Ubuntu's chromium: the snap's private /tmp hides DevToolsActivePort
                continue
        if os.path.isfile(c):
            return c
    return shell_path() if os.path.isfile(shell_path()) else None


def always_headless(path):
    """True for Chrome for Testing's headless shell, which cannot open a window (SCRAPER_HEADLESS=0)."""
    return "headless-shell" in os.path.basename(path or "")


def is_snap(path):
    """True for a snap browser: /snap/bin/chromium, or Ubuntu's /usr/bin/chromium-browser script
    that runs it."""
    if path.startswith("/snap/") or os.path.realpath(path).startswith("/snap/"):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return head.startswith(b"#!") and b"/snap/" in head


def sweep_profiles(days=2):
    """Remove temporary Chrome profiles older than `days` left by an engine that was killed
    (taskkill /F on Windows skips the cleanup). No job runs that long."""
    root, cutoff = tempfile.gettempdir(), time.time() - days * 86400
    try:
        names = [n for n in os.listdir(root) if n.startswith("gmskit-chrome-")]
    except OSError:
        return
    for n in names:
        path = os.path.join(root, n)
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def download(log=print, plat=None, opener=urllib.request.urlopen):
    """Fetch, verify and unpack the pinned headless shell into .engine/chrome/; its path."""
    plat = plat or cft_platform()
    if plat not in CFT_SHA256:
        raise BrowserError(f"No Chrome for Testing build exists for {platform.system()} {platform.machine()}. "
                           "Install Chromium, then set CHROME to its path.")
    url = CFT_URL.format(v=CFT_VERSION, p=plat)
    root = os.path.dirname(os.path.dirname(shell_path(plat)))
    make_home()
    os.makedirs(root, exist_ok=True)
    tmp = os.path.join(root, f"{plat}.zip.part")
    log(f"▶ No Chrome found, so downloading Chrome for Testing {CFT_VERSION} (headless shell, {plat}, "
        f"about 100 MB) once into .engine/ from {url}")
    h = hashlib.sha256()
    try:
        with opener(url, timeout=60) as r, open(tmp, "wb") as f:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                h.update(chunk)
                f.write(chunk)
    except OSError as e:
        _remove(tmp)
        raise BrowserError(f"Could not download Chrome for Testing from {url}: {e}") from e
    if h.hexdigest() != CFT_SHA256[plat]:
        _remove(tmp)
        raise BrowserError("The Chrome download does not match the SHA-256 pinned in engine/browser.py, "
                           "so it was deleted and not run. Try again later, or install Google Chrome.")
    staging = tempfile.mkdtemp(prefix="unpack-", dir=root)
    try:
        with zipfile.ZipFile(tmp) as z:
            for m in z.infolist():
                path = z.extract(m, staging)  # zipfile drops absolute paths and ".." parts
                mode = m.external_attr >> 16
                if mode & 0o111 and not m.is_dir():
                    os.chmod(path, 0o755)
        final = os.path.dirname(shell_path(plat))
        shutil.rmtree(final, ignore_errors=True)
        os.replace(os.path.join(staging, os.path.basename(final)), final)  # a half-unpacked shell never shows
    except (OSError, zipfile.BadZipFile) as e:
        raise BrowserError(f"Could not unpack the Chrome download: {e}") from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        _remove(tmp)
    log("  ✓ SHA-256 verified, unpacked")
    return shell_path(plat)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def start_error(path, code, stderr):
    """The message for a Chrome that exited at once, with the apt line when Linux libraries are missing."""
    missing = [l.strip() for l in stderr.splitlines() if "error while loading shared libraries" in l]
    if missing:
        return (f"Chrome cannot start: {missing[0]}\n  Install the libraries it needs "
                f"(Debian or Ubuntu):\n    {APT_LINE}")
    lines = [l.strip() for l in stderr.strip().splitlines() if l.strip()]
    fatal = [l for l in lines if "FATAL" in l]  # Chrome prints a stack trace after it
    last = fatal[0] if fatal else "; ".join(lines[-3:])
    return f"Chrome exited at once (code {code}): {path}" + (f": {last}" if last else "")


def split_proxy(url):
    """(the proxy as Chrome takes it, username, password): Chrome takes no user:pass in a proxy URL
    and knows socks5:// only (it resolves names through a SOCKS5 proxy anyway)."""
    u = urllib.parse.urlsplit(url)
    host = f"[{u.hostname}]" if ":" in (u.hostname or "") else u.hostname
    scheme = "socks5" if u.scheme == "socks5h" else u.scheme
    return (f"{scheme}://{host}" + (f":{u.port}" if u.port else ""),
            urllib.parse.unquote(u.username or ""), urllib.parse.unquote(u.password or ""))


class ProxyAuth:
    """Logs one tab in to its proxy: Chrome asks over CDP (Fetch.authRequired) when the proxy answers
    407, and gets the username and password here. With Fetch on, every request of the tab waits until
    it is continued, so a thread continues them. The credentials go to Chrome only, never to a log."""

    def __init__(self, page, user, password):
        self.page, self.user, self.password = page, user, password
        self.q = page.cdp.subscribe("Fetch.requestPaused", page.sid)
        page.cdp.subscribe("Fetch.authRequired", page.sid, q=self.q)
        self.done, self.asked, self.refused = threading.Event(), set(), False
        page.send("Fetch.enable", {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]})
        threading.Thread(target=self._run, daemon=True, name="proxy-auth").start()

    def _run(self):
        while not self.done.is_set():
            try:
                ev = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if not isinstance(ev, dict):  # the connection died
                return
            rid = ev.get("requestId")
            try:
                if "authChallenge" not in ev:
                    self.page.send("Fetch.continueRequest", {"requestId": rid})
                elif ev["authChallenge"].get("source") == "Proxy" and rid not in self.asked:
                    self.asked.add(rid)  # asked again for the same request: the proxy refused them
                    self.page.send("Fetch.continueWithAuth", {"requestId": rid, "authChallengeResponse": {
                        "response": "ProvideCredentials", "username": self.user, "password": self.password}})
                else:
                    self.refused = self.refused or rid in self.asked
                    self.page.send("Fetch.continueWithAuth", {"requestId": rid, "authChallengeResponse": {
                        "response": "CancelAuth" if rid in self.asked else "Default"}})
            except CDPError:  # the tab closed or the request finished meanwhile
                if self.page.cdp.closed:
                    return

    def close(self):
        self.done.set()
        for m in ("Fetch.requestPaused", "Fetch.authRequired"):
            self.page.cdp.unsubscribe(m, self.q, self.page.sid)


class Browser:
    """A Chrome whose DevTools socket is on 127.0.0.1; `cdp` is the browser-level CDP. Headless by
    default; headless=False runs it as a normal window off-screen (Google's full view: see details.py)."""

    def __init__(self, path=None, headless=True, profile=None, lang="en-US", timeout=30):
        self.path = path or find_chrome()
        if not self.path:
            raise BrowserError(f"No Chrome, Chromium, Edge or Brave found. Run `{PY} scripts/engine.py start`, "
                               "which downloads one, or install Google Chrome.")
        self._own_profile = profile is None
        self.profile = profile or tempfile.mkdtemp(prefix="gmskit-chrome-")
        args = [self.path, "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--disable-background-networking", "--disable-sync", "--mute-audio",
                "--disable-component-update", "--disable-domain-reliability", "--disable-breakpad",
                "--metrics-recording-only", "--no-pings", "--disable-client-side-phishing-detection",
                "--disable-blink-features=AutomationControlled", "--window-size=1280,900",
                f"--lang={lang}", "--disable-dev-shm-usage"]
        if headless and not always_headless(self.path):  # the shell is always headless
            args.append("--headless=new")
        elif not headless:
            args.append("--window-position=-3000,-3000")  # a real window, kept out of sight
        if platform.system() == "Linux" and hasattr(os, "geteuid") and os.geteuid() == 0:
            args.append("--no-sandbox")  # Chrome refuses to run as root with its sandbox (containers)
        self.proc, self.cdp = None, None
        self._err = open(os.path.join(self.profile, "chrome-stderr.log"), "w+b")
        try:
            try:
                port, path = self._launch(args, timeout)
            except BrowserError:
                # Ubuntu 23.10 and newer (AppArmor) block Chrome's sandbox for a browser that is not
                # installed under /opt, such as the downloaded headless shell. Like Playwright, run it
                # without; it only ever loads Google Maps.
                self._err.seek(0)
                if b"No usable sandbox" not in self._err.read() or "--no-sandbox" in args:
                    raise
                port, path = self._launch(args + ["--no-sandbox"], timeout)
            self.cdp = CDP(f"ws://127.0.0.1:{port}{path}", timeout=timeout)
        except BrowserError:
            self.close()
            raise
        except Exception as e:  # the DevTools websocket refused or broke: never leave Chrome running
            self.close()
            raise BrowserError(f"Could not connect to Chrome's DevTools port: {e}") from e

    def _launch(self, args, timeout):
        """Start Chrome; (DevTools port, path) once it listens, else BrowserError (Chrome stopped)."""
        self._err.seek(0)
        self._err.truncate()
        _remove(os.path.join(self.profile, "DevToolsActivePort"))
        kw = {"stdout": subprocess.DEVNULL, "stderr": self._err, "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(args + ["about:blank"], **kw)
        except OSError as e:
            raise BrowserError(f"Could not start Chrome ({self.path}): {e}") from e
        active = os.path.join(self.profile, "DevToolsActivePort")
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                self._err.seek(0)
                raise BrowserError(start_error(self.path, self.proc.returncode,
                                               self._err.read().decode("utf-8", "replace")))
            try:
                with open(active, encoding="utf-8") as f:
                    port, path = f.read().split()
                return port, path
            except (OSError, ValueError):
                time.sleep(0.1)
        raise BrowserError(f"Chrome did not open its DevTools port within {timeout} s: {self.path}")

    def version(self):
        return self.cdp.send("Browser.getVersion").get("product", "")

    def context(self, proxy=None):
        """A new browser context (its own cookies, and `proxy` for all its traffic); its id. Each
        keyword gets one, so proxies rotate without restarting Chrome."""
        params = {"disposeOnDetach": True}
        if proxy:
            params["proxyServer"] = split_proxy(proxy)[0]
        return self.cdp.send("Target.createBrowserContext", params)["browserContextId"]

    def dispose(self, context):
        try:
            self.cdp.send("Target.disposeBrowserContext", {"browserContextId": context})
        except (CDPError, OSError):
            pass

    def kill(self):
        """Stop Chrome now (the job watchdog): every CDP call still waiting fails at once."""
        try:
            self.proc.kill()
        except (OSError, AttributeError):
            pass

    def close(self):
        if self.cdp:
            try:
                self.cdp.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self._cleanup()

    def _cleanup(self):
        self._err.close()
        if self._own_profile:
            for _ in range(10):  # Windows keeps the profile locked for a moment after Chrome exits
                shutil.rmtree(self.profile, ignore_errors=True)
                if not os.path.exists(self.profile):
                    break
                time.sleep(0.3)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
