"""Chrome DevTools Protocol over a standard-library websocket client.

    cdp = CDP("ws://127.0.0.1:9222/devtools/browser/<id>")
    target = cdp.send("Target.createTarget", {"url": "about:blank"})["targetId"]
    sid = cdp.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
    cdp.send("Page.navigate", {"url": "https://example.com"}, sid)
    cdp.wait_event("Page.loadEventFired", sid)

One reader thread parses frames and routes replies to the caller that sent them (by id) and events
to a queue. Nothing here is Google-specific. Standard library only.
"""
import base64, hashlib, json, os, queue, socket, struct, threading, time, urllib.parse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(OSError):
    pass


class WebSocket:
    """RFC 6455 client for one ws:// URL: text frames in and out, fragments joined, pings answered."""

    def __init__(self, url, timeout=30):
        u = urllib.parse.urlparse(url)
        if u.scheme != "ws":
            raise WebSocketError(f"only ws:// is supported, not {u.scheme}://")
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        self.sock.settimeout(timeout)
        self._lock = threading.Lock()
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port or 80}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during the websocket handshake")
            head += chunk
        head, self._buf = head.split(b"\r\n\r\n", 1)
        status, _, rest = head.decode("latin-1").partition("\r\n")
        if " 101 " not in status:
            raise WebSocketError(f"websocket handshake refused: {status}")
        want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        accept = [v.strip() for k, _, v in (l.partition(":") for l in rest.split("\r\n"))
                  if k.strip().lower() == "sec-websocket-accept"]
        if accept != [want]:
            raise WebSocketError("websocket handshake: bad Sec-WebSocket-Accept")
        # The reader blocks on recv() for as long as the browser is quiet (a slow page, a proxy that
        # takes its time); each command has its own timeout in CDP.send instead.
        self.sock.settimeout(None)

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise WebSocketError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, opcode, payload):
        mask = os.urandom(4)  # clients must mask (RFC 6455 §5.3)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        with self._lock:
            self.sock.sendall(head + mask + masked)

    def send(self, text):
        self._frame(0x1, text.encode("utf-8"))

    def recv(self):
        """The next complete text message (fragments joined); pings answered, close raised."""
        parts, kind = [], None
        while True:
            b0, b1 = self._read(2)
            fin, opcode, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else None  # servers normally send unmasked
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if opcode == 0x8:
                raise WebSocketError("websocket closed by the server")
            if opcode == 0x9:
                self._frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                kind, parts = opcode, [data]
            elif opcode == 0x0 and kind is not None:
                parts.append(data)
            else:
                raise WebSocketError(f"unexpected websocket opcode {opcode}")
            if fin:
                msg = b"".join(parts)
                kind, parts = None, []
                return msg.decode("utf-8")

    def close(self):
        try:
            self._frame(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class CDPError(Exception):
    pass


_DEAD = object()  # put on every event queue when the connection dies


class CDP:
    """One DevTools connection. send() blocks for the reply; events reach the queues subscribed to
    (session, name) and are dropped otherwise, so a long job holds no unread events."""

    def __init__(self, ws_url, timeout=30):
        self.user_agent = None  # optional browser-wide user agent, applied to each attached tab
        self.ws = WebSocket(ws_url, timeout=timeout)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}      # id -> queue of one reply
        self._subs = {}         # (session_id, method) -> list of queues
        self.closed = None
        self._reader = threading.Thread(target=self._loop, daemon=True)
        self._reader.start()

    def _loop(self):
        try:
            while True:
                msg = json.loads(self.ws.recv())
                if "id" in msg:
                    q = self._pending.pop(msg["id"], None)
                    if q:
                        q.put(msg)
                else:
                    key = (msg.get("sessionId"), msg.get("method"))
                    for q in self._subs.get(key, ()):
                        q.put(msg.get("params", {}))
        except Exception as e:  # noqa: BLE001 - a dead connection ends every waiter with the cause
            self.closed = e
            for q in list(self._pending.values()):
                q.put({"error": {"message": f"connection lost: {e}"}})
            for qs in list(self._subs.values()):
                for q in list(qs):
                    q.put(_DEAD)

    def send(self, method, params=None, session_id=None, timeout=None):
        if self.closed:
            raise CDPError(f"connection lost: {self.closed}")
        with self._lock:
            self._id += 1
            mid = self._id
        q = queue.Queue(1)
        self._pending[mid] = q
        if self.closed:  # the reader died after the check above and will never answer
            self._pending.pop(mid, None)
            raise CDPError(f"connection lost: {self.closed}")
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        try:
            self.ws.send(json.dumps(msg))
        except OSError as e:  # Chrome died and the reader has not noticed yet: same error as a lost reply
            self._pending.pop(mid, None)
            raise CDPError(f"{method}: connection lost: {e}") from e
        try:
            reply = q.get(timeout=timeout or self.timeout)
        except queue.Empty:
            self._pending.pop(mid, None)
            raise CDPError(f"{method}: no reply within {timeout or self.timeout} s")
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    def subscribe(self, method, session_id=None, q=None):
        """A queue that receives the params of every `method` event on `session_id` from now on
        (pass q to share one queue between events)."""
        q = q or queue.Queue()
        self._subs.setdefault((session_id, method), []).append(q)
        return q

    def unsubscribe(self, method, q, session_id=None):
        subs = self._subs.get((session_id, method), [])
        if q in subs:
            subs.remove(q)

    def wait_event(self, method, session_id=None, timeout=None, q=None):
        """Block until `method` fires on `session_id` (subscribe first with q= to not miss a fast one)."""
        own = q is None
        q = q or self.subscribe(method, session_id)
        if self.closed and q.empty():
            q.put(_DEAD)
        try:
            got = q.get(timeout=timeout or self.timeout)
        except queue.Empty:
            raise CDPError(f"no {method} event within {timeout or self.timeout} s") from None
        finally:
            if own:
                self.unsubscribe(method, q, session_id)
        if got is _DEAD:
            raise CDPError(f"connection lost: {self.closed}")
        return got

    def close(self):
        self.ws.close()


