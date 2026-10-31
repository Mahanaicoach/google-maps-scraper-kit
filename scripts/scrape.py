#!/usr/bin/env python3
"""Google Maps scraper CLI for lead generation: turns a keyword and a place into a CSV of the 8 lead fields.

Uses only the Python standard library (no pip installs) and drives the kit's local engine.

Single keyword:
    python3 scripts/scrape.py "gyms in Miami FL" 25.7617 -80.1918 --depth 5

Auto-geocode (no coordinates needed):
    python3 scripts/scrape.py "cafes in Austin TX" --city "Austin, TX" --depth 5

Batch (one job, many keywords) from a file (one keyword per line):
    python3 scripts/scrape.py --keywords-file examples/queries.example.txt --city "Denver, CO"

Whole city past Google's ~120 results per search (3 x 3 map tiles, run one at a time, deduped on place_id):
    python3 scripts/scrape.py "plumbers in Phoenix AZ" --city "Phoenix, AZ" --grid 3

Check homepages only for emails, not contact and about pages:
    python3 scripts/scrape.py "gyms in Miami FL" --city "Miami, FL" --no-deep-emails

Notes:
- Required by the API: keywords, lat/lon (strings), max_time (SECONDS). This script fills them in.
- Geocoding uses OpenStreetMap Nominatim (free, no key). It allows ~1 request/sec and requires a
  descriptive User-Agent (set below). Do not loop it aggressively.
"""
import argparse, collections, csv, functools, io, ipaddress, json, math, os, re, shutil, socket, subprocess, sys, time
import html as _html, http.client, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

# The status lines below use ▶ ✓ ℹ •, and every file this script writes can hold accented business
# names. Windows picks cp1252 for both, so without this the first print raises UnicodeEncodeError
# before any work happens, and a CSV of scraped names lands in cp1252 instead of UTF-8.
# errors="replace" keeps a terminal that still cannot render a glyph from killing a finished run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):   # not a reconfigurable stream (pytest capture, a pipe wrapper)
        pass

ENV_KEYS = ("SCRAPER_BASE_URL", "SCRAPER_PORT", "SCRAPER_API_KEY", "SCRAPER_CONTAINER")


def load_env_file():
    """Read SCRAPER_* from the kit's .env, the same file docker compose reads, so a remapped
    port needs one edit. The last line for a key wins, as in compose, so a second SCRAPER_PORT
    line lower in the file (the second-copy block) is the one both sides use. Variables already
    set in the environment win over the file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, ".env")
    found = {}
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):  # shell-style .env files
                    line = line[7:]
                k, sep, v = line.partition("=")
                k, v = k.strip(), v.split(" #", 1)[0].strip().strip("\"'")  # drop a trailing comment
                if sep and k in ENV_KEYS:
                    found[k] = v
    except OSError:
        return
    for k, v in found.items():
        os.environ.setdefault(k, v)


load_env_file()
BASE = (os.environ.get("SCRAPER_BASE_URL") or f"http://localhost:{os.environ.get('SCRAPER_PORT') or '8080'}").rstrip("/")
KEY = os.environ.get("SCRAPER_API_KEY", "")
# The 8 lead fields: what you use to contact and qualify a lead.
# Everything else (geo coordinates, IDs, hours, images, review blobs) is dropped by default.
LEAD = ["title", "phone", "emails", "website", "category", "address", "review_rating", "review_count"]
UA = "google-maps-scraper-kit/1.6 (https://github.com/Mahanaicoach/google-maps-scraper-kit)"
# Business websites fetched at once by the email crawl, --socials and --tech. They are many
# different small sites, not Google, so this does not raise the rate-limit risk.
SITE_WORKERS = 16

POLL_EVERY = 8            # seconds between status checks
POLL_GRACE = 120          # the engine still has to flush results and flip the status
POLL_CEILING = 7200       # never wait longer than 2h, whatever --max-time asked for
ENGINE_MIN_RUNTIME = 180  # the engine floors anything below this (runner/webrunner)
# container_name in docker-compose.yml and SETUP's docker run; the one the script reads the log of
# and restarts. SCRAPER_CONTAINER in .env for a renamed container or a second stack.
ENGINE_CONTAINER = os.environ.get("SCRAPER_CONTAINER") or "gmaps-scraper"


def poll_attempts(max_time):
    """How many status checks to make before giving up on a job.

    Derived from the job's own max_time, because that is how long the engine is allowed to
    work: waiting any less abandons a job that is still running normally. The engine floors
    max_time at ENGINE_MIN_RUNTIME, so even a tiny --max-time needs that much patience, and
    POLL_CEILING stops an absurd value from parking the script for a day."""
    engine_runtime = max(ENGINE_MIN_RUNTIME, int(max_time or 0))
    budget = min(POLL_CEILING, engine_runtime + POLL_GRACE)
    return max(1, budget // POLL_EVERY)


_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def req(method, path, body=None):
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if KEY:
        headers["X-API-Key"] = KEY
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    # An HTTP_PROXY in the shell must not catch the local engine: that fails with a proxy error.
    opener = _NO_PROXY.open if urllib.parse.urlparse(BASE).hostname in _LOCAL_HOSTS else urllib.request.urlopen
    with opener(r, timeout=60) as resp:
        return resp.status, resp.read()


def geocode(place, with_bbox=False):
    """City/place name -> ('lat','lon') strings via Nominatim, or None.
    with_bbox=True returns ('lat','lon', [south, north, west, east]) instead."""
    q = urllib.parse.urlencode({"format": "json", "limit": 1, "q": place})
    url = f"https://nominatim.openstreetmap.org/search?{q}"
    r = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            hits = json.loads(resp.read())
        time.sleep(1)  # respect Nominatim's ~1 req/sec policy
        if hits:
            ll = (str(hits[0]["lat"]), str(hits[0]["lon"]))
            return ll + (hits[0].get("boundingbox"),) if with_bbox else ll
    except Exception as e:
        print(f"  (geocoding failed: {e})", file=sys.stderr)
    return None


def grid_cells(bbox, n):
    """Split a Nominatim bbox [south, north, west, east] into n x n cells -> [{lat, lon, radius, zoom}].
    radius (m) = 1.15 x half the cell diagonal (equirectangular), at least 1000.
    zoom = the Maps zoom whose ~1000px viewport is about as wide as the cell (11..17), so each
    search is launched looking at its own tile instead of a city-wide or block-wide view."""
    s, nn, w, e = (float(x) for x in bbox)
    dlat, dlon = (nn - s) / n, (e - w) / n
    m_per_deg = 6371000 * math.pi / 180
    cells = []
    for i in range(n):
        for j in range(n):
            lat, lon = s + (i + 0.5) * dlat, w + (j + 0.5) * dlon
            cos = math.cos(math.radians(lat))
            width = max(1.0, dlon * m_per_deg * cos)
            diag = math.hypot(dlat * m_per_deg, width)
            zoom = round(math.log2(156543.03 * cos * 1000 / width))  # 156543 m/px at zoom 0
            cells.append({"lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
                          "radius": max(1000, math.ceil(1.15 * diag / 2)),
                          "zoom": min(17, max(11, zoom)),
                          # tile geometry for split_cell(); "_" keys never reach the engine
                          "_dlat": dlat, "_dlon": dlon, "_level": 0})
    return cells


# Saturation splitting, OpenLeadFinder's fix for the ~120-per-search ceiling: a search that comes
# back "full" is hiding more, so it is re-run as 4 quarter tiles one zoom level closer, and those
# may split again up to SPLIT_LEVELS. The raw row count before dedupe is the signal. MAX_JOBS bounds
# grid tiles plus splits per place, so a dense metro cannot run forever.
SATURATION_ROWS = 100
SPLIT_LEVELS = 2
MAX_JOBS = 128
POINT_SPAN_DEG = 0.06  # what one plain search at zoom 15 roughly covers, about 6.5 km


def point_cell(lat, lon):
    """The root tile of a plain (non-grid) search: the engine body the kit always sent, zoom 15
    and radius 10000, plus the span split_cell() needs."""
    cos = max(0.1, math.cos(math.radians(float(lat))))
    return {"lat": str(lat), "lon": str(lon), "zoom": 15, "radius": 10000,
            "_dlat": POINT_SPAN_DEG, "_dlon": POINT_SPAN_DEG / cos, "_level": 0}


def split_cell(cell):
    """The 4 quarter tiles of a saturated cell: centres a quarter of the span away, half the span
    and radius, one zoom level closer."""
    lat, lon = float(cell["lat"]), float(cell["lon"])
    qlat, qlon = cell["_dlat"] / 4, cell["_dlon"] / 4
    return [dict(cell, lat=f"{lat + i * qlat:.6f}", lon=f"{lon + j * qlon:.6f}",
                 radius=max(1000, math.ceil(cell["radius"] / 2)), zoom=min(19, cell["zoom"] + 1),
                 _dlat=cell["_dlat"] / 2, _dlon=cell["_dlon"] / 2, _level=cell["_level"] + 1)
            for i in (-1, 1) for j in (-1, 1)]


def filters_active(a):
    """True when a row filter is on, so --limit cannot be judged before the filters run."""
    return any(getattr(a, k, None) for k in ("min_rating", "min_reviews", "no_website", "has_phone",
                                             "has_email", "exclude"))


def job_body(body, cell):
    """The engine body for one tile: the cell's lat/lon/zoom/radius, never its "_" geometry."""
    return dict(body, **{k: v for k, v in cell.items() if not k.startswith("_")})


