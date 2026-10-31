#!/usr/bin/env python3
"""Offline checks for the engine: job body validation, CSV rows, the sqlite store, the worker
(with a fake Chrome and fake Google results), the HTTP server and its guards, scripts/scrape.py's
own client against that server, the Chrome download's SHA-256 check and the email pass.
No network, no Chrome, no Docker.

    python3 engine/test_engine.py
"""
import csv, hashlib, http.client, io, json, os, re, shutil, sys, tempfile, threading, time, unittest, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import browser, jobs, maps, serve  # noqa: E402
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

    def __init__(self, **kw):
        self.kw, self.cdp, self.killed = kw, FakeCDP(), threading.Event()
        FakeBrowser.made.append(self)

    def kill(self):
        self.cdp.closed = OSError("killed")
        self.killed.set()

    def close(self):
        pass


class FakePage:
    def __init__(self, cdp):
        pass

    def close(self):
        pass


RESULTS = {}  # keyword -> (places, reason) for the fake collect


def fake_collect(page, url, depth=1, max_seconds=0, log=None, stop=lambda: False):
    for kw, (places, reason) in RESULTS.items():
        if maps.search_url(kw).split("/@")[0].split("?")[0] in url:
            if reason == "hang":  # Chrome stops answering until the watchdog kills it
                b = FakeBrowser.made[-1]
                b.killed.wait(10)
                raise CDPError("connection lost: killed")
            if reason == "slow":
                end = time.time() + 5
                while not stop() and time.time() < end:
                    time.sleep(0.02)
                return places, "stopped", {"batches": 1, "dom_links": 0}
            return places, reason, {"batches": 1, "dom_links": 0}
    return [], "no-feed", {"batches": 0, "dom_links": 0}


class Fakes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.saved = (browser.Browser, jobs.Page, maps.collect)
        browser.Browser, jobs.Page, maps.collect = FakeBrowser, FakePage, fake_collect
        FakeBrowser.made.clear()
        RESULTS.clear()
        self.store = jobs.Store(os.path.join(self.dir, "data"))
        self.lines = []
        self.worker = jobs.Worker(self.store, self.lines.append)

    def tearDown(self):
        browser.Browser, jobs.Page, maps.collect = self.saved
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
        self.assertIn("usernames and passwords", self.err(**dict(GOOD, proxies=["http://u:p@h:1"])))
        self.assertIn("invalid proxy", self.err(**dict(GOOD, proxies=["ftp://h:1"])))
        self.assertIn("invalid proxy", self.err(**dict(GOOD, proxies=["http://h:99999"])))
        with self.assertRaisesRegex(ValueError, "JSON object"):
            jobs.validate([1])

    def test_defaults_and_numbers(self):
        name, d = jobs.validate(dict(GOOD, lat=30.5, lon=-97, proxies=["socks5://127.0.0.1:1080"]))
        self.assertEqual((name, d["lat"], d["lon"], d["depth"], d["zoom"], d["email"]), ("t", "30.5", "-97", 1, 15, False))
        self.assertEqual(d["proxies"], ["socks5://127.0.0.1:1080"])


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
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.get(a)["Status"], "failed")
        self.assertTrue(self.store.write_csv(b, []))
        self.assertTrue(self.store.read_csv(b).startswith(b"input_id,link,title"))
        self.assertTrue(self.store.delete(b))
        self.assertIsNone(self.store.read_csv(b))
        self.assertFalse(self.store.write_csv(b, []))  # a deleted job gets no file back
        self.assertFalse(self.store.delete(b))
        self.assertEqual(set(self.store.get(a)), {"ID", "Name", "Date", "Status", "Data", "Error"})
        self.assertRegex(self.store.get(a)["Date"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")


class Worker(Fakes):
    def test_keywords_dedupe_into_one_csv(self):
        RESULTS["cafes"] = ([place(1), place(2)], "end-of-list")
        RESULTS["bakeries"] = ([place(2), place(3)], "depth")
        jid, job = self.run_one(keywords=["cafes", "bakeries"], depth=2, lang="de", proxies=["http://10.0.0.1:3128"])
        self.assertEqual((job["Status"], job["Error"]), ("ok", ""))
        rows = self.rows(jid)
        self.assertEqual([r["title"] for r in rows], ["Biz 1", "Biz 2", "Biz 3"])
        self.assertEqual([r["input_id"] for r in rows], ["cafes", "cafes", "bakeries"])
        self.assertEqual(FakeBrowser.made[0].kw, {"lang": "de", "proxy": "http://10.0.0.1:3128"})
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

    def test_blocked_fails(self):
        RESULTS["cafes in Austin"] = ([], "blocked")
        _, job = self.run_one()
        self.assertEqual(job["Status"], "failed")
        self.assertIn("unusual-traffic", job["Error"])

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
        self.store.write_csv(jid, [jobs.to_row(place(1), "k")])
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

    def test_page_and_docs(self):
        code, body, r = self.call("GET", "/")
        csp = r.getheader("Content-Security-Policy")
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        self.assertIn(f'<script nonce="{nonce}">', body.decode())
        self.assertIn("github.com/Mahanaicoach/google-maps-scraper-kit", body.decode())
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotRegex(body.decode(), r"\{[a-z_]+\}")  # every placeholder filled
        code, body, _ = self.call("GET", "/api/docs")
        self.assertEqual(json.loads(body)["paths"]["/api/v1/jobs"]["post"]["requestBody"]["content"]["application/json"]
                         ["schema"]["required"], ["name", "keywords", "lang", "lat", "lon", "max_time"])


class ApiKey(Server):
    key = "s3cret"

    def test_key(self):
        self.assertEqual(self.call("GET", "/api/v1/jobs")[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/jobs", headers={"X-API-Key": "wrong"})[0], 401)
        self.assertEqual(self.call("GET", "/api/v1/jobs", headers={"X-API-Key": "s3cret"})[0], 200)
        self.assertEqual(self.call("GET", "/")[0], 200)  # the page holds no data; it asks for the key


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
            scrape._local_docker = lambda: False  # never touch a real Docker engine from a test
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
