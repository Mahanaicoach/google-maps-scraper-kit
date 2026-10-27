#!/usr/bin/env python3
"""Offline tests for scripts/scrape.py (standard library only, no network).
Run: python3 scripts/test_scrape.py"""
import csv, importlib.util, io, json, os, sys, tempfile, threading, unittest, urllib.error
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


class HttpRequest(unittest.TestCase):
    def test_get_request_success(self):
        mock_response = mock.Mock()
        mock_response.status = 200
        mock_response.read.return_value = b'{"ok": true}'
        mock_response.__enter__ = mock.Mock(return_value=mock_response)
        mock_response.__exit__ = mock.Mock(return_value=False)

        with mock.patch("urllib.request.urlopen", return_value=mock_response):
            status, body = scrape.req("GET", "/api/v1/jobs")
            self.assertEqual(status, 200)
            self.assertEqual(body, b'{"ok": true}')

    def test_post_request_with_body(self):
        mock_response = mock.Mock()
        mock_response.status = 201
        mock_response.read.return_value = b'{"id": "job-123"}'
        mock_response.__enter__ = mock.Mock(return_value=mock_response)
        mock_response.__exit__ = mock.Mock(return_value=False)

        with mock.patch("urllib.request.urlopen", return_value=mock_response):
            status, body = scrape.req("POST", "/api/v1/jobs", {"name": "test"})
            self.assertEqual(status, 201)
            self.assertEqual(body, b'{"id": "job-123"}')

    def test_request_with_api_key(self):
        original_key = scrape.KEY
        scrape.KEY = "test-key-123"
        try:
            mock_response = mock.Mock()
            mock_response.status = 200
            mock_response.read.return_value = b'{}'
            mock_response.__enter__ = mock.Mock(return_value=mock_response)
            mock_response.__exit__ = mock.Mock(return_value=False)

            with mock.patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.return_value = mock_response
                scrape.req("GET", "/api/v1/jobs")
                # Verify X-API-Key header was set (case-insensitive check)
                call_args = mock_urlopen.call_args[0][0]
                headers_lower = {k.lower(): v for k, v in call_args.headers.items()}
                self.assertIn("x-api-key", headers_lower)
                self.assertEqual(headers_lower["x-api-key"], "test-key-123")
        finally:
            scrape.KEY = original_key

    def test_request_http_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("url", 404, "Not Found", {}, None)):
            with self.assertRaises(urllib.error.HTTPError):
                scrape.req("GET", "/api/v1/notfound")


class Geocode(unittest.TestCase):
    def test_geocode_success(self):
        mock_response = mock.Mock()
        mock_response.read.return_value = b'[{"lat": "30.2672", "lon": "-97.7431"}]'
        mock_response.__enter__ = mock.Mock(return_value=mock_response)
        mock_response.__exit__ = mock.Mock(return_value=False)

        with mock.patch("urllib.request.urlopen", return_value=mock_response), \
             mock.patch.object(scrape.time, "sleep"):
            result = scrape.geocode("Austin, TX")
            self.assertEqual(result, ("30.2672", "-97.7431"))

    def test_geocode_with_bbox(self):
        mock_response = mock.Mock()
        mock_response.read.return_value = b'[{"lat": "30.2672", "lon": "-97.7431", "boundingbox": ["30.0", "30.5", "-98.0", "-97.5"]}]'
        mock_response.__enter__ = mock.Mock(return_value=mock_response)
        mock_response.__exit__ = mock.Mock(return_value=False)

        with mock.patch("urllib.request.urlopen", return_value=mock_response), \
             mock.patch.object(scrape.time, "sleep"):
            result = scrape.geocode("Austin, TX", with_bbox=True)
            self.assertEqual(result[:2], ("30.2672", "-97.7431"))
            self.assertEqual(result[2], ["30.0", "30.5", "-98.0", "-97.5"])

    def test_geocode_empty_response(self):
        mock_response = mock.Mock()
        mock_response.read.return_value = b'[]'
        mock_response.__enter__ = mock.Mock(return_value=mock_response)
        mock_response.__exit__ = mock.Mock(return_value=False)

        with mock.patch("urllib.request.urlopen", return_value=mock_response), \
             mock.patch.object(scrape.time, "sleep"):
            result = scrape.geocode("InvalidPlace")
            self.assertIsNone(result)

    def test_geocode_network_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=Exception("Network error")):
            result = scrape.geocode("Austin, TX")
            self.assertIsNone(result)


