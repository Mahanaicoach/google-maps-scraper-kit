#!/usr/bin/env python3
"""Offline checks for the engine spike: websocket framing against a fake server, CDP routing,
place-link parsing and the APP_INITIALIZATION_STATE parser on a synthetic page. No network, no Chrome.

    python3 engine/test_cdp.py
"""
import base64, hashlib, json, os, queue, socket, struct, sys, threading, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp, details, maps  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_frame(conn):
    """(opcode, payload) of one client frame, unmasked."""
    b0, b1 = conn.recv(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", conn.recv(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", conn.recv(8))[0]
    mask = conn.recv(4)
    data = b""
    while len(data) < n:
        data += conn.recv(n - len(data))
    return b0 & 0x0F, bytes(b ^ mask[i & 3] for i, b in enumerate(data))


def _send_frame(conn, opcode, payload, fin=True):
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    conn.sendall(head + payload)


class FakeDevTools(threading.Thread):
    """Answers the handshake, then: echoes CDP requests as results, emits one event, pings once,
    and sends one big and one fragmented message when asked."""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.start()

    def run(self):
        conn, _ = self.srv.accept()
        self.srv.close()
        with conn:
            self.serve(conn)

    def serve(self, conn):
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        key = [l.split(":", 1)[1].strip() for l in head.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        while True:
            op, data = _recv_frame(conn)
            if op == 0x8:
                return
            if op == 0xA:
                continue
            msg = json.loads(data)
            if msg["method"] == "Test.big":
                _send_frame(conn, 0x1, json.dumps({"id": msg["id"], "result": {"s": "x" * 70000}}).encode())
            elif msg["method"] == "Test.fragments":
                body = json.dumps({"id": msg["id"], "result": {"ok": True}}).encode()
                _send_frame(conn, 0x1, body[:5], fin=False)
                _send_frame(conn, 0x0, body[5:], fin=True)
            elif msg["method"] == "Test.event":
                _send_frame(conn, 0x9, b"ping")
                _send_frame(conn, 0x1, json.dumps({"method": "Page.loadEventFired", "sessionId": "S1",
                                                   "params": {"timestamp": 1}}).encode())
                _send_frame(conn, 0x1, json.dumps({"id": msg["id"], "result": {}}).encode())
            elif msg["method"] == "Test.die":  # Chrome crashed: the socket closes without a reply
                return
            elif msg["method"] == "Test.error":
                _send_frame(conn, 0x1, json.dumps({"id": msg["id"], "error": {"message": "nope"}}).encode())
            else:
                _send_frame(conn, 0x1, json.dumps({"id": msg["id"], "result": {"echo": msg}}).encode())


class WebSocketAndCDP(unittest.TestCase):
    def setUp(self):
        self.srv = FakeDevTools()
        self.c = cdp.CDP(f"ws://127.0.0.1:{self.srv.port}/devtools/browser/x", timeout=5)

    def tearDown(self):
        self.c.close()

    def test_roundtrip_masked_and_routed(self):
        r = self.c.send("Target.createTarget", {"url": "about:blank"}, session_id="S1")
        self.assertEqual(r["echo"]["method"], "Target.createTarget")
        self.assertEqual(r["echo"]["sessionId"], "S1")
        self.assertEqual(r["echo"]["params"], {"url": "about:blank"})

    def test_large_frame_64bit_length(self):
        self.assertEqual(len(self.c.send("Test.big")["s"]), 70000)

    def test_fragments_joined(self):
        self.assertTrue(self.c.send("Test.fragments")["ok"])

    def test_event_and_ping(self):
        q = self.c.subscribe("Page.loadEventFired", "S1")
        self.c.send("Test.event")
        self.assertEqual(self.c.wait_event("Page.loadEventFired", "S1", timeout=5, q=q), {"timestamp": 1})

    def test_dead_connection_wakes_every_waiter(self):
        q = self.c.subscribe("Page.loadEventFired", "S1")
        with self.assertRaises(cdp.CDPError):
            self.c.send("Test.die")
        t = time.time()
        with self.assertRaisesRegex(cdp.CDPError, "connection lost"):
            self.c.wait_event("Page.loadEventFired", "S1", timeout=5, q=q)
        with self.assertRaisesRegex(cdp.CDPError, "connection lost"):
            self.c.wait_event("Page.frameNavigated", "S1", timeout=5)  # subscribed after the death
        self.assertLess(time.time() - t, 2)

    def test_broken_socket_is_a_cdp_error(self):
        self.c.ws.sock.close()  # as if Chrome died a moment before the reader noticed
        with self.assertRaisesRegex(cdp.CDPError, "connection lost"):
            self.c.send("X.y")
        self.assertEqual(self.c._pending, {})

    def test_wait_for(self):
        class Tab(cdp.Page):
            def __init__(self, answers):
                self.cdp, self.answers = self.c, list(answers)
            def eval(self, expression, timeout=None):
                a = self.answers.pop(0) if self.answers else None
                if isinstance(a, Exception):
                    raise a
                return a
        Tab.c = self.c
        gone = cdp.CDPError("Execution context was destroyed")
        self.assertEqual(Tab([gone, None, 7]).wait_for("x", timeout=2, every=0.01), 7)  # through a navigation
        with self.assertRaisesRegex(cdp.CDPError, "Session with given id not found"):
            Tab([cdp.CDPError("Session with given id not found")] * 500).wait_for("x", timeout=0.2, every=0.01)
        self.assertIsNone(Tab([None] * 500).wait_for("x", timeout=0.2, every=0.01))

    def test_quiet_browser_keeps_the_reader(self):
        c = cdp.CDP(f"ws://127.0.0.1:{FakeDevTools().port}/devtools/browser/y", timeout=0.5)
        time.sleep(1)  # longer than the command timeout with no frame at all
        self.assertIsNone(c.closed)
        self.assertEqual(c.send("X.y")["echo"]["method"], "X.y")
        c.close()

    def test_error_reply_raises(self):
        with self.assertRaises(cdp.CDPError):
            self.c.send("Test.error")

    def test_bad_handshake(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        def refuse():
            with s, s.accept()[0] as c:
                c.recv(4096)  # closing with the request unread makes Windows reset the connection
                c.sendall(b"HTTP/1.1 404 No\r\n\r\n")
        threading.Thread(target=refuse, daemon=True).start()
        with self.assertRaises(cdp.WebSocketError):
            cdp.WebSocket(f"ws://127.0.0.1:{s.getsockname()[1]}/x", timeout=5)


class Links(unittest.TestCase):
    def test_search_url(self):
        self.assertEqual(maps.search_url("coffee shops in Austin TX", 30.2672, -97.7431),
                         "https://www.google.com/maps/search/coffee%20shops%20in%20Austin%20TX/@30.2672,-97.7431,15z?hl=en")
        self.assertEqual(maps.search_url("x", lang="de"), "https://www.google.com/maps/search/x?hl=de")

    def test_parse_link(self):
        href = ("https://www.google.com/maps/place/Example+Caf%C3%A9/data=!4m7!3m6!1s0x8644b5a3c1234567:0x9abcdef012345678"
                "!8m2!3d30.2671!4d-97.7430!16s%2Fg%2F11abc!19sChIJabcDEF123456789-_")
        p = maps.parse_link(href)
        self.assertEqual(p["title"], "Example Café")
        self.assertEqual(p["data_id"], "0x8644b5a3c1234567:0x9abcdef012345678")
        self.assertEqual((p["latitude"], p["longitude"]), ("30.2671", "-97.7430"))
        self.assertEqual(p["place_id"], "ChIJabcDEF123456789-_")
        self.assertEqual(maps.parse_link(href, "Label wins")["title"], "Label wins")


def entity(**over):
    """A synthetic place entity with the fields at the positions details.FIELDS expects."""
    e = [None] * 210
    e[11] = "Example Café"
    e[13] = ["Coffee shop", "Cafe"]
    e[2] = ["1 Main St", "Austin, TX 78701"]
    e[18] = "1 Main St, Austin, TX 78701"
    e[7] = ["https://example.com/"]
    e[178] = [["+1 512-555-0100"]]
    e[4] = [None, None, "$$"] + [None] * 4 + [4.6]
    e[37] = [None, 321]
    e[9] = [None, None, 30.2671, -97.743]
    e[78] = "ChIJabc"
    e[10] = "0x1:0xff"
    e[30] = "America/Chicago"
    for k, v in over.items():
        e[int(k[1:])] = v
    return e


class Parser(unittest.TestCase):
    def test_fields_resolve(self):
        got = details.parse_entity(entity())
        self.assertEqual(got["title"], "Example Café")
        self.assertEqual(got["category"], "Coffee shop")
        self.assertEqual(got["categories"], "Coffee shop | Cafe")
        self.assertEqual(got["address_lines"], "1 Main St | Austin, TX 78701")
        self.assertEqual(got["phone"], "+1 512-555-0100")
        self.assertEqual(got["website"], "https://example.com/")
        self.assertEqual(got["review_rating"], 4.6)
        self.assertNotIn("review_count", got)  # (37, 1) is not the count: it comes from the card labels
        self.assertEqual((got["latitude"], got["longitude"]), (30.2671, -97.743))
        self.assertEqual(got["place_id"], "ChIJabc")
        self.assertEqual(got["cid"], "255")
        self.assertEqual(got["price_range"], "$$")
        self.assertNotIn("plus_code", got)
        self.assertNotIn("phone", details.parse_entity(entity(i178=None)))

    def test_batch(self):
        data = [None] * 64 + [[[None, entity()], [None, entity(i11="Other")], ["not an entry"], [None, [None] * 20]]]
        rows = details.parse_batch(")]}'\n" + json.dumps(data))
        self.assertEqual([r["title"] for r in rows], ["Example Café", "Other"])
        self.assertEqual(details.parse_batch(json.dumps(data)), rows)  # prefix optional
        self.assertEqual(details.parse_batch("not json"), [])
        self.assertEqual(details.parse_batch("[1,2,3]"), [])

    def test_batch_url(self):
        self.assertTrue(maps.BATCH_URL.match("https://www.google.com/search?tbm=map&authuser=0&hl=en&pb=!4m12"))
        self.assertTrue(maps.BATCH_URL.match("https://www.google.co.uk/search?q=x&tbm=map"))
        self.assertFalse(maps.BATCH_URL.match("https://www.google.com/maps/vt/pb=!1m4"))
        self.assertFalse(maps.BATCH_URL.match("https://www.google.com/search?q=plain"))


class PageUserAgent(unittest.TestCase):
    def test_user_agent_applied_to_every_context_before_navigation(self):
        class Connection:
            user_agent = "Mozilla/5.0 Chrome/156.0.0.0"
            def __init__(self):
                self.calls = []
            def send(self, method, params=None, session_id=None, timeout=None):
                self.calls.append((method, params, session_id))
                return {"targetId": "T", "sessionId": "S"}
        conn = Connection()
        for context in (None, "proxy-context", "reviews-context"):
            conn.calls.clear()
            cdp.Page(conn, context=context)
            self.assertEqual(conn.calls[-1], ("Emulation.setUserAgentOverride",
                             {"userAgent": conn.user_agent}, "S"))
            self.assertEqual(conn.calls[0][1].get("browserContextId"), context)
        conn.calls.clear()
        conn.user_agent = None
        cdp.Page(conn)
        self.assertNotIn("Emulation.setUserAgentOverride", [c[0] for c in conn.calls])

    def test_a_failed_setup_closes_the_tab(self):
        from unittest.mock import Mock
        conn = Mock(user_agent="Mozilla/5.0 Chrome/156.0.0.0", closed=None)
        def send(method, params=None, session_id=None, timeout=None):
            if method == "Emulation.setUserAgentOverride":
                raise cdp.CDPError("refused")
            return {"targetId": "T", "sessionId": "S"}
        conn.send.side_effect = send
        with self.assertRaises(cdp.CDPError):
            cdp.Page(conn)
        self.assertEqual(conn.send.call_args.args, ("Target.closeTarget", {"targetId": "T"}))

    def test_initial_url_loads_after_tab_configuration(self):
        from unittest.mock import Mock
        conn = Mock(user_agent="Mozilla/5.0 Chrome/156.0.0.0")
        conn.send.return_value = {"targetId": "T", "sessionId": "S"}
        conn.wait_event.return_value = {}
        cdp.Page(conn, url="https://example.com", context="proxy-context")
        calls = conn.send.call_args_list
        self.assertEqual(calls[0].args, ("Target.createTarget",
                         {"url": "about:blank", "browserContextId": "proxy-context"}))
        methods = [c.args[0] for c in calls]
        self.assertLess(methods.index("Emulation.setUserAgentOverride"), methods.index("Page.navigate"))
        self.assertEqual(calls[-1].args[:2], ("Page.navigate", {"url": "https://example.com"}))


class FakeCDP:
    def __init__(self):
        self.closed, self.q = None, queue.Queue()

    def subscribe(self, method, session_id=None):
        return self.q

    def unsubscribe(self, *a):
        pass


class FakeMapsPage:
    """Just enough of cdp.Page for maps.collect: batches arrive as responseReceived events and
    getResponseBody replies; `fail_eval` makes Runtime.evaluate of that script raise."""

    def __init__(self, bodies, fail_eval=None):
        self.cdp, self.sid, self.bodies, self.fail_eval, self.body_calls = FakeCDP(), "S", bodies, fail_eval, {}
        for rid in bodies:
            self.cdp.q.put({"requestId": rid, "response": {
                "url": "https://www.google.com/search?tbm=map&pb=x", "mimeType": "application/json"}})

    def set_cookie(self, *a):
        pass

    def navigate(self, url):
        pass

    def url(self):
        return "https://www.google.com/maps/search/x"

    def eval(self, expr):
        if expr == self.fail_eval:
            raise cdp.CDPError("Runtime.evaluate: no reply within 30 s")
        return [] if expr == maps.JS_LINKS else 1

    def wait_for(self, expr, timeout=15):
        return True

    def send(self, method, params=None):
        rid = params["requestId"]
        self.body_calls[rid] = self.body_calls.get(rid, 0) + 1
        if self.bodies[rid] is None:
            raise cdp.CDPError("No resource with given identifier found")
        return {"body": self.bodies[rid]}


def batch(*titles):
    return ")]}'\n" + json.dumps([None] * 64 + [[[None, entity(i11=t, i10=f"0x1:0x{i:x}")] for i, t in enumerate(titles, 1)]])


class Collect(unittest.TestCase):
    def test_error_mid_scroll_keeps_places(self):
        page = FakeMapsPage({"r1": batch("A", "B")}, fail_eval=maps.JS_END)
        places, reason, _ = maps.collect(page, "u", depth=3, log=lambda m: None)
        self.assertEqual([p["title"] for p in places], ["A", "B"])
        self.assertTrue(reason.startswith("error: "))

    def test_error_before_any_place_raises(self):
        page = FakeMapsPage({"r1": None}, fail_eval=maps.JS_END)
        with self.assertRaises(cdp.CDPError):
            maps.collect(page, "u", depth=3, log=lambda m: None, max_seconds=20)

    def test_unreadable_batch_is_dropped(self):
        b = maps.Batches(FakeMapsPage({"good": batch("A"), "gone": None}))
        rows = [r for _ in range(40) for r in b.drain()]
        self.assertEqual([r["title"] for r in rows], ["A"])
        self.assertEqual(b.pending, {})
        self.assertLessEqual(b.page.body_calls["gone"], 27)


if __name__ == "__main__":
    unittest.main(verbosity=1)
