"""Optional Playwright rendering for business website email and social-link enrichment.

Each render owns its Playwright instance (the API is not thread-safe). At most two run at once.
The browser's HTTP proxy pins public DNS addresses and checks connected peers, including for
redirects, scripts and iframes. It preserves the existing website fetcher's private-network guard.
"""
import select
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SLOTS = threading.BoundedSemaphore(2)
SETUP = "Install optional rendering with: python3 -m pip install -r requirements-browser.txt"


def check_available():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as e:
        raise RuntimeError(SETUP) from e
    from browser import find_chrome
    path = find_chrome()
    if not path:
        raise RuntimeError("Website rendering needs Chrome/Chromium. Install Chrome or set CHROME to its path.")
    return path


def _connect_public(host, port, public_ip, deadline):
    """Resolve once, reject mixed/private answers, and connect to a validated numeric address."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos or not all(public_ip(i[4][0]) for i in infos):
        raise OSError("non-public destination")
    for family, kind, proto, _, address in infos:
        peer = socket.socket(family, kind, proto)
        try:
            peer.settimeout(min(5, max(0.1, deadline - time.monotonic())))
            peer.connect(address)
            if not public_ip(peer.getpeername()[0]):
                raise OSError("non-public peer")
            return peer
        except OSError:
            peer.close()
    raise OSError("destination unavailable")


class _ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _relay(self, peer):
        with peer:
            while not self.server.stopped.is_set() and time.monotonic() < self.server.deadline:
                ready, _, _ = select.select([self.connection, peer], [], [], 0.2)
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (peer if source is self.connection else self.connection).sendall(data)

    def do_CONNECT(self):
        try:
            dest = urllib.parse.urlsplit("//" + self.path)
            if not dest.hostname or dest.username or dest.password or dest.path:
                raise ValueError("invalid destination")
            peer = _connect_public(dest.hostname, dest.port or 443, self.server.public_ip, self.server.deadline)
        except (OSError, ValueError, UnicodeError):
            self.send_error(403, "Destination unavailable")
            return
        try:
            self.send_response(200, "Connection established")
            self.end_headers()
            self._relay(peer)
        except OSError:
            pass
        finally:
            peer.close()

    def do_GET(self):
        try:
            dest = urllib.parse.urlsplit(self.path)
            if dest.scheme != "http" or not dest.hostname or dest.username or dest.password:
                raise ValueError("invalid destination")
            peer = _connect_public(dest.hostname, dest.port or 80, self.server.public_ip, self.server.deadline)
        except (OSError, ValueError, UnicodeError):
            self.send_error(403, "Destination unavailable")
            return
        try:
            path = urllib.parse.urlunsplit(("", "", dest.path or "/", dest.query, ""))
            lines = [f"{self.command} {path} HTTP/1.1", f"Host: {dest.netloc}", "Connection: close"]
            lines += [f"{k}: {v}" for k, v in self.headers.items()
                      if k.lower() not in ("host", "connection", "proxy-connection", "proxy-authorization",
                                           "content-length", "transfer-encoding")]
            peer.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            self._relay(peer)
        except OSError:
            peer.close()

    do_HEAD = do_GET


class _PublicProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, public_ip, deadline):
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.public_ip, self.deadline, self.stopped = public_ip, deadline, threading.Event()

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server_port}"

    def __exit__(self, *args):
        self.stopped.set()
        self.shutdown()
        self.server_close()
        self.thread.join(2)


def render(url, public_ip, timeout=20):
    """{html, status}: render public content, with bounded waits and no persistent browser state."""
    failure = lambda status: {"html": "", "status": status}
    try:
        parsed = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return failure("blocked")
        url = parsed.geturl()
    except ValueError:
        return failure("blocked")
    deadline = time.monotonic() + timeout
    if not _SLOTS.acquire(timeout=max(0, timeout)):
        return failure("timeout")
    try:
        path = check_available()
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
        def left():
            return max(1, int((deadline - time.monotonic()) * 1000))
        with _PublicProxy(public_ip, deadline) as proxy, sync_playwright() as pw:
            with pw.chromium.launch(executable_path=path, headless=True, timeout=left(),
                                    chromium_sandbox=True, proxy={"server": proxy},
                                    args=["--proxy-bypass-list=<-loopback>",
                                          "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]) as browser:
                context = browser.new_context(service_workers="block", accept_downloads=False)
                try:
                    def route_request(route):
                        req = route.request
                        if (req.resource_type in ("image", "media", "font") or req.method not in ("GET", "HEAD")
                                or urllib.parse.urlsplit(req.url).scheme not in ("http", "https")):
                            route.abort()
                        else:
                            route.continue_()
                    context.route("**/*", route_request)
                    page = context.new_page()
                    response = page.goto(url, wait_until="domcontentloaded", timeout=left())
                    if response and response.status >= 400:
                        return failure("blocked" if response.status in (401, 403, 429) else "error")
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(3000, left()))
                    except PlaywrightTimeout:
                        pass  # analytics may never settle; keep the rendered DOM
                    title = page.title().lower()
                    if any(t in title for t in ("just a moment", "access denied", "verify you are human", "captcha")):
                        return failure("blocked")
                    return {"html": page.content()[:400_000], "status": "rendered"}
                finally:
                    context.close()
    except Exception as e:
        # No exception text: URLs, proxy credentials and page contents must not leak into logs.
        return failure("timeout" if "timeout" in type(e).__name__.lower() else "error")
    finally:
        _SLOTS.release()
