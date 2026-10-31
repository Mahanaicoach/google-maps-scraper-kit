"""Scrape jobs: a sqlite3 store in .engine/data and one worker that runs the jobs one at a time.

A job runs its keywords through maps.collect in one headless Chrome, de-duplicates the places on
data_id and writes <id>.csv with the old engine's 36 column names (what Google's result batches
carry is filled, the rest stays empty) plus ours. The CSV is rewritten atomically after every
keyword, so a download while the job works returns the rows so far. A watchdog stops Chrome when a
job overruns its max_time, so no job can stick on "working". Standard library only.
"""
import concurrent.futures, csv, html, http.client, io, ipaddress, json, math, os, re, socket, sqlite3
import threading, time, urllib.parse, urllib.request, uuid
from datetime import datetime, timezone

import browser, maps
from cdp import CDPError, Page

# gosom/google-maps-scraper's CSV header, in its order, so files and scripts that read the old
# engine's output keep working. Our own columns follow.
COLUMNS = ["input_id", "link", "title", "category", "address", "open_hours", "popular_times", "website",
           "phone", "plus_code", "review_count", "review_rating", "reviews_per_rating", "latitude",
           "longitude", "cid", "status", "descriptions", "reviews_link", "thumbnail", "timezone",
           "price_range", "data_id", "street_view_url", "place_id", "images", "reservations",
           "order_online", "menu", "owner", "complete_address", "credit_cards_accepted", "about",
           "user_reviews", "user_reviews_extended", "emails"]
EXTRA = ["categories"]
RENAME = {"open_state": "status", "description": "descriptions"}  # details.FIELDS name -> CSV column

WATCHDOG_GRACE = 30   # seconds past max_time before the watchdog kills Chrome
SITE_WORKERS = 8      # business websites fetched at once by the email pass
SITE_TIMEOUT = 10     # seconds per website request
EMAIL_MIN_SECONDS = 60  # the email pass gets at least this long, even after a job used its max_time


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Job body ──
LANG = re.compile(r"^[A-Za-z]{2}$")
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def _int(body, key, default, lo, hi, label):
    v = body.get(key, default)
    if v is None:
        v = default
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"invalid {label}: a whole number from {lo} to {hi}")
    return v


def validate(body):
    """(name, data) from a create-job body, or ValueError with the old engine's message
    ("missing name", "invalid lang", ...), which the kit's docs quote."""
    if not isinstance(body, dict):
        raise ValueError("the body must be a JSON object")
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("missing name")
    if len(name) > 200:
        raise ValueError("invalid name: 200 characters at most")
    kws = body.get("keywords")
    if not isinstance(kws, list) or not kws:
        raise ValueError("missing keywords")
    if len(kws) > 500 or not all(isinstance(k, str) and k.strip() and len(k) <= 500 for k in kws):
        raise ValueError("invalid keywords: 1 to 500 non-empty strings of up to 500 characters")
    lang = body.get("lang")
    if lang in (None, ""):
        raise ValueError("missing lang")
    if not isinstance(lang, str) or not LANG.match(lang):
        raise ValueError("invalid lang")
    lat, lon = body.get("lat"), body.get("lon")
    if lat in (None, "") or lon in (None, ""):
        raise ValueError("missing geo coordinates")
    try:
        if isinstance(lat, bool) or isinstance(lon, bool):
            raise ValueError
        flat, flon = float(lat), float(lon)
    except (TypeError, ValueError):
        raise ValueError("invalid geo coordinates") from None
    if not (math.isfinite(flat) and math.isfinite(flon) and -90 <= flat <= 90 and -180 <= flon <= 180):
        raise ValueError("invalid geo coordinates")
    if body.get("max_time") in (None, 0):
        raise ValueError("missing max time")
    data = {"keywords": [k.strip() for k in kws], "lang": lang.lower(), "lat": str(lat).strip(),
            "lon": str(lon).strip(),
            "zoom": _int(body, "zoom", 15, 1, 21, "zoom"),
            "radius": _int(body, "radius", 10000, 0, 1_000_000, "radius"),
            "depth": _int(body, "depth", 1, 1, 1000, "depth"),
            "max_time": _int(body, "max_time", 600, 1, 86400, "max time"),
            "fast_mode": bool(body.get("fast_mode")), "email": bool(body.get("email")),
            "proxies": _proxies(body.get("proxies"))}
    return name.strip(), data


