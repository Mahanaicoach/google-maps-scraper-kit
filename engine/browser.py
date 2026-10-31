"""Find and run a local Chrome for the engine: headless, loopback DevTools port, private profile.

    with Browser() as b:
        page = cdp.Page(b.cdp)

Uses an installed Google Chrome, Chromium, Edge or Brave (set CHROME=/path/to/binary to pick one).
Standard library only.
"""
import os, platform, shutil, subprocess, sys, tempfile, time

from cdp import CDP

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


def find_chrome():
    """Path of a usable Chromium-based browser, or None."""
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    for c in CANDIDATES.get(platform.system(), []):
        c = os.path.expandvars(c)
        if os.path.isfile(c):
            return c
        if os.sep not in c and shutil.which(c):
            return shutil.which(c)
    return None


class Browser:
    """A headless Chrome whose DevTools socket is on 127.0.0.1; `cdp` is the browser-level CDP."""

    def __init__(self, path=None, headless=True, profile=None, lang="en-US", timeout=30):
        self.path = path or find_chrome()
        if not self.path:
            sys.exit("✗ No Chrome, Chromium, Edge or Brave found. Install Google Chrome, or set CHROME to "
                     "the browser's path.")
        self._own_profile = profile is None
        self.profile = profile or tempfile.mkdtemp(prefix="gmskit-chrome-")
        args = [self.path, "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                "--disable-background-networking", "--disable-sync", "--mute-audio",
                "--disable-blink-features=AutomationControlled", "--window-size=1280,900",
                f"--lang={lang}", "--disable-dev-shm-usage", "about:blank"]
        if headless:
            args.insert(1, "--headless=new")
        kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(args, **kw)
        active = os.path.join(self.profile, "DevToolsActivePort")
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                sys.exit(f"✗ Chrome exited at once (code {self.proc.returncode}): {self.path}")
            try:
                with open(active, encoding="utf-8") as f:
                    port, path = f.read().split()
                break
            except (OSError, ValueError):
                time.sleep(0.1)
        else:
            self.close()
            sys.exit(f"✗ Chrome did not open its DevTools port within {timeout} s: {self.path}")
        self.ws_url = f"ws://127.0.0.1:{port}{path}"
        self.cdp = CDP(self.ws_url, timeout=timeout)

    def version(self):
        return self.cdp.send("Browser.getVersion").get("product", "")

    def close(self):
        try:
            self.cdp.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        if self._own_profile:
            shutil.rmtree(self.profile, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
