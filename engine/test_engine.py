#!/usr/bin/env python3
"""Offline checks for the engine: job body validation, CSV rows, the sqlite store, the worker
(with a fake Chrome and fake Google results), the HTTP server and its guards, scripts/scrape.py's
own client against that server, the Chrome download's SHA-256 check and the email pass.
No network, no Chrome, no Docker.

    python3 engine/test_engine.py
"""
import csv, hashlib, http.client, importlib.util, io, json, os, queue, re, shutil, socket, sqlite3, subprocess, sys
import tempfile
import threading, time, unittest, unittest.mock, uuid, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import browser, details, jobs, maps, serve  # noqa: E402
from cdp import CDPError  # noqa: E402

GOOD = {"name": "t", "keywords": ["cafes in Austin"], "lang": "en", "lat": "30.2", "lon": "-97.7", "max_time": 60}


def place(n, **kw):
    return dict({"title": f"Biz {n}", "data_id": f"0x1:0x{n:x}", "place_id": f"ChIJ{n}", "cid": str(n),
                 "category": "Cafe", "review_rating": 4.5, "review_count": 10 + n,
                 "latitude": 30.2, "longitude": -97.7}, **kw)


class FakeCDP:
    closed = None


class FakeBrowser:
    """Stands in for browser.Browser: no process; kill() makes the next collect fail like a dead Chrome."""
    made = []

    path = "/usr/bin/google-chrome"

    def __init__(self, **kw):
        self.kw, self.cdp, self.killed, self.contexts = kw, FakeCDP(), threading.Event(), []
        FakeBrowser.made.append(self)

    def context(self, proxy=None):
        self.contexts.append(proxy)
        return f"ctx{len(self.contexts)}"

    def dispose(self, ctx):
        pass

    def kill(self):
        self.cdp.closed = OSError("killed")
        self.killed.set()

    def close(self):
        pass


class FakePage:
    def __init__(self, cdp, context=None):
        pass

    def set_cookie(self, *a):
        pass

    def close(self):
        pass


class FakeAuth:
    """Stands in for browser.ProxyAuth: records which tabs got credentials."""
    logins = []
    refused = False

    def __init__(self, page, user, password):
        FakeAuth.logins.append((user, password))

    def close(self):
        pass


RESULTS = {}  # keyword -> (places, reason) for the fake collect; a list of reasons answers one per search
SEARCHED = []  # the keywords the fake collect was asked for, in order


def fake_collect(page, url, depth=1, max_seconds=0, log=None, stop=lambda: False, on_batch=None):
    for kw, (places, reason) in RESULTS.items():
        if maps.search_url(kw).split("/@")[0].split("?")[0] in url:
            SEARCHED.append(kw)
            if isinstance(reason, list):
                reason = reason.pop(0) if len(reason) > 1 else reason[0]
            if reason == "hang":  # Chrome stops answering until the watchdog kills it
                b = FakeBrowser.made[-1]
                b.killed.wait(10)
                raise CDPError("connection lost: killed")
            if reason == "slow":  # its places stream in, then it scrolls until stopped
                if on_batch and places:
                    on_batch(places, 1)
                end = time.time() + 5
                while not stop() and time.time() < end:
                    time.sleep(0.02)
                return places, "stopped", {"batches": 1, "dom_links": 0}
            return places, reason, {"batches": 1, "dom_links": 0}
    return [], "no-feed", {"batches": 0, "dom_links": 0}


class Fakes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (browser.Browser, jobs.Page, maps.collect, browser.ProxyAuth)
        browser.Browser, jobs.Page, maps.collect, browser.ProxyAuth = FakeBrowser, FakePage, fake_collect, FakeAuth
        FakeBrowser.made.clear()
        FakeAuth.logins.clear()
        RESULTS.clear()
        SEARCHED.clear()
        self.store = jobs.Store(os.path.join(self.dir, "data"))
        self.lines = []
        self.worker = jobs.Worker(self.store, self.lines.append)

    def tearDown(self):
        browser.Browser, jobs.Page, maps.collect, browser.ProxyAuth = self.saved
        self.worker.shutdown()
        if self.worker.is_alive():
            self.worker.join(5)
        self.store.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_one(self, **body):
        name, data = jobs.validate(dict(GOOD, **body))
        jid = self.store.create(name, data)
        self.worker.run_job(self.store.claim())
        return jid, self.store.get(jid)

    def rows(self, jid):
        return list(csv.DictReader(io.StringIO(self.store.read_csv(jid).decode("utf-8"))))


class Validate(unittest.TestCase):
    def err(self, **body):
        with self.assertRaises(ValueError) as c:
            jobs.validate(body)
        return str(c.exception)

    def test_old_engine_messages(self):
        self.assertEqual(self.err(), "missing name")
        self.assertEqual(self.err(name="x"), "missing keywords")
        self.assertEqual(self.err(name="x", keywords=["a"]), "missing lang")
        self.assertEqual(self.err(name="x", keywords=["a"], lang="eng"), "invalid lang")
        self.assertEqual(self.err(name="x", keywords=["a"], lang="en\n"), "invalid lang")
        self.assertEqual(self.err(name="x", keywords=["a"], lang="en"), "missing geo coordinates")
        self.assertEqual(self.err(name="x", keywords=["a"], lang="en", lat="1", lon="2"), "missing max time")

    def test_bad_values(self):
        self.assertIn("invalid geo", self.err(**dict(GOOD, lat="north")))
        self.assertIn("invalid geo", self.err(**dict(GOOD, lat="91")))
        self.assertIn("invalid geo", self.err(**dict(GOOD, lon="nan")))
        self.assertIn("invalid geo", self.err(**dict(GOOD, lat=True)))
        self.assertIn("invalid depth", self.err(**dict(GOOD, depth=0)))
        self.assertIn("invalid depth", self.err(**dict(GOOD, depth="5")))
        self.assertIn("invalid max time", self.err(**dict(GOOD, max_time=-1)))
        self.assertIn("invalid keywords", self.err(**dict(GOOD, keywords=["ok", ""])))
        self.assertIn("SOCKS5", self.err(**dict(GOOD, proxies=["socks5://u:p@h:1"])))
        self.assertIn("invalid country", self.err(**dict(GOOD, country="usa")))
        self.assertIn("invalid reviews_max", self.err(**dict(GOOD, reviews_max=6)))
        self.assertIn("invalid proxy", self.err(**dict(GOOD, proxies=["ftp://h:1"])))
        self.assertIn("invalid proxy", self.err(**dict(GOOD, proxies=["http://h:99999"])))
        with self.assertRaisesRegex(ValueError, "JSON object"):
            jobs.validate([1])

    def test_defaults_and_numbers(self):
        name, d = jobs.validate(dict(GOOD, lat=30.5, lon=-97, proxies=["socks5://127.0.0.1:1080"]))
        self.assertEqual((name, d["lat"], d["lon"], d["depth"], d["zoom"], d["email"]), ("t", "30.5", "-97", 1, 15, False))
        self.assertEqual(d["proxies"], ["socks5://127.0.0.1:1080"])
        self.assertEqual((d["country"], d["reviews_max"], d["socials"], d["verify_emails"]), ("", 0, False, False))
        d = jobs.validate(dict(GOOD, country="DE", proxies=["http://u%40x:p%3A@h:1"]))[1]
        self.assertEqual((d["country"], jobs.locale(d), d["proxies"]), ("de", "en-DE", ["http://u%40x:p%3A@h:1"]))
        self.assertEqual(jobs.mask("http://u%40x:p%3A@h:1"), "http://u%40x:***@h:1")
        self.assertEqual(jobs.mask("socks5://h:1"), "socks5://h:1")
        self.assertEqual(jobs.validate(dict(GOOD, radius=5_000_000))[1]["radius"], 5_000_000)


class Rows(unittest.TestCase):
    def test_columns_and_mapping(self):
        r = jobs.to_row(dict(place(1), open_state="Open 24 hours", description="Good coffee",
                             categories="Cafe | Bakery", junk="x"), "cafes")
        self.assertEqual(len(jobs.COLUMNS), 36)
        self.assertEqual(list(r), jobs.COLUMNS + jobs.EXTRA)
        self.assertEqual((r["input_id"], r["status"], r["descriptions"], r["categories"]),
                         ("cafes", "Open 24 hours", "Good coffee", "Cafe | Bakery"))
        self.assertEqual((r["review_rating"], r["review_count"], r["emails"]), ("4.5", "11", ""))
        self.assertTrue(r["link"].startswith("https://www.google.com/maps/search/?api=1&query=Biz%201&query_place_id=ChIJ1"))
        dom = jobs.to_row({"title": "A", "link": "https://www.google.com/maps/place/A/data=!1s0x1:0x2"}, "k")
        self.assertEqual(dom["link"], "https://www.google.com/maps/place/A/data=!1s0x1:0x2")

    def test_csv_utf8_no_bom(self):
        raw = jobs.to_csv([jobs.to_row(place(1, title="Café ☕"), "k")])
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(next(csv.DictReader(io.StringIO(raw.decode())))["title"], "Café ☕")


