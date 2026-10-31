#!/usr/bin/env python3
"""The kit's own engine: the Google Maps scraper's HTTP API and web UI, on this machine only.

    python3 engine/serve.py               # port from SCRAPER_PORT (environment, then .env), default 8080
    python3 engine/serve.py --port 8190

Normally started by `python3 scripts/engine.py start`, or by `docker compose up -d` (the Dockerfile). It speaks the API of the engine the kit used
before (POST/GET/DELETE /api/v1/jobs, /api/v1/jobs/{id}/download, the same job body and "Status"
values), so scripts/scrape.py, the skill and the slash commands work unchanged. It listens on
127.0.0.1 (and ::1) only, or on 0.0.0.0 inside the Docker image, where Docker publishes the port on
the host's 127.0.0.1 only. Either way it refuses a foreign Host (DNS rebinding, 421) or Origin (cross-site
posts, 403); with SCRAPER_API_KEY set it also wants that X-API-Key (401). No telemetry: it talks to
Google Maps, and for email: true jobs to the businesses' own websites, nothing else.
Standard library only.
"""
import argparse, hmac, json, os, re, secrets, signal, socket, socketserver, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser, jobs  # noqa: E402

for _stream in (sys.stdout, sys.stderr):  # ✓ and accented names on a Windows cp1252 console
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

VERSION = "2.0.0-dev"
MARK = f"gmskit-engine/{VERSION}"  # X-Engine header: tells scripts/engine.py this engine from others
REPO = "https://github.com/Mahanaicoach/google-maps-scraper-kit"
# The web UI's sponsor box: the browser reads this static file from GitHub (cached a day, no
# tracking), so the sponsors it lists change without anyone updating their copy of the kit.
SPONSORS = "https://raw.githubusercontent.com/Mahanaicoach/google-maps-scraper-kit/master/assets/sponsors/sponsors.json"
ENV_KEYS = ("SCRAPER_PORT", "SCRAPER_API_KEY")
MAX_BODY = 1 << 20
LOCAL = r"(localhost|127\.0\.0\.1|\[::1\])"
HOST_OK = re.compile(rf"^{LOCAL}(:\d{{1,5}})?$", re.I)
ORIGIN_OK = re.compile(rf"^https?://{LOCAL}(:\d{{1,5}})?$", re.I)
JOB_PATH = re.compile(r"^/api/v1/jobs/([0-9a-f-]{36})(/download)?$")
JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_log_lock = threading.Lock()
_REPLIED = object()  # body() answered the request itself (a bad body)


def log(msg):
    """One line on stderr with a UTC timestamp and its offset. Counts only, never scraped values."""
    with _log_lock:
        print(f"{jobs.utc_now()} {msg}", file=sys.stderr, flush=True)