def _proxies(v):
    if v in (None, ""):
        return []
    if not isinstance(v, list) or len(v) > 100 or not all(isinstance(p, str) for p in v):
        raise ValueError("invalid proxies: a list of proxy URLs")
    for p in v:
        try:
            u = urllib.parse.urlsplit(p.strip())
            u.port  # noqa: B018 - raises on a bad port
        except ValueError:
            raise ValueError(f"invalid proxy: {p}") from None
        if u.scheme not in PROXY_SCHEMES or not u.hostname:
            raise ValueError("invalid proxy: use http://, https://, socks5:// or socks5h://host:port")
        if u.username or u.password:
            raise ValueError("proxy usernames and passwords are not supported by this engine yet: "
                             "use a proxy that allows your IP address")
    return [p.strip() for p in v]


# ── Rows ──
def to_row(place, keyword):
    """One CSV row (every column, as text) from a details.parse_entity / maps.parse_link place."""
    row = dict.fromkeys(COLUMNS + EXTRA, "")
    for k, v in place.items():
        k = RENAME.get(k, k)
        if k in row:
            row[k] = str(v)
    row["input_id"] = keyword
    if place.get("place_id"):  # Google's documented Maps URL for one place
        row["link"] = ("https://www.google.com/maps/search/?api=1&query="
                       + urllib.parse.quote(place.get("title", ""), safe="")
                       + "&query_place_id=" + urllib.parse.quote(place["place_id"], safe=""))
    return row


def to_csv(rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS + EXTRA, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ── Store ──
class Store:
    """Jobs in sqlite3 (jobs.db) and their results as <id>.csv beside it. One lock serialises every
    database call and every CSV read and write, so a download never meets a half-replaced file
    (Windows cannot replace a file another handle has open)."""

    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.lock = threading.RLock()
        self.db = sqlite3.connect(os.path.join(folder, "jobs.db"), check_same_thread=False)
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                            "date TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL, "
                            "error TEXT NOT NULL DEFAULT '')")

    @staticmethod
    def _job(r):
        # The old engine's job shape (capitalised keys), plus Error: why a job failed or stopped early.
        return {"ID": r[0], "Name": r[1], "Date": r[2], "Status": r[3], "Data": json.loads(r[4]), "Error": r[5]}

    def create(self, name, data):
        jid = str(uuid.uuid4())
        with self.lock, self.db:
            self.db.execute("INSERT INTO jobs VALUES (?, ?, ?, 'pending', ?, '')",
                            (jid, name, utc_now(), json.dumps(data)))
        return jid

    def get(self, jid):
        with self.lock:
            r = self.db.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone()
        return self._job(r) if r else None

    def all(self):
        with self.lock:
            return [self._job(r) for r in self.db.execute("SELECT * FROM jobs ORDER BY rowid DESC")]

    def update(self, jid, status, error=""):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET status = ?, error = ? WHERE id = ?", (status, error[:500], jid))

    def claim(self):
        """The oldest pending job, now marked working; None when there is none."""
        with self.lock, self.db:
            r = self.db.execute("SELECT * FROM jobs WHERE status = 'pending' ORDER BY rowid LIMIT 1").fetchone()
            if not r:
                return None
            self.db.execute("UPDATE jobs SET status = 'working' WHERE id = ?", (r[0],))
        job = self._job(r)
        job["Status"] = "working"
        return job

    def recover(self):
        """Jobs left on "working" by an engine that was stopped or crashed: failed, rows kept."""
        with self.lock, self.db:
            return self.db.execute("UPDATE jobs SET status = 'failed', error = 'the engine stopped while "
                                   "this job ran; its rows so far are kept' WHERE status = 'working'").rowcount

    def delete(self, jid):
        with self.lock, self.db:
            gone = self.db.execute("DELETE FROM jobs WHERE id = ?", (jid,)).rowcount
            for path in (self.csv_path(jid), self.csv_path(jid) + ".tmp"):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return bool(gone)

    def csv_path(self, jid):
        return os.path.join(self.folder, f"{jid}.csv")

    def write_csv(self, jid, rows):
        """Replace the job's CSV in one step; False (nothing written) if the job was deleted."""
        with self.lock:
            if not self.db.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone():
                return False
            tmp = self.csv_path(jid) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(to_csv(rows))
            os.replace(tmp, self.csv_path(jid))
            return True

    def read_csv(self, jid):
        with self.lock:
            try:
                with open(self.csv_path(jid), "rb") as f:
                    return f.read()
            except OSError:
                return None


