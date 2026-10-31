#!/usr/bin/env python3
"""Offline checks for the engine spike: websocket framing against a fake server, CDP routing,
place-link parsing and the APP_INITIALIZATION_STATE parser on a synthetic page. No network, no Chrome.

    python3 engine/test_cdp.py
"""
import base64, hashlib, json, os, socket, struct, sys, threading, unittest

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

    def test_error_reply_raises(self):
        with self.assertRaises(cdp.CDPError):
            self.c.send("Test.error")

    def test_bad_handshake(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        def refuse():
            with s, s.accept()[0] as c:
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