class Page:
    """One attached tab: navigate, evaluate JavaScript, wait. `context` is a browser context id
    (Browser.context) for a tab with its own cookies and proxy."""

    def __init__(self, cdp, url="about:blank", context=None):
        self.cdp = cdp
        params = {"url": "about:blank"}  # configure the tab before its first network request
        if context:
            params["browserContextId"] = context
        self.target = cdp.send("Target.createTarget", params)["targetId"]
        self.sid = cdp.send("Target.attachToTarget", {"targetId": self.target, "flatten": True})["sessionId"]
        for domain in ("Page", "Runtime", "Network"):
            cdp.send(f"{domain}.enable", session_id=self.sid)
        if cdp.user_agent:
            self.send("Emulation.setUserAgentOverride", {"userAgent": cdp.user_agent})
        if url != "about:blank":
            self.navigate(url)

    def send(self, method, params=None, timeout=None):
        return self.cdp.send(method, params, self.sid, timeout)

    def set_cookie(self, name, value, domain):
        self.send("Network.setCookie", {"name": name, "value": value, "domain": domain, "path": "/",
                                        "secure": True})

    def navigate(self, url, timeout=45):
        loaded = self.cdp.subscribe("Page.loadEventFired", self.sid)
        try:
            r = self.send("Page.navigate", {"url": url}, timeout=timeout)
            if r.get("errorText"):
                raise CDPError(f"navigate {url}: {r['errorText']}")
            self.cdp.wait_event("Page.loadEventFired", self.sid, timeout, q=loaded)
        finally:
            self.cdp.unsubscribe("Page.loadEventFired", loaded, self.sid)

    def eval(self, expression, timeout=None):
        """The JSON value of a JavaScript expression (awaited if it is a promise)."""
        r = self.send("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                            "awaitPromise": True}, timeout)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise CDPError(f"JavaScript: {d.get('exception', {}).get('description') or d.get('text')}")
        return r.get("result", {}).get("value")

    def url(self):
        return self.eval("location.href")

    def wait_for(self, expression, timeout=15, every=0.25):
        """Poll a JavaScript expression until it is truthy; None if it never is. An error while the
        page navigates ("Execution context was destroyed") is retried; a dead connection is not, and
        a tab that never answered at all raises its last error rather than look like an empty page."""
        end, answered, err = time.time() + timeout, False, None
        while time.time() < end:
            try:
                v = self.eval(expression)
                answered = True
            except CDPError as e:
                if self.cdp.closed:
                    raise
                v, err = None, e
            if v:
                return v
            time.sleep(every)
        if err and not answered:
            raise err
        return None

    def close(self):
        try:
            self.cdp.send("Target.closeTarget", {"targetId": self.target})
        except (CDPError, OSError):  # a dead socket fails in send() before the reader notices
            pass