def in_bbox(row, bbox, margin=0.10):
    """True if the row's latitude/longitude sit inside bbox [south, north, west, east] grown by
    `margin` of its size on each side. Rows with no usable coordinates are kept (True)."""
    try:
        lat, lon = float(row.get("latitude")), float(row.get("longitude"))
    except (TypeError, ValueError):
        return True
    s, n, w, e = (float(x) for x in bbox)
    my, mx = (n - s) * margin, (e - w) * margin
    return s - my <= lat <= n + my and w - mx <= lon <= e + mx


def _keys(r, weak=False):
    """The identities a row carries: place_id, cid, link, name+address. With weak=True also
    name+phone and name+website, which a --fields results file may be the only thing to match on.
    They are weak because two branches of a chain share a website and sometimes a phone, so they
    never dedupe rows within a run, only match an exclusion list."""
    ks = [f"{k}:{r[k]}" for k in ("place_id", "cid", "link") if r.get(k)]
    t = r.get("title")
    if t:
        ks.append(f"ta:{t}|{r.get('address', '')}")
        if weak:
            ks += [f"t{c[0]}:{t}|{r[c]}" for c in ("phone", "website") if r.get(c)]
    return ks


def dedupe(rows, exclude=()):
    """Keep the first row per place; drop rows already present in `exclude` (an earlier run)."""
    seen, out = {k for r in exclude for k in _keys(r, weak=True)}, []
    for r in rows:
        if not any(k in seen for k in _keys(r, weak=True)):
            seen.update(_keys(r)); out.append(r)
    return out


def load_rows(path):
    """Rows from an earlier results file, .csv or .json."""
    with open(path, encoding="utf-8-sig", newline="") as f:  # -sig: also reads the kit's own CSVs
        rows = json.load(f) if path.lower().endswith(".json") else list(csv.DictReader(f))
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError(f"{path} is not a results file (expected a list of rows)")
    return rows


def _local_docker():
    return urllib.parse.urlparse(BASE).hostname in ("localhost", "127.0.0.1", "::1") \
        and shutil.which("docker") is not None

