"""Optional Playwright rendering for business website email and social-link enrichment.

Each job owns one browser on a dedicated asyncio thread, with at most two isolated pages at once.
The browser's HTTP proxy pins public DNS addresses and checks connected peers, including for
redirects, scripts and iframes. It preserves the existing website fetcher's private-network guard.
"""
import asyncio
import concurrent.futures
import select
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SLOTS = threading.BoundedSemaphore(2)
_MIN_START = 3  # starting the driver with milliseconds left wastes time and trips Node timers
_CLOSE_SECONDS = 5  # each of browser.close() and pw.stop(); close took 1.6-2.3 s live, and hung 24-300 s at times
SETUP = "Install optional rendering in a virtual environment: see SETUP.md, Optional website rendering (Playwright)."


def _quiet_target_closed(loop, context):
    """Playwright settles the protocol calls of a cancelled or closed page with TargetClosedError once
    the browser is gone. Nothing can await those internal futures, so asyncio would otherwise print
    "Future exception was never retrieved" when the run ends. Everything else still gets reported."""
    if type(context.get("exception")).__name__ != "TargetClosedError":
        loop.default_exception_handler(context)


def merge_status(current, incoming):
    """A successful fetch survives later failed enrichment attempts."""
    if current == "rendered" or (current == "http" and incoming != "rendered"):
        return current
    return incoming


class Budget:
    """One job's lazy browser, shared deadline and two-page concurrency limit.

    Playwright objects stay on their owning event-loop thread. Call close() after enrichment.
    """
    def __init__(self, seconds=30):
        self.seconds, self.deadline, self.lock = seconds, None, threading.Lock()
        self.loop = self.thread = self.session = None
        self.closed = False

    def fetch(self, url, public_ip):
        with self.lock:
            if self.closed:
                return {"html": "", "status": "budget_exhausted"}
            if self.deadline is None:
                self.deadline = time.monotonic() + self.seconds
            if self.deadline - time.monotonic() < (_MIN_START if self.loop is None else 0.5):
                return {"html": "", "status": "budget_exhausted"}
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                self.loop.set_exception_handler(_quiet_target_closed)
                self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
                self.thread.start()
            future = asyncio.run_coroutine_threadsafe(self._fetch(url, public_ip), self.loop)
        try:
            return future.result()
        except concurrent.futures.CancelledError:  # close() ran while this page was still rendering
            return {"html": "", "status": "budget_exhausted"}

    async def _fetch(self, url, public_ip):
        if self.session is None:
            self.session = _Session(public_ip, self.deadline)
        return await self.session.render(url)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            if self.loop is None:
                return
            future = asyncio.run_coroutine_threadsafe(self._close(), self.loop)
        try:
            future.result()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
            self.loop.close()

    async def _close(self):
        # On the loop, after any fetch scheduled before close() has created the session.
        if self.session is not None:
            await self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


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