# ── Worker ──
class Worker(threading.Thread):
    """Runs pending jobs one at a time, oldest first: never two scrapes against Google at once."""

    def __init__(self, store, log=print):
        super().__init__(daemon=True, name="jobs")
        self.store, self.log = store, log
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self._running = (None, None)  # (job id, stop event) of the job in progress
        self._browser = None

    def run(self):
        while not self.stopped.is_set():
            job = self.store.claim()
            if not job:
                self.wake.wait(5)
                self.wake.clear()
                continue
            try:
                self.run_job(job)
            except Exception as e:  # noqa: BLE001 - one bad job must never stop the worker
                self.log(f"job {job['ID']} failed: {type(e).__name__}: {e}")
                self.store.update(job["ID"], "failed", f"{type(e).__name__}: {e}")

    def cancel(self, jid):
        """Stop `jid` if it is the job in progress (it is being deleted)."""
        rid, stop = self._running
        if rid == jid and stop:
            stop.set()

    def shutdown(self):
        """Stop the job in progress and its Chrome (the engine is exiting)."""
        self.stopped.set()
        self.wake.set()
        _, stop = self._running
        if stop:
            stop.set()
        b = self._browser
        if b:
            b.close()

    def run_job(self, job):
        jid, d = job["ID"], job["Data"]
        deadline = time.monotonic() + d["max_time"]
        stop = threading.Event()
        self._running = (jid, stop)
        rows, seen, errors, blocked = [], set(), [], False
        kws = d["keywords"]
        self.log(f"job {jid} started: {len(kws)} keyword(s), depth {d['depth']}, max_time {d['max_time']} s")

        def watchdog():  # Chrome hung or the job overran: kill Chrome, and every CDP call fails at once
            stop.set()
            b = self._browser
            if b:
                b.kill()
            self.log(f"job {jid}: watchdog stopped Chrome {WATCHDOG_GRACE} s after max_time")
        timer = threading.Timer(d["max_time"] + WATCHDOG_GRACE, watchdog)
        timer.daemon = True
        timer.start()
        try:
            self._browser = browser.Browser(lang=d["lang"], proxy=(d["proxies"] or [None])[0])
            for i, kw in enumerate(kws, 1):
                left = deadline - time.monotonic()
                if stop.is_set() or left <= 0:
                    break
                url = maps.search_url(kw, d["lat"], d["lon"], d["zoom"], d["lang"])
                page = None
                started = time.monotonic()
                try:
                    page = Page(self._browser.cdp)
                    places, reason, stats = maps.collect(page, url, depth=d["depth"], max_seconds=left,
                                                         log=lambda m: None, stop=stop.is_set)
                except (CDPError, OSError) as e:
                    errors.append(str(e))
                    self.log(f"job {jid} keyword {i}/{len(kws)}: error: {e}")
                    if self._browser.cdp.closed:  # Chrome is gone: the other keywords would fail too
                        break
                    continue
                finally:
                    if page and not self._browser.cdp.closed:
                        page.close()
                blocked |= reason == "blocked"
                new = 0
                for p in places:
                    key = p.get("data_id") or p.get("place_id") or p.get("link") or p.get("title")
                    if key and key not in seen:
                        seen.add(key)
                        rows.append(to_row(p, kw))
                        new += 1
                if not self.store.write_csv(jid, rows):
                    break  # deleted
                self.log(f"job {jid} keyword {i}/{len(kws)}: {len(places)} places, {new} new, "
                         f"{stats['batches']} batches, {time.monotonic() - started:.1f} s ({reason})")
        except browser.BrowserError as e:
            errors.append(str(e))
            self.log(f"job {jid}: {e}")
        finally:
            timer.cancel()
            if self._browser:
                self._browser.close()
                self._browser = None
        if d["email"] and rows and not stop.is_set():
            n = add_emails(rows, max(deadline - time.monotonic(), EMAIL_MIN_SECONDS))
            self.log(f"job {jid}: emails found for {n}/{sum(1 for r in rows if r['website'])} websites")
        self._running = (None, None)
        if not self.store.write_csv(jid, rows):
            self.log(f"job {jid} deleted while it ran")
            return
        if rows or not (errors or blocked):
            note = "stopped at max_time; the rows found so far are kept" \
                if time.monotonic() >= deadline or (stop.is_set() and not errors) else ""
            if errors:
                note = f"{len(errors)} keyword(s) failed: {errors[-1]}"
            self.store.update(jid, "ok", note)
            self.log(f"job {jid} ok: {len(rows)} places")
        else:
            reason = ("Google answered with its unusual-traffic page: this IP is rate-limited for now. "
                      "Wait, or add proxies") if blocked else errors[-1]
            self.store.update(jid, "failed", reason)
            self.log(f"job {jid} failed: {reason}")