class EnrichSocials(unittest.TestCase):
    def test_socials_with_html_links(self):
        html = """
        <html>
        <a href="https://instagram.com/acmebiz">Instagram</a>
        <a href="https://facebook.com/acmebiz">Facebook</a>
        <a href="https://linkedin.com/company/acmebiz">LinkedIn</a>
        </html>
        """
        results = [{"title": "Acme Biz", "website": "https://acme.com"}]

        with mock.patch.object(scrape, "_fetch_cached", return_value=html), \
             mock.patch.object(sys, "stdout"):
            scrape.enrich_socials(results, workers=1)

        self.assertEqual(results[0]["instagram"], "https://instagram.com/acmebiz")
        self.assertEqual(results[0]["facebook"], "https://facebook.com/acmebiz")
        self.assertEqual(results[0]["linkedin"], "https://linkedin.com/company/acmebiz")

    def test_socials_no_links(self):
        html = "<html><body>No social links here</body></html>"
        results = [{"title": "No Socials", "website": "https://nosocials.com"}]

        with mock.patch.object(scrape, "_fetch_cached", return_value=html), \
             mock.patch.object(sys, "stdout"):
            scrape.enrich_socials(results, workers=1)

        self.assertEqual(results[0]["instagram"], "")
        self.assertEqual(results[0]["facebook"], "")
        self.assertEqual(results[0]["linkedin"], "")

    def test_socials_junk_handles(self):
        html = """
        <a href="https://instagram.com/home">Home</a>
        <a href="https://facebook.com/pages">Pages</a>
        <a href="https://linkedin.com/company/about">About</a>
        """
        results = [{"title": "Junk Socials", "website": "https://junk.com"}]

        with mock.patch.object(scrape, "_fetch_cached", return_value=html), \
             mock.patch.object(sys, "stdout"):
            scrape.enrich_socials(results, workers=1)

        # All should be empty (junk handles filtered)
        self.assertEqual(results[0]["instagram"], "")
        self.assertEqual(results[0]["facebook"], "")
        self.assertEqual(results[0]["linkedin"], "")

    def test_socials_no_website(self):
        results = [{"title": "No Website", "website": ""}]

        with mock.patch.object(sys, "stdout"):
            scrape.enrich_socials(results, workers=1)

        # Should not crash, keys should be set but empty
        self.assertEqual(results[0]["instagram"], "")
        self.assertEqual(results[0]["facebook"], "")
        self.assertEqual(results[0]["linkedin"], "")


class EnrichEmails(unittest.TestCase):
    def test_emails_from_homepage(self):
        html = '<a href="mailto:info@acme.com">Contact</a>'
        results = [{"title": "Acme", "website": "https://acme.com", "emails": ""}]

        with mock.patch.object(scrape, "_fetch_cached", return_value=html), \
             mock.patch.object(sys, "stdout"):
            scrape.enrich_emails(results, workers=1)

        self.assertEqual(results[0]["emails"], "info@acme.com")

    def test_emails_from_contact_pages(self):
        home_html = '<html><body>No emails here</body></html>'
        contact_html = '<a href="mailto:sales@acme.com">Sales</a>'
        results = [{"title": "Acme", "website": "https://acme.com", "emails": ""}]

        with mock.patch.object(scrape, "_fetch_cached", return_value=home_html), \
             mock.patch.object(scrape, "_fetch_html", return_value=contact_html), \
             mock.patch.object(sys, "stdout"):
            scrape.enrich_emails(results, workers=1)

        self.assertEqual(results[0]["emails"], "sales@acme.com")

    def test_emails_junk_filtering(self):
        html = """
        <a href="mailto:info@acme.com">Good</a>
        <a href="mailto:hello@example.com">Junk domain</a>
        <a href="mailto:logo@2x.png">Junk extension</a>
        """
        results = [{"title": "Acme", "website": "https://acme.com", "emails": ""}]

        with mock.patch.object(scrape, "_fetch_cached", return_value=html), \
             mock.patch.object(sys, "stdout"):
            scrape.enrich_emails(results, workers=1)

        # Only valid email should remain
        self.assertEqual(results[0]["emails"], "info@acme.com")

    def test_emails_no_website(self):
        results = [{"title": "No Website", "website": "", "emails": ""}]

        with mock.patch.object(sys, "stdout"):
            scrape.enrich_emails(results, workers=1)

        # Should not crash, emails should remain empty
        self.assertEqual(results[0]["emails"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
