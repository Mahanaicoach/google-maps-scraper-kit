#!/usr/bin/env python3
"""Offline tests for scripts/scrape.py (standard library only, no network).
Run: python3 scripts/test_scrape.py"""
import csv, importlib.util, io, json, os, sys, tempfile, threading, unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "scrape", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape.py"))
scrape = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrape)
scrape.ENGINE_CONTAINER = "kit-tests-no-such-container"  # never read or restart a real engine

BBOX = ["30.0", "30.5", "-98.0", "-97.5"]  # Nominatim order: south, north, west, east


class GridCells(unittest.TestCase):
    def test_count_centers_radius(self):
        cells = scrape.grid_cells(BBOX, 3)
        self.assertEqual(len(cells), 9)
        for c in cells:
            self.assertTrue(30.0 < float(c["lat"]) < 30.5)
            self.assertTrue(-98.0 < float(c["lon"]) < -97.5)
            self.assertGreaterEqual(c["radius"], 1000)
            self.assertEqual(len(c["lat"].split(".")[1]), 6)
            self.assertEqual(c["zoom"], 13)  # ~16 km tiles: city-district view, not a 3 km block

    def test_zoom_follows_tile_size(self):
        self.assertEqual(scrape.grid_cells(BBOX, 1)[0]["zoom"], 11)                # ~48 km: clamped
        self.assertEqual(scrape.grid_cells(["0", "0.001", "0", "0.001"], 1)[0]["zoom"], 17)  # 110 m: clamped

    def test_one_cell_is_bbox_center(self):
        (c,) = scrape.grid_cells(BBOX, 1)
        self.assertEqual((c["lat"], c["lon"]), ("30.250000", "-97.750000"))
        self.assertGreater(c["radius"], 30000)  # ~0.5 x 0.5 degree box

    def test_tiny_box_floors_at_1000(self):
        self.assertEqual(scrape.grid_cells(["0", "0.001", "0", "0.001"], 2)[0]["radius"], 1000)


class Dedupe(unittest.TestCase):
    def test_key_order(self):
        rows = [
            {"place_id": "P1", "cid": "C1", "title": "a"},
            {"place_id": "P1", "cid": "C9", "title": "dup place_id"},
            {"cid": "C2", "link": "L1"},
            {"cid": "C2", "link": "L9", "title": "dup cid"},
            {"link": "L3"},
            {"link": "L3", "title": "dup link"},
            {"title": "Joe's", "address": "1 Main"},
            {"title": "Joe's", "address": "1 Main", "phone": "dup title+address"},
            {"title": "Joe's", "address": "2 Main"},
        ]
        out = scrape.dedupe(rows)
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0]["title"], "a")  # first occurrence wins
        self.assertNotIn("dup", json.dumps(out))


class InBbox(unittest.TestCase):
    def test_margin_and_missing(self):
        self.assertTrue(scrape.in_bbox({"latitude": "30.2", "longitude": "-97.7"}, BBOX))
        self.assertTrue(scrape.in_bbox({"latitude": "30.54", "longitude": "-97.7"}, BBOX))   # inside 10%
        self.assertFalse(scrape.in_bbox({"latitude": "30.56", "longitude": "-97.7"}, BBOX))  # past 10%
        self.assertFalse(scrape.in_bbox({"latitude": "30.2", "longitude": "-98.1"}, BBOX))
        self.assertTrue(scrape.in_bbox({"latitude": "", "longitude": ""}, BBOX))
        self.assertTrue(scrape.in_bbox({"latitude": "n/a"}, BBOX))
        self.assertTrue(scrape.in_bbox({}, BBOX))