def engine_run_ended(since):
    """True if the local engine's log shows a scraper run ending after `since` (UTC, ISO 8601).
    The scripts run one job at a time, so that is this job's run."""
    if not _local_docker():
        return False
    try:
        out = subprocess.run(["docker", "logs", "--since", since, ENGINE_CONTAINER],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and "scrapemate exited" in out.stdout + out.stderr

def engine_error(job_id):
    """The engine's own reason for a failed job, from the local log: "error scraping job <id>: ...".
    A proxy typo, for example, otherwise surfaces only as a generic "failed"."""
    if not _local_docker():
        return ""
    try:
        out = subprocess.run(["docker", "logs", "--since", "15m", ENGINE_CONTAINER],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    for line in reversed((out.stdout + out.stderr).splitlines()):
        if f"error scraping job {job_id}" in line:
            return line.split(f"job {job_id}:", 1)[-1].strip()[:200]
    return ""

def restart_engine():
    """Restart the local engine and wait until its API answers again. Without the wait, the next
    grid tile hit the proxy while the engine was still starting and failed with HTTP 502."""
    if not _local_docker():
        return False
    try:
        if subprocess.run(["docker", "restart", ENGINE_CONTAINER],
                          capture_output=True, timeout=90).returncode != 0:
            return False
    except (OSError, subprocess.SubprocessError):
        return False
    for _ in range(30):  # up to a minute; it usually answers within a few seconds
        try:
            req("GET", "/api/v1/jobs")
            return True
        except Exception:
            time.sleep(2)
    return False

def recover_stuck_job(job_id):
    """Engine v1.18.x can finish a job, rows written, without marking it done, and then holds every
    later job on "pending" until it restarts (gosom/google-maps-scraper#346). Keep the rows and
    restart the local engine so the next scrape runs."""
    rows = download_rows(job_id)
    print(f"\n⚠ The engine finished this job but did not mark it done (gosom/google-maps-scraper#346); "
          f"keeping its {len(rows)} rows.")
    try:  # otherwise it sits in the job list as "working" forever, with no Download button in the web UI
        req("DELETE", f"/api/v1/jobs/{job_id}")
        print("  Deleted the stuck job from the engine's list; its rows are in your file.")
    except Exception:
        print(f"  Remove it from the job list yourself: curl -X DELETE {BASE}/api/v1/jobs/{job_id}")
    if restart_engine():
        print("  Restarted the engine, which would otherwise hold every later job on \"pending\".")
    else:
        print("  If the next scrape stays \"pending\", run: docker restart " + ENGINE_CONTAINER)
    return rows

PENDING_RESTART_AFTER = 6  # polls (~48 s) a new job may sit on "pending" before an idle engine is judged stuck
FAILED_RETRY_WAIT = 60     # seconds before the one retry of a "failed" job (usually a passing Google block)


def engine_idle(seconds=45):
    """True if the local engine logged no scraper activity in the last `seconds`. A job that stays
    "pending" on an idle engine means an earlier job (web UI, raw API) left it wedged
    (gosom/google-maps-scraper#346); a busy engine is just finishing another job first."""
    if not _local_docker():
        return False
    try:
        out = subprocess.run(["docker", "logs", "--since", f"{seconds}s", ENGINE_CONTAINER],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    # A failing `docker logs` (wrong container name, daemon down) must not read as "idle".
    return out.returncode == 0 and "scrapemate" not in out.stdout + out.stderr


class JobFailed(RuntimeError):
    pass


def run_job(body):
    """Create one engine job, poll it, download it -> (job_id, raw CSV rows). A job the engine marks
    "failed" (usually a passing Google block) is retried once after a pause. Raises RuntimeError."""
    for attempt in (1, 2):
        try:
            return _run_job_once(body)
        except JobFailed as e:
            reason = e.args[1] if len(e.args) > 1 else engine_error(e.args[0])
            said = f" The engine says: {reason}." if reason else ""
            if "proxy" in reason.lower():  # a bad proxy fails the same way every time: do not retry
                raise RuntimeError(f"\n✗ Job failed: the proxy did not work.{said}\n"
                                   "  This engine needs user:pass for socks5 proxies; an http:// proxy works without.")
            if attempt == 2:
                raise RuntimeError(f"\n✗ Job failed twice.{said} You may be rate-limited — wait or add proxies.")
            print(f"\n⚠ Job failed.{said or ' Google may be throttling this IP.'} "
                  f"Waiting {FAILED_RETRY_WAIT} s, then retrying once…")
            time.sleep(FAILED_RETRY_WAIT)


def _get(path):
    """GET with three tries: a poll or a download must survive a passing 502 or timeout."""
    for attempt in range(3):
        try:
            return req("GET", path)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
        except OSError:
            if attempt == 2:
                raise
        time.sleep(3)


def _run_job_once(body, restarted=False):
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        _, raw = req("POST", "/api/v1/jobs", body)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"✗ Create failed: HTTP {e.code} — {e.read().decode()[:200]}")
    job_id = json.loads(raw).get("id")
    if not job_id:
        raise RuntimeError("✗ No job id returned.")
    print(f"  job id: {job_id}")

    attempts = poll_attempts(body.get("max_time"))
    print(f"▶ Polling (up to ~{attempts * POLL_EVERY // 60} min)…")
    status, ended, pending, n_rows, last_rows = None, 0, 0, None, None
    for i in range(attempts):
        _, raw = _get(f"/api/v1/jobs/{job_id}")
        status = json.loads(raw).get("Status")
        progress = ""
        if status == "working":  # the engine writes rows as it goes, so the download shows progress
            try:
                n_rows = len(download_rows(job_id))
                progress = f", {n_rows} rows so far"
            except Exception:
                pass
        print(f"\r  status: {str(status):<10} (attempt {i + 1}{progress})", end="", flush=True)
        if status == "ok":
            print(); break
        if status == "failed":
            raise JobFailed(job_id)
        if status == "working" and not n_rows:
            # A job the engine could not even start (a bad proxy, a driver error) stays "working"
            # forever with no rows, and holds every later job on "pending". Read the engine's reason,
            # drop the dead job, restart the engine and report.
            reason = engine_error(job_id)
            if reason:
                try:
                    req("DELETE", f"/api/v1/jobs/{job_id}")
                except Exception:
                    pass
                restart_engine()
                raise JobFailed(job_id, reason)
        # Stuck (gosom#346): the engine log says the run ended and the row count has stopped moving.
        # The second condition guards against a Docker VM clock that runs ahead of the host.
        if status == "working" and n_rows == last_rows and engine_run_ended(since):
            ended += 1
            if ended > 2:  # the status normally flips within seconds of the run ending
                return job_id, recover_stuck_job(job_id)
        last_rows = n_rows
        if status == "pending":
            pending += 1
            if pending % PENDING_RESTART_AFTER == 0 and not restarted and engine_idle():
                # Delete the pending job first, or the restarted engine runs it as well as the new one.
                print("\n⚠ The engine has not started this job and shows no activity: an earlier job left it "
                      "stuck (gosom/google-maps-scraper#346). Restarting the engine and re-creating the job…")
                try:
                    req("DELETE", f"/api/v1/jobs/{job_id}")
                except Exception:
                    pass
                if not restart_engine():
                    raise RuntimeError(f"\n✗ The engine never started this job and could not be restarted. "
                                       f"Run: docker restart {ENGINE_CONTAINER}")
                return _run_job_once(body, restarted=True)
        time.sleep(POLL_EVERY)
    else:
        # The engine writes rows as it goes, and v1.18.x can leave a finished job stuck on
        # "working" (gosom/google-maps-scraper#346), so take whatever is on disk.
        rows = download_rows(job_id)
        if not rows:
            raise RuntimeError(
                f"\n✗ Gave up waiting after ~{attempts * POLL_EVERY // 60} min with no rows yet. The "
                f"engine keeps the job, so check it later:\n"
                f"    curl {BASE}/api/v1/jobs/{job_id}\n"
                f"  and once its Status is \"ok\", download the CSV with\n"
                f"    curl -o results.csv {BASE}/api/v1/jobs/{job_id}/download")
        print(f"\n⚠ Job still \"{status}\" after ~{attempts * POLL_EVERY // 60} min; "
              f"keeping the {len(rows)} rows it has written. If the next scrape stays \"pending\", "
              f"restart the engine: docker restart {ENGINE_CONTAINER}")
        return job_id, rows

    return job_id, download_rows(job_id)


def download_rows(job_id):
    _, raw = _get(f"/api/v1/jobs/{job_id}/download")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))


def collect_keywords(a):
    kws = []
    if a.keyword:
        kws.append(a.keyword)
    kws.extend(a.also or [])
    if a.keywords_file:
        try:
            with open(a.keywords_file, encoding="utf-8-sig") as f:
                lines = [line.strip() for line in f]
            kws.extend(line for line in lines if line and not line.startswith("#"))
        except (OSError, UnicodeDecodeError) as e:
            sys.exit(f"✗ Cannot read --keywords-file {a.keywords_file}: {getattr(e, 'strerror', e)}")
    return list(dict.fromkeys(kws))  # de-dupe, keep order


