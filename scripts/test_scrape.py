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
    def run_stuck(self, csv_body):
        def fake_req(method, path, body=None):
            if method == "POST":
                return 201, b'{"id":"stuck"}'
            if path.endswith("/download"):
                return 200, csv_body
            return 200, b'{"Status":"working"}'
        with mock.patch.object(scrape, "req", fake_req), mock.patch.object(scrape.time, "sleep"), \
             mock.patch.object(scrape, "poll_attempts", return_value=2), redirect_stdout(io.StringIO()):
            return scrape.run_job({"max_time": 1})

    def test_keeps_written_rows(self):
        _, rows = self.run_stuck(b"title,phone\nA,1\nB,2\n")
        self.assertEqual([r["title"] for r in rows], ["A", "B"])

    def test_no_rows_still_errors(self):
        with self.assertRaises(RuntimeError):
            self.run_stuck(b"")


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


class TechSignals(unittest.TestCase):
    def test_detects_pixel_platform_viewport(self):
        html = ('<script src="https://connect.facebook.net/en_US/fbevents.js"></script>'
                '<meta name="viewport" content="width=device-width"><link href="/wp-content/a.css">'
                "<script>gtag('config','AW-123456789')</script>")
        self.assertEqual(scrape._find_tech(html), {"fb_pixel": "yes", "google_ads_tag": "yes",
                         "analytics": "yes", "cms": "wordpress", "mobile_friendly": "yes"})

    def test_unreachable_site_is_unknown_not_no(self):
        self.assertEqual(set(scrape._find_tech("").values()), {""})

    def test_intent_score(self):
        hot = {"fb_pixel": "yes", "google_ads_tag": "yes", "analytics": "yes",
               "review_count": "1,204", "review_rating": "4.6", "emails": "a@b.com"}
        self.assertEqual(scrape.intent_score(hot), 100)
        self.assertEqual(scrape.intent_score({}), 0)
        self.assertEqual(scrape.intent_score({"review_count": "12", "review_rating": "n/a"}), 8)


class CleanEngineEmails(unittest.TestCase):
    def test_drops_placeholders_keeps_real(self):
        # The left column is what the engine actually returned on a depth 1 Austin scrape.
        rows = [{"emails": "user@domain.com"},
                {"emails": "user@domain.com, hello@funkysolcoffee.com"},
                {"emails": "orders@desnudo.com, example@gmail.com, u003eorders@desnudo.com"},
                {"emails": "office@afugacoffee.com"},
                {"emails": ""}]
        changed = scrape.clean_engine_emails(rows)
        self.assertEqual([r["emails"] for r in rows],
                         ["", "hello@funkysolcoffee.com", "orders@desnudo.com",
                          "office@afugacoffee.com", ""])
        self.assertEqual(changed, 3)  # untouched rows are not counted

    def test_missing_column_is_not_an_error(self):
        rows = [{"title": "no emails field"}]
        self.assertEqual(scrape.clean_engine_emails(rows), 0)


class IsRealEmail(unittest.TestCase):
    def test_placeholders_and_vendors(self):
        for e in ("user@domain.com", "example@gmail.com", "youremail@yourdomain.com",
                  "name@company.org", "a@b.wixpress.com", "logo@2x.png"):
            self.assertFalse(scrape.is_real_email(e), e)
        for e in ("info@acmeplumbing.com", "owner@gmail.com", "hello@funkysol.co.uk"):
            self.assertTrue(scrape.is_real_email(e), e)


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
    return {"title": f"Biz {pid}", "place_id": pid, "cid": "c" + pid, "latitude": lat,
            "longitude": "-97.7", "phone": "+1 555", "emails": f"{pid.lower()}@biz.net"}

CELL_ROWS = {"job-1": [_row("A"), _row("B"), _row("C")],
             "job-2": [_row("C"), _row("D")],
             "job-3": None,  # this cell fails
             "job-4": [_row("A"), _row("E"), _row("F", lat="40.0")]}  # F is outside the city box


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
        self.assertIn("dropped 1 rows outside the city box", text)
        with open(out, newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], scrape.LEAD)
        self.assertEqual([r[0] for r in rows[1:]], ["Biz A", "Biz B", "Biz C", "Biz D", "Biz E"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
