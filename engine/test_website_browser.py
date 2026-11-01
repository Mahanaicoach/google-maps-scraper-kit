"""Offline checks; GMSKIT_BROWSER_LIVE=1 also renders a local JavaScript fixture with Playwright."""
import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jobs
import website_browser as web


class PublicProxy(unittest.TestCase):
    def test_private_or_mixed_dns_never_connects(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch.object(socket, 'getaddrinfo', return_value=answers), patch.object(socket, 'socket') as make:
            with self.assertRaises(OSError):
                web._connect_public('untrusted.test', 443, jobs._public_ip, time.monotonic() + 5)
            make.assert_not_called()

    def test_connection_uses_resolved_ip_and_checks_peer(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        peer = Mock()
        peer.getpeername.return_value = ('127.0.0.1', 443)
        with patch.object(socket, 'getaddrinfo', return_value=answers), patch.object(socket, 'socket', return_value=peer):
            with self.assertRaises(OSError):
                web._connect_public('untrusted.test', 443, jobs._public_ip, time.monotonic() + 5)
            peer.connect.assert_called_once_with(('93.184.216.34', 443))
            peer.close.assert_called()

    def test_invalid_url_is_blocked_without_starting_playwright(self):
        for url in ('file:///etc/passwd', 'ftp://example.com', 'https://user:password@example.com', 'http://['):
            with patch.object(web, 'check_available') as available:
                self.assertEqual(web.render(url, jobs._public_ip)['status'], 'blocked')
                available.assert_not_called()

    def test_missing_dependency_is_actionable(self):
        with patch.dict(sys.modules, {'playwright': None, 'playwright.sync_api': None}):
            with self.assertRaisesRegex(RuntimeError, 'requirements-browser.txt'):
                web.check_available()


class BrowserBudget(unittest.TestCase):
    def test_expired_budget_skips_browser_and_caps_each_page(self):
        with patch.object(web.time, "monotonic", return_value=100) as clock, \
             patch.object(web, "render", return_value={"html": "ok", "status": "rendered"}) as render:
            budget = web.Budget(30)
            budget.fetch("https://fixture-business.test", jobs._public_ip)
            self.assertEqual(render.call_args.kwargs["timeout"], 20)
            clock.return_value = 128
            budget.fetch("https://fixture-business.test/contact", jobs._public_ip)
            self.assertEqual(render.call_args.kwargs["timeout"], 2)
            clock.return_value = 131
            self.assertEqual(budget.fetch("https://fixture-business.test/about", jobs._public_ip)["status"], "budget_exhausted")
            self.assertEqual(render.call_count, 2)


class WebsiteEnrichment(unittest.TestCase):
    def test_all_http_work_finishes_before_browser_retries(self):
        rows = [{"website": f"https://fixture-{i}.test", "emails": ""} for i in range(20)]
        fetched = []
        def http(url):
            fetched.append(url)
            return ""
        def render(*a, **kw):
            self.assertEqual(len(fetched), len(rows))
            return {"html": "", "status": "blocked"}
        with patch.object(web, "render", side_effect=render) as browser:
            jobs.add_site_data(rows, 5, browser_sites=True, get=http)
        self.assertEqual(browser.call_count, len(rows))

    def test_engine_retries_empty_http_for_emails_and_socials_once(self):
        html = '<a href="mailto:hello@fixture-business.test">Email</a><a href="https://instagram.com/fixture_business">IG</a>'
        rows = [{'website': 'https://fixture-business.test', 'emails': ''}]
        with patch.object(web, 'render', return_value={'html': html, 'status': 'rendered'}) as render:
            jobs.add_site_data(rows, 5, socials=True, browser_sites=True, get=lambda _: '')
        self.assertEqual(rows[0]['emails'], 'hello@fixture-business.test')
        self.assertEqual(rows[0]['instagram'], 'https://instagram.com/fixture_business')
        self.assertEqual(rows[0]['website_fetch_status'], 'rendered')
        render.assert_called_once()

    def test_engine_keeps_fast_path_and_reports_blocks_without_fabricating_data(self):
        rows = [{'website': 'https://fixture-business.test', 'emails': ''}]
        with patch.object(web, 'render') as render:
            jobs.add_site_data(rows, 5, browser_sites=True, get=lambda _: 'hello@fixture-business.test')
            render.assert_not_called()
        self.assertEqual(rows[0]['website_fetch_status'], 'http')
        rows[0]['emails'] = ''
        with patch.object(web, 'render', return_value={'html': '', 'status': 'blocked'}):
            jobs.add_site_data(rows, 5, socials=True, browser_sites=True, get=lambda _: '')
        self.assertEqual(rows[0]['emails'], '')
        self.assertNotIn('instagram', rows[0])
        self.assertEqual(rows[0]['website_fetch_status'], 'blocked')

    def test_api_flag_is_typed_and_adds_status_column(self):
        body = {'name': 'test', 'keywords': ['cafes'], 'lang': 'en', 'lat': '1', 'lon': '2', 'max_time': 60}
        with self.assertRaisesRegex(ValueError, 'invalid browser_sites'):
            jobs.validate(dict(body, browser_sites='false'))
        with patch.object(web, 'check_available', return_value='/chrome'):
            _, data = jobs.validate(dict(body, browser_sites=True))
        self.assertIn('website_fetch_status', jobs.fields(data))


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/blocked':
            self.send_error(403)
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.2:{self.server.server_port}/')
            self.end_headers()
            return
        if self.path == '/data':
            body = json.dumps({'mail': ['hello', 'fixture-business.test'], 'profile': 'fixture_business'}).encode()
        else:
            body = b'''<!doctype html><title>Local business fixture</title><main>Loading</main>
            <script>fetch('/data').then(r=>r.json()).then(d=>{
            document.querySelector('main').innerHTML = '<a href="mailto:'+d.mail.join('@')+'">Contact</a>'+
            '<a href="https://instagram.com/'+d.profile+'">Instagram</a>'; });</script>'''
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(os.environ.get('GMSKIT_BROWSER_LIVE') == '1', 'optional real-browser fixture')
class LiveBrowser(unittest.TestCase):
    def test_javascript_links_and_private_destinations(self):
        with ThreadingHTTPServer(('127.0.0.1', 0), Fixture) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/'
                # Only this in-process test fixture is permitted. Production uses the public-IP guard.
                local_fixture = lambda ip: ip == '127.0.0.1'
                result = web.render(url, local_fixture)
                self.assertEqual(result['status'], 'rendered')
                self.assertEqual(jobs.find_emails(result['html']), ['hello@fixture-business.test'])
                self.assertEqual(jobs.find_socials(result['html'])['instagram'], 'https://instagram.com/fixture_business')
                self.assertEqual(web.render(url + 'blocked', local_fixture)['status'], 'blocked')
                self.assertEqual(web.render(url + 'redirect', local_fixture)['status'], 'blocked')
                self.assertEqual(web.render(url, jobs._public_ip)['status'], 'blocked')
            finally:
                server.shutdown()
                thread.join(2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