class Store(Fakes):
    def test_lifecycle(self):
        a = self.store.create("a", {"x": 1})
        b = self.store.create("b", {"x": 2})
        self.assertEqual([j["ID"] for j in self.store.all()], [b, a])  # newest first
        self.assertEqual(self.store.claim()["ID"], a)  # oldest pending first
        self.assertEqual(self.store.get(a)["Status"], "working")
        self.assertEqual(self.store.recover(), (1, 0))  # resumed on the next start
        self.assertEqual((self.store.get(a)["Status"], self.store.get(a)["Progress"]["resumes"]), ("pending", 1))
        self.assertEqual(self.store.append(b, [], jobs.fields({})), len(",".join(jobs.COLUMNS + jobs.EXTRA)) + 2)
        self.assertTrue(self.store.read_csv(b).startswith(b"input_id,link,title"))
        self.assertTrue(self.store.delete(b))
        self.assertIsNone(self.store.read_csv(b))
        self.assertIsNone(self.store.append(b, [], jobs.fields({})))  # a deleted job gets no file back
        self.assertFalse(self.store.finish(b, [], jobs.fields({}), "ok", "", {}))
        self.assertFalse(self.store.delete(b))
        self.assertEqual(set(self.store.get(a)), {"ID", "Name", "Date", "Status", "Data", "Error", "Progress"})
        self.assertRegex(self.store.get(a)["Date"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")

    def test_import_kit1(self):
        # Kit 1.x's data folder as its engine leaves it: jobs.db in WAL mode, rows still in the WAL
        # (the writer is open), <id>.csv beside it.
        old = os.path.join(self.dir, "kit1")
        os.makedirs(old)
        w = sqlite3.connect(os.path.join(old, "jobs.db"))
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, "
                  "data TEXT NOT NULL, created_at INT NOT NULL, updated_at INT NOT NULL)")
        done, stuck, nocsv, baddate, big = (str(uuid.uuid4()) for _ in range(5))
        body = json.dumps({"keywords": ["cafe", "bar"], "lang": "en", "max_time": 180 * 10 ** 9})
        with w:
            for jid, status in ((done, "ok"), (stuck, "working"), (nocsv, "ok"), ("../evil", "ok"), (big, "ok")):
                w.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, 1793466309, 1793466322)", (jid, "old " + status, status, body))
            w.execute("INSERT INTO jobs VALUES (?, 'odd', 'ok', ?, 'not a time', 0)", (baddate, body))
        for jid in (done, stuck, "../evil", baddate):
            with open(os.path.join(old, f"{jid}.csv"), "w", newline="") as f:
                f.write("input_id,title\n1,\"A\nB\"\n2,C\n")
        with open(os.path.join(old, f"{big}.csv"), "w", newline="") as f:
            f.write("input_id,descriptions\n1," + "x" * 200_000 + "\n")  # past csv's 128 KB field limit
        logged = []
        try:
            self.assertEqual(self.store.import_kit1(old, logged.append), 3)
        finally:
            w.close()
        self.assertEqual(logged, [f"kit 1.x job {baddate} not imported: ValueError"])  # skipped, the rest kept
        self.assertIsNone(self.store.get(baddate))
        self.assertFalse(os.path.exists(self.store.csv_path(baddate)))
        self.assertEqual(jobs.public(self.store.get(big))["Progress"]["places"], 1)
        a, b = self.store.get(done), self.store.get(stuck)
        self.assertEqual((a["Status"], a["Name"], a["Date"], a["Data"]["max_time"]), ("ok", "old ok", "2026-10-31T17:05:09+00:00", 180))
        self.assertEqual(jobs.public(a)["Progress"], {"keywords_done": 2, "keywords_total": 2, "places": 2, "percent": 100, "stage": "done"})
        self.assertEqual((b["Status"], b["Error"]), ("failed", "kit 1.x job that was working there; its rows are kept"))
        self.assertEqual(self.store.read_csv(done), b"input_id,title\n1,\"A\nB\"\n2,C\n")
        self.assertIsNone(self.store.get(nocsv))
        self.assertIsNone(self.store.get("../evil"))  # not a job id: skipped
        self.store.delete(done)
        self.assertEqual(self.store.import_kit1(old), 0)  # once only: a deleted job stays deleted
        self.assertIsNone(self.store.get(done))
        self.assertEqual(self.store.import_kit1(os.path.join(self.dir, "none")), 0)


class Worker(Fakes):
    def test_keywords_dedupe_into_one_csv(self):
        RESULTS["cafes"] = ([place(1), place(2)], "end-of-list")
        RESULTS["bakeries"] = ([place(2), place(3)], "depth")
        jid, job = self.run_one(keywords=["cafes", "bakeries"], depth=2, lang="de", proxies=["http://10.0.0.1:3128"])
        self.assertEqual((job["Status"], job["Error"]), ("ok", ""))
        rows = self.rows(jid)
        self.assertEqual([r["title"] for r in rows], ["Biz 1", "Biz 2", "Biz 3"])
        self.assertEqual([r["input_id"] for r in rows], ["cafes", "cafes", "bakeries"])
        self.assertEqual(FakeBrowser.made[0].kw, {"lang": "de", "headless": True})
        self.assertEqual(FakeBrowser.made[0].contexts, ["http://10.0.0.1:3128"] * 2)
        self.assertFalse(any(re.search(r"Biz \d", l) for l in self.lines))  # counts only in the log

    def test_no_results_is_ok_with_header(self):
        jid, job = self.run_one(keywords=["nothing here"])
        self.assertEqual(job["Status"], "ok")
        self.assertEqual(self.rows(jid), [])

    def test_no_chrome_fails_with_reason(self):
        def no_chrome(**kw):
            raise browser.BrowserError("No Chrome, Chromium, Edge or Brave found.")
        browser.Browser = no_chrome
        _, job = self.run_one()
        self.assertEqual(job["Status"], "failed")
        self.assertIn("No Chrome", job["Error"])

    def test_no_feed_is_tried_once_more(self):
        RESULTS["cafes in Austin"] = ([place(1)], ["no-feed", "end-of-list"])  # once, then the list
        _, job = self.run_one()
        self.assertEqual((job["Status"], SEARCHED.count("cafes in Austin"), job["Error"]), ("ok", 2, ""))
        self.assertEqual(job["Progress"]["places"], 1)
        SEARCHED.clear()
        RESULTS["cafes in Austin"] = ([], "no-feed")  # twice: a note, no third look
        _, job = self.run_one()
        self.assertEqual((job["Status"], SEARCHED.count("cafes in Austin")), ("ok", 2))
        self.assertIn("no result list twice", job["Error"])

    def test_blocked_fails(self):
        RESULTS["cafes in Austin"] = ([], "blocked")
        _, job = self.run_one()
        self.assertEqual(job["Status"], "failed")
        self.assertIn("unusual-traffic", job["Error"])

    def test_block_stops_the_job(self):
        RESULTS["cafes"] = ([place(1)], "end-of-list")
        RESULTS["bakeries"] = ([], "blocked")
        RESULTS["florists"] = ([place(9)], "end-of-list")
        jid, job = self.run_one(keywords=["cafes", "bakeries", "florists"])
        self.assertEqual(job["Status"], "ok")  # the rows from before the block are kept
        self.assertIn("unusual-traffic", job["Error"])
        self.assertEqual([r["title"] for r in self.rows(jid)], ["Biz 1"])  # florists never searched

    def test_deleted_before_it_starts(self):
        name, data = jobs.validate(GOOD)
        jid = self.store.create(name, data)
        job = self.store.claim()
        self.store.delete(jid)
        self.worker.run_job(job)
        self.assertEqual(FakeBrowser.made, [])  # no Chrome for a job that is gone

    def test_worker_survives_a_store_error(self):
        calls = []
        def claim():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk full")
            return None
        self.store.claim = claim
        self.worker.wake.set()
        self.worker.start()
        deadline = time.time() + 5
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.05)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(self.worker.is_alive())

    def test_email_pass_stops_when_asked(self):
        rows = [{"website": f"https://s{i}.com", "emails": ""} for i in range(3)]
        t = time.monotonic()
        jobs.add_site_data(rows, 30, get=lambda url: time.sleep(5) or "", stop=lambda: time.monotonic() - t > 0.3)
        self.assertLess(time.monotonic() - t, 3)

    def test_watchdog_kills_a_hung_chrome(self):
        RESULTS["cafes in Austin"] = ([], "hang")
        saved, jobs.WATCHDOG_GRACE = jobs.WATCHDOG_GRACE, 0.2
        try:
            t = time.monotonic()
            _, job = self.run_one(max_time=1)
        finally:
            jobs.WATCHDOG_GRACE = saved
        self.assertLess(time.monotonic() - t, 5)
        self.assertTrue(FakeBrowser.made[0].killed.is_set())
        self.assertEqual(job["Status"], "failed")

    def test_delete_while_running(self):
        RESULTS["cafes in Austin"] = ([place(1)], "slow")
        name, data = jobs.validate(GOOD)
        jid = self.store.create(name, data)
        t = threading.Thread(target=self.worker.run_job, args=(self.store.claim(),))
        t.start()
        time.sleep(0.2)
        self.worker.cancel(jid)
        self.store.delete(jid)
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertIsNone(self.store.get(jid))
        self.assertFalse(os.path.exists(self.store.csv_path(jid)))

    def test_email_pass(self):
        RESULTS["cafes in Austin"] = ([place(1, website="https://www.biz1.com"), place(2), place(3, website="x.org")], "end-of-list")
        pages = {"https://www.biz1.com": 'mail <a href="mailto:hello@biz1.com">us</a> or sales&#64;biz1.com',
                 "x.org": "logo@2x.png nothing"}
        saved = jobs.fetch
        jobs.fetch = lambda url, timeout=0: pages.get(url, "")
        try:
            jid, job = self.run_one(email=True)
        finally:
            jobs.fetch = saved
        self.assertEqual(job["Status"], "ok")
        self.assertEqual([r["emails"] for r in self.rows(jid)], ["hello@biz1.com, sales@biz1.com", "", ""])
        self.assertNotIn("instagram", self.rows(jid)[0])  # columns only for the passes a job asked for