class PollAttempts(unittest.TestCase):
    def test_follows_max_time(self):
        # The budget tracks max_time instead of the old fixed 120 attempts (960s), which
        # gave up on a batch the engine was still working through.
        self.assertEqual(scrape.poll_attempts(600), (600 + 120) // 8)      # 90
        self.assertEqual(scrape.poll_attempts(3600), (3600 + 120) // 8)    # 465, was 120
        self.assertGreater(scrape.poll_attempts(3600), 120)

    def test_floors_at_engine_minimum(self):
        # The engine runs a job for at least 180s however small max_time is, so waiting
        # only max_time would abandon it.
        for tiny in (1, 60, 179, 180):
            self.assertEqual(scrape.poll_attempts(tiny), (180 + 120) // 8)
        self.assertEqual(scrape.poll_attempts(0), (180 + 120) // 8)
        self.assertEqual(scrape.poll_attempts(None), (180 + 120) // 8)

    def test_ceiling_and_never_zero(self):
        self.assertEqual(scrape.poll_attempts(10 ** 9), 7200 // 8)         # clamped at 2h
        self.assertGreaterEqual(scrape.poll_attempts(-5), 1)


class StuckJob(unittest.TestCase):
    """A job the engine never flips to "ok" (gosom/google-maps-scraper#346) keeps its written rows."""
    def run_stuck(self, csv_body, run_ended=False, attempts=2):
        polls, deleted = [], []
        def fake_req(method, path, body=None):
            if method == "POST":
                return 201, b'{"id":"stuck"}'
            if method == "DELETE":
                deleted.append(path)
                return 200, b""
            if path.endswith("/download"):
                return 200, csv_body
            polls.append(path)
            return 200, b'{"Status":"working"}'
        with mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep"), \
             mock.patch.object(scrape, "poll_attempts", return_value=attempts), \
             mock.patch.object(scrape, "engine_run_ended", return_value=run_ended), \
             mock.patch.object(scrape, "restart_engine", return_value=True) as restart, \
             redirect_stdout(io.StringIO()):
            job_id, rows = scrape.run_job({"max_time": 1})
        return rows, len(polls), restart.called, deleted

    def test_keeps_written_rows(self):
        rows, _, restarted, deleted = self.run_stuck(b"title,phone\nA,1\nB,2\n")
        self.assertEqual([r["title"] for r in rows], ["A", "B"])
        self.assertFalse(restarted)
        self.assertEqual(deleted, [])  # the budget ran out, but the job may still be running

    def test_no_rows_still_errors(self):
        with self.assertRaises(RuntimeError):
            self.run_stuck(b"")

    def test_ended_run_recovered_in_seconds_and_engine_restarted(self):
        # The engine log says the run ended and the row count has stopped moving for 3 polls: stop
        # after 4 polls (about 32 s) instead of the whole budget, keep the rows, restart the engine.
        rows, polls, restarted, deleted = self.run_stuck(b"title\nA\n", run_ended=True, attempts=90)
        self.assertEqual([r["title"] for r in rows], ["A"])
        self.assertEqual(polls, 4)
        self.assertTrue(restarted)
        self.assertEqual(deleted, ["/api/v1/jobs/stuck"])  # no longer a "working" ghost in the job list

    def test_remote_engine_is_never_restarted(self):
        with mock.patch.object(scrape, "BASE", "http://scraper.example:8080"):
            self.assertFalse(scrape.engine_run_ended("2026-01-01T00:00:00Z"))
            self.assertFalse(scrape.engine_idle())
            self.assertFalse(scrape.restart_engine())


class PendingAndFailed(unittest.TestCase):
    def run_statuses(self, statuses, idle=True):
        """Drive run_job through a status sequence; count job creations and engine restarts."""
        posts, deleted, it = [], [], iter(statuses)
        def fake_req(method, path, body=None):
            if method == "POST":
                posts.append(body)
                return 201, b'{"id":"j%d"}' % len(posts)
            if method == "DELETE":
                deleted.append(path)
                return 200, b""
            if path.endswith("/download"):
                return 200, b"title\nA\n"
            return 200, json.dumps({"Status": next(it)}).encode()
        with mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep") as slept, \
             mock.patch.object(scrape, "poll_attempts", return_value=50), \
             mock.patch.object(scrape, "engine_run_ended", return_value=False), \
             mock.patch.object(scrape, "engine_idle", return_value=idle), \
             mock.patch.object(scrape, "restart_engine", return_value=True) as restart, \
             redirect_stdout(io.StringIO()):
            job_id, rows = scrape.run_job({"max_time": 1})
        return job_id, rows, len(posts), restart.call_count, slept, deleted

    def test_engine_error_reads_the_failed_job_line(self):
        log = mock.Mock(returncode=0, stdout="x\n2026/10/30 22:38:59 error scraping job j9: failed to start auth proxy for socks5://h:1: username and password are required\n", stderr="")
        with mock.patch.object(scrape, "_local_docker", return_value=True), \
             mock.patch.object(scrape.subprocess, "run", return_value=log):
            self.assertTrue(scrape.engine_error("j9").startswith("failed to start auth proxy"))
            self.assertEqual(scrape.engine_error("other"), "")

    def test_pending_on_idle_engine_restarts_once(self):
        # An earlier web UI job wedged the engine: our job never leaves "pending". Delete it, restart,
        # create it again, and never loop on a second pending streak.
        statuses = ["pending"] * scrape.PENDING_RESTART_AFTER + ["pending"] * (scrape.PENDING_RESTART_AFTER + 2) + ["working", "ok"]
        job_id, rows, posts, restarts, _, deleted = self.run_statuses(statuses)
        self.assertEqual((job_id, posts, restarts, deleted), ("j2", 2, 1, ["/api/v1/jobs/j1"]))
        self.assertEqual([r["title"] for r in rows], ["A"])

    def test_pending_on_busy_engine_waits(self):
        # The engine is still running someone else's job: pending is normal, never restart.
        statuses = ["pending"] * (scrape.PENDING_RESTART_AFTER + 3) + ["ok"]
        _, _, posts, restarts, _, _ = self.run_statuses(statuses, idle=False)
        self.assertEqual((posts, restarts), (1, 0))

    def test_bad_proxy_fails_at_once_with_the_engine_reason(self):
        reason = "failed to start auth proxy for socks5://127.0.0.1:1: username and password are required"
        with mock.patch.object(scrape, "engine_error", return_value=reason), \
             self.assertRaises(RuntimeError) as cm:
            self.run_statuses(["failed"])
        self.assertIn("username and password", str(cm.exception))
        self.assertIn("user:pass", str(cm.exception))

    def test_dead_working_job_is_reported_from_the_engine_log(self):
        # The engine logged "error scraping job", left the job on "working" with no rows and never
        # flipped it: delete it, restart the engine, tell the user the engine's reason.
        reason = "failed to start auth proxy for socks5://h:1: username and password are required"
        deletes = []
        def fake_req(method, path, body=None):
            if method == "POST":
                return 201, b'{"id":"j1"}'
            if method == "DELETE":
                deletes.append(path)
                return 200, b""
            if path.endswith("/download"):
                return 200, b"title\n"  # header only: zero rows
            return 200, b'{"Status":"working"}'
        with mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep"), \
             mock.patch.object(scrape, "poll_attempts", return_value=50), \
             mock.patch.object(scrape, "engine_run_ended", return_value=False), \
             mock.patch.object(scrape, "engine_error", return_value=reason), \
             mock.patch.object(scrape, "restart_engine", return_value=True) as restart, \
             redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError) as cm:
            scrape.run_job({"max_time": 1})
        self.assertIn("username and password", str(cm.exception))
        self.assertEqual((deletes, restart.call_count), (["/api/v1/jobs/j1"], 1))

    def test_failed_job_is_retried_once(self):
        job_id, rows, posts, _, slept, _ = self.run_statuses(["failed", "working", "ok"])
        self.assertEqual((job_id, posts), ("j2", 2))
        slept.assert_any_call(scrape.FAILED_RETRY_WAIT)
        self.assertEqual([r["title"] for r in rows], ["A"])
        with self.assertRaises(RuntimeError):
            self.run_statuses(["failed", "failed"])

    def test_restart_waits_until_engine_answers(self):
        # Right after `docker restart` the proxy answers 502 for a few seconds; the next grid tile
        # used to hit that and fail. restart_engine must only return once the API answers.
        answers = iter([OSError("502"), OSError("502"), (200, b"null")])
        def fake_req(method, path, body=None):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        done = mock.Mock(returncode=0)
        with mock.patch.object(scrape, "_local_docker", return_value=True), \
             mock.patch.object(scrape.subprocess, "run", return_value=done), \
             mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep") as slept:
            self.assertTrue(scrape.restart_engine())
        self.assertEqual(slept.call_count, 2)


class ExtractEmails(unittest.TestCase):
    def test_mailto_regex_junk_preference_cap(self):
        html = """
          <a href="mailto:Owner@Gmail.com?subject=Hi">Mail</a>
          <a href="mailto:INFO@acmeplumbing.com">info</a>
          Write to sales@acmeplumbing.com or info@acmeplumbing.com.
          <img src="logo@2x.png"> hello@example.com x@sentry.io a@b.wixpress.com
          name@company.org bob@wordpress.com jane@outside.net
        """
        out = scrape.extract_emails(html, "www.acmeplumbing.com")
        self.assertEqual(out, ["info@acmeplumbing.com", "sales@acmeplumbing.com", "owner@gmail.com"])

    def test_empty(self):
        self.assertEqual(scrape.extract_emails("", "x.com"), [])
        self.assertEqual(scrape.extract_emails("no emails here", "x.com"), [])

    def test_entities_and_obfuscation(self):
        html = "Mail info&#64;acme.com, jane [at] acme [dot] com or bob (at) acme dot co"
        self.assertEqual(scrape.extract_emails(html, "acme.com"),
                         ["info@acme.com", "jane@acme.com", "bob@acme.co"])

    def test_prose_is_not_an_email(self):
        self.assertEqual(scrape.extract_emails("Meet us at noon dot com, look at me dot org", "x.com"), [])


class PublicHostGuard(unittest.TestCase):
    def test_private_addresses_blocked(self):
        for host in ("localhost", "127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", ""):
            self.assertFalse(scrape._is_public_host(host), host)
        self.assertTrue(scrape._is_public_host("8.8.8.8"))

    def test_fetch_refuses_local_engine(self):
        with mock.patch.object(scrape._OPENER, "open") as opened:
            self.assertEqual(scrape._fetch_html("http://localhost:8080/api/v1/jobs"), "")
        opened.assert_not_called()

    def test_redirect_to_private_address_refused(self):
        handler = scrape._PublicRedirectsOnly()
        req = scrape.urllib.request.Request("http://8.8.8.8/")
        self.assertIsNone(handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/admin"))

    @unittest.skipIf(scrape.urllib.request.getproxies(), "a proxy in the environment owns the connect")
    def test_rebinding_caught_at_connect(self):
        # DNS rebinding: the name check saw a public address, the connect lands on 127.0.0.1.
        # No request may reach the local server.
        hits = []
        class Local(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200); self.end_headers(); self.wfile.write(b"secret")
            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Local)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(scrape, "_is_public_host", return_value=True):
                self.assertEqual(scrape._fetch_html(f"http://127.0.0.1:{srv.server_port}/"), "")
                with self.assertRaises(OSError):
                    scrape._public_connection(("127.0.0.1", srv.server_port))
        finally:
            srv.shutdown(); srv.server_close()
        self.assertEqual(hits, [])


class TechSignals(unittest.TestCase):
    def test_detects_pixel_platform_viewport(self):
        html = ('<script src="https://connect.facebook.net/en_US/fbevents.js"></script>'
                '<meta name="viewport" content="width=device-width"><link href="/wp-content/a.css">'
                "<script>gtag('config','AW-123456789')</script>")
        self.assertEqual(scrape._find_tech(html), {"fb_pixel": "yes", "google_ads_tag": "yes",
                         "analytics": "yes", "bing_ads": "no", "linkedin_insight": "no",
                         "tiktok_pixel": "no", "cms": "wordpress", "mobile_friendly": "yes"})
        more = '<script src="https://bat.bing.com/bat.js"></script><script>ttq.load("X"); _linkedin_partner_id="1"</script>'
        self.assertEqual({k: v for k, v in scrape._find_tech(more).items() if v == "yes"},
                         {"bing_ads": "yes", "linkedin_insight": "yes", "tiktok_pixel": "yes"})

    def test_more_socials(self):
        html = ('<a href="https://www.tiktok.com/@acme.shop">t</a> <a href="https://youtube.com/@AcmeTV">y</a>'
                ' <a href="https://x.com/acme_inc">x</a> <a href="https://twitter.com/intent/tweet">no</a>')
        out = scrape._find_socials(html)
        self.assertEqual((out["tiktok"], out["youtube"], out["x"]),
                         ("https://www.tiktok.com/@acme.shop", "https://youtube.com/@AcmeTV", "https://x.com/acme_inc"))
        self.assertEqual(scrape._find_socials('<a href="https://twitter.com/intent/tweet">no</a>')["x"], "")

    def test_unreachable_site_is_unknown_not_no(self):
        self.assertEqual(set(scrape._find_tech("").values()), {""})

    def test_intent_score(self):
        hot = {"fb_pixel": "yes", "google_ads_tag": "yes", "analytics": "yes",
               "review_count": "1,204", "review_rating": "4.6", "emails": "a@b.com"}
        self.assertEqual(scrape.intent_score(hot), 100)
        self.assertEqual(scrape.intent_score({}), 0)
        self.assertEqual(scrape.intent_score({"review_count": "12", "review_rating": "n/a"}), 8)


class EmailCrawl(unittest.TestCase):
    def test_template_junk_dropped(self):
        # Placeholders and a JSON-escaped prefix seen on real Austin coffee-shop sites.
        html = ("user@domain.com example@gmail.com hello@funkysolcoffee.com "
                "\\u003eorders@desnudo.com orders@desnudo.com")
        self.assertEqual(scrape.extract_emails(html, "funkysolcoffee.com"),
                         ["hello@funkysolcoffee.com", "orders@desnudo.com"])
        # Seen in a new-user test run: an image name and a site-builder placeholder.
        self.assertEqual(scrape.extract_emails("hero@2x-1.avif info@mysite.com", "x.com"), [])

    def test_homepage_only_skips_contact_pages(self):
        rows = [{"website": "http://acme.com", "emails": ""}]
        home = '<a href="/contact">Contact</a>'
        with mock.patch.object(scrape, "_fetch_cached", return_value=home), \
             mock.patch.object(scrape, "_fetch_html", return_value="sales@acme.com") as pages, \
             redirect_stdout(io.StringIO()):
            scrape.enrich_emails(rows, deep=False)
            pages.assert_not_called()
            self.assertEqual(rows[0]["emails"], "")
            scrape.enrich_emails(rows, deep=True)
        self.assertEqual(rows[0]["emails"], "sales@acme.com")

    def test_engine_never_asked_for_emails(self):
        sent = []
        def fake_run_job(body):
            sent.append(body)
            return "job-x", [_row("A")]
        argv = ["scrape.py", "dentists in Austin TX", "30.2", "-97.7", "--no-email",
                "--out", os.path.join(tempfile.mkdtemp(), "leads.csv")]
        with mock.patch.object(scrape, "req", return_value=(200, b"null")), \
             mock.patch.object(scrape, "run_job", fake_run_job), \
             mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
            scrape.main()
        self.assertIs(sent[0]["email"], False)
        self.assertEqual(sent[0]["name"], "dentists in Austin TX")  # the web UI lists this name


class IsRealEmail(unittest.TestCase):
    def test_placeholders_and_vendors(self):
        for e in ("user@domain.com", "example@gmail.com", "youremail@yourdomain.com",
                  "name@company.org", "a@b.wixpress.com", "logo@2x.png",
                  "support@linktr.ee", "help@sites.officite.com"):  # seen in audit runs
            self.assertFalse(scrape.is_real_email(e), e)
        for e in ("info@acmeplumbing.com", "owner@gmail.com", "hello@funkysol.co.uk"):
            self.assertTrue(scrape.is_real_email(e), e)


class CleanWebsite(unittest.TestCase):
    def test_strips_tracking_keeps_real_params(self):
        self.assertEqual(scrape.clean_website("https://a.com/x/?utm_source=gbp&utm_medium=organic#top"),
                         "https://a.com/x/")
        self.assertEqual(scrape.clean_website("https://www.hpb.com/store?storeid=HPB-005&utm_campaign=x"),
                         "https://www.hpb.com/store?storeid=HPB-005")
        self.assertEqual(scrape.clean_website("http://plain.com/"), "http://plain.com/")
        self.assertEqual(scrape.clean_website(""), "")
        self.assertEqual(scrape.clean_website("http://x.com/?a=1;b=2#top"), "http://x.com/?a=1;b=2")  # never re-encoded
        self.assertEqual(scrape.clean_website("http://x.com/#/store/5"), "http://x.com/#/store/5")  # hash routing kept


class KeywordsFile(unittest.TestCase):
    def test_missing_file_is_a_clean_error(self):
        a = mock.Mock(keyword=None, also=[], keywords_file="no-such-file.txt")
        with self.assertRaises(SystemExit) as cm:
            scrape.collect_keywords(a)
        self.assertIn("Cannot read --keywords-file", str(cm.exception))


class ContactLinks(unittest.TestCase):
    def test_discovers_same_site_only(self):
        html = """<a href="/contact-us#form">Get in touch</a>
                  <a href="https://www.acme.com/team">About us</a>
                  <a href="https://facebook.com/acme/about">FB</a>
                  <a href="mailto:a@acme.com">contact</a>
                  <a href="/services">Services</a>"""
        out = scrape.contact_links(html, "https://acme.com")
        self.assertEqual(out[:2], ["https://acme.com/contact-us", "https://www.acme.com/team"])
        self.assertEqual(len(out), 3)
        self.assertEqual(out[2], "https://acme.com/contact")  # first fixed fallback
        self.assertFalse(any("facebook" in u or "mailto" in u for u in out))

    def test_fallbacks_and_max(self):
        self.assertEqual(scrape.contact_links("", "acme.com"),
                         ["http://acme.com/contact", "http://acme.com/contact-us", "http://acme.com/about"])
        many = "".join(f'<a href="/contact{i}">x</a>' for i in range(6))
        self.assertEqual(len(scrape.contact_links(many, "http://acme.com")), 3)


# ── End to end against a mock engine ──
FIELDS = ["input_id", "link", "title", "category", "address", "website", "phone", "review_count",
          "review_rating", "latitude", "longitude", "cid", "place_id", "emails"]

def _row(pid, lat="30.2"):
    return {"title": f"Biz {pid}", "place_id": pid, "cid": "c" + pid, "latitude": lat, "website": f"http://{pid.lower()}.biz",
            "longitude": "-97.7", "phone": "+1 555", "emails": f"{pid.lower()}@biz.net"}

CELL_ROWS = {"job-1": [_row("A"), _row("B"), _row("C")],
             "job-2": [_row("C"), _row("D")],
             "job-3": None, "job-4": None,  # this cell fails, and so does its one retry
             "job-5": [_row("A"), _row("E"), _row("F", lat="40.0")]}  # F is outside the city box


class MockEngine(BaseHTTPRequestHandler):
    created, polls = 0, {}

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        MockEngine.created += 1
        self._send(201, json.dumps({"id": f"job-{MockEngine.created}"}))

    def do_GET(self):
        parts = self.path.strip("/").split("/")  # api v1 jobs [id [download]]
        if len(parts) == 3:
            return self._send(200, "[]")
        jid = parts[3]
        if len(parts) == 5:
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=FIELDS, restval="")
            w.writeheader(); w.writerows(CELL_ROWS[jid])
            return self._send(200, buf.getvalue(), "text/csv")
        n = MockEngine.polls[jid] = MockEngine.polls.get(jid, 0) + 1
        status = "failed" if CELL_ROWS[jid] is None else ("ok" if n >= 2 else "working")
        self._send(200, json.dumps({"Status": status}))


class EndToEnd(unittest.TestCase):
    def test_grid_run(self):
        MockEngine.created, MockEngine.polls = 0, {}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), MockEngine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        out = os.path.join(tempfile.mkdtemp(), "leads.csv")
        argv = ["scrape.py", "plumbers in Austin TX", "--city", "Austin, TX",
                "--grid", "2", "--no-deep-emails", "--out", out]
        log = io.StringIO()
        try:
            with mock.patch.object(scrape, "BASE", f"http://127.0.0.1:{srv.server_port}"), \
                 mock.patch.object(scrape, "geocode", return_value=("30.25", "-97.75", BBOX)), \
                 mock.patch.object(scrape.time, "sleep"), \
                 mock.patch.object(sys, "argv", argv), redirect_stdout(log):
                scrape.main()
        finally:
            srv.shutdown(); srv.server_close()
        text = log.getvalue()
        self.assertIn("cell 4/4: 3 rows, 6 unique so far", text)
        self.assertIn("cell 3/4 skipped", text)
        self.assertIn("Incomplete: 1 of 4 tiles failed", text)
        self.assertIn("dropped 1 rows outside the city box", text)
        with open(out, newline="", encoding="utf-8-sig") as f:  # the kit writes a BOM for Excel
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], scrape.LEAD)
        self.assertEqual([r[0] for r in rows[1:]], ["Biz A", "Biz B", "Biz C", "Biz D", "Biz E"])

    def test_two_cities_filters_exclude_limit(self):
        # --city twice = two jobs into one deduped file; --exclude drops a place from an earlier lean
        # CSV (title+address only); --min-reviews and --limit trim the rest.
        MockEngine.created, MockEngine.polls = 0, {}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), MockEngine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        d = tempfile.mkdtemp()
        out, earlier = os.path.join(d, "leads.csv"), os.path.join(d, "earlier.csv")
        with open(earlier, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=scrape.LEAD); w.writeheader()
            w.writerow({"title": "Biz A", "address": ""})
        argv = ["scrape.py", "plumbers", "--city", "Austin, TX", "--city", "Round Rock, TX", "--no-email",
                "--exclude", earlier, "--limit", "2", "--out", out, "--socials", "--fields", "title,phone"]
        log = io.StringIO()
        try:
            with mock.patch.object(scrape, "BASE", f"http://127.0.0.1:{srv.server_port}"), \
                 mock.patch.object(scrape, "geocode", return_value=("30.25", "-97.75")) as geo, \
                 mock.patch.object(scrape, "_fetch_cached", return_value='<a href="https://instagram.com/biz_b">ig</a>'), \
                 mock.patch.object(scrape.time, "sleep"), \
                 mock.patch.object(sys, "argv", argv), redirect_stdout(log):
                scrape.main()
        finally:
            srv.shutdown(); srv.server_close()
        self.assertEqual(geo.call_count, 2)
        self.assertIn("kept 3 of 4 rows", log.getvalue())  # A, B, C + C, D -> 4 unique, minus excluded A
        with open(out, newline="", encoding="utf-8-sig") as f:  # the kit writes a BOM for Excel
            rows = list(csv.DictReader(f))
        self.assertEqual([r["title"] for r in rows], ["Biz B", "Biz C"])  # --limit 2
        # --socials works even though --fields dropped the website column: enrichment reads the raw rows
        self.assertEqual(rows[0]["instagram"], "https://instagram.com/biz_b")


class Filters(unittest.TestCase):
    def test_keep_row(self):
        a = mock.Mock(min_rating=4.5, min_reviews=10, no_website=False, has_phone=True)
        self.assertTrue(scrape.keep_row({"review_rating": "4.6", "review_count": "1,204", "phone": "+1"}, a))
        self.assertFalse(scrape.keep_row({"review_rating": "4.4", "review_count": "50", "phone": "+1"}, a))
        self.assertFalse(scrape.keep_row({"review_rating": "4.9", "review_count": "9", "phone": "+1"}, a))
        self.assertFalse(scrape.keep_row({"review_rating": "4.9", "review_count": "99", "phone": ""}, a))
        self.assertFalse(scrape.keep_row({"review_rating": "", "review_count": "99", "phone": "+1"}, a))  # unrated
        nw = mock.Mock(min_rating=None, min_reviews=None, no_website=True, has_phone=False)
        self.assertTrue(scrape.keep_row({"website": ""}, nw))
        self.assertFalse(scrape.keep_row({"website": "http://x.com"}, nw))

    def test_chain_branches_are_not_merged(self):
        # Two branches of a chain share the website (and sometimes the phone): distinct place_ids
        # must both survive, and only an exclusion list may match on name+phone or name+website.
        branches = [{"place_id": "P1", "title": "Anytime Fitness", "address": "1 A St", "website": "http://af.com", "phone": "+1 1"},
                    {"place_id": "P2", "title": "Anytime Fitness", "address": "9 B St", "website": "http://af.com", "phone": "+1 1"}]
        self.assertEqual(len(scrape.dedupe(branches)), 2)
        self.assertEqual(len(scrape.dedupe(branches, exclude=[{"title": "Anytime Fitness", "address": "9 B St"}])), 1)

    def test_exclude_matches_lean_rows(self):
        new = [{"place_id": "P1", "title": "Joe's", "address": "1 Main"}, {"place_id": "P2", "title": "Ann's"}]
        self.assertEqual(scrape.dedupe(new, exclude=[{"title": "Joe's", "address": "1 Main"}]), new[1:])
        self.assertEqual(scrape.dedupe(new, exclude=[{"place_id": "P2"}]), new[:1])
        # A --fields file without address still matches on name plus phone or website.
        new2 = [{"place_id": "P3", "title": "Ann's", "address": "2 Main", "phone": "+1 555", "website": "http://ann.com"}]
        self.assertEqual(scrape.dedupe(new2, exclude=[{"title": "Ann's", "phone": "+1 555"}]), [])
        self.assertEqual(scrape.dedupe(new2, exclude=[{"title": "Ann's", "website": "http://ann.com"}]), [])
        self.assertEqual(scrape.dedupe(new2, exclude=[{"title": "Ann's"}]), new2)  # name alone is not enough

    def test_load_rows_csv_and_json(self):
        d = tempfile.mkdtemp()
        j = os.path.join(d, "a.json"); open(j, "w").write('[{"title": "A"}]')
        c = os.path.join(d, "a.csv"); open(c, "w", encoding="utf-8-sig").write("title,phone\nB,1\n")
        self.assertEqual(scrape.load_rows(j), [{"title": "A"}])
        self.assertEqual(scrape.load_rows(c), [{"title": "B", "phone": "1"}])  # BOM stripped, key is clean


class RegexSpeed(unittest.TestCase):
    def test_long_runs_are_linear(self):
        import time as _t
        blob = "a" * 100_000 + " contact: info@acme.com " + "b" * 100_000
        t0 = _t.perf_counter()
        self.assertEqual(scrape.extract_emails(blob, "acme.com"), ["info@acme.com"])
        self.assertLess(_t.perf_counter() - t0, 1.0)  # was about a minute per 60 KB run


class Resilience(unittest.TestCase):
    def test_get_retries_transient_errors(self):
        answers = iter([scrape.urllib.error.HTTPError("u", 502, "bad gateway", {}, None),
                        OSError("timed out"), (200, b"null")])
        def fake_req(method, path, body=None):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        with mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep") as slept:
            self.assertEqual(scrape._get("/api/v1/jobs"), (200, b"null"))
        self.assertEqual(slept.call_count, 2)
        with mock.patch.object(scrape, "req", side_effect=scrape.urllib.error.HTTPError("u", 404, "nf", {}, None)), \
             self.assertRaises(scrape.urllib.error.HTTPError):
            scrape._get("/api/v1/jobs/x")  # a 4xx is final, no retry

    def test_pending_restart_failure_raises_instead_of_polling_a_deleted_job(self):
        statuses = iter(["pending"] * 20)
        def fake_req(method, path, body=None):
            if method == "POST":
                return 201, b'{"id":"j1"}'
            if method == "DELETE":
                return 200, b""
            return 200, json.dumps({"Status": next(statuses)}).encode()
        with mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep"), \
             mock.patch.object(scrape, "poll_attempts", return_value=50), \
             mock.patch.object(scrape, "engine_run_ended", return_value=False), \
             mock.patch.object(scrape, "engine_idle", return_value=True), \
             mock.patch.object(scrape, "restart_engine", return_value=False), \
             redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError) as cm:
            scrape.run_job({"max_time": 1})
        self.assertIn("docker restart", str(cm.exception))

    def test_failing_docker_logs_is_not_idle(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="Error: No such container")
        with mock.patch.object(scrape, "_local_docker", return_value=True), \
             mock.patch.object(scrape.subprocess, "run", return_value=failed):
            self.assertFalse(scrape.engine_idle())
            self.assertFalse(scrape.engine_run_ended("2026-01-01T00:00:00Z"))


class VerifyEmails(unittest.TestCase):
    def test_mx_host_labels_and_drops_dead_domains(self):
        answers = {
            "gws.com":  {"Status": 0, "Answer": [{"type": 15, "data": "10 aspmx.l.google.com."}]},
            "m365.com": {"Status": 0, "Answer": [{"type": 15, "data": "0 m365-com.mail.protection.outlook.com."}]},
            "own.com":  {"Status": 0, "Answer": [{"type": 15, "data": "10 mail.own.com."}]},
            "nomx.com": {"Status": 0},
            "dead.com": {"Status": 3},
        }
        scrape.mx_host.cache_clear()
        rows = [{"emails": "a@gws.com, b@dead.com"}, {"emails": "c@m365.com"}, {"emails": "d@own.com"},
                {"emails": "e@nomx.com"}, {"emails": "f@dead.com"}, {"emails": ""}]
        with mock.patch.object(scrape, "_doh_mx", side_effect=lambda dom: answers[dom]), \
             redirect_stdout(io.StringIO()):
            scrape.verify_emails(rows)
        self.assertEqual([(r["emails"], r.get("email_host", "-")) for r in rows],
                         [("a@gws.com", "google_workspace"), ("c@m365.com", "microsoft_365"),
                          ("d@own.com", "other"), ("e@nomx.com", "none"), ("", ""), ("", "-")])
        scrape.mx_host.cache_clear()
        with mock.patch.object(scrape, "_doh_mx", side_effect=OSError("offline")):
            self.assertEqual(scrape.mx_host("x.com"), "")  # lookup failed: keep the address, no label


class HostileInput(unittest.TestCase):
    """A listing can carry a malformed website; nothing in the crawl may raise on it."""
    BAD = "http://[bad"

    def test_url_helpers_never_raise(self):
        self.assertEqual(scrape.clean_website(self.BAD), self.BAD)
        self.assertEqual(scrape._host(self.BAD), "")
        self.assertEqual(scrape.extract_emails("a@b.com", self.BAD), ["a@b.com"])
        self.assertEqual(scrape._fetch_html(self.BAD), "")
        links = scrape.contact_links('<a href="http://[x">contact</a><a href="/contact">c</a>', self.BAD)
        self.assertTrue(all(isinstance(u, str) for u in links))

    def test_one_bad_row_does_not_abort_the_run(self):
        rows = [{"website": "boom"}, {"website": "fine"}]
        def work(r):
            if r["website"] == "boom":
                raise ValueError("parser blew up")
            r["seen"] = "yes"
        log = io.StringIO()
        with redirect_stdout(log):
            scrape._each_site(rows, work, "x", workers=2)
        self.assertEqual(rows[1].get("seen"), "yes")
        self.assertIn("1 skipped after an error", log.getvalue())

    def test_exclude_file_must_be_a_list_of_rows(self):
        d = tempfile.mkdtemp()
        j = os.path.join(d, "job.json"); open(j, "w").write('{"job": {"keywords": ["x"]}}')
        with self.assertRaises(ValueError):
            scrape.load_rows(j)


class EnvFile(unittest.TestCase):
    def test_export_prefix_comment_quotes_and_trailing_slash(self):
        d = tempfile.mkdtemp(); os.mkdir(os.path.join(d, "scripts"))
        open(os.path.join(d, ".env"), "w").write(
            "export SCRAPER_PORT=8081 # my port\nSCRAPER_BASE_URL='http://localhost:8081/'\n# SCRAPER_API_KEY=nope\n")
        saved = {k: os.environ.pop(k, None) for k in scrape.ENV_KEYS}
        try:
            with mock.patch.object(scrape, "__file__", os.path.join(d, "scripts", "scrape.py")):
                scrape.load_env_file()
            self.assertEqual(os.environ.get("SCRAPER_PORT"), "8081")
            self.assertEqual(os.environ.get("SCRAPER_BASE_URL"), "http://localhost:8081/")
            self.assertIsNone(os.environ.get("SCRAPER_API_KEY"))
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_repeated_key_takes_the_last_value_like_compose(self):
        # Uncommenting .env.example's second-copy block leaves a second SCRAPER_PORT line below the
        # default one. Compose takes the last value, so the scripts must too, or they create jobs
        # on the other copy's engine.
        d = tempfile.mkdtemp(); os.mkdir(os.path.join(d, "scripts"))
        with open(os.path.join(d, ".env"), "w") as f:
            f.write("SCRAPER_PORT=8080\nCOMPOSE_PROJECT_NAME=x\nSCRAPER_PORT=8081\n")
        saved = {k: os.environ.pop(k, None) for k in scrape.ENV_KEYS}
        try:
            with mock.patch.object(scrape, "__file__", os.path.join(d, "scripts", "scrape.py")):
                scrape.load_env_file()
            self.assertEqual(os.environ.get("SCRAPER_PORT"), "8081")
            os.environ["SCRAPER_PORT"] = "9000"  # a real environment variable still beats the file
            with mock.patch.object(scrape, "__file__", os.path.join(d, "scripts", "scrape.py")):
                scrape.load_env_file()
            self.assertEqual(os.environ.get("SCRAPER_PORT"), "9000")
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_base_url_has_no_trailing_slash(self):
        # "http://host/" + "/api/..." would be "//api/...", which the proxy answers with a 307 that
        # urllib will not follow for a POST.
        with mock.patch.dict(os.environ, {"SCRAPER_BASE_URL": "http://localhost:8090/"}):
            self.assertEqual((os.environ.get("SCRAPER_BASE_URL") or "").rstrip("/"), "http://localhost:8090")
        self.assertFalse(scrape.BASE.endswith("/"))


class ArgumentBounds(unittest.TestCase):
    def run_main(self, *extra):
        argv = ["scrape.py", "x in y", "1", "2", *extra]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()), \
             self.assertRaises(SystemExit) as cm:
            scrape.main()
        return str(cm.exception)

    def test_nothing_listening_fails_in_ten_seconds_not_sixty(self):
        refused = scrape.urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        with mock.patch.object(scrape, "req", side_effect=refused), mock.patch.object(scrape.time, "sleep") as slept:
            self.assertIn("Docker Desktop", self.run_main())
        self.assertEqual(slept.call_count, 4)  # five tries, two seconds apart

    def test_has_email_needs_the_crawl(self):
        self.assertIn("--has-email", self.run_main("--has-email", "--no-email"))

    def test_rejects_values_the_engine_would_choke_on(self):
        self.assertIn("--depth", self.run_main("--depth", "0"))
        self.assertIn("--max-time", self.run_main("--max-time", "0"))  # the engine says "missing max time"
        self.assertIn("--limit", self.run_main("--limit", "-1"))
        bad_dir = os.path.join(tempfile.mkdtemp(), "a-file")
        open(bad_dir, "w").close()  # a file where the folder should be
        self.assertIn("--out", self.run_main("--out", os.path.join(bad_dir, "x", "leads.csv")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
