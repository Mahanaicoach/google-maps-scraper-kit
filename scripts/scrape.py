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
import argparse, csv, functools, io, ipaddress, json, math, os, re, shutil, socket, subprocess, sys, time
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

ENV_KEYS = ("SCRAPER_BASE_URL", "SCRAPER_PORT", "SCRAPER_API_KEY")


def load_env_file():
    """Read SCRAPER_* from the kit's .env, the same file docker compose reads, so a remapped
    port needs one edit. Variables already set in the environment win over the file."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, ".env")
    try:
        with open(path) as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and k in ENV_KEYS and k not in os.environ:
                    os.environ[k] = v.strip().strip("\"'")
    except OSError:
        pass


load_env_file()
BASE = os.environ.get("SCRAPER_BASE_URL") or f"http://localhost:{os.environ.get('SCRAPER_PORT') or '8080'}"
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
ENGINE_CONTAINER = "gmaps-scraper"  # container_name in docker-compose.yml and SETUP's docker run


def poll_attempts(max_time):
    """How many status checks to make before giving up on a job.

    Derived from the job's own max_time, because that is how long the engine is allowed to
    work: waiting any less abandons a job that is still running normally. The engine floors
    max_time at ENGINE_MIN_RUNTIME, so even a tiny --max-time needs that much patience, and
    POLL_CEILING stops an absurd value from parking the script for a day."""
    engine_runtime = max(ENGINE_MIN_RUNTIME, int(max_time or 0))
    budget = min(POLL_CEILING, engine_runtime + POLL_GRACE)
    return max(1, budget // POLL_EVERY)


def req(method, path, body=None):
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if KEY:
        headers["X-API-Key"] = KEY
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=60) as resp:
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
                          "zoom": min(17, max(11, zoom))})
    return cells


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


def dedupe(rows):
    """Keep the first row per place_id, else cid, else link, else title+address."""
    seen, out = set(), []
    for r in rows:
        key = next((f"{k}:{r[k]}" for k in ("place_id", "cid", "link") if r.get(k)),
                   f"ta:{r.get('title', '')}|{r.get('address', '')}")
        if key not in seen:
            seen.add(key); out.append(r)
    return out


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
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return "scrapemate exited" in out.stdout + out.stderr

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
    if restart_engine():
        print("  Restarted the engine, which would otherwise hold every later job on \"pending\".")
    else:
        print("  If the next scrape stays \"pending\", run: docker restart " + ENGINE_CONTAINER)
    return rows

def run_job(body):
    """Create one engine job, poll it, download it -> (job_id, raw CSV rows). Raises RuntimeError."""
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
    status, ended = None, 0
    for i in range(attempts):
        _, raw = req("GET", f"/api/v1/jobs/{job_id}")
        status = json.loads(raw).get("Status")
        print(f"\r  status: {str(status):<10} (attempt {i + 1})", end="", flush=True)
        if status == "ok":
            print(); break
        if status == "failed":
            raise RuntimeError("\n✗ Job failed. If this keeps happening you may be rate-limited — wait or add proxies.")
        if status == "working" and engine_run_ended(since):
            ended += 1
            if ended > 2:  # the status normally flips within seconds of the run ending
                return job_id, recover_stuck_job(job_id)
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
    _, raw = req("GET", f"/api/v1/jobs/{job_id}/download")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))


def collect_keywords(a):
    kws = []
    if a.keyword:
        kws.append(a.keyword)
    kws.extend(a.also or [])
    if a.keywords_file:
        with open(a.keywords_file) as f:
            kws.extend(line.strip() for line in f if line.strip() and not line.startswith("#"))
    # de-dupe, keep order
    seen, out = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k); out.append(k)
    return out


# ── Optional social enrichment (Instagram / Facebook / LinkedIn) ──────────────
# Pure code (HTTP + regex) → ZERO LLM tokens. Visits each business website once.
SOCIAL_RE = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)", re.I),
    "facebook":  re.compile(r"https?://(?:www\.|m\.|web\.)?facebook\.com/([A-Za-z0-9_.\-]+)", re.I),
    "linkedin":  re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in|school)/([A-Za-z0-9_.\-%]+)", re.I),
}
SOCIAL_FIELDS = list(SOCIAL_RE)
_SKIP_HANDLE = {"", "home", "pages", "people", "help", "about", "policies", "policy",
                "legal", "tos", "privacy", "settings", "sharer", "tr", "profile.php",
                "plugins", "dialog", "intent", "login", "share.php", "permalink.php",
                "p", "reel", "reels", "explore", "stories", "tv", "watch", "events",
                "groups", "marketplace", "gaming", "photo", "hashtag", "search"}

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
    if not _is_public_host(urllib.parse.urlparse(url).hostname or ""):
        return ""
    try:
        r = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; " + UA + ")", "Accept": "text/html"})
        with _OPENER.open(r, timeout=timeout) as resp:
            return resp.read(400_000).decode("utf-8", "replace")
    except Exception:
        return ""

@functools.lru_cache(maxsize=512)  # ponytail: homepages only; evicted pages just refetch
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

def enrich_sites(results, find, fields, label, workers=None):
    """Merge find(homepage html) into every lead that has a website. Zero LLM tokens."""
    for r in results:                       # make sure every row has the keys
        for k in fields:
            r.setdefault(k, "")
    todo = [r for r in results if r.get("website")]
    done = 0
    def work(r):
        r.update(find(_fetch_cached(r["website"])))
    with ThreadPoolExecutor(max_workers=workers or SITE_WORKERS) as ex:
        for _ in ex.map(work, todo):
            done += 1
            print(f"\r  {label}: {done}/{len(todo)} sites checked", end="", flush=True)
    print()


# ── Optional ad and tech signals (--tech): who already spends on marketing ──────
# Regexes from OpenLeadFinder's website layer. They run on the homepage already fetched above.
TECH_RE = {
    "fb_pixel":       re.compile(r"connect\.facebook\.net/[^\"']*fbevents\.js|fbq\(", re.I),
    "google_ads_tag": re.compile(r"googleadservices\.com|AW-\d{6,}|googleads\.g\.doubleclick\.net", re.I),
    "analytics":      re.compile(r"googletagmanager\.com/gtag|gtag\(|UA-\d{4,}|G-[A-Z0-9]{6,}", re.I),
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
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"mailto:([^\"'>\s?]+)", re.I)
HREF_RE = re.compile(r"<a\s[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_JUNK_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".css", ".js")
_JUNK_DOMAIN = ("example", "sentry", "wixpress", "wix.com", "godaddy", "squarespace", "wordpress",
                "domain.com", "email.com", "yourdomain", "mysite.com", "schema.org", "w3.org", "sentry.io")
_JUNK_LOCAL = {"name", "email", "user", "you", "someone", "example", "username",
               "youremail", "yourname", "firstname", "lastname", "sample"}
# A JSON-escaped <, >, ", ' or & glued to the address (">orders@x.com" in page source),
# which the regex reads as part of the local part: u003eorders@x.com.
_ESCAPE_PREFIX = re.compile(r"^(?:u00(?:3c|3e|22|27|26))+")

def _host(url):
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h

def is_real_email(e):
    """False for anything not worth sending to: a template placeholder (user@domain.com,
    example@gmail.com), a vendor/boilerplate domain, or an image path that parsed as an address."""
    local, _, dom = e.rpartition("@")
    if not EMAIL_RE.fullmatch(e) or e.endswith(_JUNK_EXT):
        return False
    return local not in _JUNK_LOCAL and not any(j in dom for j in _JUNK_DOMAIN)


# "name [at] domain [dot] com", "name (at) domain dot com" and friends, adapted from OpenLeadFinder.
# The "at" must be bracketed and the domain must end in a known TLD: a bare " at " also matched
# prose such as "meet at noon dot com".
_AT = r"(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\})"
_DOT = r"(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|\s+dot\s+)"
_TLD = (r"(?:com|net|org|co|io|biz|info|us|uk|ca|au|de|fr|es|it|nl|eu|edu|gov|me|app|dev|"
        r"shop|online|store|xyz|ai)")
_DEOBF_RE = re.compile(rf"([A-Za-z0-9._%+\-]+)\s*{_AT}\s*([A-Za-z0-9.\-]+?)\s*{_DOT}\s*({_TLD})\b", re.I)

def extract_emails(html, site_host):
    """Emails from mailto: links, plain text and obfuscated spans, junk dropped, site-domain first,
    max 3. HTML entities are decoded first, so info&#64;site.com counts."""
    site = _host(site_host or "")
    text = _html.unescape(html or "")
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
    for href, text in HREF_RE.findall(html or ""):
        if not re.search(r"contact|about", href + " " + text, re.I):
            continue
        url = urllib.parse.urldefrag(urllib.parse.urljoin(base_url, href.strip()))[0]
        if url.startswith(("http://", "https://")) and _host(url) == host and url not in out:
            out.append(url)
    for path in ("/contact", "/contact-us", "/about", "/about-us"):
        url = urllib.parse.urljoin(base_url, path)
        if len(out) < 3 and url not in out:
            out.append(url)
    return out[:3]