# ── Optional social enrichment (Instagram / Facebook / LinkedIn) ──────────────
# Pure code (HTTP + regex) → ZERO LLM tokens. Visits each business website once.
SOCIAL_RE = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)", re.I),
    "facebook":  re.compile(r"https?://(?:www\.|m\.|web\.)?facebook\.com/([A-Za-z0-9_.\-]+)", re.I),
    "linkedin":  re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in|school)/([A-Za-z0-9_.\-%]+)", re.I),
    "tiktok":    re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]+)", re.I),
    "youtube":   re.compile(r"https?://(?:www\.)?youtube\.com/(?:@|channel/|c/|user/)([A-Za-z0-9_.\-]+)", re.I),
    "x":         re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})\b", re.I),
}
SOCIAL_FIELDS = list(SOCIAL_RE)
_SKIP_HANDLE = {"", "home", "pages", "people", "help", "about", "policies", "policy",
                "legal", "tos", "privacy", "settings", "sharer", "tr", "profile.php",
                "plugins", "dialog", "intent", "login", "share.php", "permalink.php",
                "p", "reel", "reels", "explore", "stories", "tv", "watch", "events",
                "groups", "marketplace", "gaming", "photo", "hashtag", "search", "share", "i"}

def _is_public_ip(ip):
    try:
        return ipaddress.ip_address(ip.split("%")[0]).is_global
    except ValueError:
        return False

@functools.lru_cache(maxsize=1024)
def _is_public_host(host):
    """True only if every address the host resolves to is public. Websites come from third-party
    listings, so one could point a fetch at this machine or its network instead (the engine API on
    localhost:8080, a router admin page, a cloud metadata endpoint)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    return bool(infos) and all(_is_public_ip(i[4][0]) for i in infos)

def _public_connection(address, *args, **kwargs):
    """socket.create_connection that refuses a private peer. The connect resolves the host again,
    so a DNS answer that changed after _is_public_host (rebinding) is caught here, before a byte
    of the request is sent."""
    sock = socket.create_connection(address, *args, **kwargs)
    peer = sock.getpeername()[0]
    if not _is_public_ip(peer):
        sock.close()
        raise OSError(f"refused non-public address {peer}")
    return sock

class _PublicPeer:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _public_connection

class _PublicHTTPConnection(_PublicPeer, http.client.HTTPConnection):
    pass

class _PublicHTTPSConnection(_PublicPeer, http.client.HTTPSConnection):
    pass

class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTPConnection, req)

class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)

class _PublicRedirectsOnly(urllib.request.HTTPRedirectHandler):
    """A public site may not redirect the fetch to a private address either."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_public_host(urllib.parse.urlparse(newurl).hostname or ""):
            return None  # urllib then raises HTTPError, which _fetch_html turns into ""
        return super().redirect_request(req, fp, code, msg, headers, newurl)

# Behind a proxy from the environment the socket peer is the proxy, often on a private address,
# so only the resolve-time check applies there.
_HANDLERS = [_PublicRedirectsOnly]
if not urllib.request.getproxies():
    _HANDLERS += [_PublicHTTPHandler, _PublicHTTPSHandler]
_OPENER = urllib.request.build_opener(*_HANDLERS)

def _fetch_html(url, timeout=10):
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        if not _is_public_host(urllib.parse.urlparse(url).hostname or ""):
            return ""
        r = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; " + UA + ")", "Accept": "text/html"})
        with _OPENER.open(r, timeout=timeout) as resp:
            # Read in chunks against a wall-clock deadline: a site that drips bytes forever would
            # otherwise hold one of the SITE_WORKERS until 400 KB arrived.
            data, deadline = b"", time.monotonic() + 2 * timeout
            while len(data) < 400_000 and time.monotonic() < deadline:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                data += chunk
            return data[:400_000].decode("utf-8", "replace")
    except Exception:
        return ""

@functools.lru_cache(maxsize=256)  # ponytail: homepages only (up to 400 KB each); evicted pages just refetch
def _fetch_cached(url):
    """Homepage fetch shared by the email crawl, --socials and --tech (one fetch per site)."""
    return _fetch_html(url, timeout=8)

def _find_socials(html):
    out = {k: "" for k in SOCIAL_FIELDS}
    for plat, rx in SOCIAL_RE.items():
        for m in rx.finditer(html or ""):
            h = m.group(1).lower()
            if h in _SKIP_HANDLE:
                continue
            if plat == "facebook" and (h.isdigit() or len(h) < 3):  # skip junk like /2008
                continue
            out[plat] = m.group(0).rstrip('"\'/').replace("\\", "")
            break
    return out

def _each_site(todo, work, label, workers=None):
    """Run work(row) over the rows, SITE_WORKERS at a time, with one progress line. One row that
    blows up (a malformed website, an odd page) is skipped; it never aborts the run."""
    done, failed = 0, 0
    def safe(r):
        try:
            work(r)
        except Exception:
            return 1
        return 0
    with ThreadPoolExecutor(max_workers=workers or SITE_WORKERS) as ex:
        for f in ex.map(safe, todo):
            done, failed = done + 1, failed + f
            print(f"\r  {label}: {done}/{len(todo)} checked", end="", flush=True)
    print(f"  ({failed} skipped after an error)" if failed else "")

def enrich_sites(results, find, fields, label, workers=None):
    """Merge find(homepage html) into every lead that has a website. Zero LLM tokens."""
    for r in results:                       # make sure every row has the keys
        for k in fields:
            r.setdefault(k, "")
    _each_site([r for r in results if r.get("website")],
               lambda r: r.update(find(_fetch_cached(r["website"]))), label, workers)


# ── Optional ad and tech signals (--tech): who already spends on marketing ──────
# Regexes from OpenLeadFinder's website layer. They run on the homepage already fetched above.
TECH_RE = {
    "fb_pixel":       re.compile(r"connect\.facebook\.net/[^\"']*fbevents\.js|fbq\(", re.I),
    "google_ads_tag": re.compile(r"googleadservices\.com|AW-\d{6,}|googleads\.g\.doubleclick\.net", re.I),
    "analytics":      re.compile(r"googletagmanager\.com/gtag|gtag\(|UA-\d{4,}|G-[A-Z0-9]{6,}", re.I),
    "bing_ads":       re.compile(r"bat\.bing\.com|window\.uetq", re.I),
    "linkedin_insight": re.compile(r"snap\.licdn\.com|_linkedin_partner_id", re.I),
    "tiktok_pixel":   re.compile(r"analytics\.tiktok\.com|ttq\.load\(", re.I),
}
CMS_RE = [
    ("wordpress",   re.compile(r"/wp-content/|/wp-includes/", re.I)),
    ("shopify",     re.compile(r"cdn\.shopify\.com|shopify\.theme", re.I)),
    ("wix",         re.compile(r"static\.wixstatic\.com|_wixcss|wix\.com", re.I)),
    ("squarespace", re.compile(r"static1\.squarespace|squarespace\.com", re.I)),
    ("webflow",     re.compile(r"webflow\.com", re.I)),
]
VIEWPORT_RE = re.compile(r"<meta[^>]+name=[\"']viewport[\"']", re.I)
TECH_FIELDS = list(TECH_RE) + ["cms", "mobile_friendly"]