class _Session:
    def __init__(self, public_ip, deadline):
        self.public_ip, self.deadline = public_ip, deadline
        self.pages, self.start_lock = asyncio.Semaphore(2), asyncio.Lock()
        self.browser = self.pw = self.proxy = None
        self.tasks = set()
        self.has_slot = False
        self.start_error = None

    async def start(self):
        async with self.start_lock:
            if self.browser is not None:
                return
            if self.start_error is not None:  # one launch attempt per job; close() frees what it took
                raise self.start_error
            try:
                if self.deadline - time.monotonic() < _MIN_START:
                    raise TimeoutError("browser budget exhausted")
                path = check_available()
                self.has_slot = _SLOTS.acquire(blocking=False)
                if not self.has_slot:
                    raise TimeoutError("browser capacity unavailable")
                from playwright.async_api import async_playwright
                self.proxy = _PublicProxy(self.public_ip, self.deadline)
                proxy = self.proxy.__enter__()
                self.pw = await async_playwright().start()
                self.browser = await self.pw.chromium.launch(
                    executable_path=path, headless=True, timeout=self.left(self.deadline),
                    chromium_sandbox=True, proxy={"server": proxy},
                    args=["--proxy-bypass-list=<-loopback>",
                          "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])
            except Exception as e:
                self.start_error = e
                raise

    @staticmethod
    def left(deadline):
        remaining = deadline - time.monotonic()
        if remaining < 0.5:
            raise TimeoutError("browser budget exhausted")
        return int((remaining - 0.2) * 1000)

    async def render(self, url):
        stage = "validate"
        failure = lambda status: {"html": "", "status": status, "stage": stage}
        task = asyncio.current_task()
        self.tasks.add(task)
        acquired = False
        try:
            parsed = urllib.parse.urlsplit(url if "://" in url else "https://" + url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                return failure("blocked")
            url = parsed.geturl()
            stage = "capacity"
            remaining = self.deadline - time.monotonic()
            if remaining < 0.5:
                return failure("budget_exhausted")
            await asyncio.wait_for(self.pages.acquire(), remaining)
            acquired = True
            stage = "browser_start"
            await self.start()
            deadline = min(self.deadline, time.monotonic() + 20)
            left = lambda: self.left(deadline)
            from playwright.async_api import TimeoutError as PlaywrightTimeout
            context = await self.browser.new_context(service_workers="block", accept_downloads=False)
            try:
                async def route_request(route):
                    req = route.request
                    if (req.resource_type in ("image", "media", "font") or req.method not in ("GET", "HEAD")
                            or urllib.parse.urlsplit(req.url).scheme not in ("http", "https")):
                        await route.abort()
                    else:
                        await route.continue_()
                await context.route("**/*", route_request)
                page = await context.new_page()
                stage = "navigation"
                response = await page.goto(url, wait_until="commit", timeout=min(10000, left()))
                if response and response.status >= 400:
                    return failure("blocked" if response.status in (401, 403, 429) else "error")
                stage = "rendering"
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=min(3000, left()))
                except PlaywrightTimeout:
                    pass  # slow third-party scripts must not discard an already usable page
                try:
                    await page.wait_for_function(r"""() => document.body && (
                        document.querySelector(`a[href^="mailto:"], a[href*="instagram.com/"],
                        a[href*="facebook.com/"], a[href*="linkedin.com/"], a[href*="tiktok.com/"],
                        a[href*="youtube.com/"], a[href*="twitter.com/"], a[href*="x.com/"]`) ||
                        /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i.test(document.body.innerText))""",
                        timeout=min(2000, left()))
                except PlaywrightTimeout:
                    pass  # not every business publishes contact details
                stage = "read_dom"
                title = (await page.title()).lower()
                if any(t in title for t in ("just a moment", "access denied", "verify you are human", "captcha")):
                    return failure("blocked")
                return {"html": (await page.content())[:400_000], "status": "rendered", "stage": "complete"}
            finally:
                await context.close()
        except ValueError:
            return failure("blocked")
        except Exception as e:
            # Never log exception text: it may contain URLs, credentials or page content.
            return failure("timeout" if "timeout" in type(e).__name__.lower() else "error")
        finally:
            if acquired:
                self.pages.release()
            self.tasks.discard(task)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.proxy is not None:
            # Nothing is served while the browser closes; its relays would otherwise run on
            # until the deadline.
            self.proxy.__exit__()
        try:
            if self.browser is not None:
                # Chrome's graceful close can hang for minutes after many pages; pw.stop() below
                # ends the driver, which ends the browser it launched.
                await asyncio.wait_for(self.browser.close(), _CLOSE_SECONDS)
        except Exception:
            pass
        finally:
            try:
                if self.pw is not None:
                    await asyncio.wait_for(self.pw.stop(), _CLOSE_SECONDS)
            except Exception:
                pass
            finally:
                if self.has_slot:
                    _SLOTS.release()


def render(url, public_ip, timeout=20):
    """Standalone diagnostic render; enrichment uses one Budget across the whole job."""
    with Budget(timeout) as budget:
        return budget.fetch(url, public_ip)
