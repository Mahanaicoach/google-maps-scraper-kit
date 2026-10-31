#!/usr/bin/env python3
"""Offline tests for scripts/engine.py and scrape.py's no-Docker hooks (standard library only, no
network). Run: python3 scripts/test_engine.py"""
import importlib.util, io, json, os, sys, tempfile, threading, unittest, urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


engine, scrape = load("engine"), load("scrape")
scrape.ENGINE_CONTAINER = "kit-tests-no-such-container"  # never read or restart a real engine


class ConfRules(unittest.TestCase):
    def test_reads_every_page_fix_from_ui_branding_conf(self):
        with open(os.path.join(os.path.dirname(HERE), "ui-branding.conf"), encoding="utf-8") as f:
            pairs, csp = engine.conf_rules(f.read())
        found = dict(pairs)
        self.assertIn('id="depth" name="depth" value="10">', found)
        self.assertEqual(found["cdn.redoc.ly/redoc/latest/"], "cdn.redoc.ly/redoc/v2.5.0/")
        script = found["</body>"]
        self.assertIn("htmx:beforeSwap", script)
        self.assertTrue(script.endswith("</body>"))
        self.assertIn("connect-src 'self' raw.githubusercontent.com", csp)
        self.assertIn(r"/^\s*<tbody/", script)  # nginx keeps \s; so must the native proxy

    def test_quotes_and_escapes(self):
        pairs, csp = engine.conf_rules(r"""sub_filter 'it\'s' "say \"hi\"";  add_header Content-Security-Policy "default-src 'self'";""")
        self.assertEqual(pairs, [("it's", 'say "hi"')])
        self.assertEqual(engine.conf_rules(r"sub_filter 'a' '/\d+\s/';")[0], [("a", r"/\d+\s/")])
        self.assertEqual(csp, "default-src 'self'")


class Build(unittest.TestCase):
    def pick(self, system, machine, rosetta=True):
        with mock.patch.object(engine.platform, "system", return_value=system), \
             mock.patch.object(engine.platform, "machine", return_value=machine), \
             mock.patch.object(engine.subprocess, "run", return_value=mock.Mock(returncode=0 if rosetta else 1)):
            return engine.build()[0]

    def test_each_desktop_gets_its_build(self):
        self.assertTrue(self.pick("Darwin", "arm64").endswith("darwin-amd64"))
        self.assertTrue(self.pick("Darwin", "x86_64").endswith("darwin-amd64"))
        self.assertTrue(self.pick("Linux", "x86_64").endswith("linux-amd64"))
        self.assertTrue(self.pick("Windows", "AMD64").endswith("windows-amd64.exe"))

    def test_unsupported_machines_point_to_docker(self):
        for system, machine in (("Linux", "aarch64"), ("FreeBSD", "amd64")):
            with self.assertRaises(SystemExit) as e:
                self.pick(system, machine)
            self.assertIn("Docker", str(e.exception))
        with self.assertRaises(SystemExit) as e:
            self.pick("Darwin", "arm64", rosetta=False)
        self.assertIn("softwareupdate --install-rosetta", str(e.exception))


class Download(unittest.TestCase):
    def test_a_download_that_fails_the_checksum_is_deleted_not_run(self):
        home = tempfile.mkdtemp()
        with mock.patch.object(engine, "HOME", home), \
             mock.patch.object(engine, "build", return_value=("engine-bin", "0" * 64)), \
             mock.patch.object(engine.urllib.request, "urlopen", return_value=io.BytesIO(b"not the engine")), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                engine.binary()
        self.assertIn("SHA-256", str(e.exception))
        self.assertEqual(os.listdir(home), [])

    def test_restart_never_downloads(self):
        # scrape.py may run `engine.py restart` unattended; fetching code is for an explicit start.
        with mock.patch.object(engine, "HOME", tempfile.mkdtemp()), \
             mock.patch.object(engine, "build", return_value=("engine-bin", "0" * 64)), \
             mock.patch.object(engine.urllib.request, "urlopen") as fetched:
            with self.assertRaises(SystemExit) as e:
                engine.binary(download=False)
        fetched.assert_not_called()
        self.assertIn("engine.py start", str(e.exception))