def enrich_emails(results, deep=True, workers=None):
    """Fill empty `emails` from the homepage, then (deep) up to 3 contact/about pages. Zero LLM tokens."""
    todo = [r for r in results if r.get("website") and not r.get("emails")]
    done = 0
    def work(r):
        site = r["website"]
        home = _fetch_cached(site)
        found = extract_emails(home, site)
        if deep and home and not found:
            pages = "\n".join(_fetch_html(u, timeout=8) for u in contact_links(home, site))
            found = extract_emails(pages, site)
        r["emails"] = ", ".join(found)
    with ThreadPoolExecutor(max_workers=workers or SITE_WORKERS) as ex:
        for _ in ex.map(work, todo):
            done += 1
            print(f"\r  emails: {done}/{len(todo)} sites checked", end="", flush=True)
    print()


def main():
    ap = argparse.ArgumentParser(description="Scrape Google Maps business listings.")
    ap.add_argument("keyword", nargs="?", help='e.g. "coffee shops in Austin TX"')
    ap.add_argument("lat", nargs="?", help="latitude (optional if --city given)")
    ap.add_argument("lon", nargs="?", help="longitude (optional if --city given)")
    ap.add_argument("--keyword", dest="also", action="append", help="extra keyword (repeatable)")
    ap.add_argument("--keywords-file", help="file with one keyword per line (batch)")
    ap.add_argument("--city", help="city/place to auto-geocode for lat/lon")
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
    ap.add_argument("--email", dest="email", action="store_true", default=True, help=argparse.SUPPRESS)
    ap.add_argument("--no-email", dest="email", action="store_false",
                    help="skip the website crawl for emails (emails are on by default)")
    ap.add_argument("--no-deep-emails", dest="deep_emails", action="store_false",
                    help="check homepages only, not contact and about pages")
    ap.add_argument("--grid", type=int, metavar="N",
                    help="split the --city area into N x N map tiles, one job each, deduped "
                         "(breaks Google's ~120 per search cap; max 10)")
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
                    help="also detect Meta Pixel, Google Ads tag, analytics, site platform and mobile "
                         "support on each website, and sort by an intent_score (0 LLM tokens; slower)")
    a = ap.parse_args()

    keywords = collect_keywords(a)
    if not keywords:
        sys.exit("✗ No keywords. Pass a keyword, --keyword, or --keywords-file.")

    if len(a.lang) != 2 or not a.lang.isalpha():
        sys.exit(f"✗ --lang must be a 2-letter code (got {a.lang!r}); the engine rejects anything else.")

    if a.grid is not None:
        if not a.city:
            sys.exit("✗ --grid needs --city: the city's bounding box is what gets split into tiles.")
        if not 1 <= a.grid <= 10:
            sys.exit("✗ --grid must be between 1 and 10 (10 x 10 = 100 jobs is the ceiling).")

    # Resolve coordinates: explicit lat/lon, else geocode --city, else geocode first keyword.
    # --grid always geocodes --city, because it needs the city's bounding box.
    lat, lon = a.lat, a.lon
    if a.grid:
        print(f"▶ Geocoding \"{a.city}\"…")
        geo = geocode(a.city, with_bbox=True)
        if not geo or not geo[2]:
            sys.exit("✗ Could not resolve a bounding box. Try a clearer --city.")
        lat, lon, bbox = geo
        cells = grid_cells(bbox, a.grid)
        print(f"  → bbox S{bbox[0]} N{bbox[1]} W{bbox[2]} E{bbox[3]}, {len(cells)} tiles")
    elif not (lat and lon):
        place = a.city or keywords[0]
        print(f"▶ Geocoding \"{place}\"…")
        coords = geocode(place)
        if not coords:
            sys.exit("✗ Could not resolve coordinates. Pass lat/lon or a clearer --city.")
        lat, lon = coords
        print(f"  → {lat}, {lon}")

    # --- WARN, don't block: flag high-volume jobs (proceed anyway) ---
    if a.depth >= 15 or len(keywords) >= 10 or (a.grid or 1) ** 2 >= 4:
        print("⚠️  Heads-up: this is a large job (high depth, many keywords or a grid).")
        print("    Running it back-to-back without proxies can get your IP temporarily rate-limited by")
        print("    Google (clears in minutes–hours; returns empty/failed jobs meanwhile). For big or")
        print("    repeated runs, add proxies. Proceeding…\n")

    # Health check
    try:
        req("GET", "/api/v1/jobs")
    except Exception as e:
        sys.exit(f"✗ Scraper not reachable at {BASE} — run 'docker compose up -d' first.\n  ({e})")

    body = {"name": "scrape-py", "keywords": keywords, "lang": a.lang, "zoom": 15,
            "lat": str(lat), "lon": str(lon), "fast_mode": False, "radius": 10000,
            # The engine's own email pass visits every website in a browser, one per worker: it took
            # most of each job and found fewer emails than the crawl below (OpenLeadFinder's
            # approach), so the engine only reads Google Maps.
            "depth": a.depth, "email": False, "max_time": a.max_time}
    skipped = []  # grid tiles that failed
    if not a.grid:
        print(f"▶ Creating job: {len(keywords)} keyword(s) @ {lat},{lon} depth={a.depth}")
        for k in keywords:
            print(f"    • {k}")
        try:
            job_id, rows = run_job(body)
        except RuntimeError as e:
            sys.exit(str(e))
    else:
        print(f"▶ Grid {a.grid}x{a.grid}: {len(cells)} jobs, one at a time, "
              f"{len(keywords)} keyword(s) depth={a.depth}")
        for k in keywords:
            print(f"    • {k}")
        job_id, rows = None, []
        for i, cell in enumerate(cells, 1):  # sequential on purpose: never parallel jobs against Google
            print(f"▶ Cell {i}/{len(cells)} @ {cell['lat']},{cell['lon']} radius={cell['radius']}m")
            try:
                jid, cell_rows = run_job(dict(body, **cell))
            except Exception as e:
                print(f"\n  ⚠ cell {i}/{len(cells)} skipped: {str(e).strip()}")
                skipped.append(i)
                continue
            job_id = job_id or jid
            rows = dedupe(rows + cell_rows)
            print(f"  cell {i}/{len(cells)}: {len(cell_rows)} rows, {len(rows)} unique so far")
        if job_id is None:
            sys.exit("✗ Every grid cell failed. You may be rate-limited — wait or add proxies.")
        # radius does not clip results in normal mode, so tiles leak outside the city
        kept = [r for r in rows if in_bbox(r, bbox)]
        print(f"  dropped {len(rows) - len(kept)} rows outside the city box")
        rows = kept
    if a.full:
        fields = list(rows[0].keys()) if rows else LEAD
    elif a.fields:
        fields = [c.strip() for c in a.fields.split(",") if c.strip()]
    else:
        fields = LEAD
    print(f"✓ Done — {len(rows)} businesses (fields: {', '.join(fields)}).")
    if skipped:
        print(f"⚠ Incomplete: {len(skipped)} of {len(cells)} tiles failed (cells "
              f"{', '.join(map(str, skipped))}), so those parts of the city are missing. Rerun later.")

    if a.email and "emails" in fields:  # on the raw rows, so --fields without website still works
        pages = "homepage, then contact and about pages" if a.deep_emails else "homepage only"
        print(f"▶ Emails: checking each business website ({pages}, {SITE_WORKERS} at a time)…")
        enrich_emails(rows, deep=a.deep_emails)
        total, n = sum(1 for r in rows if r.get("emails")), len(rows)
        print(f"  emails found for {total}/{n} businesses ({round(100 * total / n) if n else 0}%)")
    results = [{k: r.get(k, "") for k in fields} for r in rows]

    if a.socials:
        print("▶ Finding socials (Instagram / Facebook / LinkedIn) on each website…")
        print("  ℹ Cost: this runs in CODE = 0 LLM tokens. It only adds ~40–50 tokens per business")
        print("    IF you load the results into an AI's context (negligible if you keep the file on disk).")
        enrich_sites(results, _find_socials, SOCIAL_FIELDS, "socials")
        fields = fields + SOCIAL_FIELDS
        found = sum(1 for r in results if any(r.get(k) for k in SOCIAL_FIELDS))
        print(f"  socials found for {found}/{len(results)} businesses")

    if a.tech:
        print("▶ Checking ad pixels, analytics and site platform on each website (0 LLM tokens)…")
        enrich_sites(results, _find_tech, TECH_FIELDS, "tech")
        for r in results:
            r["intent_score"] = intent_score(r)
        results.sort(key=lambda r: r["intent_score"], reverse=True)
        fields = fields + TECH_FIELDS + ["intent_score"]
        ads = sum(1 for r in results if "yes" in (r.get("fb_pixel"), r.get("google_ads_tag")))
        print(f"  Meta Pixel or Google Ads tag on {ads}/{len(results)} websites; "
              f"rows sorted by intent_score (highest first)")

    # Default output is a CSV file (opens in Excel / Google Sheets). Use --json (or a .json --out path) for JSON.
    as_json = a.json or (a.out and a.out.lower().endswith(".json"))
    out = a.out or f"results-{job_id[:8]}.{'json' if as_json else 'csv'}"
    if as_json:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
    else:
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(results)
    print(f"  saved → {out}")
    for r in results[:5]:
        tail = f" | IG:{r.get('instagram','') or '—'}" if a.socials else f" | {r.get('website','')}"
        print(f"  • {r.get('title','')} | {r.get('phone','')} | {r.get('emails','')}{tail}")


if __name__ == "__main__":
    main()