# ── Email pass (email: true): each business homepage over plain HTTP, emails only ──
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
JUNK_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
JUNK_DOMAIN = ("example.", "sentry", "wixpress.com", "domain.com", "yourdomain", "email.com")
UA = "Mozilla/5.0 (compatible; google-maps-scraper-kit; +https://github.com/Mahanaicoach/google-maps-scraper-kit)"


def _public_ip(ip):
    try:
        return ipaddress.ip_address(ip.split("%")[0]).is_global
    except ValueError:
        return False


def _public_host(host):
    """True only if every address of `host` is public. Websites come from third-party listings, so
    one could point the fetch at this machine (the engine itself) or its network."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False
    return bool(infos) and all(_public_ip(i[4][0]) for i in infos)


def _public_connection(address, *args, **kwargs):
    """create_connection that refuses a private peer: catches a DNS answer that changed (rebinding)."""
    sock = socket.create_connection(address, *args, **kwargs)
    if not _public_ip(sock.getpeername()[0]):
        sock.close()
        raise OSError("refused a non-public address")
    return sock


class _HTTP(http.client.HTTPConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = _public_connection


class _HTTPS(http.client.HTTPSConnection):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._create_connection = _public_connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTP, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPS, req, context=self._context)


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _public_host(urllib.parse.urlsplit(newurl).hostname or ""):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# No proxy from the environment: these go straight to the business websites.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPHandler, _HTTPSHandler, _Redirects)


def fetch(url, timeout=SITE_TIMEOUT):
    """Up to 400 KB of a public web page as text; "" on any failure."""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        if not _public_host(urllib.parse.urlsplit(url).hostname or ""):
            return ""
        r = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
        with _OPENER.open(r, timeout=timeout) as resp:
            return resp.read(400_000).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a broken website is just a website without emails
        return ""


def site_host(url):
    try:
        h = (urllib.parse.urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def find_emails(text, site=""):
    """Up to 3 addresses from page text, placeholders and image names dropped, the site's own first."""
    found = []
    for e in EMAIL_RE.findall(html.unescape(text or "")):
        e = urllib.parse.unquote(e).lower()
        dom = e.rpartition("@")[2]
        if e.endswith(JUNK_SUFFIX) or any(j in dom for j in JUNK_DOMAIN) or e in found:
            continue
        found.append(e)
    own = lambda e: site and (e.endswith("@" + site) or e.endswith("." + site))
    return sorted(found, key=lambda e: not own(e))[:3]


def add_emails(rows, seconds, get=None):
    """Fill `emails` (joined by ", ", like the old engine) from each row's website homepage, for at
    most `seconds`; the number of rows that got one."""
    get = get or fetch
    todo = [r for r in rows if r.get("website") and not r.get("emails")]
    pool = concurrent.futures.ThreadPoolExecutor(SITE_WORKERS)
    futs = {pool.submit(lambda r=r: find_emails(get(r["website"]), site_host(r["website"]))): r for r in todo}
    done, _ = concurrent.futures.wait(futs, timeout=seconds)
    pool.shutdown(wait=False, cancel_futures=True)
    n = 0
    for f in done:
        if not f.exception() and f.result():
            futs[f]["emails"] = ", ".join(f.result())
            n += 1
    return n
