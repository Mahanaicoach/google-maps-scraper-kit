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

Skip the deep email pass over contact and about pages (on by default whenever emails are on):
    python3 scripts/scrape.py "gyms in Miami FL" --city "Miami, FL" --no-deep-emails

Notes:
- Required by the API: keywords, lat/lon (strings), max_time (SECONDS). This script fills them in.
- Geocoding uses OpenStreetMap Nominatim (free, no key). It allows ~1 request/sec and requires a
  descriptive User-Agent (set below). Do not loop it aggressively.
"""
import argparse, csv, functools, io, json, math, os, re, sys, time, urllib.request, urllib.parse, urllib.error
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
UA = "google-maps-scraper-kit/1.1 (https://github.com/Mahanaicoach/google-maps-scraper-kit)"


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


def run_job(body):
    """Create one engine job, poll it, download it -> (job_id, raw CSV rows). Raises RuntimeError."""
    try:
        _, raw = req("POST", "/api/v1/jobs", body)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"✗ Create failed: HTTP {e.code} — {e.read().decode()[:200]}")
    job_id = json.loads(raw).get("id")
    if not job_id:
        raise RuntimeError("✗ No job id returned.")
    print(f"  job id: {job_id}")

    print("▶ Polling…")
    status = None
    for i in range(120):
        _, raw = req("GET", f"/api/v1/jobs/{job_id}")
        status = json.loads(raw).get("Status")
        print(f"\r  status: {str(status):<10} (attempt {i + 1})", end="", flush=True)
        if status == "ok":
            print(); break
        if status == "failed":
            raise RuntimeError("\n✗ Job failed. If this keeps happening you may be rate-limited — wait or add proxies.")
        time.sleep(8)
    else:
        raise RuntimeError("\n✗ Timed out.")

    _, raw = req("GET", f"/api/v1/jobs/{job_id}/download")
    return job_id, list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))


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
_SKIP_HANDLE = {"", "home", "pages", "people", "help", "about", "policies", "policy",
                "legal", "tos", "privacy", "settings", "sharer", "tr", "profile.php",
                "plugins", "dialog", "intent", "login", "share.php", "permalink.php",
                "p", "reel", "reels", "explore", "stories", "tv", "watch", "events",
                "groups", "marketplace", "gaming", "photo", "hashtag", "search"}

def _fetch_html(url, timeout=10):
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        r = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; " + UA + ")", "Accept": "text/html"})
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.read(400_000).decode("utf-8", "replace")
    except Exception:
        return ""

@functools.lru_cache(maxsize=512)  # ponytail: homepages only; evicted pages just refetch
def _fetch_cached(url):
    """Homepage fetch shared by the deep email pass and --socials (one fetch per site)."""
    return _fetch_html(url, timeout=8)

def _find_socials(html):
    out = {"instagram": "", "facebook": "", "linkedin": ""}
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

def enrich_socials(results, workers=8):
    """Add instagram/facebook/linkedin to each lead by scanning its website. Zero LLM tokens."""
    for r in results:                       # make sure every row has the keys
        r.setdefault("instagram", ""); r.setdefault("facebook", ""); r.setdefault("linkedin", "")
    todo = [r for r in results if r.get("website")]
    done = 0
    def work(r):
        r.update(_find_socials(_fetch_cached(r["website"])))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in ex.map(work, todo):
            done += 1
            print(f"\r  socials: {done}/{len(todo)} sites checked", end="", flush=True)
    print()


# ── Deep email pass: contact/about pages for rows the engine left without an email ──
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MAILTO_RE = re.compile(r"mailto:([^\"'>\s?]+)", re.I)
HREF_RE = re.compile(r"<a\s[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_JUNK_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
_JUNK_DOMAIN = ("example", "sentry", "wixpress", "wix.com", "godaddy", "squarespace", "wordpress",
                "domain.com", "email.com", "yourdomain", "schema.org", "w3.org", "sentry.io")
_JUNK_LOCAL = {"name", "email", "user", "you", "someone", "example", "username",
               "youremail", "yourname", "firstname", "lastname", "sample"}

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


def clean_engine_emails(results):
    """Re-filter the engine's own `emails` column through is_real_email.

    The engine copies whatever it finds on the page, placeholders included, and until now only the
    addresses this script scraped itself were filtered. That let user@domain.com through and counted
    it as a hit, so the found rate printed below overstated how many leads were reachable.
    Returns the number of rows changed; a row left empty here still gets the deep pass."""
    changed = 0
    for r in results:
        raw = [e.strip().lower() for e in (r.get("emails") or "").split(",") if e.strip()]
        kept = list(dict.fromkeys(e for e in raw if is_real_email(e)))
        if kept != raw:
            r["emails"] = ", ".join(kept)
            changed += 1
    return changed


def extract_emails(html, site_host):
    """Emails from mailto: links and plain text, junk dropped, site-domain first, max 3."""
    site = _host(site_host or "")
    found = []
    for e in MAILTO_RE.findall(html or "") + EMAIL_RE.findall(html or ""):
        e = urllib.parse.unquote(e).strip().lower()
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

def enrich_emails(results, workers=8):
    """Fill empty `emails` from the homepage, then up to 3 contact/about pages. Zero LLM tokens."""
    todo = [r for r in results if r.get("website") and not r.get("emails")]
    done = 0
    def work(r):
        site = r["website"]
        home = _fetch_cached(site)
        found = extract_emails(home, site)
        if home and not found:
            pages = "".join(_fetch_html(u, timeout=8) for u in contact_links(home, site))
            found = extract_emails(pages, site)
        r["emails"] = ", ".join(found)
    with ThreadPoolExecutor(max_workers=workers) as ex:
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
    # Emails are ON by default (they're the most valuable lead field). Visits each business
    # website to find an address — a bit slower. Use --no-email to skip for a fast run.
    ap.add_argument("--email", dest="email", action="store_true", default=True, help=argparse.SUPPRESS)
    ap.add_argument("--no-email", dest="email", action="store_false",
                    help="skip email extraction for a faster run (emails are on by default)")
    ap.add_argument("--no-deep-emails", dest="deep_emails", action="store_false",
                    help="skip the second email pass over contact/about pages (on whenever emails are on)")
    ap.add_argument("--grid", type=int, metavar="N",
                    help="split the --city area into N x N map tiles, one job each, deduped "
                         "(breaks Google's ~120 per search cap; max 10)")
    ap.add_argument("--max-time", type=int, default=600, help="job time limit in SECONDS")
    ap.add_argument("--out", default=None, help="output file path (default: results-<id>.csv). "
                    "The extension picks the format: .csv (default) or .json")
    ap.add_argument("--json", action="store_true", help="write JSON instead of the default CSV")
    ap.add_argument("--full", action="store_true", help="keep ALL raw columns (default: lean lead fields)")
    ap.add_argument("--fields", help="comma-separated columns to keep (overrides the default lead set)")
    ap.add_argument("--socials", action="store_true",
                    help="also find Instagram/Facebook/LinkedIn from each website (0 LLM tokens; slower)")
    a = ap.parse_args()

    keywords = collect_keywords(a)
    if not keywords:
        sys.exit("✗ No keywords. Pass a keyword, --keyword, or --keywords-file.")

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

    if a.email:
        print("ℹ Email extraction is ON (visits each business website; a bit slower). "
              "Use --no-email for a fast run.")

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

    body = {"name": "scrape-py", "keywords": keywords, "lang": "en", "zoom": 15,
            "lat": str(lat), "lon": str(lon), "fast_mode": False, "radius": 10000,
            "depth": a.depth, "email": a.email, "max_time": a.max_time}
    if not a.grid:
        print(f"▶ Creating job: {len(keywords)} keyword(s) @ {lat},{lon} depth={a.depth} email={a.email}")
        for k in keywords:
            print(f"    • {k}")
        try:
            job_id, rows = run_job(body)
        except RuntimeError as e:
            sys.exit(str(e))
    else:
        print(f"▶ Grid {a.grid}x{a.grid}: {len(cells)} jobs, one at a time, "
              f"{len(keywords)} keyword(s) depth={a.depth} email={a.email}")
        for k in keywords:
            print(f"    • {k}")
        job_id, rows = None, []
        for i, cell in enumerate(cells, 1):  # sequential on purpose: never parallel jobs against Google
            print(f"▶ Cell {i}/{len(cells)} @ {cell['lat']},{cell['lon']} radius={cell['radius']}m")
            try:
                jid, cell_rows = run_job(dict(body, **cell))
            except Exception as e:
                print(f"\n  ⚠ cell {i}/{len(cells)} skipped: {str(e).strip()}")
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
    results = [{k: r.get(k, "") for k in fields} for r in rows]
    print(f"✓ Done — {len(results)} businesses (fields: {', '.join(fields)}).")

    if "emails" in fields:
        dropped = clean_engine_emails(results)
        if dropped:
            print(f"  dropped placeholder emails (user@domain.com and the like) on {dropped} row(s)")

    if a.email and a.deep_emails and "emails" in fields:
        print("▶ Deep email pass: checking contact/about pages for rows with no email…")
        engine = sum(1 for r in results if r.get("emails"))
        enrich_emails(results)
        total = sum(1 for r in results if r.get("emails"))
        n = len(results)
        print(f"  emails: {engine}/{n} from the engine, +{total - engine} from contact pages "
              f"= {total}/{n} ({round(100 * total / n) if n else 0}%)")

    if a.socials:
        print("▶ Finding socials (Instagram / Facebook / LinkedIn) on each website…")
        print("  ℹ Cost: this runs in CODE = 0 LLM tokens. It only adds ~40–50 tokens per business")
        print("    IF you load the results into an AI's context (negligible if you keep the file on disk).")
        enrich_socials(results)
        fields = fields + ["instagram", "facebook", "linkedin"]
        found = sum(1 for r in results if r.get("instagram") or r.get("facebook") or r.get("linkedin"))
        print(f"  socials found for {found}/{len(results)} businesses")

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