def _find_tech(html):
    """yes/no per signal, the site platform, and mobile support. Blank when the site did not load,
    so "unknown" never reads as "no pixel"."""
    if not html:
        return {k: "" for k in TECH_FIELDS}
    yn = lambda hit: "yes" if hit else "no"
    out = {k: yn(rx.search(html)) for k, rx in TECH_RE.items()}
    out["cms"] = next((name for name, rx in CMS_RE if rx.search(html)), "")
    out["mobile_friendly"] = yn(VIEWPORT_RE.search(html))
    return out

def _num(v, kind=float):
    try:
        return kind(str(v).replace(",", ""))
    except ValueError:
        return 0

def intent_score(r):
    """0-100 "already spends on marketing" score: ad pixels dominate, an established, reachable
    business adds a little. A heuristic, adapted from OpenLeadFinder without its live ad checks."""
    s = 35 * (r.get("fb_pixel") == "yes") + 25 * (r.get("google_ads_tag") == "yes")
    s += 10 * (r.get("analytics") == "yes")
    reviews = _num(r.get("review_count"), int)
    s += 15 if reviews >= 50 else 8 if reviews >= 10 else 0
    s += 10 * (_num(r.get("review_rating")) >= 4.0)
    s += 5 * bool(r.get("emails"))
    return s


