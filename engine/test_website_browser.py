"""Offline checks; GMSKIT_BROWSER_LIVE=1 also renders a local JavaScript fixture with Playwright."""
import asyncio
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

    def test_nearly_expired_budget_never_starts_the_driver(self):
        with patch.object(web, "check_available") as available:
            result = web.render("https://fixture-business.test", jobs._public_ip, timeout=0.001)
        self.assertEqual(result["status"], "budget_exhausted")
        available.assert_not_called()

    def test_missing_dependency_is_actionable(self):
        with patch.dict(sys.modules, {'playwright': None, 'playwright.sync_api': None}):
            with self.assertRaisesRegex(RuntimeError, 'virtual environment'):
                web.check_available()


class BrowserBudget(unittest.TestCase):
    def test_one_session_per_budget_until_the_deadline(self):
        with patch.object(web._Session, "render", return_value={"html": "ok", "status": "rendered"}) as render, \
             web.Budget(30) as budget:
            budget.fetch("https://fixture-business.test", jobs._public_ip)
            session = budget.session
            budget.fetch("https://fixture-business.test/contact", jobs._public_ip)
            self.assertIs(budget.session, session)
            budget.deadline = time.monotonic()
            self.assertEqual(budget.fetch("https://fixture-business.test/about", jobs._public_ip)["status"], "budget_exhausted")
            self.assertEqual(render.call_count, 2)
        self.assertEqual(budget.fetch("https://fixture-business.test", jobs._public_ip)["status"], "budget_exhausted")

    def test_failed_launch_never_leaks_a_browser_slot(self):
        with patch.object(web, "check_available", side_effect=RuntimeError("no browser")):
            with web.Budget(30) as budget:
                for _ in range(3):
                    self.assertEqual(budget.fetch("https://fixture-business.test", jobs._public_ip)["status"], "error")
        slots = [web._SLOTS.acquire(blocking=False) for _ in range(2)]
        for ok in slots:
            if ok:
                web._SLOTS.release()
        self.assertEqual(slots, [True, True])

    def test_hung_browser_close_still_stops_the_driver(self):
        stopped = []
        async def hang():
            await asyncio.sleep(60)
        async def stop():
            stopped.append(True)
        async def render(session, url):
            session.browser, session.pw = Mock(close=hang), Mock(stop=stop)
            return {"html": "", "status": "rendered"}
        with patch.object(web._Session, "render", render), patch.object(web, "_CLOSE_SECONDS", 0.2):
            budget = web.Budget(30)
            budget.fetch("https://fixture-business.test", jobs._public_ip)
            started = time.monotonic()
            budget.close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(stopped, [True])

    def test_close_during_a_render_returns_budget_exhausted_not_an_exception(self):
        started = threading.Event()
        async def render(session, url):
            session.tasks.add(asyncio.current_task())  # as the real render registers itself
            started.set()
            await asyncio.sleep(60)
        with patch.object(web._Session, "render", render):
            budget = web.Budget(30)
            out = {}
            t = threading.Thread(target=lambda: out.update(budget.fetch("https://fixture-business.test", jobs._public_ip)), daemon=True)
            t.start()
            self.assertTrue(started.wait(5))
            budget.close()
            t.join(5)
        self.assertEqual(out.get("status"), "budget_exhausted")

    def test_close_stops_the_proxy_before_the_browser_and_bounds_the_driver_stop(self):
        order = []
        async def hang():
            await asyncio.sleep(60)
        def browser_close():
            order.append("browser")
            return hang()
        async def render(session, url):
            session.browser = Mock(close=browser_close)
            session.pw = Mock(stop=hang)
            session.proxy = Mock(__exit__=Mock(side_effect=lambda *a: order.append("proxy")))
            return {"html": "", "status": "rendered"}
        with patch.object(web._Session, "render", render), patch.object(web, "_CLOSE_SECONDS", 0.2):
            budget = web.Budget(30)
            budget.fetch("https://fixture-business.test", jobs._public_ip)
            started = time.monotonic()
            budget.close()
        self.assertLess(time.monotonic() - started, 2)  # two bounded waits, not a hung driver
        self.assertEqual(order, ["proxy", "browser"])

    def test_target_closed_futures_do_not_print_at_exit_but_other_errors_do(self):
        TargetClosedError = type("TargetClosedError", (Exception,), {})
        loop = asyncio.new_event_loop()
        loop.set_exception_handler(web._quiet_target_closed)
        with patch.object(asyncio.BaseEventLoop, "default_exception_handler") as default:
            loop.call_exception_handler({"message": "Future exception was never retrieved", "exception": TargetClosedError("gone")})
            default.assert_not_called()
            loop.call_exception_handler({"message": "Future exception was never retrieved", "exception": RuntimeError("real")})
            default.assert_called_once()
        loop.close()
        with patch.object(web._Session, "render", return_value={"html": "", "status": "rendered"}), web.Budget(30) as budget:
            budget.fetch("https://fixture-business.test", jobs._public_ip)
            self.assertIs(budget.loop.get_exception_handler(), web._quiet_target_closed)

    def test_status_keeps_the_best_fetch(self):
        self.assertEqual(web.merge_status("http", "budget_exhausted"), "http")
        self.assertEqual(web.merge_status("http", "rendered"), "rendered")
        self.assertEqual(web.merge_status("rendered", "blocked"), "rendered")
        self.assertEqual(web.merge_status("error", "blocked"), "blocked")
        self.assertEqual(web.merge_status(None, "timeout"), "timeout")


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
        with patch.object(web._Session, "render", side_effect=render) as browser:
            jobs.add_site_data(rows, 5, browser_sites=True, get=http)
        self.assertEqual(browser.call_count, len(rows))

    def test_engine_retries_empty_http_for_emails_and_socials_once(self):
        html = '<a href="mailto:hello@fixture-business.test">Email</a><a href="https://instagram.com/fixture_business">IG</a>'
        rows = [{'website': 'https://fixture-business.test', 'emails': ''}]
        with patch.object(web._Session, 'render', return_value={'html': html, 'status': 'rendered'}) as render:
            jobs.add_site_data(rows, 5, socials=True, browser_sites=True, get=lambda _: '')
        self.assertEqual(rows[0]['emails'], 'hello@fixture-business.test')
        self.assertEqual(rows[0]['instagram'], 'https://instagram.com/fixture_business')
        self.assertEqual(rows[0]['website_fetch_status'], 'rendered')
        render.assert_called_once()

    def test_engine_keeps_fast_path_and_reports_blocks_without_fabricating_data(self):
        rows = [{'website': 'https://fixture-business.test', 'emails': ''}]
        with patch.object(web._Session, 'render') as render:
            jobs.add_site_data(rows, 5, browser_sites=True, get=lambda _: 'hello@fixture-business.test')
            render.assert_not_called()
        self.assertEqual(rows[0]['website_fetch_status'], 'http')
        rows[0]['emails'] = ''
        with patch.object(web._Session, 'render', return_value={'html': '', 'status': 'blocked'}):
            jobs.add_site_data(rows, 5, socials=True, browser_sites=True, get=lambda _: '')
        self.assertEqual(rows[0]['emails'], '')
        self.assertNotIn('instagram', rows[0])
        self.assertEqual(rows[0]['website_fetch_status'], 'blocked')

    def test_budget_cut_retries_are_not_reported_unchecked_and_keep_http_status(self):
        rows = [{'website': f'https://fixture-{i}.test', 'emails': ''} for i in range(6)]
        def slow(url):
            time.sleep(1.5)
            return {'html': '', 'status': 'blocked'}
        with patch.object(web._Session, 'render', side_effect=slow) as render:
            # 4 s budget, 1.5 s per page: some retries are still queued when it ends.
            _, unchecked = jobs.add_site_data(rows, 4, browser_sites=True, get=lambda _: '<p>no contact</p>')
            self.assertLess(render.call_count, len(rows))
        self.assertEqual(unchecked, 0)
        self.assertEqual({r['website_fetch_status'] for r in rows}, {'http'})

    def test_http_timeouts_are_labelled(self):
        rows = [{'website': 'https://slow.test', 'emails': ''}]
        with patch.object(web._Session, 'render') as render:
            _, unchecked = jobs.add_site_data(rows, 0.5, browser_sites=True, get=lambda _: time.sleep(2) or '')
        self.assertEqual(unchecked, 1)
        self.assertEqual(rows[0]['website_fetch_status'], 'timeout')
        render.assert_not_called()

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
        if self.path == '/slow-script':
            self.server.release_script.wait(25)
            self.send_response(200)
            self.end_headers()
            return
        if self.path == '/blocked':
            self.send_error(403)
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.2:{self.server.server_port}/')
            self.end_headers()
            return
        if self.path == '/slow-page':
            body = b'''<!doctype html><title>Business</title>
            <a href="mailto:hello@fixture-business.test">Contact</a>
            <script src="/slow-script"></script>'''
        elif self.path == '/data':
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
    def test_usable_contacts_survive_slow_script(self):
        with ThreadingHTTPServer(('127.0.0.1', 0), Fixture) as server:
            server.release_script = threading.Event()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = web.render(f'http://127.0.0.1:{server.server_port}/slow-page',
                                    lambda ip: ip == '127.0.0.1', timeout=15)
                self.assertEqual(result['status'], 'rendered', result.get('stage'))
                self.assertEqual(jobs.find_emails(result['html']), ['hello@fixture-business.test'])
            finally:
                server.release_script.set()
                server.shutdown()
                thread.join(2)

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