class Emails(unittest.TestCase):
    def test_find_emails(self):
        text = "a@gmail.com x img@2x.png user@example.com info@shop.com sentry@o1.sentry.io info@shop.com"
        self.assertEqual(jobs.find_emails(text, "shop.com"), ["info@shop.com", "a@gmail.com"])

    def test_public_only(self):
        self.assertFalse(jobs._public_ip("127.0.0.1"))
        self.assertFalse(jobs._public_ip("192.168.1.1"))
        self.assertFalse(jobs._public_ip("169.254.169.254"))
        self.assertFalse(jobs._public_ip("::1"))
        self.assertTrue(jobs._public_ip("8.8.8.8"))
        self.assertFalse(jobs._public_host("localhost"))
        self.assertEqual(jobs.fetch("http://127.0.0.1:9/"), "")
        self.assertEqual(jobs.site_host("https://www.Shop.com/x"), "shop.com")

    def test_private_peer_and_redirect_refused(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        with self.assertRaisesRegex(OSError, "non-public"):
            jobs._public_connection(srv.getsockname())
        srv.close()
        import urllib.request
        req = urllib.request.Request("https://example.com/")
        self.assertIsNone(jobs._Redirects().redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1:8080/api/v1/jobs"))
        self.assertIsNone(jobs._Redirects().redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/"))


class Server(Fakes):
    key = ""

    def setUp(self):
        super().setUp()
        self.servers = serve.make_servers(0, self.store, self.worker, self.key)
        self.port = self.servers[0].server_address[1]
        for s in self.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        super().tearDown()

    def call(self, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"localhost:{self.port}"}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, data, h)
        r = c.getresponse()
        out = (r.status, r.read(), r)
        c.close()
        return out


class Api(Server):
    def test_flow(self):
        code, body, r = self.call("GET", "/api/v1/jobs")
        self.assertEqual((code, body), (200, b"null"))  # the docs' health check prints null
        self.assertTrue(r.getheader("X-Engine").startswith("gmskit-engine/"))
        code, body, _ = self.call("POST", "/api/v1/jobs", {})
        self.assertEqual((code, json.loads(body)), (422, {"code": 422, "message": "missing name"}))
        code, body, _ = self.call("POST", "/api/v1/jobs", GOOD)
        self.assertEqual(code, 201)
        jid = json.loads(body)["id"]
        self.assertTrue(self.worker.wake.is_set())
        job = json.loads(self.call("GET", f"/api/v1/jobs/{jid}")[1])
        self.assertEqual((job["ID"], job["Name"], job["Status"], job["Data"]["keywords"]), (jid, "t", "pending", ["cafes in Austin"]))
        self.assertEqual(self.call("GET", f"/api/v1/jobs/{jid}/download")[0], 404)
        self.store.append(jid, [jobs.to_row(place(1), "k")], jobs.fields({}))
        code, body, r = self.call("GET", f"/api/v1/jobs/{jid}/download")
        self.assertEqual((code, r.getheader("Content-Type")), (200, "text/csv; charset=utf-8"))
        self.assertEqual(self.call("GET", f"/download?id={jid}")[1], body)
        self.assertEqual(len(json.loads(self.call("GET", "/api/v1/jobs")[1])), 1)
        self.assertEqual(self.call("DELETE", f"/api/v1/jobs/{jid}")[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/v1/jobs/{jid}")[0], 404)
        self.assertEqual(self.call("GET", f"/api/v1/jobs/{jid}")[0], 404)
        self.assertEqual(self.call("GET", "/download?id=../../etc/passwd")[0], 404)

    def test_guards(self):
        self.assertEqual(self.call("GET", "/api/v1/jobs", headers={"Host": "evil.com"})[0], 421)
        self.assertEqual(self.call("GET", "/api/v1/jobs", headers={"Host": "localhost.evil.com"})[0], 421)
        self.assertEqual(self.call("POST", "/api/v1/jobs", GOOD, {"Origin": "https://evil.com"})[0], 403)
        self.assertEqual(self.call("POST", "/api/v1/jobs", GOOD, {"Origin": "null"})[0], 403)
        self.assertEqual(self.call("POST", "/api/v1/jobs", GOOD, {"Origin": "http://localhost.evil.com"})[0], 403)
        self.assertEqual(self.call("POST", "/api/v1/jobs", GOOD, {"Origin": f"http://localhost:{self.port}"})[0], 201)
        self.assertEqual(self.call("GET", "/api/v1/jobs", headers={"Host": f"127.0.0.1:{self.port}"})[0], 200)
        self.assertEqual(self.call("POST", "/api/v1/jobs", raw=b"{nope")[0], 400)
        self.assertEqual(self.call("POST", "/api/v1/jobs", raw=b"\xff\xfe")[0], 400)
        self.assertEqual(self.call("POST", "/api/v1/jobs", raw=b"", headers={"Content-Length": "99999999"})[0], 413)
        self.assertEqual(self.call("POST", "/api/v1/jobs", raw=b"0\r\n\r\n", headers={"Transfer-Encoding": "chunked"})[0], 411)
        self.assertEqual(self.call("POST", "/elsewhere", GOOD)[0], 404)

    def test_refused_post_closes_the_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/elsewhere", json.dumps(GOOD), {"Host": "localhost"})
        r = c.getresponse()
        r.read()
        self.assertEqual((r.status, r.getheader("Connection")), (404, "close"))
        c.close()

    def test_page_and_docs(self):
        code, body, r = self.call("GET", "/")
        csp = r.getheader("Content-Security-Policy")
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        self.assertIn(f'<script nonce="{nonce}">', body.decode())
        self.assertIn("github.com/Mahanaicoach/google-maps-scraper-kit", body.decode())
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("connect-src 'self' https://raw.githubusercontent.com;", csp)  # sponsors.json, nothing else
        self.assertIn("assets/sponsors/sponsors.json", body.decode())
        self.assertNotRegex(body.decode(), r"\{[a-z_]+\}")  # every placeholder filled
        code, body, _ = self.call("GET", "/api/openapi.json")
        self.assertEqual(json.loads(body)["paths"]["/api/v1/jobs"]["post"]["requestBody"]["content"]["application/json"]
                         ["schema"]["required"], ["name", "keywords", "lang", "lat", "lon", "max_time"])


class ApiKey(Server):
    key = "s3cret"

    def test_key(self):
        self.assertEqual(self.call("GET", "/api/v1/jobs")[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/jobs", headers={"X-API-Key": "wrong"})[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/jobs", headers={"X-API-Key": "s3cret"})[0], 200)
        self.assertEqual(self.call("GET", "/")[0], 200)  # the page holds no data; it asks for the key


class Bind(Fakes):
    """--host 0.0.0.0 is for the Docker image; elsewhere the engine keeps to loopback."""

    def test_any_address_has_no_ipv6_twin(self):
        # Mocked: a real 0.0.0.0 bind would open a port to the network on the machine running the tests.
        with unittest.mock.patch.object(serve, "Server") as v4, unittest.mock.patch.object(serve, "Server6") as v6:
            serve.make_servers(0, self.store, self.worker, host="0.0.0.0")
        v4.assert_called_once_with(("0.0.0.0", 0), serve.Handler)
        v6.assert_not_called()

    def test_any_address_refused_outside_a_container(self):
        argv = ["serve.py", "--host", "0.0.0.0", "--port", "0", "--data", self.dir]
        with unittest.mock.patch.object(serve, "in_container", return_value=False), \
             unittest.mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as cm:
            serve.main()
        self.assertIn("Docker image only", str(cm.exception))


class ChromeStartErrors(unittest.TestCase):
    """scripts/scrape.py names Chrome start errors by their first words (and never retries them):
    the engine's messages must keep starting that way."""

    def test_scrape_recognises_every_start_error(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
        import scrape
        msgs = [browser.start_error("/x/chrome", 127, "chrome: error while loading shared libraries: libnss3.so"),
                browser.start_error("/x/chrome", 1, "[1:2:FATAL:x.cc(1)] boom")]
        saved = browser.find_chrome
        browser.find_chrome = lambda: None
        try:
            for path in (None, os.path.join(tempfile.gettempdir(), "no-such-chrome-binary")):
                with self.assertRaises(browser.BrowserError) as cm:
                    browser.Browser(path=path, timeout=1)
                msgs.append(str(cm.exception))
        finally:
            browser.find_chrome = saved
        for m in msgs:
            self.assertTrue(m.startswith(scrape.CHROME_START), m)


class ScrapeClient(Server):
    """scripts/scrape.py's own create, poll and download against this engine, with the worker running."""

    def test_run_job(self):
        RESULTS["cafes in Austin"] = ([place(1), place(2)], "end-of-list")
        os.environ["SCRAPER_BASE_URL"] = f"http://localhost:{self.port}"
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
        try:
            import scrape
            scrape.BASE = os.environ["SCRAPER_BASE_URL"]
            scrape.POLL_EVERY = 1
            self.worker.start()
            out = io.StringIO()
            saved, sys.stdout = sys.stdout, out
            try:
                jid, rows = scrape.run_job({"name": "t", "keywords": ["cafes in Austin"], "lang": "en",
                                            "lat": "30.2", "lon": "-97.7", "depth": 1, "email": False,
                                            "max_time": 60, "zoom": 15, "fast_mode": False, "radius": 10000})
            finally:
                sys.stdout = saved
        finally:
            del os.environ["SCRAPER_BASE_URL"]
        self.assertEqual([r["title"] for r in rows], ["Biz 1", "Biz 2"])
        self.assertEqual(self.store.get(jid)["Status"], "ok")
        for k in scrape.LEAD:
            self.assertIn(k, rows[0])


class Download(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (browser.HOME, dict(browser.CFT_SHA256))
        browser.HOME = self.dir

    def tearDown(self):
        browser.HOME = self.saved[0]
        browser.CFT_SHA256.clear()
        browser.CFT_SHA256.update(self.saved[1])
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def zipped():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            info = zipfile.ZipInfo("chrome-headless-shell-linux64/chrome-headless-shell")
            info.external_attr = 0o100755 << 16
            z.writestr(info, b"#!/bin/sh\n")
            z.writestr("chrome-headless-shell-linux64/libfoo.so", b"x")
        return buf.getvalue()

    def opener(self, data):
        return lambda url, timeout=0: io.BytesIO(data)

    def test_platforms(self):
        self.assertEqual(browser.cft_platform("Darwin", "arm64"), "mac-arm64")
        self.assertEqual(browser.cft_platform("Darwin", "x86_64"), "mac-x64")
        self.assertEqual(browser.cft_platform("Linux", "aarch64"), "linux-arm64")
        self.assertEqual(browser.cft_platform("Linux", "x86_64"), "linux64")
        self.assertEqual(browser.cft_platform("Windows", "AMD64"), "win64")
        self.assertEqual(browser.cft_platform("Windows", "ARM64"), "win64")
        self.assertIsNone(browser.cft_platform("Linux", "riscv64"))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", h) for h in self.saved[1].values()))

    def test_mismatch_is_deleted_and_never_run(self):
        with self.assertRaises(browser.BrowserError) as c:
            browser.download(lambda m: None, "linux64", self.opener(self.zipped()))
        self.assertIn("does not match the SHA-256", str(c.exception))
        leftovers = [f for _, _, fs in os.walk(self.dir) for f in fs]
        self.assertEqual(leftovers, [])

    def test_verified_download_unpacks(self):
        data = self.zipped()
        browser.CFT_SHA256["linux64"] = hashlib.sha256(data).hexdigest()
        path = browser.download(lambda m: None, "linux64", self.opener(data))
        self.assertEqual(path, browser.shell_path("linux64"))
        self.assertTrue(os.path.isfile(path))
        if os.name != "nt":
            self.assertTrue(os.access(path, os.X_OK))
            self.assertEqual(os.stat(self.dir).st_mode & 0o777, 0o700)
        self.assertFalse(any(f.endswith(".part") for _, _, fs in os.walk(self.dir) for f in fs))

    def test_missing_libraries_hint(self):
        err = "chrome: error while loading shared libraries: libnss3.so: cannot open shared object file\n"
        msg = browser.start_error("/x/chrome", 127, err)
        self.assertIn("libnss3.so", msg)
        self.assertIn("sudo apt-get install -y libnss3", msg)
        self.assertIn("code 3", browser.start_error("/x/chrome", 3, ""))
        trace = "[1:2:FATAL:zygote_host_impl_linux.cc(130)] No usable sandbox!\n#0 0x1 base::debug\n#1 0x2 x\n#2 0x3 y\n"
        self.assertIn("No usable sandbox", browser.start_error("/x/chrome", -5, trace))


FAKE_CHROME = """#!{py}
import os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(" ".join(args) + "\\n")
if os.environ.get("FAKE_SANDBOX") and "--no-sandbox" not in args:
    sys.stderr.write("[1:2:FATAL:zygote_host_impl_linux.cc(130)] No usable sandbox!\\n#0 0x1 trace\\n")
    sys.exit(1)
profile = [a.split("=", 1)[1] for a in args if a.startswith("--user-data-dir=")][0]
with open(os.path.join(profile, "DevToolsActivePort"), "w") as f:
    f.write("{port}\\n/devtools/browser/x\\n")  # nothing listens there
time.sleep(60)
"""


@unittest.skipIf(os.name == "nt", "the fake Chrome is a script with a shebang")
class LaunchChrome(unittest.TestCase):
    """browser.Browser against a fake Chrome: cleanup on a dead DevTools port, the sandbox relaunch."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.exe = os.path.join(self.dir, "fake-chrome")
        with open(self.exe, "w") as f:
            f.write(FAKE_CHROME.format(py=sys.executable, port=port))
        os.chmod(self.exe, 0o755)
        os.environ["FAKE_LOG"] = os.path.join(self.dir, "argv.log")

    def tearDown(self):
        os.environ.pop("FAKE_LOG", None)
        os.environ.pop("FAKE_SANDBOX", None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def launches(self):
        with open(os.environ["FAKE_LOG"]) as f:
            return f.read().splitlines()

    def test_user_agent_keeps_running_version_and_platform(self):
        for native in ("Mozilla/5.0 (X11; Linux x86_64) HeadlessChrome/157.0.1.2 Safari/537.36",
                       "Mozilla/5.0 (Macintosh) Chrome/156.0.1.2 Safari/537.36"):
            with unittest.mock.patch.object(browser.Browser, "_launch", return_value=(1, "/test")), \
                 unittest.mock.patch.object(browser, "CDP") as connection:
                connection.return_value.user_agent = None
                connection.return_value.send.return_value = {"userAgent": native}
                with browser.Browser(path=self.exe) as b:
                    expected = native.replace("HeadlessChrome/", "Chrome/") if "HeadlessChrome/" in native else None
                    self.assertEqual(b.cdp.user_agent, expected)
                    b.cdp.send.assert_called_once_with("Browser.getVersion")

    def test_dead_devtools_port_leaves_nothing(self):
        profile = os.path.join(self.dir, "profile")
        os.mkdir(profile)
        b = None
        with self.assertRaisesRegex(browser.BrowserError, "DevTools"):
            b = browser.Browser(path=self.exe, profile=profile, headless=False, timeout=5)
        self.assertIsNone(b)
        self.assertIn("--window-position=-3000,-3000", self.launches()[0])  # a window, out of sight
        self.assertTrue(browser.always_headless(browser.shell_path("linux64")))
        self.assertFalse(browser.always_headless("/usr/bin/google-chrome"))
        self.assertNotIn("--headless", self.launches()[0])
        out = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
        self.assertNotIn(self.exe, out)  # the fake Chrome was stopped

    def test_temp_profile_removed(self):
        tmp = os.path.join(self.dir, "tmp")
        os.mkdir(tmp)
        saved, tempfile.tempdir = tempfile.tempdir, tmp  # Browser's mkdtemp lands here, not in the shared temp
        try:
            with self.assertRaises(browser.BrowserError):
                browser.Browser(path=self.exe, timeout=5)
        finally:
            tempfile.tempdir = saved
        self.assertEqual(os.listdir(tmp), [])

    def test_no_usable_sandbox_relaunches_once_without_it(self):
        os.environ["FAKE_SANDBOX"] = "1"
        with self.assertRaisesRegex(browser.BrowserError, "DevTools"):  # second launch got past the sandbox
            browser.Browser(path=self.exe, timeout=5)
        runs = self.launches()
        self.assertEqual(len(runs), 2)
        self.assertNotIn("--no-sandbox", runs[0])
        self.assertIn("--no-sandbox", runs[1])

    def test_snap_wrapper_skipped(self):
        wrapper = os.path.join(self.dir, "chromium-browser")
        with open(wrapper, "w") as f:
            f.write("#!/bin/sh\nexec /snap/bin/chromium \"$@\"\n")
        self.assertTrue(browser.is_snap(wrapper))
        self.assertTrue(browser.is_snap("/snap/bin/chromium"))
        self.assertFalse(browser.is_snap(self.exe))


def load_engine_cli():
    path = os.path.join(os.path.dirname(HERE), "scripts", "engine.py")
    spec = importlib.util.spec_from_file_location("engine_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class EngineCli(unittest.TestCase):
    def test_port_with_a_wildcard_listener_is_not_free(self):
        cli = load_engine_cli()
        s = socket.socket()
        s.bind(("0.0.0.0", 0))
        s.listen(8)  # both checks connect; a full backlog would refuse the second
        port = s.getsockname()[1]
        try:
            self.assertFalse(cli.port_free(port))
            self.assertTrue(serve.port_taken(port))
        finally:
            s.close()
        self.assertFalse(serve.port_taken(port))

    def test_stop_never_kills_a_stranger(self):
        cli = load_engine_cli()
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(cli.kill(p.pid))  # "nothing of ours to stop"
            time.sleep(0.3)
            self.assertIsNone(p.poll())
        finally:
            p.kill()
            p.wait()


def wait_until(cond, seconds=5):
    end = time.time() + seconds
    while not cond() and time.time() < end:
        time.sleep(0.02)
    return cond()


class Resume(Fakes):
    """P3: rows stream into the CSV, Progress says how far a job is, and a job the engine was stopped
    in the middle of carries on from its last finished keyword."""

    def start(self, **body):
        name, data = jobs.validate(dict(GOOD, **body))
        jid = self.store.create(name, data)
        t = threading.Thread(target=self.worker.run_job, args=(self.store.claim(),))
        t.start()
        return jid, t

    def test_rows_stream_in_and_a_stopped_engine_resumes(self):
        RESULTS["cafes"] = ([place(1)], "end-of-list")
        RESULTS["bakeries"] = ([place(2), place(3)], "slow")
        RESULTS["florists"] = ([place(4)], "end-of-list")
        jid, t = self.start(keywords=["cafes", "bakeries", "florists"])
        self.assertTrue(wait_until(lambda: self.store.get(jid)["Progress"].get("places") == 3))
        self.assertEqual(len(self.rows(jid)), 3)  # bakeries' places are in before the keyword ends
        prog = jobs.public(self.store.get(jid))["Progress"]
        self.assertEqual((prog["keywords_done"], prog["keywords_total"], prog["places"], prog["stage"]), (1, 3, 3, "search"))
        self.assertTrue(33 < prog["percent"] <= 66)
        self.worker.shutdown(wait=5)  # the engine stops mid-keyword
        t.join(5)
        self.assertEqual(self.store.get(jid)["Status"], "pending")
        RESULTS["bakeries"] = ([place(2), place(3)], "end-of-list")
        SEARCHED.clear()
        jobs.Worker(self.store, self.lines.append).run_job(self.store.claim())  # the next start
        job = self.store.get(jid)
        self.assertEqual((job["Status"], job["Error"]), ("ok", ""))
        self.assertEqual(SEARCHED, ["bakeries", "florists"])  # cafes was finished, bakeries was not
        self.assertEqual([r["title"] for r in self.rows(jid)], ["Biz 1", "Biz 2", "Biz 3", "Biz 4"])
        self.assertEqual(jobs.public(job)["Progress"], {"keywords_done": 3, "keywords_total": 3, "places": 4,
                                                        "percent": 100, "stage": "done"})

    def crashed(self, keywords, **progress):
        """A job an engine crashed in: one keyword finished, part of the next one appended, a torn line."""
        name, data = jobs.validate(dict(GOOD, keywords=keywords))
        jid = self.store.create(name, data)
        size = self.store.append(jid, [jobs.to_row(place(1), keywords[0])], jobs.fields(data))
        self.store.append(jid, [jobs.to_row(place(2), keywords[1])], jobs.fields(data))
        with open(self.store.csv_path(jid), "ab") as f:
            f.write(b'"cafes,Biz 9,"half a ro')
        self.store.claim()
        self.store.set_progress(jid, dict({"done": 1, "total": len(keywords), "bytes": size, "stage": "search"}, **progress))
        return jid, size

    def test_crash_cuts_the_unfinished_keyword_and_resumes(self):
        jid, size = self.crashed(["cafes", "bakeries"], elapsed=20)
        self.assertEqual(self.store.recover(), (1, 0))
        self.assertEqual(os.path.getsize(self.store.csv_path(jid)), size)  # torn line and bakeries' row gone
        RESULTS["bakeries"] = ([place(2), place(3)], "end-of-list")
        self.worker.run_job(self.store.claim())
        self.assertEqual(SEARCHED, ["bakeries"])
        self.assertEqual([r["title"] for r in self.rows(jid)], ["Biz 1", "Biz 2", "Biz 3"])
        self.assertTrue(any("resumed at keyword 2/2 with 1 places, 40 s left" in l for l in self.lines))

    def test_too_many_restarts_fail_as_before(self):
        jid, size = self.crashed(["cafes", "bakeries"], resumes=jobs.MAX_RESUMES)
        self.assertEqual(self.store.recover(), (0, 1))
        job = self.store.get(jid)
        self.assertEqual(job["Status"], "failed")
        self.assertIn("the engine stopped while this job ran (4 times); its rows so far are kept", job["Error"])
        self.assertEqual(os.path.getsize(self.store.csv_path(jid)), size)

    def test_no_time_left_ends_without_searching(self):
        jid, _ = self.crashed(["cafes", "bakeries"], elapsed=60)
        self.store.recover()
        self.worker.run_job(self.store.claim())
        job = self.store.get(jid)
        self.assertEqual((job["Status"], SEARCHED, FakeBrowser.made), ("ok", [], []))
        self.assertIn("stopped at max_time", job["Error"])
        self.assertEqual([r["title"] for r in self.rows(jid)], ["Biz 1"])

    def test_old_store_gets_the_progress_column(self):
        folder = os.path.join(self.dir, "old")
        os.makedirs(folder)
        db = sqlite3.connect(os.path.join(folder, "jobs.db"))
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, name TEXT NOT NULL, date TEXT NOT NULL, "
                   "status TEXT NOT NULL, data TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')")
        db.execute("INSERT INTO jobs VALUES ('x', 'n', 'd', 'ok', '{}', '')")
        db.commit()
        db.close()
        old = jobs.Store(folder)
        self.assertEqual(old.get("x")["Progress"], {})
        old.db.close()


class Backoff(Fakes):
    def setUp(self):
        super().setUp()
        self.saved_waits = (jobs.BACKOFF, jobs.ROTATE_WAIT)
        jobs.BACKOFF, jobs.ROTATE_WAIT = (0.05, 0.05), 0.01

    def tearDown(self):
        jobs.BACKOFF, jobs.ROTATE_WAIT = self.saved_waits
        super().tearDown()

    def test_retry_through_the_next_proxy(self):
        RESULTS["cafes in Austin"] = ([place(1)], ["blocked", "end-of-list"])
        jid, job = self.run_one(proxies=["http://a:1", "http://b:2"])
        self.assertEqual((job["Status"], job["Error"]), ("ok", ""))
        self.assertEqual(FakeBrowser.made[0].contexts, ["http://a:1", "http://b:2"])
        self.assertTrue(any("retry 1/2 in 0.01 s through the next proxy" in l for l in self.lines))

    def test_gives_up_after_bounded_retries(self):
        RESULTS["cafes in Austin"] = ([], ["blocked"])
        _, job = self.run_one()
        self.assertEqual(job["Status"], "failed")
        self.assertIn("unusual-traffic page", job["Error"])  # what scripts/scrape.py looks for
        self.assertIn("Waited and retried 2 time(s)", job["Error"])
        self.assertEqual(SEARCHED, ["cafes in Austin"] * 3)

    def test_a_failed_proxy_hands_the_keyword_to_the_next(self):
        calls = []
        def collect(page, url, **kw):
            calls.append(FakeBrowser.made[-1].contexts[-1])
            if len(calls) == 1:
                raise CDPError("navigate https://x: net::ERR_PROXY_CONNECTION_FAILED")
            return [place(1)], "end-of-list", {"batches": 1, "dom_links": 0}
        maps.collect = collect
        jid, job = self.run_one(proxies=["http://dead:1", "http://ok:2"])
        self.assertEqual((job["Status"], job["Error"], calls), ("ok", "", ["http://dead:1", "http://ok:2"]))
        self.assertEqual(len(self.rows(jid)), 1)

    def test_proxies_take_turns(self):
        for k in ("a", "b", "c"):
            RESULTS[k] = ([], "end-of-list")
        self.run_one(keywords=["a", "b", "c"], proxies=["http://u:pw-s3cret@p1:1", "http://p2:2"])
        self.assertEqual(FakeBrowser.made[0].contexts, ["http://u:pw-s3cret@p1:1", "http://p2:2", "http://u:pw-s3cret@p1:1"])
        self.assertFalse(any("s3cret" in l for l in self.lines))
        self.assertEqual(FakeAuth.logins, [("u", "pw-s3cret")] * 2)  # only the tabs on the proxy with a login


class ProxyLogin(unittest.TestCase):
    """browser.ProxyAuth against a fake DevTools connection: continue every paused request, answer
    the proxy's 407 once with the job's credentials, then give up (wrong credentials)."""

    def test_answers(self):
        class C:
            closed = None
            def __init__(self):
                self.sent = []
            def subscribe(self, method, sid=None, q=None):
                self.q = q or queue.Queue()
                return self.q
            def unsubscribe(self, *a):
                pass
        class P:
            sid = "s"
            def __init__(self):
                self.cdp = C()
            def send(self, method, params=None, timeout=None):
                self.cdp.sent.append((method, params))
                return {}
        page = P()
        auth = browser.ProxyAuth(page, "user", "pw")
        q = page.cdp.q
        q.put({"requestId": "1", "request": {"url": "https://www.google.com/"}})
        q.put({"requestId": "2", "authChallenge": {"source": "Proxy"}})
        q.put({"requestId": "2", "authChallenge": {"source": "Proxy"}})
        q.put({"requestId": "3", "authChallenge": {"source": "Server"}})
        self.assertTrue(wait_until(lambda: len(page.cdp.sent) == 5))
        auth.close()
        sent = page.cdp.sent
        self.assertEqual(sent[0], ("Fetch.enable", {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]}))
        self.assertEqual(sent[1], ("Fetch.continueRequest", {"requestId": "1"}))
        self.assertEqual([m[1]["authChallengeResponse"]["response"] for m in sent[2:]],
                         ["ProvideCredentials", "CancelAuth", "Default"])
        self.assertEqual(sent[2][1]["authChallengeResponse"]["username"], "user")
        self.assertTrue(auth.refused)  # the job then says "proxy login refused", not Chrome's vague error
        self.assertIn("proxy login refused", jobs.LOGIN_REFUSED)
        self.assertTrue(jobs.PROXY_FAILED.search(jobs.LOGIN_REFUSED))
        self.assertFalse(jobs.PROXY_FAILED.search("navigate https://x/maps/search/tunnel%20car%20wash: net::ERR_TIMED_OUT"))

    def test_split(self):
        self.assertEqual(browser.split_proxy("http://u%40x:p%3A@h:3128"), ("http://h:3128", "u@x", "p:"))
        self.assertEqual(browser.split_proxy("socks5h://[::1]:1080"), ("socks5://[::1]:1080", "", ""))


def entity(days=7, reviews=False):
    """A place entity shaped like Google's (made-up values) with the full view's fields."""
    e = [None] * 260
    e[11], e[10], e[78] = "Biz", "0x1:0x2", "ChIJx"
    e[4] = [None, None, None, ["https://search.google.com/local/reviews?placeid=ChIJx", "349 reviews"],
            None, None, None, 4.7, 349]
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    week = [[names[n - 1], n, [2026, 11, n], [["7\u202fAM\u20139\u202fPM", [[7], [21]]]], 0, 1] for n in (6, 7, 1, 2, 3, 4, 5)]
    e[203] = [week[:days], [week[0], 0, 4, None, ["Open \u00b7 Closes 9\u202fPM"]]]
    if reviews:
        def review(stars, text):
            r = [None] * 4
            r[0] = "id"
            r[1] = [None, None, 1, 1, [None, None, ["https://example.com/u"], None, None, ["A Reviewer"]], None, "4 months ago"]
            r[2] = [[stars]] + [None] * 14 + [[[text]]]
            return [r]
        e[175] = [None] * 10
        e[175][3] = [6, 12, 6, 24, 301]
        e[175][9] = [[[review(5, "Great\u202fcoffee"), review(2, "Slow"), review(4, "")]]]
    return e


class ExtraDetailsParser(unittest.TestCase):
    def test_photos_from_search_and_panel_are_valid_json_and_deduplicated(self):
        e = [None] * 204
        photo = [None] * 7
        photo[6] = ["https://lh3.googleusercontent.com/business-photo"]
        e[37] = [[photo]]
        e[72] = [[photo, None, [None] * 7]]
        panel = [None] * 4
        panel[2], panel[3] = "Exterior", [[None] * 6 + [["https://lh3.googleusercontent.com/exterior"]]]
        e[171] = [[panel]]
        out = details.parse_entity(e)
        images = json.loads(out["images"])
        self.assertEqual(images, [{"title": "Exterior", "image": "https://lh3.googleusercontent.com/exterior"},
                                  {"title": "", "image": "https://lh3.googleusercontent.com/business-photo"}])
        row = jobs.to_row(out, "x")
        self.assertEqual(json.loads(row["images"]), images)
        photo[6] = ["javascript:alert(1)"]
        e[171] = None
        self.assertNotIn("images", details.parse_entity(e))
        for bad in ("https://u:p@lh3.googleusercontent.com/x", "https://a.com/x\ny", "https://a.com/" + "x" * 1000):
            photo[6] = [bad]
            self.assertNotIn("images", details.parse_entity(e))
        many = [[None] * 6 + [[f"https://lh3.googleusercontent.com/{n}"]] for n in range(50)]
        e[37] = [many]
        self.assertEqual(len(json.loads(details.parse_entity(e)["images"])), details.IMAGES_MAX)

class FullView(unittest.TestCase):
    """details.py fields that only Google's full view carries (a Chrome window), from made-up data."""

    def test_count_link_and_week(self):
        out = details.parse_entity(entity())
        self.assertEqual((out["review_count"], out["review_rating"]), (349, 4.7))
        self.assertEqual(out["reviews_link"], "https://search.google.com/local/reviews?placeid=ChIJx")
        week = json.loads(out["open_hours"])
        self.assertEqual(list(week)[:2], ["Monday", "Tuesday"])
        self.assertEqual(week["Sunday"], ["7 AM\u20139 PM"])
        self.assertEqual(out["open_state"], "Open \u00b7 Closes 9 PM" if False else out["open_state"])

    def test_limited_view_gives_no_week(self):
        self.assertNotIn("open_hours", details.parse_entity(entity(days=1)))  # today only is not a week

    def test_place_panel_reviews_without_names(self):
        body = ")]}'\n" + json.dumps([None] * 6 + [entity(reviews=True)])
        out = details.parse_place(body, limit=2)
        self.assertEqual(json.loads(out["reviews_per_rating"]), {"1": 6, "2": 12, "3": 6, "4": 24, "5": 301})
        self.assertEqual(json.loads(out["user_reviews"]), [{"Rating": 5, "When": "4 months ago", "Description": "Great coffee"},
                                                           {"Rating": 2, "When": "4 months ago", "Description": "Slow"}])
        self.assertNotIn("Reviewer", json.dumps(out))
        self.assertEqual(details.parse_place("not json"), {})


class Locale(unittest.TestCase):
    def test_search_url_and_end_of_list(self):
        self.assertEqual(maps.search_url("a b", 1, 2, 15, "de", "at"), "https://www.google.com/maps/search/a%20b/@1,2,15z?hl=de&gl=at")
        self.assertTrue(maps.search_url("x").endswith("?hl=en"))
        self.assertTrue(maps.ended({"cls": True, "text": ""}))
        self.assertTrue(maps.ended({"cls": False, "text": "… Vous êtes arrivé à la fin de la liste."}))
        self.assertFalse(maps.ended({"cls": False, "text": "Biz 1"}))
        self.assertFalse(maps.ended(None))
        self.assertIn('input[name="set_eom"][value="true"]', maps.JS_CONSENT)  # the reject form, any language
        self.assertIn("tout refuser", maps.JS_CONSENT)

    def test_429_marks_the_batches_limited(self):
        q = queue.Queue()
        class C:
            closed = None
            def subscribe(self, *a):
                return q
        class P:
            sid, cdp = "s", C()
        b = maps.Batches(P())
        q.put({"requestId": "1", "response": {"url": "https://www.google.com/search?tbm=map&pb=x", "status": 429,
                                              "mimeType": "text/html"}})
        self.assertEqual((b.drain(), b.limited, b.pending), ([], 1, {}))


class Websites(unittest.TestCase):
    def test_socials_and_mx(self):
        rows = [{"website": "https://shop.com", "emails": ""}, {"website": "https://gone.com", "emails": ""}]
        pages = {"https://shop.com": 'info@shop.com <a href="https://www.instagram.com/shopco/">ig</a> '
                                     '<a href="https://facebook.com/sharer">x</a> <a href="https://facebook.com/shopco">fb</a>',
                 "https://gone.com": "hi@gone-forever.com"}
        hosts = {"shop.com": "google_workspace", "gone-forever.com": "nxdomain"}
        with unittest.mock.patch.object(jobs, "mx_host", side_effect=hosts.get):
            n = jobs.add_site_data(rows, 10, socials=True, verify=True, get=pages.get)
        self.assertEqual(n, (1, 0))
        self.assertEqual((rows[0]["emails"], rows[0]["email_host"]), ("info@shop.com", "google_workspace"))
        self.assertEqual((rows[0]["instagram"], rows[0]["facebook"]), ("https://www.instagram.com/shopco", "https://facebook.com/shopco"))
        self.assertEqual(rows[1]["emails"], "")  # its only address is on a domain that does not exist

    def test_mx_answers(self):
        def doh(answer):
            r = unittest.mock.MagicMock()
            r.__enter__.return_value = io.BytesIO(json.dumps(answer).encode())
            return unittest.mock.patch.object(jobs._OPENER, "open", return_value=r)
        jobs._MX.clear()
        with doh({"Status": 3}):
            self.assertEqual(jobs.mx_host("a.test"), "nxdomain")
        with doh({"Status": 0, "Answer": [{"type": 15, "data": "1 aspmx.l.google.com."}]}):
            self.assertEqual(jobs.mx_host("b.test"), "google_workspace")
        with doh({"Status": 0}):
            self.assertEqual(jobs.mx_host("c.test"), "none")
        with doh({"Status": 2}):  # SERVFAIL: no verdict, and not kept for the next job
            self.assertEqual(jobs.mx_host("d.test"), "")
        self.assertNotIn("d.test", jobs._MX)
        jobs._MX.clear()

    def test_same_social_patterns_as_scrape(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
        import scrape
        self.assertEqual({k: r.pattern for k, r in jobs.SOCIAL_RE.items()}, {k: r.pattern for k, r in scrape.SOCIAL_RE.items()})
        self.assertEqual(jobs.SOCIALS, scrape.SOCIAL_FIELDS)
        self.assertEqual(jobs.SKIP_HANDLE, scrape._SKIP_HANDLE)


class ApiP3(Server):
    def test_progress_json_and_docs(self):
        code, body, _ = self.call("POST", "/api/v1/jobs", dict(GOOD, proxies=["http://u:pw-s3cret@h:1"]))
        jid = json.loads(body)["id"]
        code, body, _ = self.call("GET", f"/api/v1/jobs/{jid}")
        job = json.loads(body)
        self.assertEqual(job["Data"]["proxies"], ["http://u:***@h:1"])
        self.assertNotIn(b"s3cret", body + self.call("GET", "/api/v1/jobs")[1])
        self.assertEqual(job["Progress"], {"keywords_done": 0, "keywords_total": 1, "places": 0, "percent": 0,
                                           "stage": "queued"})
        self.store.append(jid, [jobs.to_row(place(1, title="Café"), "k")], jobs.fields({}))
        code, body, r = self.call("GET", f"/api/v1/jobs/{jid}/download?format=json")
        self.assertEqual((code, r.getheader("Content-Type")), (200, "application/json; charset=utf-8"))
        rows = json.loads(body)
        self.assertEqual((len(rows), rows[0]["title"], rows[0]["review_count"]), (1, "Café", "11"))
        self.assertEqual(self.call("GET", f"/api/v1/jobs/{jid}/download?format=xml")[0], 400)
        self.assertEqual(self.call("GET", f"/download?id={jid}&format=json")[1], body)
        code, body, r = self.call("GET", "/api/docs")
        page, csp = body.decode(), r.getheader("Content-Security-Policy")
        self.assertEqual((code, r.getheader("Content-Type")), (200, "text/html; charset=utf-8"))
        self.assertNotIn("<script", page)
        nonce = re.search(r"nonce-([^']+)'", csp).group(1)
        self.assertIn(f'<style nonce="{nonce}">', page)
        self.assertNotIn("script-src", csp)
        self.assertIn("<code>reviews_max</code>", page)
        spec = json.loads(self.call("GET", "/api/openapi.json")[1])
        self.assertEqual(spec["paths"]["/api/v1/jobs"]["post"]["requestBody"]["content"]["application/json"]
                         ["schema"]["required"], ["name", "keywords", "lang", "lat", "lon", "max_time"])
        ui = self.call("GET", "/")[1].decode()
        self.assertIn('id="country"', ui)
        self.assertIn('list="kwlist"', ui)
        self.assertIn(serve.CATEGORIES, ui)  # loaded by the browser from GitHub, pinned to a commit
        self.assertRegex(serve.CATEGORIES, r"/[0-9a-f]{40}/")
        script = re.search(r"<script nonce=[^>]+>(.*)</script>", ui, re.S).group(1)
        self.assertNotIn("'\n'", script)  # PAGE must say \\n: a bare newline in a JS string breaks the page


class ApiKeyDocs(Server):
    key = "s3cret"

    def test_docs_need_no_key(self):
        self.assertEqual(self.call("GET", "/api/docs")[0], 200)
        self.assertEqual(self.call("GET", "/api/openapi.json")[0], 200)
        self.assertEqual(self.call("GET", "/api/v1/jobs/00000000-0000-0000-0000-000000000000/download")[0], 401)



class LoopOne(Fakes):
    """Audit loop 1: blocks with no places yet, resumes after the search, refused logins, races."""

    def setUp(self):
        super().setUp()
        self.saved_waits = (jobs.BACKOFF, jobs.ROTATE_WAIT)
        jobs.BACKOFF, jobs.ROTATE_WAIT = (0.05, 0.05), 0.01

    def tearDown(self):
        jobs.BACKOFF, jobs.ROTATE_WAIT = self.saved_waits
        super().tearDown()

    def test_real_collect_calls_a_429_blocked(self):
        q = queue.Queue()
        class C:
            closed = None
            def subscribe(self, *a):
                return q
            def unsubscribe(self, *a):
                pass
        class P:
            sid, cdp = "s", C()
            def set_cookie(self, *a):
                pass
            def navigate(self, url):
                q.put({"requestId": "1", "response": {"url": "https://www.google.com/search?tbm=map&pb=x",
                                                      "status": 429, "mimeType": "text/html"}})
            def url(self):
                return "https://www.google.com/maps/search/x"
            def wait_for(self, expr, timeout=15):
                return True
            def eval(self, expr):
                return {"cls": False, "text": ""} if expr == maps.JS_END else []
            def send(self, *a, **k):
                raise CDPError("no body")
        t = time.time()
        real_collect = self.saved[2]  # Fakes swapped maps.collect for the fake one
        places, reason, _ = real_collect(P(), "https://www.google.com/maps/search/x", depth=3, log=lambda m: None)
        self.assertEqual((places, reason), ([], "blocked"))  # not "blocked+dom-fallback"
        self.assertLess(time.time() - t, 5)  # no 10 s wait for a first batch that Google refused

    def test_resume_after_the_reviews_keeps_them(self):
        RESULTS["cafes in Austin"] = ([place(1, website="https://a.com"), place(2)], "end-of-list")
        def reviews(rows, d, deadline, stop, proxies, save=None, notes=None):
            for r in rows:
                r["user_reviews"] = '[{"Rating": 5}]'
            return len(rows)
        self.worker.add_reviews, self.worker.headless = reviews, False  # window mode
        stopping = lambda *a, **k: self.worker.shutdown() or (0, 0)  # the engine stops during the website pass
        with unittest.mock.patch.object(jobs, "add_site_data", side_effect=stopping):
            jid, job = self.run_one(reviews_max=1, email=True)
        self.assertEqual((job["Status"], job["Progress"]["stage"]), ("pending", "websites"))
        w2 = jobs.Worker(self.store, self.lines.append)
        w2.add_reviews = lambda *a, **k: self.fail("the reviews were done already")
        with unittest.mock.patch.object(jobs, "fetch", return_value="hi@a.com"):
            w2.run_job(self.store.claim())
        rows = self.rows(jid)
        self.assertEqual(SEARCHED, ["cafes in Austin"])  # searched once, across the restart
        self.assertEqual([r["user_reviews"] for r in rows], ['[{"Rating": 5}]'] * 2)
        self.assertEqual((rows[0]["emails"], self.store.get(jid)["Status"]), ("hi@a.com", "ok"))

    def test_blocked_job_resumed_in_its_website_pass_keeps_rows_and_its_note(self):
        RESULTS["cafes in Austin"] = ([place(1, website="https://a.com")], ["slow-blocked"])
        def collect(page, url, on_batch=None, **kw):
            SEARCHED.append("cafes in Austin")
            on_batch([place(1, website="https://a.com")], 1)  # streamed, then Google refuses the rest
            return [], "blocked", {"batches": 1, "dom_links": 0}
        maps.collect = collect
        stopping = lambda *a, **k: self.worker.shutdown() or (0, 0)
        with unittest.mock.patch.object(jobs, "add_site_data", side_effect=stopping):
            jid, job = self.run_one(email=True)
        self.assertEqual(job["Status"], "pending")
        SEARCHED.clear()
        with unittest.mock.patch.object(jobs, "fetch", return_value=""):
            jobs.Worker(self.store, self.lines.append).run_job(self.store.claim())
        job = self.store.get(jid)
        self.assertEqual(SEARCHED, [])  # never back to the block
        self.assertEqual((job["Status"], [r["title"] for r in self.rows(jid)]), ("ok", ["Biz 1"]))
        self.assertIn("unusual-traffic page", job["Error"])
        self.assertIn("Waited and retried 2 time(s)", job["Error"])

    def test_a_refused_login_stops_the_job(self):
        def collect(page, url, **kw):
            SEARCHED.append(url)
            raise CDPError(jobs.LOGIN_REFUSED)
        maps.collect = collect
        _, job = self.run_one(keywords=["a", "b", "c"], proxies=["http://u:bad@h:1"])
        self.assertEqual((job["Status"], len(SEARCHED)), ("failed", 1))
        self.assertEqual(job["Error"], jobs.LOGIN_REFUSED + "; 2 keyword(s) after it were not searched")

    def test_shutdown_before_the_job_starts(self):
        name, data = jobs.validate(GOOD)
        jid = self.store.create(name, data)
        job = self.store.claim()
        self.worker.stopped.set()
        self.worker.run_job(job)
        self.assertEqual((self.store.get(jid)["Status"], FakeBrowser.made), ("pending", []))

    def test_an_engine_that_cannot_bind_leaves_the_jobs_alone(self):
        name, data = jobs.validate(GOOD)
        jid = self.store.create(name, data)
        self.store.claim()  # another engine on this data folder runs it
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        argv = ["serve.py", "--port", str(s.getsockname()[1]), "--data", os.path.join(self.dir, "data")]
        try:
            with unittest.mock.patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
                serve.main()
        finally:
            s.close()
        self.assertEqual(self.store.get(jid)["Status"], "working")


class LoopTwo(Fakes):
    """Audit loop 2: Chrome dying on its own, dead proxies, headless reviews, masks, notes."""

    def setUp(self):
        super().setUp()
        self.saved_waits = (jobs.BACKOFF, jobs.ROTATE_WAIT)
        jobs.BACKOFF, jobs.ROTATE_WAIT = (0.05, 0.05), 0.01

    def tearDown(self):
        jobs.BACKOFF, jobs.ROTATE_WAIT = self.saved_waits
        super().tearDown()

    def test_chrome_that_dies_is_started_again(self):
        def collect(page, url, **kw):
            SEARCHED.append(url.split("/search/")[1].split("/")[0])
            if len(SEARCHED) == 2:  # Chrome crashes during the second keyword
                FakeBrowser.made[-1].cdp.closed = OSError("gone")
                raise CDPError("connection lost: gone")
            return [place(len(SEARCHED))], "end-of-list", {"batches": 1, "dom_links": 0}
        maps.collect = collect
        jid, job = self.run_one(keywords=["a", "b", "c"])
        self.assertEqual((job["Status"], job["Error"]), ("ok", ""))
        self.assertEqual((SEARCHED, len(FakeBrowser.made)), (["a", "b", "b", "c"], 2))
        self.assertEqual(len(self.rows(jid)), 3)

    def test_chrome_that_keeps_dying_says_what_was_not_searched(self):
        def collect(page, url, **kw):
            FakeBrowser.made[-1].cdp.closed = OSError("gone")
            raise CDPError("connection lost: gone")
        maps.collect = collect
        _, job = self.run_one(keywords=["a", "b", "c"])
        self.assertEqual(job["Status"], "failed")
        self.assertIn("2 keyword(s) after it were not searched", job["Error"])
        self.assertEqual(len(FakeBrowser.made), 1 + jobs.RELAUNCHES)

    def test_a_dead_proxy_is_skipped_after_it_fails(self):
        calls = []
        def collect(page, url, **kw):
            calls.append(FakeBrowser.made[-1].contexts[-1])
            if "dead" in calls[-1]:
                raise CDPError("navigate https://x: net::ERR_PROXY_CONNECTION_FAILED")
            return [], "end-of-list", {"batches": 1, "dom_links": 0}
        maps.collect = collect
        _, job = self.run_one(keywords=["a", "b", "c", "d"], proxies=["http://dead:1", "http://ok:2"])
        self.assertEqual((job["Status"], calls), ("ok", ["http://dead:1"] + ["http://ok:2"] * 4))

    def test_headless_runs_the_reviews_pass(self):
        RESULTS["cafes in Austin"] = ([place(1)], "end-of-list")
        calls = []
        self.worker.add_reviews = lambda *a, **k: calls.append(a[1]["reviews_max"]) or 0
        _, job = self.run_one(reviews_max=2)
        self.assertEqual(job["Status"], "ok")
        self.assertEqual(calls, [2])
        self.assertNotIn("limited view", job["Error"])

    def test_errors_survive_a_restart_mid_search(self):
        name, data = jobs.validate(dict(GOOD, keywords=["a", "b", "c"]))
        jid = self.store.create(name, data)
        self.store.claim()
        self.store.set_progress(jid, {"done": 1, "total": 3, "stage": "search", "failed": 1,
                                      "last_error": "navigate x: net::ERR_NAME_NOT_RESOLVED"})
        self.store.recover()
        maps.collect = lambda page, url, **kw: ([place(2)], "end-of-list", {"batches": 1, "dom_links": 0})
        self.worker.run_job(self.store.claim())
        self.assertIn("1 keyword(s) failed: navigate x: net::ERR_NAME_NOT_RESOLVED", self.store.get(jid)["Error"])

    def test_late_website_pass_is_not_a_max_time_stop(self):
        RESULTS["cafes in Austin"] = ([place(1, website="https://a.com")], "end-of-list")
        def slow_pass(rows, seconds, **kw):
            time.sleep(1.2)  # runs past the job's 1 s max_time
            return 0, 0
        with unittest.mock.patch.object(jobs, "add_site_data", side_effect=slow_pass):
            _, job = self.run_one(email=True, max_time=1)
        self.assertEqual((job["Status"], job["Error"]), ("ok", ""))

    def test_masks_and_deep_json(self):
        self.assertEqual(jobs.mask("http://TOKEN123@h:80"), "http://***@h:80")
        self.assertEqual(jobs.mask("http://u:p@h:80"), "http://u:***@h:80")
        self.assertEqual(jobs.mask("http://h:80"), "http://h:80")
        servers = serve.make_servers(0, self.store, self.worker)
        port = servers[0].server_address[1]
        for srv in servers:
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("POST", "/api/v1/jobs", b"[" * 100000, {"Host": "localhost"})
            self.assertEqual(c.getresponse().status, 400)
            c.close()
        finally:
            for srv in servers:
                srv.shutdown()
                srv.server_close()


class LoopThree(Fakes):
    """Audit loop 3: passes that run out of time say so; a timeout with the DOM fallback counts."""

    def test_website_pass_out_of_time_says_so(self):
        RESULTS["cafes in Austin"] = ([place(1, website="https://a.com"), place(2, website="https://b.com")], "end-of-list")
        with unittest.mock.patch.object(jobs, "fetch", side_effect=lambda url, timeout=0: time.sleep(3) or ""), \
             unittest.mock.patch.object(jobs, "EMAIL_MIN_SECONDS", 0.3):
            _, job = self.run_one(email=True, max_time=1)
        self.assertEqual(job["Status"], "ok")
        self.assertIn("2 website(s) not checked: ran out of time", job["Error"])

    def test_reviews_out_of_time_say_so(self):
        RESULTS["cafes in Austin"] = ([place(1), place(2), place(3)], "end-of-list")
        self.worker.headless = False
        saved = (maps.place_panel, details.parse_place)
        maps.place_panel = lambda page, url: time.sleep(0.6) or "body"
        details.parse_place = lambda body, limit=5: {"user_reviews": "[]"}
        try:
            _, job = self.run_one(reviews_max=1, max_time=1)
        finally:
            maps.place_panel, details.parse_place = saved
        self.assertIn("reviews read for 2 of 3 places: ran out of time", job["Error"])

    def test_timeout_with_the_dom_fallback_is_a_timeout(self):
        RESULTS["cafes in Austin"] = ([place(1)], "timeout+dom-fallback")
        _, job = self.run_one()
        self.assertIn("stopped at max_time", job["Error"])

if __name__ == "__main__":
    unittest.main(verbosity=1)
