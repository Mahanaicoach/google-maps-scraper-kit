#!/usr/bin/env python3
"""Offline tests for scripts/scrape.py (standard library only, no network).
Run: python3 scripts/test_scrape.py"""
import csv, importlib.util, io, json, os, pathlib, sys, tempfile, threading, unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "scrape", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape.py"))
scrape = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrape)

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
        # The budget tracks max_time instead of a fixed number of attempts, which gave up on a
        # batch the engine was still working through.
        self.assertEqual(scrape.poll_attempts(600), (600 + 120) // scrape.POLL_EVERY)
        self.assertEqual(scrape.poll_attempts(3600), (3600 + 120) // scrape.POLL_EVERY)
        self.assertEqual(scrape.poll_attempts(1), (1 + 120) // scrape.POLL_EVERY)  # no 180 s floor any more
        self.assertEqual(scrape.poll_attempts(None), scrape.poll_attempts(1))

    def test_ceiling_and_never_zero(self):
        self.assertEqual(scrape.poll_attempts(10 ** 9), 7200 // scrape.POLL_EVERY)  # clamped at 2h
        self.assertGreaterEqual(scrape.poll_attempts(-5), 1)


def run_jobs(jobs, attempts=50, downloads=None):
    """Drive scrape.run_job against a fake engine. `jobs` holds one list of (Status, Error) polls per
    job the script creates; `downloads` maps a job id to its CSV (404 when missing).
    -> (result or the RuntimeError, job count, the sleeps, the printed text)."""
    posts, it, out = [], None, io.StringIO()
    downloads = downloads or {}
    def fake_req(method, path, body=None):
        nonlocal it
        if method == "POST":
            posts.append(body)
            it = iter(jobs[len(posts) - 1])
            return 201, b'{"id":"j%d"}' % len(posts)
        jid = path.split("/")[4]
        if path.endswith("/download"):
            if jid not in downloads:
                raise scrape.urllib.error.HTTPError(path, 404, "no results", {}, io.BytesIO(b""))
            return 200, downloads[jid]
        status, error = next(it)
        return 200, json.dumps({"ID": jid, "Status": status, "Error": error}).encode()
    with mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep") as slept, \
         mock.patch.object(scrape, "poll_attempts", return_value=attempts), redirect_stdout(out):
        try:
            result = scrape.run_job({"max_time": 1})
        except RuntimeError as e:
            result = e
    return result, len(posts), slept, out.getvalue()


class JobOutcomes(unittest.TestCase):
    """What the script does with each Status and Error the engine reports."""
    CSV = b"title,phone\nA,1\nB,2\n"

    def test_ok(self):
        (jid, rows), posts, _, text = run_jobs([[("pending", ""), ("working", ""), ("ok", "")]],
                                               downloads={"j1": self.CSV})
        self.assertEqual((jid, [r["title"] for r in rows], posts), ("j1", ["A", "B"], 1))
        self.assertNotIn("engine says", text)

    def test_ok_with_a_note_says_why_it_stopped_early(self):
        note = "stopped at max_time; the rows found so far are kept"
        err, posts, _, _ = run_jobs([[("ok", note)]], downloads={"j1": self.CSV})
        self.assertIsInstance(err, scrape.Partial)  # the rows count, the job is incomplete
        self.assertEqual((len(err.rows), posts), (2, 1))
        self.assertIn(f"The engine says: {note}", str(err))

    def test_ctrl_c_keeps_the_job_id_for_its_message(self):
        def fake_req(method, path, body=None):
            if method == "POST":
                return 201, b'{"id":"j7"}'
            raise KeyboardInterrupt
        with mock.patch.object(scrape, "req", fake_req), redirect_stdout(io.StringIO()), \
             self.assertRaises(KeyboardInterrupt):
            scrape.run_job({"max_time": 1})
        self.assertEqual(scrape.CURRENT_JOB, "j7")  # __main__ prints its DELETE command
        scrape.CURRENT_JOB = None

    def test_failed_job_is_retried_once(self):
        reason = "Page.navigate: no reply within 30 s"
        (jid, rows), posts, slept, text = run_jobs([[("failed", reason)], [("working", ""), ("ok", "")]],
                                                   downloads={"j2": self.CSV})
        self.assertEqual((jid, posts), ("j2", 2))
        slept.assert_any_call(scrape.FAILED_RETRY_WAIT)
        self.assertIn(reason, text)
        err, posts, _, _ = run_jobs([[("failed", reason)], [("failed", reason)]])
        self.assertIsInstance(err, RuntimeError)
        self.assertEqual(posts, 2)
        self.assertIn("failed twice", str(err))
        self.assertIn(reason, str(err))

    def test_chrome_start_error_is_not_retried_or_blamed_on_google(self):
        for reason in ("Chrome exited at once (code 1): /usr/bin/chromium: Trace/breakpoint trap",
                       "Chrome cannot start: libnss3.so: cannot open shared object file\n  Install the libraries",
                       "No Chrome, Chromium, Edge or Brave found. Run `python3 scripts/engine.py start`"):
            err, posts, slept, text = run_jobs([[("failed", reason)]])
            self.assertIsInstance(err, RuntimeError, reason)
            self.assertEqual(posts, 1)
            slept.assert_not_called()
            self.assertIn("could not start Chrome", str(err))
            self.assertIn("scripts/engine.py restart", str(err))
            self.assertNotIn("throttl", (str(err) + text).lower())
            self.assertNotIn("rate-limit", (str(err) + text).lower())

    def test_proxy_error_is_not_retried(self):
        reason = "1 keyword(s) failed: navigate https://www.google.com/maps/search/x: net::ERR_PROXY_CONNECTION_FAILED"
        err, posts, _, _ = run_jobs([[("failed", reason)]])
        self.assertEqual(posts, 1)
        self.assertIn("proxy did not work", str(err))
        self.assertIn("ERR_PROXY_CONNECTION_FAILED", str(err))

    def test_keyword_naming_a_tunnel_is_not_a_proxy_error(self):
        # The engine's message holds the search URL, so the keyword is in it.
        reason = "navigate https://www.google.com/maps/search/tunnel%20car%20wash%20socks: net::ERR_INTERNET_DISCONNECTED"
        err, posts, _, _ = run_jobs([[("failed", reason)], [("failed", reason)]])
        self.assertEqual(posts, 2)  # retried like any other failure
        self.assertNotIn("proxy", str(err))

    def test_google_block_is_not_retried(self):
        reason = ("Google answered with its unusual-traffic page: this IP is rate-limited for now. "
                  "Wait, or add proxies")
        err, posts, slept, _ = run_jobs([[("failed", reason)]])
        self.assertIsInstance(err, scrape.Blocked)
        self.assertEqual(posts, 1)
        slept.assert_not_called()
        self.assertIn("unusual-traffic page", str(err))
        # An ok job that stopped on the block keeps its rows, and still says stop.
        note = "Google answered with its unusual-traffic page: this IP is rate-limited for now. " \
               "Wait, or add proxies. The rows found before that are kept"
        err, _, _, _ = run_jobs([[("ok", note)]], downloads={"j1": self.CSV})
        self.assertIsInstance(err, scrape.Blocked)
        self.assertEqual((err.job_id, len(err.rows)), ("j1", 2))

    def test_failed_part_way_keeps_its_rows(self):
        # The engine was stopped while the job ran: it marks the job failed and keeps the rows.
        reason = "the engine stopped while this job ran; its rows so far are kept"
        err, posts, _, _ = run_jobs([[("working", ""), ("failed", reason)]], downloads={"j1": self.CSV})
        self.assertIsInstance(err, scrape.Partial)
        self.assertEqual((err.job_id, len(err.rows), posts), ("j1", 2, 1))
        self.assertIn(reason, str(err))

    def test_engine_gone_mid_poll_is_a_clean_error(self):
        # Docker restarting the engine, a crash, or the job deleted from the web UI: a RuntimeError
        # the callers handle, never a traceback.
        gone = scrape.urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        def fake_req(method, path, body=None):
            if method == "POST":
                return 201, b'{"id":"j1"}'
            raise gone
        with mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep"), \
             redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError) as cm:
            scrape.run_job({"max_time": 1})
        self.assertIn("Lost the engine while job j1 ran", str(cm.exception))
        self.assertIsNone(scrape.CURRENT_JOB)
        with mock.patch.object(scrape, "req", side_effect=gone), self.assertRaises(RuntimeError) as cm:
            scrape.run_job({"max_time": 1})
        self.assertIn("Could not reach the engine", str(cm.exception))

    def test_budget_runs_out_keeps_written_rows(self):
        (jid, rows), posts, _, text = run_jobs([[("working", "")] * 3], attempts=3, downloads={"j1": self.CSV})
        self.assertEqual((jid, len(rows), posts), ("j1", 2, 1))
        self.assertIn("keeps running in the engine", text)

    def test_budget_runs_out_with_no_rows_is_a_clean_error(self):
        for jobs in ([[("pending", "")] * 3], [[("working", "")] * 3]):
            err, _, _, _ = run_jobs(jobs, attempts=3)  # the download answers 404: no rows yet
            self.assertIsInstance(err, RuntimeError)
            self.assertIn("Gave up waiting", str(err))


class StopsWhenBlocked(unittest.TestCase):
    """After Google's unusual-traffic page, no more tiles or cities are searched."""
    def run_main(self, argv, results):
        out = os.path.join(tempfile.mkdtemp(), "leads.csv")
        log = io.StringIO()
        with mock.patch.object(scrape, "req", return_value=(200, b"null")), \
             mock.patch.object(scrape, "run_job", side_effect=results) as run, \
             mock.patch.object(scrape, "geocode", side_effect=lambda place, with_bbox=False: ("30.25", "-97.75") + ((BBOX,) if with_bbox else ())), \
             mock.patch.object(sys, "argv", ["scrape.py", *argv, "--no-email", "--out", out]), redirect_stdout(log):
            try:
                scrape.main()
            except SystemExit as e:  # a run cut short by Google exits non-zero, after writing the file
                log.write(str(e))
        with open(out, newline="", encoding="utf-8-sig") as f:
            return run.call_count, [r["title"] for r in csv.DictReader(f)], log.getvalue()

    def test_grid_stops_at_the_blocked_cell(self):
        calls, titles, text = self.run_main(["plumbers", "--city", "Austin, TX", "--grid", "2"],
                                            [("job-1", [_row("A"), _row("B")]), scrape.Blocked("✗ Job failed.")])
        self.assertEqual((calls, titles), (2, ["Biz A", "Biz B"]))  # cells 3 and 4 never searched
        self.assertIn("Stopped at cell 2/4", text)
        self.assertIn("Incomplete: Google is rate-limiting this IP", text)

    def test_a_tile_that_failed_part_way_counts_as_incomplete(self):
        _, titles, text = self.run_main(["plumbers", "--city", "Austin, TX", "--grid", "2"],
                                        [scrape.Partial("⚠ part way", "job-1", [_row("A")])] +
                                        [("job-%d" % n, [_row("B")]) for n in (2, 3, 4)])
        self.assertEqual(titles, ["Biz A", "Biz B"])
        self.assertIn("Incomplete: 1 of 4 tiles or places failed", text)
        self.assertNotIn("rate-limiting", text)

    def test_a_single_job_cut_short_is_reported_incomplete(self):
        _, titles, text = self.run_main(["plumbers in Austin TX", "30.2", "-97.7"],
                                        [scrape.Partial("⚠ The engine says: stopped at max_time", "job-1", [_row("A")])])
        self.assertEqual(titles, ["Biz A"])
        self.assertIn("Incomplete: 1 of 1 tiles or places", text)

    def test_cities_stop_at_the_blocked_one(self):
        calls, titles, _ = self.run_main(["plumbers", "--city", "Austin, TX", "--city", "Waco, TX"],
                                         [scrape.Blocked("⚠ blocked", "job-1", [_row("A")]), ("job-2", [_row("B")])])
        self.assertEqual((calls, titles), (1, ["Biz A"]))


class NoDockerPaths(unittest.TestCase):
    def test_script_never_reads_or_restarts_a_container(self):
        # The old engine's workarounds read `docker logs` and ran `docker restart`, which could restart
        # another Docker engine on this machine while the script talked to an engine on another port.
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape.py"), encoding="utf-8") as f:
            src = f.read()
        for banned in ("subprocess", "docker logs", "docker restart", '"docker"', "SCRAPER_CONTAINER"):
            self.assertNotIn(banned, src)


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
            if CELL_ROWS[jid] is None:  # a failed tile wrote no rows
                return self._send(404, '{"message": "no results for this job (yet)"}')
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
        self.assertIn("Incomplete: 1 of 4 tiles or places failed", text)
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


class NothingLeft(unittest.TestCase):
    def test_filters_dropping_every_row_is_said_plainly(self):
        MockEngine.created, MockEngine.polls = 0, {}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), MockEngine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        out = os.path.join(tempfile.mkdtemp(), "leads.csv")
        argv = ["scrape.py", "plumbers in Austin TX", "30.2", "-97.7", "--no-email", "--min-rating", "5", "--out", out]
        log = io.StringIO()
        try:
            with mock.patch.object(scrape, "BASE", f"http://127.0.0.1:{srv.server_port}"), \
                 mock.patch.object(scrape.time, "sleep"), mock.patch.object(sys, "argv", argv), redirect_stdout(log):
                scrape.main()
        finally:
            srv.shutdown(); srv.server_close()
        self.assertIn("Nothing left: the filters and --exclude dropped all 3 rows", log.getvalue())
        self.assertNotIn("✓ Done", log.getvalue())

    def run_min_reviews(self, counts):
        """--min-reviews 10 over the mock engine's three rows, with these review_count values."""
        MockEngine.created, MockEngine.polls = 0, {}
        saved = CELL_ROWS["job-1"]
        CELL_ROWS["job-1"] = [dict(_row(p), review_count=c) for p, c in zip("ABC", counts)]
        srv = ThreadingHTTPServer(("127.0.0.1", 0), MockEngine)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        out = os.path.join(tempfile.mkdtemp(), "leads.csv")
        argv = ["scrape.py", "plumbers in Austin TX", "30.2", "-97.7", "--no-email", "--min-reviews", "10", "--out", out]
        log = io.StringIO()
        try:
            with mock.patch.object(scrape, "BASE", f"http://127.0.0.1:{srv.server_port}"), \
                 mock.patch.object(scrape.time, "sleep"), mock.patch.object(sys, "argv", argv), redirect_stdout(log):
                scrape.main()
        finally:
            srv.shutdown(); srv.server_close()
            CELL_ROWS["job-1"] = saved
        with open(out, newline="", encoding="utf-8-sig") as f:
            return [r["title"] for r in csv.DictReader(f)], log.getvalue()

    def test_min_reviews_warns_instead_of_dropping_every_row(self):
        # The engine leaves review_count empty, which would read as 0 and drop every row.
        titles, text = self.run_min_reviews(["", "", ""])
        self.assertEqual(titles, ["Biz A", "Biz B", "Biz C"])
        self.assertIn("--min-reviews ignored", text)

    def test_min_reviews_still_filters_real_counts(self):
        titles, text = self.run_min_reviews(["12", "3", "1,204"])
        self.assertEqual(titles, ["Biz A", "Biz C"])
        self.assertNotIn("--min-reviews ignored", text)


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
        j = os.path.join(d, "a.json"); pathlib.Path(j).write_text('[{"title": "A"}]')
        c = os.path.join(d, "a.csv"); pathlib.Path(c).write_text("title,phone\nB,1\n", encoding="utf-8-sig")
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
        # HTTPError owns a temp file; close both at the end or Python warns about it at exit.
        e502 = scrape.urllib.error.HTTPError("u", 502, "bad gateway", {}, io.BytesIO(b""))  # a body: close() needs one on 3.9
        e404 = scrape.urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b""))
        self.addCleanup(e502.close); self.addCleanup(e404.close)
        answers = iter([e502, OSError("timed out"), (200, b"null")])
        def fake_req(method, path, body=None):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        with mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep") as slept:
            self.assertEqual(scrape._get("/api/v1/jobs"), (200, b"null"))
        self.assertEqual(slept.call_count, 2)
        with mock.patch.object(scrape, "req", side_effect=e404), self.assertRaises(scrape.urllib.error.HTTPError):
            scrape._get("/api/v1/jobs/x")  # a 4xx is final, no retry


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
        j = os.path.join(d, "job.json"); pathlib.Path(j).write_text('{"job": {"keywords": ["x"]}}')
        with self.assertRaises(ValueError):
            scrape.load_rows(j)


class EnvFile(unittest.TestCase):
    def test_export_prefix_comment_quotes_and_trailing_slash(self):
        d = tempfile.mkdtemp(); os.mkdir(os.path.join(d, "scripts"))
        pathlib.Path(d, ".env").write_text(
            "SCRAPER_PORT=8080\nexport SCRAPER_PORT=8081 # my port\nSCRAPER_BASE_URL='http://localhost:8081/'\n# SCRAPER_API_KEY=nope\n")
        saved = {k: os.environ.pop(k, None) for k in scrape.ENV_KEYS}
        try:
            with mock.patch.object(scrape, "__file__", os.path.join(d, "scripts", "scrape.py")):
                scrape.load_env_file()
            self.assertEqual(os.environ.get("SCRAPER_PORT"), "8081")  # the last line wins, as in compose
            self.assertEqual(os.environ.get("SCRAPER_BASE_URL"), "http://localhost:8081/")
            self.assertIsNone(os.environ.get("SCRAPER_API_KEY"))
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
            self.assertIn("scripts/engine.py start", self.run_main())
        self.assertEqual(slept.call_count, 4)  # five tries, two seconds apart

    def test_has_email_needs_the_crawl(self):
        self.assertIn("--has-email", self.run_main("--has-email", "--no-email"))

    def test_rejects_values_the_engine_would_choke_on(self):
        self.assertIn("--country", self.run_main("--country", "usa"))
        self.assertIn("--depth", self.run_main("--depth", "0"))
        self.assertIn("--max-time", self.run_main("--max-time", "0"))  # the engine says "missing max time"
        self.assertIn("--limit", self.run_main("--limit", "-1"))
        bad_dir = os.path.join(tempfile.mkdtemp(), "a-file")
        pathlib.Path(bad_dir).write_text("")  # a file where the folder should be
        self.assertIn("--out", self.run_main("--out", os.path.join(bad_dir, "x", "leads.csv")))



class EngineProgress(unittest.TestCase):
    """The poll line shows the engine's Progress, and a poll survives an engine restart (it resumes)."""

    def test_progress_text(self):
        job = {"Progress": {"keywords_done": 1, "keywords_total": 3, "places": 41, "percent": 50, "stage": "search"}}
        self.assertEqual(scrape.progress_text(job), ", keyword 2/3, 41 places, 50%")
        job["Progress"].update(keywords_done=3, stage="websites", percent=100)
        self.assertEqual(scrape.progress_text(job), ", checking websites, 41 places, 100%")
        self.assertEqual(scrape.progress_text({}), "")  # an engine without Progress

    def test_poll_waits_out_a_restart_and_prints_progress(self):
        calls, out = [], io.StringIO()
        prog = {"keywords_done": 1, "keywords_total": 2, "places": 20, "percent": 50, "stage": "search"}
        answers = [OSError("refused")] * 5 + [{"Status": "pending", "Progress": prog},
                                              {"Status": "working", "Progress": prog}, {"Status": "ok"}]
        def fake_req(method, path, body=None):
            calls.append(path)
            if method == "POST":
                return 201, b'{"id":"j1"}'
            if path.endswith("/download"):
                return 200, b"title\nA\n"
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return 200, json.dumps(a).encode()
        with mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep"), \
             redirect_stdout(out):
            jid, rows = scrape.run_job({"max_time": 60, "keywords": ["a", "b"]})
        self.assertEqual((jid, len(rows)), ("j1", 1))
        self.assertIn("keyword 2/2, 20 places, 50%", out.getvalue())
        self.assertFalse(any(c.endswith("/download") for c in calls[:-1]))  # no download per poll any more

    def test_unpublished_popular_times_warns_without_marking_completed_search_partial(self):
        for note, partial in (("popular times unavailable for 1/1 places", False),
                              ("stopped at max_time; popular times unavailable for 1/1 places", True)):
            def fake_req(method, path, body=None):
                if method == "POST":
                    return 201, b'{"id":"j1"}'
                if path.endswith("/download"):
                    return 200, b"title\nA\n"
                return 200, json.dumps({"Status": "ok", "Error": note}).encode()
            out = io.StringIO()
            with mock.patch.object(scrape, "req", fake_req), redirect_stdout(out):
                if partial:
                    with self.assertRaises(scrape.Partial):
                        scrape.run_job({"max_time": 60, "keywords": ["a"]})
                else:
                    self.assertEqual(scrape.run_job({"max_time": 60, "keywords": ["a"]})[0], "j1")
            self.assertIn("popular times unavailable", out.getvalue())

    def test_details_requests_enrichment_and_keeps_raw_columns(self):
        bodies = []
        def fake_run_job(body):
            bodies.append(body)
            return "j1", [{"title": "Example", "images": '[{"image":"https://example.com/photo"}]',
                           "popular_times": '{"Monday":{"8":20}}'}]
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "o.csv")
            argv = ["scrape.py", "x in y", "1", "2", "--details", "--no-email", "--out", out]
            with mock.patch.object(scrape, "req", return_value=(200, b"null")), \
                 mock.patch.object(scrape, "run_job", fake_run_job), mock.patch.object(sys, "argv", argv), \
                 redirect_stdout(io.StringIO()):
                scrape.main()
            with open(out) as f:
                row = next(csv.DictReader(f))
            self.assertEqual(json.loads(row["popular_times"]), {"Monday": {"8": 20}})
            self.assertTrue(json.loads(row["images"]))
        self.assertIs(bodies[0]["extra_details"], True)

    def test_country_goes_into_the_job(self):
        bodies = []
        def fake_run_job(body):
            bodies.append(body)
            return "j1", []
        argv = ["scrape.py", "x in y", "1", "2", "--country", "DE", "--no-email",
                "--out", os.path.join(tempfile.mkdtemp(), "o.csv")]
        with mock.patch.object(scrape, "req", return_value=(200, b"null")), \
             mock.patch.object(scrape, "run_job", fake_run_job), mock.patch.object(sys, "argv", argv), \
             redirect_stdout(io.StringIO()):
            scrape.main()
        self.assertEqual(bodies[0]["country"], "de")

if __name__ == "__main__":
    unittest.main(verbosity=2)