class Processes(unittest.TestCase):
    def test_stop_never_kills_a_process_that_is_not_the_kits(self):
        # After a reboot the pid in state.json can belong to anything; this test runner is not ours.
        self.assertFalse(engine.ours(os.getpid()))
        with mock.patch.object(engine.os, "killpg", create=True) as killpg, \
             mock.patch.object(engine.subprocess, "run", wraps=engine.subprocess.run) as run:
            engine.kill(os.getpid())
        killpg.assert_not_called()
        self.assertFalse(any("taskkill" in str(c) for c in run.call_args_list))


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _reply(self, ctype, body, extra=()):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Security-Policy", "default-src 'self'")
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path == "/":
            return self._reply("text/html; charset=utf-8", b"<a href=x>gosom</a></body>")
        self._reply("application/json", b'{"echo": "%s"}' % self.path.encode())

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._reply("application/json", self.rfile.read(n))


class Proxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        handler = engine.make_handler(cls.up.server_port, [("gosom", "kit"), ("</body>", "<script>x</script></body>")],
                                      "default-src 'self'; connect-src 'self' raw.githubusercontent.com")
        cls.px = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        for s in (cls.up, cls.px):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.px.server_port}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.up, cls.px):
            s.shutdown(); s.server_close()

    def fetch(self, path="/", method="GET", data=None, headers=None):
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def test_other_hosts_and_sites_are_refused(self):
        self.assertEqual(self.fetch(headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(self.fetch(headers={"Host": "localhost.evil.example:8080"})[0], 421)
        self.assertEqual(self.fetch("/api/v1/jobs", "POST", b"{}", {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.fetch("/api/v1/jobs", "POST", b"{}", {"Origin": "null"})[0], 403)

    def test_local_requests_pass(self):
        for host in ("localhost:8080", "127.0.0.1:8080", "[::1]:8080", "LOCALHOST"):
            self.assertEqual(self.fetch("/api/v1/jobs", headers={"Host": host})[0], 200, host)
        code, _, body = self.fetch("/api/v1/jobs", "POST", b'{"a": 1}', {"Origin": "http://localhost:8080"})
        self.assertEqual((code, body), (200, b'{"a": 1}'))

    def test_every_reply_says_it_is_the_native_proxy(self):
        _, headers, _ = self.fetch("/api/v1/jobs")
        self.assertTrue(headers["X-Engine"].startswith("gmskit-native/"))
        self.assertEqual(engine.probe(self.px.server_port), "native")
        self.assertEqual(engine.probe(self.up.server_port), "other")  # e.g. the Docker engine
        self.assertIsNone(engine.probe(1))

    def test_head_keeps_the_real_length_and_chunked_bodies_are_refused(self):
        code, headers, body = self.fetch("/api/v1/jobs", "HEAD")
        self.assertEqual((code, body), (200, b""))
        self.assertEqual(headers["Content-Length"], str(len(b'{"echo": "/api/v1/jobs"}')))
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.px.server_port, timeout=10)
        c.putrequest("POST", "/api/v1/jobs"); c.putheader("Transfer-Encoding", "chunked"); c.endheaders()
        c.send(b"2\r\n{}\r\n0\r\n\r\n")
        r = c.getresponse(); r.read(); c.close()
        self.assertEqual(r.status, 411)

    def test_html_gets_the_page_fixes_and_csp_json_does_not(self):
        code, headers, body = self.fetch("/")
        self.assertEqual(body, b"<a href=x>kit</a><script>x</script></body>")
        self.assertIn("raw.githubusercontent.com", headers["Content-Security-Policy"])
        _, headers, body = self.fetch("/api/v1/jobs?gosom=1")
        self.assertEqual(body, b'{"echo": "/api/v1/jobs?gosom=1"}')  # JSON passes through untouched

    def test_engine_down_is_a_502_not_a_crash(self):
        handler = engine.make_handler(1, [], None)  # nothing listens on port 1
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            r = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/v1/jobs")
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(r, timeout=10)
            self.assertEqual(e.exception.code, 502)
            e.exception.close()
        finally:
            srv.shutdown(); srv.server_close()


class ScrapeHooks(unittest.TestCase):
    """scrape.py reads the native engine's log file and restarts it through engine.py."""
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = os.path.join(self.dir, "engine.log")
        self.state = os.path.join(self.dir, "state.json")
        with open(self.state, "w") as f:
            json.dump({"port": 8080, "log": self.log}, f)
        self.patches = [mock.patch.object(scrape, "ENGINE_STATE", self.state),
                        mock.patch.object(scrape, "BASE", "http://localhost:8080"),
                        mock.patch.object(scrape, "_answers_as_native", return_value=True)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def write_log(self, *lines):
        with open(self.log, "w") as f:
            f.write("\n".join(lines) + "\n")

    def test_native_only_for_the_port_it_serves(self):
        self.assertIsNotNone(scrape.native_engine())
        with mock.patch.object(scrape, "BASE", "http://localhost:9090"):
            self.assertIsNone(scrape.native_engine())
        with mock.patch.object(scrape, "BASE", "http://scraper.example:8080"):
            self.assertIsNone(scrape.native_engine())

    def test_stale_state_with_docker_on_the_port_is_not_native(self):
        # After a reboot state.json can outlive the engine while Docker serves 8080.
        with mock.patch.object(scrape, "_answers_as_native", return_value=False):
            self.assertIsNone(scrape.native_engine())
            self.assertEqual(scrape.restart_hint(), f"docker restart {scrape.ENGINE_CONTAINER}")

    def test_engine_error_reads_the_plain_go_log_lines(self):
        self.write_log("2026/10/31 04:37:35 error scraping job abc-123: bad proxy url",
                       '{"time":"2026-10-31T01:00:00Z","message":"scrapemate exited"}')
        self.assertEqual(scrape.engine_error("abc-123"), "bad proxy url")

    def test_log_read_from_since_on(self):
        self.write_log('{"time":"2026-10-31T01:00:00.5Z","message":"scrapemate exited"}',
                       "INFO a plain line without a time",
                       '{"time":"2026-10-31T01:05:00.1Z","message":"job finished"}')
        self.assertTrue(scrape.engine_run_ended("2026-10-31T00:59:59Z"))
        self.assertFalse(scrape.engine_run_ended("2026-10-31T01:00:01Z"))  # the exit is older than the job
        self.assertTrue(scrape.engine_run_ended("2026-10-31T01:00:00Z"))   # same second counts

    def test_missing_log_is_not_idle(self):
        os.remove(self.state)
        with open(self.state, "w") as f:
            json.dump({"port": 8080, "log": os.path.join(self.dir, "nope.log")}, f)
        self.assertEqual(scrape.engine_log("2026-01-01T00:00:00Z"), (False, ""))
        self.assertFalse(scrape.engine_idle())

    def test_restart_goes_through_engine_py_not_docker(self):
        ran = []
        def fake_run(cmd, **kw):
            ran.append(cmd)
            return mock.Mock(returncode=0)
        with mock.patch.object(scrape.subprocess, "run", fake_run), \
             mock.patch.object(scrape, "req", return_value=(200, b"null")):
            self.assertTrue(scrape.restart_engine())
        self.assertTrue(ran[0][1].endswith("engine.py") and ran[0][2] == "restart", ran)
        self.assertEqual(scrape.restart_hint(), "python3 scripts/engine.py restart")


if __name__ == "__main__":
    unittest.main(verbosity=2)