# ── Email crawl: each business website's homepage, then its contact/about pages ──
# The lookbehind pins each match attempt to the start of a run of local-part characters: without
# it the scan is quadratic, and a 60 KB run of "a" (a minified blob) took a minute per page.
_LOCAL = r"[A-Za-z0-9._%+-]"
EMAIL_RE = re.compile(rf"(?<!{_LOCAL}){_LOCAL}+@[A-Za-z0-9.-]+\.[A-Za-z]{{2,}}")
MAILTO_RE = re.compile(r"mailto:([^\"'>\s?]+)", re.I)
HREF_RE = re.compile(r"<a\s[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_JUNK_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".css", ".js")
_JUNK_DOMAIN = ("example", "sentry", "wixpress", "wix.com", "godaddy", "squarespace", "wordpress",
                "domain.com", "email.com", "yourdomain", "mysite.com", "schema.org", "w3.org", "sentry.io",
                # link-in-bio pages and website agencies: their support address is not the business's
                "linktr.ee", "linkin.bio", "beacons.ai", "officite")
_JUNK_LOCAL = {"name", "email", "user", "you", "someone", "example", "username",
               "youremail", "yourname", "firstname", "lastname", "sample"}
# A JSON-escaped <, >, ", ' or & glued to the address (">orders@x.com" in page source),
# which the regex reads as part of the local part: u003eorders@x.com.
_ESCAPE_PREFIX = re.compile(r"^(?:u00(?:3c|3e|22|27|26))+")

def _host(url):
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        h = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:  # "http://[bad": a malformed website value from a listing
        return ""
    return h[4:] if h.startswith("www.") else h

# Google Business Profile links often carry campaign tags (?utm_source=gbp&utm_medium=organic),
# about one website in four. They bloat the column and hide that two rows share one site.
_TRACKING_PARAMS = ("utm_", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid")

def clean_website(url):
    """Drop tracking parameters and the fragment. Other parameters stay: some store pages need
    them (store?storeid=...)."""
    if not url:
        return ""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:  # malformed: leave it as the listing had it
        return url
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    kept = [(k, v) for k, v in q if not k.lower().startswith(_TRACKING_PARAMS)]
    frag = p.fragment if p.fragment.startswith(("/", "!")) else ""  # keep hash-routed store pages
    if len(kept) == len(q) and frag == p.fragment:
        return url  # nothing to drop: never re-encode a query the site chose
    query = p.query if len(kept) == len(q) else urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit(p._replace(query=query, fragment=frag))

def is_real_email(e):
    """False for anything not worth sending to: a template placeholder (user@domain.com,
    example@gmail.com), a vendor/boilerplate domain, or an image path that parsed as an address."""
    local, _, dom = e.rpartition("@")
    if not EMAIL_RE.fullmatch(e) or e.endswith(_JUNK_EXT):
        return False
    labels = re.split(r"[.-]", dom)  # whole labels: sentry.wixpress.com is junk, sentrysecurity.com is not
    junk = any((dom == j or dom.endswith("." + j)) if "." in j else j in labels for j in _JUNK_DOMAIN)
    return local not in _JUNK_LOCAL and not junk


# "name [at] domain [dot] com", "name (at) domain dot com" and friends, adapted from OpenLeadFinder.
# The "at" must be bracketed and the domain must end in a known TLD: a bare " at " also matched
# prose such as "meet at noon dot com".
_AT = r"(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\})"
_DOT = r"(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+)"
_TLD = (r"(?:com|net|org|co|io|biz|info|us|uk|ca|au|de|fr|es|it|nl|eu|edu|gov|me|app|dev|"
        r"shop|online|store|xyz|ai)")
_DEOBF_RE = re.compile(rf"(?<!{_LOCAL})({_LOCAL}+)\s*{_AT}\s*([A-Za-z0-9.\-]+?)\s*{_DOT}\s*({_TLD})\b", re.I)

def extract_emails(html, site_host):
    """Emails from mailto: links, plain text and obfuscated spans, junk dropped, site-domain first,
    max 3. HTML entities are decoded first, so info&#64;site.com counts."""
    site = _host(site_host or "")
    text = _html.unescape(html or "")[:400_000]
    found = []
    deobf = [f"{m[0]}@{m[1]}.{m[2]}" for m in _DEOBF_RE.findall(text)]
    for e in MAILTO_RE.findall(text) + EMAIL_RE.findall(text) + deobf:
        e = _ESCAPE_PREFIX.sub("", urllib.parse.unquote(e).strip().lower())
        if not is_real_email(e) or e in found:
            continue
        found.append(e)
    on_site = lambda e: site and (e.endswith("@" + site) or e.endswith("." + site))
    return sorted(found, key=lambda e: not on_site(e))[:3]

def contact_links(html, base_url):
    """Up to 3 same-site contact/about URLs: discovered links first, then fixed fallbacks."""
    if not base_url.startswith(("http://", "https://")):
        base_url = "http://" + base_url
    host, out = _host(base_url), []
    if not host:  # malformed or empty website: nothing to join the paths onto
        return out
    for href, text in HREF_RE.findall(html or ""):
        if not re.search(r"contact|about|team|staff|support", href + " " + text, re.I):
            continue
        try:
            url = urllib.parse.urldefrag(urllib.parse.urljoin(base_url, href.strip()))[0]
        except ValueError:  # a malformed href on the page
            continue
        if url.startswith(("http://", "https://")) and _host(url) == host and url not in out:
            out.append(url)
    for path in ("/contact", "/contact-us", "/about", "/about-us"):
        url = urllib.parse.urljoin(base_url, path)
        if len(out) < 3 and url not in out:
            out.append(url)
    return out[:3]

def enrich_emails(results, deep=True, workers=None):
    """Fill empty `emails` from the homepage, then (deep) up to 3 contact/about pages. Zero LLM tokens."""
    def work(r):
        site = r["website"]
        home = _fetch_cached(site)
        found = extract_emails(home, site)
        if deep and home and not found:
            pages = "\n".join(_fetch_html(u, timeout=8) for u in contact_links(home, site))
            found = extract_emails(pages, site)
        r["emails"] = ", ".join(found)
    _each_site([r for r in results if r.get("website") and not r.get("emails")], work, "emails", workers)


# ── Optional email domain check (--verify-emails): can the domain receive mail, and who hosts it ──
def _doh_mx(domain):
    """MX lookup over DNS-over-HTTPS (Google's public resolver, no key) -> the JSON answer."""
    url = "https://dns.google/resolve?" + urllib.parse.urlencode({"name": domain, "type": "MX"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=8) as r:
        return json.load(r)

@functools.lru_cache(maxsize=1024)
def mx_host(domain):
    """'nxdomain' when the domain does not exist, 'none' when it has no MX record, else who runs
    its mail: 'google_workspace', 'microsoft_365' or 'other'. '' when the lookup itself failed."""
    try:
        d = _doh_mx(domain)
    except Exception:
        return ""
    if d.get("Status") == 3:
        return "nxdomain"
    mx = " ".join(a.get("data", "") for a in d.get("Answer") or [] if a.get("type") == 15).lower()
    if not mx:
        return "none" if d.get("Status") == 0 else ""
    if "google.com" in mx or "googlemail.com" in mx:
        return "google_workspace"
    return "microsoft_365" if "outlook.com" in mx else "other"

def verify_emails(rows):
    """Drop addresses on domains that do not exist; label the rest with email_host."""
    def work(r):
        kept = [(e, mx_host(e.rpartition("@")[2])) for e in r["emails"].split(", ") if e]
        kept = [(e, h) for e, h in kept if h != "nxdomain"]
        r["emails"] = ", ".join(e for e, _ in kept)
        r["email_host"] = kept[0][1] if kept else ""
    _each_site([r for r in rows if r.get("emails")], work, "email domains", workers=8)


def keep_row(r, a):
    """The row filters, minus --has-email (applied after the crawl). A missing rating or review
    count reads as 0, so --min-rating and --min-reviews drop unrated listings."""
    return not ((a.min_rating and _num(r.get("review_rating")) < a.min_rating)
                or (a.min_reviews and _num(r.get("review_count"), int) < a.min_reviews)
                or (a.no_website and r.get("website"))
                or (a.has_phone and not r.get("phone")))


def scrape_place(a, body, city, keywords):
    """All the engine jobs for one place: a single search, or --grid tiles, one at a time, clipped to
    the city box when there is one. A search that comes back full (SATURATION_ROWS) splits into 4
    closer searches unless --no-split -> (first job id or None, rows, failed job count, job count)."""
    lat, lon, bbox, cells = a.lat, a.lon, None, None
    if a.grid:  # always geocodes, because it needs the bounding box
        print(f"▶ Geocoding \"{city}\"…")
        geo = geocode(city, with_bbox=True)
        if not geo or not geo[2]:
            print("✗ Could not resolve a bounding box. Try a clearer --city.")
            return None, [], 0, 0
        lat, lon, bbox = geo
        cells = grid_cells(bbox, a.grid)
        print(f"  → bbox S{bbox[0]} N{bbox[1]} W{bbox[2]} E{bbox[3]}, {len(cells)} tiles")
    elif city or not (lat and lon):  # explicit lat/lon, else geocode --city, else the first keyword
        place = city or keywords[0]
        print(f"▶ Geocoding \"{place}\"…")
        coords = geocode(place)
        if not coords:
            print("✗ Could not resolve coordinates. Pass lat/lon or a clearer --city.")
            return None, [], 0, 0
        lat, lon = coords
        print(f"  → {lat}, {lon}")
    # The job name is what the web UI lists, so make it say what the job was.
    body = dict(body, lat=str(lat), lon=str(lon), name=(f"{keywords[0]} @ {city}" if city else keywords[0])[:80])
    for k in keywords:
        print(f"    • {k}")
    if cells:
        print(f"▶ Grid {a.grid}x{a.grid}: {len(cells)} jobs, one at a time, {len(keywords)} keyword(s) depth={a.depth}")
    else:
        print(f"▶ Creating job: {len(keywords)} keyword(s) @ {lat},{lon} depth={a.depth}")
    queue = list(cells or [point_cell(lat, lon)])
    job_id, rows, skipped, i, total, capped, warned = None, [], 0, 0, len(queue), False, False
    enough = a.limit if a.limit and not filters_active(a) else None  # rows wanted; None = all
    while queue:  # sequential on purpose: never parallel jobs against Google
        cell, i = queue.pop(0), i + 1
        tiled = bool(cells) or cell["_level"] > 0
        if tiled:
            print(f"▶ Cell {i}/{total} @ {cell['lat']},{cell['lon']} radius={cell['radius']}m zoom={cell['zoom']}")
        try:
            jid, cell_rows = run_job(job_body(body, cell))
        except KeyboardInterrupt:  # keep what the finished jobs returned instead of losing them all
            print(f"\n⚠ Stopped early: keeping the rows from the {i - 1 - skipped} finished job(s).")
            skipped, a.stop = skipped + 1 + len(queue), True
            break
        except Exception as e:
            if not tiled:  # the one plain search failed: nothing to keep
                print(str(e))
                return None, [], 0, 0
            print(f"\n  ⚠ cell {i}/{total} skipped: {str(e).strip()}")
            skipped += 1
            continue
        job_id = job_id or jid
        rows = dedupe(rows + cell_rows)
        if tiled:
            print(f"  cell {i}/{total}: {len(cell_rows)} rows, {len(rows)} unique so far")
        if enough and len(rows) >= enough:
            print(f"  --limit {enough} reached: skipping the remaining {len(queue)} job(s)")
            total -= len(queue)
            break
        # Google's cap is per search, so a batch job is "full" when its biggest keyword is.
        per_keyword = max([0] + list(collections.Counter(r.get("input_id") for r in cell_rows).values()))
        if a.split and per_keyword >= SATURATION_ROWS and cell["_level"] < SPLIT_LEVELS:
            if total + 4 > MAX_JOBS:
                if not capped:
                    print(f"  ⚠ {MAX_JOBS} jobs reached for this place; dense areas left unsplit.")
                capped = True
                continue
            print(f"  ⚡ dense area ({per_keyword} rows): splitting it into 4 closer searches")
            if not warned:
                print("  ℹ Each split adds 4 jobs against Google; add --no-split to keep one job per search.")
                warned = True
            queue[0:0] = split_cell(cell)  # look closer right away, then carry on with the grid
            total += 4
    if bbox:  # radius does not clip results in normal mode, so tiles leak outside the city
        kept = [r for r in rows if in_bbox(r, bbox)]
        print(f"  dropped {len(rows) - len(kept)} rows outside the city box")
        rows = kept
    return job_id, rows, skipped, total


def main():
    ap = argparse.ArgumentParser(description="Scrape Google Maps business listings.")
    ap.add_argument("keyword", nargs="?", help='e.g. "coffee shops in Austin TX"')
    ap.add_argument("lat", nargs="?", help="latitude (optional if --city given)")
    ap.add_argument("lon", nargs="?", help="longitude (optional if --city given)")
    ap.add_argument("--keyword", dest="also", action="append", help="extra keyword (repeatable)")
    ap.add_argument("--keywords-file", help="file with one keyword per line (batch)")
    ap.add_argument("--city", action="append",
                    help="city/place to auto-geocode for lat/lon; repeatable, one run per city into one file")
    ap.add_argument("--proxy", action="append", metavar="URL",
                    help="proxy for the engine, repeatable: http://, https://, socks5:// or socks5h://; "
                         "this engine needs user:pass@ for socks5; the engine rotates them")
    ap.add_argument("--depth", type=int, default=5)
    # The engine passes this to Google as the interface language, which decides the language of
    # the category names it returns (and nudges which listings come back). It has to match the
    # market you are scraping: "en" on a Brazilian city labels every row "Bakery" instead of
    # "Padaria". The engine validates it as exactly 2 characters.
    ap.add_argument("--lang", default="en", metavar="XX",
                    help="interface language the engine asks Google for, 2 letters "
                         "(default: en; use the local language, e.g. pt, es, de)")
    # Emails are ON by default (they're the most valuable lead field): the script crawls each
    # business website after the Maps job. --no-email skips the crawl.
    ap.add_argument("--no-email", dest="email", action="store_false", default=True,
                    help="skip the website crawl for emails (emails are on by default)")
    ap.add_argument("--no-deep-emails", dest="deep_emails", action="store_false",
                    help="check homepages only, not contact and about pages")
    ap.add_argument("--grid", type=int, metavar="N",
                    help="split the --city area into N x N map tiles, one job each, deduped "
                         "(covers a whole city past Google's ~120 per search cap; max 10)")
    ap.add_argument("--no-split", dest="split", action="store_false", default=True,
                    help=f"do not re-run a search that comes back full ({SATURATION_ROWS}+ rows) as 4 "
                         f"closer searches; by default every full search or tile splits, up to "
                         f"{SPLIT_LEVELS} levels and {MAX_JOBS} jobs per place")
    ap.add_argument("--max-time", type=int, default=600,
                    help="job time limit in SECONDS; the script waits that long for the "
                         "engine, plus a short grace (default: 600)")
    ap.add_argument("--out", default=None, help="output file path (default: results-<id>.csv). "
                    "The extension picks the format: .csv (default) or .json")
    ap.add_argument("--json", action="store_true", help="write JSON instead of the default CSV")
    ap.add_argument("--full", action="store_true", help="keep ALL raw columns (default: lean lead fields)")
    ap.add_argument("--fields", help="comma-separated columns to keep (overrides the default lead set)")
    ap.add_argument("--socials", action="store_true",
                    help="also find Instagram/Facebook/LinkedIn from each website (0 LLM tokens; slower)")
    ap.add_argument("--tech", action="store_true",
                    help="also detect Meta Pixel, Google Ads tag, analytics, Bing, LinkedIn and TikTok "
                         "pixels, site platform and mobile support on each website, and sort by an "
                         "intent_score (0 LLM tokens; slower)")
    ap.add_argument("--verify-emails", action="store_true",
                    help="check each email domain's MX record over DNS (dns.google, no key): drops "
                         "addresses on domains that do not exist and adds an email_host column "
                         "(google_workspace, microsoft_365, other, none)")
    f = ap.add_argument_group("filters", "applied before the email crawl, except --has-email")
    f.add_argument("--min-rating", type=float, metavar="X", help="keep rows rated X or higher")
    f.add_argument("--min-reviews", type=int, metavar="N", help="keep rows with N or more reviews")
    f.add_argument("--no-website", action="store_true", help="keep only businesses without a website")
    f.add_argument("--has-phone", action="store_true", help="keep only rows with a phone number")
    f.add_argument("--has-email", action="store_true", help="keep only rows where an email was found")
    f.add_argument("--exclude", action="append", metavar="FILE",
                   help="drop places already in this earlier results file (.csv or .json); repeatable")
    f.add_argument("--limit", type=int, metavar="N", help="stop at N rows after filtering")
    a = ap.parse_args()

    keywords = collect_keywords(a)
    if not keywords:
        sys.exit("✗ No keywords. Pass a keyword, --keyword, or --keywords-file.")

    if len(a.lang) != 2 or not a.lang.isalpha():
        sys.exit(f"✗ --lang must be a 2-letter code (got {a.lang!r}); the engine rejects anything else.")
    if a.depth < 1:
        sys.exit("✗ --depth must be 1 or more.")
    if a.max_time < 1:
        sys.exit("✗ --max-time must be 1 or more seconds (the engine runs a job for at least 180).")
    if a.limit is not None and a.limit < 1:
        sys.exit("✗ --limit must be 1 or more.")
    if a.has_email and not a.email:
        sys.exit("✗ --has-email needs the email crawl: drop --no-email.")
    if a.verify_emails and not a.email:
        sys.exit("✗ --verify-emails needs the email crawl: drop --no-email.")
    if a.json and a.out and a.out.lower().endswith(".csv"):
        sys.exit("✗ --json with a .csv --out: drop --json, or name the file .json.")
    if not a.city and a.lat and a.lon:
        try:
            float(a.lat), float(a.lon)
        except ValueError:
            sys.exit(f"✗ lat and lon must be numbers, e.g. 30.2672 -97.7431 (got {a.lat!r} {a.lon!r}).")
    a.stop = False  # set by scrape_place on Ctrl-C, so the next --city is not started
    if a.city and (a.lat or a.lon):
        print("ℹ --city given, so the lat/lon arguments are ignored.")
    if a.out and os.path.dirname(a.out):  # fail now, not after the scrape, if the folder is unusable
        try:
            os.makedirs(os.path.dirname(a.out), exist_ok=True)
        except OSError as e:
            sys.exit(f"✗ Cannot create the folder for --out: {e}")

    if a.grid is not None:
        if not a.city:
            sys.exit("✗ --grid needs --city: the city's bounding box is what gets split into tiles.")
        if not 1 <= a.grid <= 10:
            sys.exit(f"✗ --grid must be between 1 and 10 (10 x 10 = 100 tiles; with splits at most {MAX_JOBS} jobs per place).")

    cities = a.city or [None]
    try:
        excluded = [r for p in a.exclude or [] for r in load_rows(p)]
    except (OSError, ValueError) as e:
        sys.exit(f"✗ Cannot read --exclude file: {e}")

    # --- WARN, don't block: flag high-volume jobs (proceed anyway) ---
    if a.depth >= 15 or len(keywords) >= 10 or (a.grid or 1) ** 2 >= 4 or len(cities) >= 3:
        print("⚠️  Heads-up: this is a large job (high depth, many keywords or a grid).")
        print("    Running it back-to-back without proxies can get your IP temporarily rate-limited by")
        print("    Google (clears in minutes–hours; returns empty/failed jobs meanwhile). For big or")
        print("    repeated runs, add proxies. Proceeding…\n")

    # Health check. Right after `docker compose up -d` the proxy answers 502 while the engine starts
    # (up to a minute), and the README's next line runs at once, so wait for that. Nothing listening at
    # all (connection refused) means the stack is down: say so after 10 s, not after a minute of dots.
    refused, err = 0, None
    for i in range(30):
        try:
            req("GET", "/api/v1/jobs")
            err = None
            break
        except urllib.error.HTTPError as e:
            if e.code < 500:  # the engine is up but refuses us (wrong API key, wrong URL): say so at once
                sys.exit(f"✗ The engine at {BASE} answered HTTP {e.code} to the health check: {e.read().decode()[:200]}")
            err = e
        except Exception as e:
            err = e
            refused += isinstance(getattr(e, "reason", e), ConnectionRefusedError)
        if refused >= 5:
            break
        print("\r  waiting for the engine to start" + "." * (i + 1), end="", flush=True)
        time.sleep(2)
    if err is not None:
        sys.exit(f"\n✗ Scraper not reachable at {BASE}. Is Docker Desktop open? Start the engine with "
                 f"'docker compose up -d' in the kit folder, wait 10 seconds, then try again.\n  ({err})")
    if i:
        print()

    body = {"keywords": keywords, "lang": a.lang, "zoom": 15,
            "fast_mode": False, "radius": 10000,
            # The engine's own email pass visits every website in a browser, one per worker: it took
            # most of each job and found fewer emails than the crawl below (OpenLeadFinder's
            # approach), so the engine only reads Google Maps.
            "depth": a.depth, "email": False, "max_time": a.max_time}
    if a.proxy:
        body["proxies"] = a.proxy
    job_id, rows, skipped, ncells, failed_places = None, [], 0, 0, 0
    for city in cities:  # one place at a time, like the grid tiles
        jid, place_rows, s, n = scrape_place(a, body, city, keywords)
        job_id, skipped, ncells = job_id or jid, skipped + s, ncells + n
        failed_places += jid is None
        rows = dedupe(rows + place_rows)
        if a.stop:
            break
    if job_id is None:
        sys.exit("✗ No job finished; the reason is in the ✗ line above.")
    if failed_places:
        print(f"⚠ Incomplete: {failed_places} of {len(cities)} places returned nothing (see the ✗ lines above).")
    for r in rows:
        r["website"] = clean_website(r.get("website", ""))
    before = len(rows)
    rows = [r for r in dedupe(rows, exclude=excluded) if keep_row(r, a)]
    if len(rows) != before:
        print(f"  filters and --exclude kept {len(rows)} of {before} rows")
    if a.limit and not a.has_email:
        rows = rows[:a.limit]
    if a.full:
        fields = list(rows[0].keys()) if rows else LEAD
    elif a.fields:
        fields = [c.strip() for c in a.fields.split(",") if c.strip()]
    else:
        fields = LEAD
    if a.has_email and "emails" not in fields:
        sys.exit("✗ --has-email needs the emails column: add emails to --fields.")
    if a.verify_emails and "emails" not in fields:
        sys.exit("✗ --verify-emails needs the emails column: add emails to --fields.")
    if before and not rows:
        print(f"⚠ Nothing left: the filters and --exclude dropped all {before} rows. Loosen them and rerun; "
              f"the file below has only a header.")
    else:
        print(f"✓ Done — {len(rows)} businesses (fields: {', '.join(fields)}).")
    if skipped:
        print(f"⚠ Incomplete: {skipped} of {ncells} tiles failed, so parts of the area are missing. Rerun later.")

    if a.email and "emails" in fields and not a.no_website:  # no website, nothing to crawl
        pages = "homepage, then contact and about pages" if a.deep_emails else "homepage only"
        print(f"▶ Emails: checking each business website ({pages}, {SITE_WORKERS} at a time)…")
        enrich_emails(rows, deep=a.deep_emails)
        total, n = sum(1 for r in rows if r.get("emails")), len(rows)
        print(f"  emails found for {total}/{n} businesses ({round(100 * total / n) if n else 0}%)")
        if a.verify_emails:
            print("▶ Checking each email domain's MX record over DNS (dns.google)…")
            verify_emails(rows)
            fields = fields + ["email_host"]
            dead = total - sum(1 for r in rows if r.get("emails"))
            print(f"  {dead} businesses lost their addresses: the domains do not exist")
    if a.has_email:
        rows = [r for r in rows if r.get("emails")][:a.limit or None]
        print(f"  --has-email kept {len(rows)} rows")

    # Enrichment runs on the raw rows, which always carry the website, whatever --fields keeps.
    if a.socials:
        print("▶ Finding socials (Instagram / Facebook / LinkedIn / TikTok / YouTube / X) on each website…")
        enrich_sites(rows, _find_socials, SOCIAL_FIELDS, "socials")
        fields = fields + SOCIAL_FIELDS
        found = sum(1 for r in rows if any(r.get(k) for k in SOCIAL_FIELDS))
        print(f"  socials found for {found}/{len(rows)} businesses")

    if a.tech:
        print("▶ Checking ad pixels, analytics and site platform on each website (0 LLM tokens)…")
        enrich_sites(rows, _find_tech, TECH_FIELDS, "tech")
        for r in rows:
            r["intent_score"] = intent_score(r)
        rows.sort(key=lambda r: r["intent_score"], reverse=True)
        fields = fields + TECH_FIELDS + ["intent_score"]
        ads = sum(1 for r in rows if "yes" in (r.get("fb_pixel"), r.get("google_ads_tag")))
        print(f"  Meta Pixel or Google Ads tag on {ads}/{len(rows)} websites; "
              f"rows sorted by intent_score (highest first)")
    results = [{k: r.get(k, "") for k in fields} for r in rows]

    # Default output is a CSV file (opens in Excel / Google Sheets). Use --json (or a .json --out path) for JSON.
    as_json = a.json or (a.out and a.out.lower().endswith(".json"))
    out = a.out or f"results-{job_id[:8]}.{'json' if as_json else 'csv'}"
    def write(path):
        if as_json:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
        else:
            # utf-8-sig: the BOM makes Excel on Windows read accented names as UTF-8 instead of cp1252.
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(results)
    try:
        write(out)
    except OSError as e:  # the target is locked (open in Excel) or unwritable: never lose a finished scrape
        fallback = f"results-{job_id[:8]}.{'json' if as_json else 'csv'}"
        print(f"⚠ Could not write {out} ({e.strerror}); writing {fallback} instead.")
        out = fallback
        write(out)
    print(f"  saved → {os.path.abspath(out)}  (opens in Excel or Google Sheets)")
    for r in results[:5]:
        tail = f" | IG:{r.get('instagram','') or '—'}" if a.socials else f" | {r.get('website','')}"
        print(f"  • {r.get('title','')} | {r.get('phone','')} | {r.get('emails','')}{tail}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(f"\n✗ Stopped. A job already created keeps running in the engine; list jobs at {BASE}/api/v1/jobs")