def read_env():
    """SCRAPER_PORT and SCRAPER_API_KEY: the environment wins, then the kit's .env (last line wins,
    as in docker compose and scripts/scrape.py)."""
    found = {}
    try:
        with open(os.path.join(browser.KIT, ".env"), encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                k, sep, v = line.strip().removeprefix("export ").partition("=")
                if sep and k.strip() in ENV_KEYS:
                    found[k.strip()] = v.split(" #", 1)[0].strip().strip("\"'")
    except OSError:
        pass
    return {k: os.environ.get(k, found.get(k, "")) for k in ENV_KEYS}


OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Google Maps Scraper Kit engine", "version": VERSION,
             "description": "Local only. Create a job, poll its Status until ok or failed, download the CSV. "
                            f"Source and docs: {REPO}"},
    "paths": {
        "/api/v1/jobs": {
            "get": {"summary": "List jobs, newest first (null when there are none)"},
            "post": {"summary": "Create a job; returns 201 {\"id\": \"<uuid>\"}, or 422 {\"code\", \"message\"}",
                     "requestBody": {"content": {"application/json": {"schema": {
                         "type": "object", "required": ["name", "keywords", "lang", "lat", "lon", "max_time"],
                         "properties": {
                             "name": {"type": "string", "description": "any label"},
                             "keywords": {"type": "array", "items": {"type": "string"}},
                             "lang": {"type": "string", "description": "2-letter interface language, e.g. en"},
                             "lat": {"type": "string"}, "lon": {"type": "string"},
                             "max_time": {"type": "integer", "description": "seconds"},
                             "depth": {"type": "integer", "description": "result batches per keyword, about 20 places each (default 1)"},
                             "zoom": {"type": "integer", "description": "map zoom 1-21 (default 15)"},
                             "email": {"type": "boolean", "description": "visit each website for emails"},
                             "proxies": {"type": "array", "items": {"type": "string"},
                                         "description": "http(s):// or socks5://host:port; the first is used"},
                             "radius": {"type": "integer", "description": "accepted, not used"},
                             "fast_mode": {"type": "boolean", "description": "accepted, not used"}}}}}}}},
        "/api/v1/jobs/{id}": {
            "get": {"summary": "One job: ID, Name, Date, Status (pending, working, ok, failed), Data, Error"},
            "delete": {"summary": "Delete a job and its results (stops it if it is running)"}},
        "/api/v1/jobs/{id}/download": {"get": {"summary": "The job's CSV (the rows so far while it works)"}},
    },
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Google Maps Scraper Kit</title>
<style nonce="{nonce}">
:root {{ color-scheme: light dark; --bg: #fff; --fg: #1d1d1f; --muted: #6e6e73; --line: #d2d2d7; --accent: #0a66c2; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #111; --fg: #f2f2f2; --muted: #a1a1a6; --line: #333; --accent: #4ea1ff; }} }}
body {{ margin: 0 auto; max-width: 960px; padding: 16px; background: var(--bg); color: var(--fg);
       font: 15px/1.45 system-ui, -apple-system, Segoe UI, sans-serif; }}
h1 {{ font-size: 1.4em; margin: 0 0 4px; }} a {{ color: var(--accent); }} .muted {{ color: var(--muted); }}
form {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); margin: 16px 0; }}
label {{ display: grid; gap: 4px; font-size: .9em; }} .wide {{ grid-column: 1 / -1; }}
input, textarea, button {{ font: inherit; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--fg); }}
button {{ cursor: pointer; }} button.primary {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
table {{ width: 100%; border-collapse: collapse; }} th, td {{ text-align: left; padding: 6px; border-bottom: 1px solid var(--line); }}
td.err {{ color: var(--muted); font-size: .85em; }} .row {{ display: flex; gap: 6px; flex-wrap: wrap; }}
#msg {{ min-height: 1.4em; }} .scroll {{ overflow-x: auto; }} [hidden] {{ display: none !important; }}
</style></head><body>
<h1>Google Maps Scraper Kit</h1>
<p class="muted">Local engine {version}. <a href="{repo}" rel="noopener">GitHub</a> ·
<a href="/api/docs">API</a> · Business listings only. Results stay on this machine.</p>
<p class="muted" id="sponsors"><a href="https://github.com/sponsors/Mahanaicoach" rel="noopener">Become a sponsor</a></p>
<form id="f">
  <label class="wide">Keywords, one per line<textarea id="keywords" rows="3" required
    placeholder="coffee shops in Austin TX"></textarea></label>
  <label>Latitude<input id="lat" required placeholder="30.2672" inputmode="decimal"></label>
  <label>Longitude<input id="lon" required placeholder="-97.7431" inputmode="decimal"></label>
  <label>Depth (about 20 places each)<input id="depth" type="number" min="1" max="1000" value="5"></label>
  <label>Language<input id="lang" value="en" maxlength="2" pattern="[A-Za-z]{{2}}"></label>
  <label>Time limit (seconds)<input id="max_time" type="number" min="1" max="86400" value="600"></label>
  <label><span>Emails</span><span><input type="checkbox" id="email" checked> visit each website</span></label>
  <div class="wide row"><button class="primary">Start scraping</button>
    <span class="muted">Coordinates: right-click a spot in Google Maps and click the numbers.</span></div>
  <label class="wide" id="keybox" hidden>API key (SCRAPER_API_KEY)<input id="key" type="password" autocomplete="off"></label>
</form>
<p id="msg" role="status"></p>
<div class="scroll"><table><thead><tr><th>Job</th><th>Created (UTC)</th><th>Status</th><th></th></tr></thead>
<tbody id="jobs"></tbody></table></div>
<script nonce="{nonce}">
const $ = id => document.getElementById(id);
let key = ''; try {{ key = localStorage.getItem('gmskit-key') || ''; }} catch (e) {{}}
$('key').value = key;
$('key').addEventListener('change', () => {{ key = $('key').value; try {{ localStorage.setItem('gmskit-key', key); }} catch (e) {{}} load(); }});
function api(method, path, body) {{
  const h = {{}}; if (key) h['X-API-Key'] = key; if (body) h['Content-Type'] = 'application/json';
  return fetch(path, {{method, headers: h, body: body ? JSON.stringify(body) : undefined}}).then(r => {{
    if (r.status === 401) {{ $('keybox').hidden = false; throw new Error('this engine needs its API key'); }}
    return r;
  }});
}}
function say(t) {{ $('msg').textContent = t; }}
function cell(tr, text, cls) {{ const td = document.createElement('td'); td.textContent = text; if (cls) td.className = cls; tr.appendChild(td); return td; }}
function button(td, label, fn) {{ const b = document.createElement('button'); b.type = 'button'; b.textContent = label; b.addEventListener('click', fn); td.appendChild(b); }}
async function download(j) {{
  const r = await api('GET', '/api/v1/jobs/' + j.ID + '/download');
  if (!r.ok) return say('No results yet for this job.');
  const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob());
  a.download = 'results-' + j.ID.slice(0, 8) + '.csv'; document.body.appendChild(a); a.click(); a.remove();
}}
async function remove(j) {{
  if (!confirm('Delete this job and its results?')) return;
  const r = await api('DELETE', '/api/v1/jobs/' + j.ID); say(r.ok ? 'Deleted.' : 'Could not delete it.'); load();
}}
async function load() {{
  try {{
    const list = await (await api('GET', '/api/v1/jobs')).json() || [];
    const tb = $('jobs'); tb.replaceChildren();
    for (const j of list) {{
      const tr = document.createElement('tr');
      const name = cell(tr, j.Name); if (j.Error) {{ const s = document.createElement('div'); s.className = 'muted'; s.textContent = j.Error; name.appendChild(s); }}
      cell(tr, j.Date.replace('T', ' ').replace('+00:00', '')); cell(tr, j.Status);
      const act = cell(tr, ''); act.className = 'row';
      if (j.Status !== 'pending') button(act, 'Download', () => download(j));
      button(act, 'Delete', () => remove(j));
      tb.appendChild(tr);
    }}
    if (!list.length) {{ const tr = document.createElement('tr'); cell(tr, 'No jobs yet.', 'err').colSpan = 4; tb.appendChild(tr); }}
  }} catch (e) {{ say(e.message); }}
}}
$('f').addEventListener('submit', async ev => {{
  ev.preventDefault();
  const keywords = $('keywords').value.split('\\n').map(s => s.trim()).filter(Boolean);
  const body = {{name: keywords[0] || 'job', keywords, lang: $('lang').value.trim().toLowerCase(),
    lat: $('lat').value.trim(), lon: $('lon').value.trim(), depth: +$('depth').value,
    max_time: +$('max_time').value, email: $('email').checked, zoom: 15}};
  try {{
    const r = await api('POST', '/api/v1/jobs', body), d = await r.json();
    say(r.ok ? 'Job created. It runs in the background; this list refreshes by itself.' : 'Not started: ' + d.message);
    load();
  }} catch (e) {{ say(e.message); }}
}});
load(); setInterval(load, 3000);
(function () {{  // sponsor box: the "ui": true entries of sponsors.json, as text, https links only
  const box = $('sponsors'), key = 'gmskit-sponsors', day = 86400000;
  function render(list) {{
    const shown = Array.isArray(list) ? list.filter(s => s && s.ui === true && typeof s.name === 'string'
      && typeof s.url === 'string' && s.url.startsWith('https://')).slice(0, 4) : [];
    if (!shown.length) return;
    box.replaceChildren('Sponsored by ');
    shown.forEach((s, i) => {{
      if (i) box.append(' | ');
      const a = document.createElement('a');
      a.href = s.url; a.target = '_blank'; a.rel = 'noopener noreferrer sponsored'; a.textContent = s.name.slice(0, 40);
      box.append(a);
      if (typeof s.tagline === 'string' && s.tagline) box.append(': ' + s.tagline.slice(0, 120));
    }});
  }}
  try {{
    const c = JSON.parse(localStorage.getItem(key) || 'null');
    if (c && Date.now() - c.t < day) return render(c.list);
  }} catch (e) {{}}
  fetch('{sponsors}', {{cache: 'no-store', credentials: 'omit'}}).then(r => r.ok ? r.json() : null).then(d => {{
    if (!d || !Array.isArray(d.sponsors)) return;
    render(d.sponsors);
    try {{ localStorage.setItem(key, JSON.stringify({{t: Date.now(), list: d.sponsors}})); }} catch (e) {{}}
  }}).catch(() => {{}});
}})();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gmskit-engine"
    sys_version = ""

    def log_message(self, fmt, *args):  # errors are logged by reply(); polls would flood the log
        pass

    def reply(self, code, body=b"", ctype="application/json", headers=()):
        if ctype == "application/json":
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Engine", MARK)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        if self.close_connection:  # tell the client too, so it never reuses the socket
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        if code >= 400:
            log(f"{self.command} {self.path.split('?')[0]} -> {code}")

    def fail(self, code, message):
        if self.command != "GET":  # a refused POST or DELETE may leave its body unread on the socket
            self.close_connection = True
        self.reply(code, {"code": code, "message": message})

    def guard(self):
        """Host (DNS rebinding) and Origin (cross-site posts) checks, plus the optional key."""
        if not HOST_OK.match(self.headers.get("Host", "")):
            self.close_connection = True
            self.fail(421, "this engine answers to localhost only")
            return False
        origin = self.headers.get("Origin")
        if origin is not None and not ORIGIN_OK.match(origin):
            self.close_connection = True
            self.fail(403, "requests from other websites are refused")
            return False
        key, path = self.server.api_key, self.path.split("?")[0]
        if key and path not in ("/", "/api/docs") and not hmac.compare_digest(
                self.headers.get("X-API-Key", "").encode(), key.encode()):
            self.fail(401, "missing or wrong X-API-Key")
            return False
        return True

    def body(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True
            self.fail(411, "send a Content-Length")
            return _REPLIED
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_BODY:
            self.close_connection = True
            self.fail(413 if n > MAX_BODY else 400, "the body must be 1 MB or less")
            return _REPLIED
        try:
            return json.loads(self.rfile.read(n) or b"null")
        except ValueError:  # bad JSON or bad UTF-8
            self.fail(400, "the body is not valid JSON")
            return _REPLIED

    def download(self, jid):
        data = self.server.store.read_csv(jid) if JOB_ID.match(jid or "") else None
        if data is None:
            return self.fail(404, "no results for this job (yet)")
        self.reply(200, data, "text/csv; charset=utf-8",
                   [("Content-Disposition", f'attachment; filename="{jid}.csv"')])

    def do_GET(self):
        if not self.guard():
            return
        path, _, query = self.path.partition("?")
        store = self.server.store
        if path == "/api/v1/jobs":
            return self.reply(200, store.all() or None)  # null when empty, as before: the docs' health check
        m = JOB_PATH.match(path)
        if m:
            if m.group(2):
                return self.download(m.group(1))
            job = store.get(m.group(1))
            return self.reply(200, job) if job else self.fail(404, "job not found")
        if path == "/download":  # the old web UI's download link
            return self.download(urllib.parse.parse_qs(query).get("id", [""])[0])
        if path == "/":
            nonce = secrets.token_urlsafe(16)
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                   "connect-src 'self' https://raw.githubusercontent.com; img-src 'self' data:; "
                   "form-action 'self'; base-uri 'none'; "
                   "frame-ancestors 'none'")
            page = PAGE.format(nonce=nonce, version=VERSION, repo=REPO, sponsors=SPONSORS).encode()
            return self.reply(200, page, "text/html; charset=utf-8", [("Content-Security-Policy", csp)])
        if path == "/api/docs":
            return self.reply(200, OPENAPI)
        self.fail(404, "not found")

    def do_POST(self):
        if not self.guard():
            return
        if self.path != "/api/v1/jobs":
            return self.fail(404, "not found")
        body = self.body()
        if body is _REPLIED:
            return
        try:
            name, data = jobs.validate(body)
        except ValueError as e:
            return self.fail(422, str(e))
        jid = self.server.store.create(name, data)
        self.server.worker.wake.set()
        log(f"job {jid} created: {len(data['keywords'])} keyword(s)")
        self.reply(201, {"id": jid})

    def do_DELETE(self):
        if not self.guard():
            return
        m = JOB_PATH.match(self.path)
        if not m or m.group(2):
            return self.fail(404, "not found")
        self.server.worker.cancel(m.group(1))
        if not self.server.store.delete(m.group(1)):
            return self.fail(404, "job not found")
        log(f"job {m.group(1)} deleted")
        self.reply(200, {"message": "deleted"})


class Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR would let a second server share the port
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind also calls socket.getfqdn(), a reverse DNS lookup that took over
        # 30 s on a macOS runner before the engine could answer. The name is never used here.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


def port_taken(port):
    """True if something answers on 127.0.0.1:port. With SO_REUSEADDR, macOS lets a new server bind
    127.0.0.1:port while another listens on *:port (the Docker engine), and takes its traffic."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


class Server6(Server):
    address_family = socket.AF_INET6


def in_container():
    """True inside a Docker or Podman container (each leaves its marker file at the root)."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def make_servers(port, store, worker, api_key="", host="127.0.0.1"):
    """The server on host:port. On 127.0.0.1 also one on [::1]:port when this machine has IPv6
    loopback (Windows tries ::1 first for "localhost" and waits ~2 s before falling back)."""
    if port and port_taken(port):
        raise OSError(f"something already listens on port {port}")
    servers = [Server((host, port), Handler)]
    if host == "127.0.0.1":
        try:
            servers.append(Server6(("::1", servers[0].server_address[1]), Handler))
        except OSError:
            pass
    for s in servers:
        s.store, s.worker, s.api_key = store, worker, api_key
    return servers


def main():
    ap = argparse.ArgumentParser(description="The kit's Google Maps engine (local HTTP API and web UI).")
    ap.add_argument("--port", type=int, help="default: SCRAPER_PORT from the environment or .env, else 8080")
    ap.add_argument("--data", default=os.path.join(browser.HOME, "data"), help="jobs and results folder")
    ap.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "0.0.0.0"),
                    help="0.0.0.0 is for the Docker image only, where Docker publishes the port on the "
                         "host's 127.0.0.1 (default: 127.0.0.1)")
    a = ap.parse_args()
    if a.host != "127.0.0.1" and not in_container():
        # On a laptop or server 0.0.0.0 opens the engine to the network: the Host check stops DNS
        # rebinding from a browser, not a machine that sends "Host: localhost" itself.
        sys.exit("✗ --host 0.0.0.0 is for the Docker image only. On this machine the engine listens on "
                 "127.0.0.1, which keeps other machines out.")
    env = read_env()
    try:
        port = a.port or int(env["SCRAPER_PORT"] or 8080)
    except ValueError:
        sys.exit(f"✗ SCRAPER_PORT is not a number: {env['SCRAPER_PORT']!r}")
    browser.make_home()
    browser.sweep_profiles()
    store = jobs.Store(a.data)
    if n := store.recover():
        log(f"{n} job(s) were still working when the engine last stopped: marked failed, rows kept")
    worker = jobs.Worker(store, log)
    try:
        servers = make_servers(port, store, worker, env["SCRAPER_API_KEY"], a.host)
    except OSError as e:
        sys.exit(f"✗ Cannot listen on {a.host}:{port}: {e}")
    worker.start()
    chrome = browser.find_chrome()
    where = f"http://localhost:{port}" if a.host == "127.0.0.1" else \
        f"{a.host}:{port} in this container, published on the host's 127.0.0.1"
    log(f"engine {VERSION} on {where} (browser: {chrome or 'none found'}"
        f"{', API key required' if env['SCRAPER_API_KEY'] else ''})")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.shutdown()
        log("engine stopped")


if __name__ == "__main__":
    main()
