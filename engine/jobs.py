"""Scrape jobs: a sqlite3 store in .engine/data and one worker that runs the jobs one at a time.

A job runs its keywords through maps.collect in one Chrome, each keyword in its own browser context
(own cookies, the next proxy of the job's list), de-duplicates the places on data_id and appends them
to <id>.csv as the result batches arrive, with the old engine's 36 column names (what Google's result
batches carry is filled, the rest stays empty) plus ours. Appends and downloads share one lock, so a
download while the job works returns whole rows. The job's Progress (keywords done, places, percent)
is saved as it goes; a job the engine was stopped in the middle of resumes from its last finished
keyword on the next start. When Google answers with its unusual-traffic page or 429, the keyword
waits and retries a bounded number of times (through the next proxy when there is one), then the job
stops with a clear Error. A watchdog stops Chrome when a job overruns its max_time, so no job can
stick on "working". Standard library only.
"""
import concurrent.futures, csv, html, http.client, io, ipaddress, json, math, os, re, socket
import shutil, sqlite3, tempfile, threading, time, urllib.parse, urllib.request, uuid
from datetime import datetime, timezone

import browser, details, maps
from cdp import CDPError, Page

csv.field_size_limit(2 ** 31 - 1)  # a long cell (a description, a 1.x file) can pass csv's 128 KB default

# Kit 1.x's CSV header, in its order, so files and scripts that read the old engine's output keep
# working. Our own columns follow.
COLUMNS = ["input_id", "link", "title", "category", "address", "open_hours", "popular_times", "website",
           "phone", "plus_code", "review_count", "review_rating", "reviews_per_rating", "latitude",
           "longitude", "cid", "status", "descriptions", "reviews_link", "thumbnail", "timezone",
           "price_range", "data_id", "street_view_url", "place_id", "images", "reservations",
           "order_online", "menu", "owner", "complete_address", "credit_cards_accepted", "about",
           "user_reviews", "user_reviews_extended", "emails"]
EXTRA = ["categories"]
SOCIALS = ["instagram", "facebook", "linkedin", "tiktok", "youtube", "x"]  # the names scripts/scrape.py --socials uses
RENAME = {"open_state": "status", "description": "descriptions"}  # details.FIELDS name -> CSV column

# Seconds past max_time before the watchdog kills Chrome. maps.collect's page load and first waits
# (navigate 45 s, consent 15 s, feed 15 s, first batch 10 s) sit outside its max_seconds, so a
# shorter grace could kill a keyword that was about to return its rows.
WATCHDOG_GRACE = 90
SITE_WORKERS = 8      # business websites fetched at once by the website pass
SITE_TIMEOUT = 10     # seconds per website request
EMAIL_MIN_SECONDS = 60  # the website pass gets at least this long, even after a job used its max_time
BACKOFF = (30, 90)    # seconds to wait before each retry of a keyword Google refused; then the job stops
ROTATE_WAIT = 3       # seconds before the retry when the next proxy can take it instead
MAX_RESUMES = 3       # restarts of the engine mid-job before the job is failed instead of resumed
LOGIN_REFUSED = "proxy login refused: the proxy answered 407 to its username and password"
# Chrome's errors for a proxy that failed, and ours. Matched as written: an error also holds the
# search URL, so a keyword such as "tunnel car wash" must not read as a proxy error.
PROXY_FAILED = re.compile(r"\bERR_(?:PROXY|SOCKS|TUNNEL)_|proxy login refused")
TIMED_OUT = re.compile(r"\bERR_(?:CONNECTION_)?TIMED_OUT\b")  # through a proxy: maybe the proxy, so try the next
RELAUNCHES = 2        # times a job starts Chrome again after it died on its own (a crash, out of memory)
REVIEWS_MAX = 5       # the reviews a place panel shows first; more would need its review list


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Job body ──
LANG = re.compile(r"[A-Za-z]{2}")  # fullmatch: "$" would let "en\n" through
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
    if not isinstance(lang, str) or not LANG.fullmatch(lang):
        raise ValueError("invalid lang")
    country = body.get("country")
    if country in (None, ""):
        country = ""
    elif not isinstance(country, str) or not LANG.fullmatch(country):
        raise ValueError("invalid country: a 2-letter country code such as us or de")
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
    data = {"keywords": [k.strip() for k in kws], "lang": lang.lower(), "country": country.lower(),
            "lat": str(lat).strip(), "lon": str(lon).strip(),
            "zoom": _int(body, "zoom", 15, 1, 21, "zoom"),
            "radius": _int(body, "radius", 10000, 0, 10**9, "radius"),  # accepted, not used
            "depth": _int(body, "depth", 1, 1, 1000, "depth"),
            "max_time": _int(body, "max_time", 600, 1, 86400, "max time"),
            "fast_mode": bool(body.get("fast_mode")), "email": bool(body.get("email")),
            "verify_emails": bool(body.get("verify_emails")), "socials": bool(body.get("socials")),
            "reviews_max": _int(body, "reviews_max", 0, 0, REVIEWS_MAX, "reviews_max"),
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
            raise ValueError("invalid proxy: use http://, https://, socks5:// or socks5h://host:port") from None
        if u.scheme not in PROXY_SCHEMES or not u.hostname:
            raise ValueError("invalid proxy: use http://, https://, socks5:// or socks5h://host:port")
        if (u.username or u.password) and u.scheme.startswith("socks"):
            raise ValueError("invalid proxy: Chrome cannot log in to a SOCKS5 proxy; use its http:// address "
                             "with user:pass, or one that allows your IP address")
    return [p.strip() for p in v]


def mask(proxy):
    """A proxy URL with its password replaced by *** (or its username, when that is the only
    secret, as some providers do): what the API and the web UI show."""
    u = urllib.parse.urlsplit(proxy)
    if not (u.username or u.password):
        return proxy
    user, host = u.netloc.rpartition("@")[0], u.netloc.rpartition("@")[2]
    shown = user.partition(":")[0] + ":***" if u.password else "***"
    return urllib.parse.urlunsplit(u._replace(netloc=f"{shown}@{host}"))


def locale(d):
    """Chrome's --lang (its Accept-Language) for a job: en, or en-US when the job names a country."""
    return d["lang"] + (f"-{d['country'].upper()}" if d.get("country") else "")


# ── Rows ──
def fields(d):
    """A job's CSV columns: the old engine's 36, ours, then those its website pass fills."""
    return COLUMNS + EXTRA + (SOCIALS if d.get("socials") else []) + (["email_host"] if d.get("verify_emails") else [])


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


def place_key(p):
    """What makes two places (or rows) the same place."""
    return p.get("data_id") or p.get("place_id") or p.get("link") or p.get("title")


def to_csv(rows, cols=None, header=True):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols or COLUMNS + EXTRA, extrasaction="ignore")
    if header:
        w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


# ── Store ──
class Store:
    """Jobs in sqlite3 (jobs.db) and their results as <id>.csv beside it. One lock serialises every
    database call and every CSV read, append and replace, so a download never meets a half-written
    row or a half-replaced file (Windows cannot replace a file another handle has open)."""

    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.folder = folder
        self.lock = threading.RLock()
        self.db = sqlite3.connect(os.path.join(folder, "jobs.db"), check_same_thread=False)
        with self.lock, self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, name TEXT NOT NULL, "
                            "date TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL, "
                            "error TEXT NOT NULL DEFAULT '', progress TEXT NOT NULL DEFAULT '{}')")
            if "progress" not in [c[1] for c in self.db.execute("PRAGMA table_info(jobs)")]:  # a P1 or P2 store
                self.db.execute("ALTER TABLE jobs ADD COLUMN progress TEXT NOT NULL DEFAULT '{}'")

    SELECT = "SELECT id, name, date, status, data, error, progress FROM jobs"

    @staticmethod
    def _job(r):
        # The old engine's job shape (capitalised keys), plus Error (why a job failed or stopped early)
        # and Progress (the worker's own record; public() is what the API shows).
        return {"ID": r[0], "Name": r[1], "Date": r[2], "Status": r[3], "Data": json.loads(r[4]), "Error": r[5],
                "Progress": json.loads(r[6] or "{}")}

    def create(self, name, data):
        jid = str(uuid.uuid4())
        with self.lock, self.db:
            self.db.execute("INSERT INTO jobs (id, name, date, status, data, error, progress) "
                            "VALUES (?, ?, ?, 'pending', ?, '', '{}')", (jid, name, utc_now(), json.dumps(data)))
        return jid

    def get(self, jid):
        with self.lock:
            r = self.db.execute(self.SELECT + " WHERE id = ?", (jid,)).fetchone()
        return self._job(r) if r else None

    def all(self):
        with self.lock:
            return [self._job(r) for r in self.db.execute(self.SELECT + " ORDER BY rowid DESC")]

    def update(self, jid, status, error=""):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET status = ?, error = ? WHERE id = ?", (status, error[:500], jid))

    def set_progress(self, jid, progress):
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET progress = ? WHERE id = ?", (json.dumps(progress), jid))

    def claim(self):
        """The oldest pending job, now marked working; None when there is none."""
        with self.lock, self.db:
            r = self.db.execute(self.SELECT + " WHERE status = 'pending' ORDER BY rowid LIMIT 1").fetchone()
            if not r:
                return None
            self.db.execute("UPDATE jobs SET status = 'working' WHERE id = ?", (r[0],))
        job = self._job(r)
        job["Status"] = "working"
        return job

    def recover(self, max_resumes=MAX_RESUMES):
        """Jobs left on "working" by an engine that was stopped or crashed: back to pending, so they
        resume from their last finished keyword (the rows of the keyword cut short are removed, a
        torn last line too). After max_resumes such restarts a job is failed instead, rows kept, as
        before resumable jobs. (resumed, failed)"""
        resumed = failed = 0
        with self.lock, self.db:
            for jid, prog in self.db.execute("SELECT id, progress FROM jobs WHERE status = 'working'").fetchall():
                p = json.loads(prog or "{}")
                if p.get("stage", "search") == "search":
                    self.cut(jid, p.get("bytes", 0))
                if p.get("resumes", 0) >= max_resumes:
                    self.db.execute("UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                                    (f"the engine stopped while this job ran ({p['resumes'] + 1} times); its rows "
                                     "so far are kept", jid))
                    failed += 1
                else:
                    p["resumes"] = p.get("resumes", 0) + 1
                    self.db.execute("UPDATE jobs SET status = 'pending', progress = ? WHERE id = ?",
                                    (json.dumps(p), jid))
                    resumed += 1
        return resumed, failed

    def delete(self, jid):
        with self.lock, self.db:
            gone = self.db.execute("DELETE FROM jobs WHERE id = ?", (jid,)).rowcount
            for path in (self.csv_path(jid), self.csv_path(jid) + ".tmp"):
                try:
                    os.remove(path)
                except OSError:
                    pass
        return bool(gone)

    def import_kit1(self, folder, log=print):
        """Kit 1.x's jobs and their CSVs, from the old engine's data folder (docker-compose.yml mounts
        its volume read-only on .engine/kit1), so an update from 1.x keeps them in the job list. Once
        only: a marker file stops it next time, so a job deleted here stays deleted. Jobs with no CSV
        (never run) are left out, and one that cannot be read is skipped and logged by id. Jobs
        imported; an unreadable old database raises sqlite3.Error and is tried again on the next start."""
        marker = os.path.join(self.folder, "kit1-imported")
        if os.path.exists(marker) or not os.path.isfile(os.path.join(folder, "jobs.db")):
            return 0
        # The old database is in WAL mode on a read-only volume: read a copy, its WAL included.
        tmp = tempfile.mkdtemp(dir=self.folder)
        try:
            for f in ("jobs.db", "jobs.db-wal"):
                if os.path.isfile(os.path.join(folder, f)):
                    shutil.copyfile(os.path.join(folder, f), os.path.join(tmp, f))
            old = sqlite3.connect(os.path.join(tmp, "jobs.db"))
            try:
                rows = old.execute("SELECT id, name, status, data, created_at FROM jobs ORDER BY created_at").fetchall()
            finally:
                old.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        n = 0
        for jid, name, status, data, created in rows:
            try:
                plain = str(uuid.UUID(str(jid))) == jid  # a job id, never a path, goes into a file name
            except ValueError:
                plain = False
            src = os.path.join(folder, f"{jid}.csv")
            if not plain or not os.path.isfile(src) or self.get(jid):
                continue
            try:  # one odd old job must never stop the engine from starting
                try:
                    d = json.loads(data or "{}")
                except ValueError:
                    d = {}
                if not isinstance(d, dict):
                    d = {}
                if not isinstance(d.get("keywords"), list):
                    d["keywords"] = []
                if isinstance(d.get("max_time"), int):
                    d["max_time"] //= 10 ** 9  # nanoseconds in 1.x, seconds here
                with open(src, newline="", encoding="utf-8", errors="replace") as f:
                    places = max(sum(1 for _ in csv.reader(f)) - 1, 0)
                date = datetime.fromtimestamp(int(created), timezone.utc).isoformat(timespec="seconds")
                ok = status == "ok"
                with self.lock, self.db:
                    shutil.copyfile(src, self.csv_path(jid))
                    self.db.execute("INSERT INTO jobs (id, name, date, status, data, error, progress) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (jid, str(name), date, "ok" if ok else "failed", json.dumps(d),
                                     "" if ok else f"kit 1.x job that was {status} there; its rows are kept",
                                     json.dumps({"done": len(d.get("keywords") or []) if ok else 0, "places": places,
                                                 "stage": "done" if ok else ""})))
            except Exception as e:
                try:
                    os.remove(self.csv_path(jid))
                except OSError:
                    pass
                log(f"kit 1.x job {jid} not imported: {type(e).__name__}")
                continue
            n += 1
        with open(marker, "w"):
            pass
        return n

    def csv_path(self, jid):
        return os.path.join(self.folder, f"{jid}.csv")

    def _exists(self, jid):
        return self.db.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone() is not None

    def append(self, jid, rows, cols):
        """Add rows to the job's CSV (the header first on an empty file); the file's size after it,
        or None (nothing written) if the job was deleted."""
        with self.lock:
            if not self._exists(jid):
                return None
            path = self.csv_path(jid)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if rows or not size:
                with open(path, "ab") as f:
                    f.write(to_csv(rows, cols, header=not size))
            return os.path.getsize(path)

    def cut(self, jid, size):
        """Shorten the job's CSV to `size` bytes (its length at the last finished keyword)."""
        with self.lock:
            path = self.csv_path(jid)
            try:
                if not size:
                    os.remove(path)
                elif os.path.getsize(path) > size:
                    with open(path, "r+b") as f:
                        f.truncate(size)
            except OSError:
                pass

    def rewrite(self, jid, rows, cols):
        """Replace the job's CSV in one step (after a pass that changed rows); its new size, or None
        (nothing written) if the job was deleted."""
        with self.lock:
            if not self._exists(jid):
                return None
            tmp = self.csv_path(jid) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(to_csv(rows, cols))
            os.replace(tmp, self.csv_path(jid))
            return os.path.getsize(self.csv_path(jid))

    def finish(self, jid, rows, cols, status, error, progress):
        """Replace the job's CSV in one step and record how the job ended, under one lock; False
        (nothing written) if the job was deleted."""
        with self.lock:
            if self.rewrite(jid, rows, cols) is None:
                return False
            with self.db:
                self.db.execute("UPDATE jobs SET status = ?, error = ?, progress = ? WHERE id = ?",
                                (status, error[:500], json.dumps(progress), jid))
            return True

    def read_csv(self, jid):
        with self.lock:
            try:
                with open(self.csv_path(jid), "rb") as f:
                    return f.read()
            except OSError:
                return None

    def read_rows(self, jid):
        """The job's rows as dicts ([] before its first row)."""
        data = self.read_csv(jid)
        return list(csv.DictReader(io.StringIO(data.decode("utf-8", "replace")))) if data else []


def public(job):
    """A job as the API shows it: proxy passwords masked, Progress without the worker's bookkeeping."""
    d, p = job["Data"], job["Progress"]
    total = len(d.get("keywords") or [])
    stage = p.get("stage") or ("queued" if job["Status"] == "pending" else "")
    if job["Status"] == "ok":
        stage = "done"
    return dict(job, Data=dict(d, proxies=[mask(x) for x in d.get("proxies") or []]),
                Progress={"keywords_done": p.get("done", 0), "keywords_total": total, "places": p.get("places", 0),
                          "percent": 100 if job["Status"] == "ok" else p.get("percent", 0), "stage": stage})


# ── Worker ──
class Worker(threading.Thread):
    """Runs pending jobs one at a time, oldest first: never two scrapes against Google at once.
    headless=False runs Chrome as an off-screen window. Both get Google's full view (review counts,
    the week's hours, reviews): see browser.py."""

    def __init__(self, store, log=print, headless=True):
        super().__init__(daemon=True, name="jobs")
        self.store, self.log, self.headless = store, log, headless
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self._running = (None, None)  # (job id, stop event) of the job in progress
        self._browser = None

    def run(self):
        while not self.stopped.is_set():
            job = None
            try:
                job = self.store.claim()
                if not job:
                    self.wake.wait(5)
                    self.wake.clear()
                    continue
                self.run_job(job)
            except Exception as e:  # noqa: BLE001 - one bad job (or a full disk) must never stop the worker
                self.log(f"job {job['ID'] if job else '-'} failed: {type(e).__name__}: {e}")
                try:
                    if job:
                        self.store.update(job["ID"], "failed", f"{type(e).__name__}: {e}")
                except Exception:  # noqa: BLE001
                    pass
                self.stopped.wait(2)

    def cancel(self, jid):
        """Stop `jid` if it is the job in progress (it is being deleted)."""
        rid, stop = self._running
        if rid == jid and stop:
            stop.set()

    def shutdown(self, wait=0):
        """Stop the job in progress and its Chrome (the engine is exiting). With `wait`, give the job
        that many seconds to save its progress, so the next start resumes it."""
        self.stopped.set()
        self.wake.set()
        _, stop = self._running
        if stop:
            stop.set()
        b = self._browser
        if b:
            b.close()
        if wait and self.is_alive():
            self.join(wait)

    def run_job(self, job):
        jid, d = job["ID"], job["Data"]
        p = dict(job["Progress"])
        kws, cols, proxies = d["keywords"], fields(d), d.get("proxies") or []
        started, used = time.monotonic(), p.get("elapsed", 0)
        budget = max(0, d["max_time"] - used)  # a resumed job has the rest of its max_time
        deadline = started + budget
        stop = threading.Event()
        self._running = (jid, stop)
        if self.stopped.is_set():  # shutdown() came between claim() and now
            stop.set()
        if not self.store.get(jid):  # deleted between claim() and now, before cancel() could see it
            self._running = (None, None)
            return
        done, stage = min(p.get("done", 0), len(kws)), p.get("stage", "search")
        reviews = d.get("reviews_max") and stage in ("search", "reviews")
        if stage == "search":
            self.store.cut(jid, p.get("bytes", 0))  # the rows of a keyword cut short are searched again
        rows = self.store.read_rows(jid)  # [] when the cut removed the file
        seen = {place_key(r) for r in rows}
        # What an earlier run of this job saw, for its final note (a job resumed after its search).
        errors = [p["last_error"]] * p.get("failed", 0) if p.get("last_error") else []
        notes, blocked, retries, rot, refused = [], p.get("blocked", False), p.get("retried", 0), 0, set()
        bad, relaunched, timed_out = set(), 0, {"yes": p.get("timed_out", False)}  # bad: proxies that failed

        def pick(i):  # the keyword's proxy in turn, past the ones that failed (all failed: in turn anyway)
            if not proxies:
                return None
            turn = [proxies[(i + rot + k) % len(proxies)] for k in range(len(proxies))]
            return next((x for x in turn if x not in bad), turn[0])
        cur = {"i": done, "kw": ""}
        if done or p.get("resumes"):
            self.log(f"job {jid} resumed at keyword {done + 1}/{len(kws)} with {len(rows)} places, {budget:.0f} s left")
        else:
            self.log(f"job {jid} started: {len(kws)} keyword(s), depth {d['depth']}, max_time {d['max_time']} s")

        def save(**kw):
            p.update(kw, elapsed=round(used + time.monotonic() - started, 1), places=len(rows))
            self.store.set_progress(jid, p)

        def on_batch(places, batches):  # streams each batch's new places into the CSV
            fresh = []
            for place in places:
                k = place_key(place)
                if k and k not in seen:
                    seen.add(k)
                    fresh.append(to_row(place, cur["kw"]))
            rows.extend(fresh)
            if fresh and self.store.append(jid, fresh, cols) is None:
                stop.set()  # deleted
            save(percent=int(100 * (cur["i"] + min(batches / d["depth"], 1)) / len(kws)))

        def watchdog():  # Chrome hung or the job overran: kill Chrome, and every CDP call fails at once
            timed_out["yes"] = True
            stop.set()
            b = self._browser
            if b:
                b.kill()
            self.log(f"job {jid}: watchdog stopped Chrome {WATCHDOG_GRACE} s after max_time")
        timer = threading.Timer(budget + WATCHDOG_GRACE, watchdog)
        timer.daemon = True
        timer.start()
        try:
            save(stage=stage, done=done, total=len(kws))
            if stage == "search" and done < len(kws) and budget <= 0:
                timed_out["yes"] = True  # resumed with no time left: the other keywords stay unsearched
            if ((stage == "search" and done < len(kws)) or reviews) and budget > 0 and not stop.is_set():
                self._browser = browser.Browser(lang=locale(d), headless=self.headless)
            i, again = done, False
            while stage == "search" and i < len(kws) and self._browser:
                left = deadline - time.monotonic()
                if left <= 0:
                    timed_out["yes"] = True
                if stop.is_set() or left <= 0:
                    break
                cur.update(i=i, kw=kws[i])
                proxy = pick(i)
                url = maps.search_url(kws[i], d["lat"], d["lon"], d["zoom"], d["lang"], d.get("country", ""))
                t0 = time.monotonic()
                try:
                    places, reason, stats = self._collect(proxy, url, d["depth"], left, stop, on_batch)
                except (CDPError, OSError) as e:
                    if str(e) == LOGIN_REFUSED:
                        refused.add(proxy)
                    if PROXY_FAILED.search(str(e)):
                        bad.add(proxy)
                    if (self._browser.cdp.closed and not stop.is_set() and relaunched < RELAUNCHES
                            and deadline - time.monotonic() > 0):
                        relaunched += 1  # Chrome died on its own: a new one takes the same keyword
                        self.log(f"job {jid} keyword {i + 1}/{len(kws)}: Chrome stopped ({e}); starting it again")
                        self._browser.close()
                        self._browser = browser.Browser(lang=locale(d), headless=self.headless)
                        continue
                    if ((PROXY_FAILED.search(str(e)) or TIMED_OUT.search(str(e))) and len(proxies) > 1
                            and retries < len(BACKOFF) and len(refused) < len(set(proxies))
                            and not self._browser.cdp.closed and not stop.is_set()):
                        retries, rot = retries + 1, rot + 1
                        self.log(f"job {jid} keyword {i + 1}/{len(kws)}: the proxy failed; retry {retries}/"
                                 f"{len(BACKOFF)} through the next proxy")
                        continue
                    errors.append(str(e))
                    self.log(f"job {jid} keyword {i + 1}/{len(kws)}: error: {e}")
                    # Chrome is gone, or no proxy takes the job's login: the other keywords would fail too
                    if self._browser.cdp.closed or (refused and len(refused) >= len(set(proxies))):
                        if not stop.is_set() and len(kws) - i > 1:
                            errors[-1] += f"; {len(kws) - i - 1} keyword(s) after it were not searched"
                        break
                    i, retries, again = i + 1, 0, False
                    save(failed=len(errors), last_error=errors[-1])
                    continue
                on_batch(places, stats["batches"])  # places no batch carried (one place, the DOM fallback)
                if stop.is_set():  # deleted, or the engine is exiting: this keyword did not finish
                    break
                if reason == "blocked":  # Google limits this IP: wait, try again, then give up
                    if retries < len(BACKOFF) and not stop.is_set():
                        wait = ROTATE_WAIT if len(proxies) > 1 else BACKOFF[retries]
                        if time.monotonic() + wait < deadline:
                            retries, rot = retries + 1, rot + 1
                            self.log(f"job {jid} keyword {i + 1}/{len(kws)}: Google's unusual-traffic page; "
                                     f"retry {retries}/{len(BACKOFF)} in {wait} s"
                                     + (" through the next proxy" if len(proxies) > 1 else ""))
                            save()
                            if stop.wait(wait):
                                break
                            continue
                    blocked = True
                    self.log(f"job {jid} keyword {i + 1}/{len(kws)}: Google's unusual-traffic page; stopping")
                    break
                if reason == "no-feed" and not again and time.monotonic() + 30 < deadline:
                    again = True  # Google sometimes shows no result list once (seen in CI): look once more
                    if len(proxies) > 1:
                        rot += 1  # through the next proxy
                    self.log(f"job {jid} keyword {i + 1}/{len(kws)}: no result list; one more try")
                    continue
                if reason == "no-feed":
                    notes.append(f"keyword {i + 1}: Google showed no result list{' twice' if again else ''} "
                                 "(no results for it, or a page the engine cannot read)")
                retries, again = 0, False
                if reason.startswith("timeout"):  # "timeout+dom-fallback" too
                    timed_out["yes"] = True
                size = self.store.append(jid, [], cols)
                if size is None:
                    break  # deleted
                if reason.startswith("error: "):
                    errors.append(reason[7:])
                save(done=i + 1, bytes=size, percent=int(100 * (i + 1) / len(kws)), failed=len(errors),
                     last_error=errors[-1] if errors else "")
                self.log(f"job {jid} keyword {i + 1}/{len(kws)}: {len(places)} places, {len(rows)} in all, "
                         f"{stats['batches']} batches, {time.monotonic() - t0:.1f} s ({reason})")
                if reason.startswith("error: ") and self._browser.cdp.closed:  # Chrome failed mid-scroll;
                    if stop.is_set() or relaunched >= RELAUNCHES or deadline <= time.monotonic():  # rows kept
                        if not stop.is_set() and len(kws) - i > 1:
                            errors[-1] += f"; {len(kws) - i - 1} keyword(s) after it were not searched"
                        break
                    relaunched += 1
                    self._browser.close()
                    self._browser = browser.Browser(lang=locale(d), headless=self.headless)
                i += 1
            if stage == "search" and not stop.is_set():  # the search is over, for good: never search again
                stage = "reviews" if reviews else "websites"
                save(stage=stage, bytes=self.store.append(jid, [], cols), blocked=blocked, retried=retries,
                     failed=len(errors), last_error=errors[-1] if errors else "", timed_out=timed_out["yes"])
            if stage == "reviews" and rows and self._browser and not (stop.is_set() or blocked):
                n = self.add_reviews(rows, d, deadline, stop, [x for x in proxies if x not in bad] or proxies,
                                     save, notes)
                self.store.rewrite(jid, rows, cols)  # keep the reviews if the engine stops during the next pass
                self.log(f"job {jid}: reviews for {n}/{sum(1 for r in rows if r.get('place_id'))} places")
            if stage == "reviews" and not stop.is_set():
                stage = "websites"
                save(stage=stage)
        except browser.BrowserError as e:
            errors.append(str(e))
            self.log(f"job {jid}: {e}")
        finally:
            timer.cancel()
            if self._browser:
                self._browser.close()
                self._browser = None
        if stage == "websites" and (d["email"] or d.get("socials")) and rows and not stop.is_set():
            n, unchecked = add_site_data(rows, max(deadline - time.monotonic(), EMAIL_MIN_SECONDS), email=d["email"],
                                         socials=d.get("socials"), verify=d.get("verify_emails"), stop=stop.is_set)
            self.log(f"job {jid}: website data for {n}/{sum(1 for r in rows if r['website'])} websites")
            if unchecked and not stop.is_set():
                notes.append(f"{unchecked} website(s) not checked: ran out of time")
        self._running = (None, None)
        if self.stopped.is_set() and self.store.get(jid):  # the engine is exiting: resume on the next start
            if stage != "search":  # rows a pass changed; while searching the CSV is the record already
                self.store.rewrite(jid, rows, cols)
            save()
            self.store.update(jid, "pending")
            self.log(f"job {jid} paused at keyword {p.get('done', 0) + 1}/{len(kws)} ({stage}): resumes when the "
                     "engine starts again")
            return
        block = ("Google answered with its unusual-traffic page: this IP is rate-limited for now. "
                 + (f"Waited and retried {retries} time(s). " if retries else "") + "Wait, or add proxies")
        p.update(stage="done", elapsed=round(used + time.monotonic() - started, 1), places=len(rows))
        if rows or not (errors or blocked):
            heads = []
            if timed_out["yes"]:  # the search ran out of time (the passes after it note their own)
                heads.append("stopped at max_time; the rows found so far are kept")
            if errors:
                heads.append(f"{len(errors)} keyword(s) failed: {errors[-1]}")
            if blocked:
                heads, notes = [f"{block}. The rows found before that are kept"], []
            status, note = "ok", "; ".join(heads + notes)
        else:
            status = "failed"
            note = block if blocked else errors[-1]
        if not self.store.finish(jid, rows, cols, status, note, p):
            self.log(f"job {jid} deleted while it ran")
            return
        self.log(f"job {jid} {status}: {len(rows)} places" + (f": {note}" if status == "failed" else ""))

    def _collect(self, proxy, url, depth, left, stop, on_batch):
        """One keyword in its own browser context, through `proxy`; maps.collect's answer."""
        ctx = page = auth = None
        try:
            ctx = self._browser.context(proxy)
            page = Page(self._browser.cdp, context=ctx)
            _, user, password = browser.split_proxy(proxy) if proxy else ("", "", "")
            if user or password:
                auth = browser.ProxyAuth(page, user, password)
            try:
                got = maps.collect(page, url, depth=depth, max_seconds=left, log=lambda m: None,
                                   stop=stop.is_set, on_batch=on_batch)
            except CDPError:
                if auth and auth.refused:  # Chrome only says ERR_HTTP_RESPONSE_CODE_FAILURE
                    raise CDPError(LOGIN_REFUSED) from None
                raise
            if auth and auth.refused and not got[0]:
                raise CDPError(LOGIN_REFUSED)
            return got
        finally:
            if auth:
                auth.close()
            if page and not self._browser.cdp.closed:
                page.close()
            if ctx and not self._browser.cdp.closed:
                self._browser.dispose(ctx)

    def add_reviews(self, rows, d, deadline, stop, proxies, save=lambda: None, notes=None):
        """Review count, stars histogram and up to reviews_max reviews per place, from each place's
        panel (the result batches carry no review texts): one page load per place, until the
        deadline. The number of places that got reviews."""
        todo = [r for r in rows if r.get("place_id") and not r.get("user_reviews")]
        ctx = page = auth = None
        n = 0
        try:
            proxy = proxies[0] if proxies else None
            ctx = self._browser.context(proxy)
            page = Page(self._browser.cdp, context=ctx)
            _, user, password = browser.split_proxy(proxy) if proxy else ("", "", "")
            if user or password:
                auth = browser.ProxyAuth(page, user, password)
            for k, v in maps.CONSENT_COOKIES.items():
                page.set_cookie(k, v, ".google.com")
            for k, r in enumerate(todo):
                if stop.is_set():
                    break
                if time.monotonic() >= deadline:
                    if notes is not None:
                        notes.append(f"reviews read for {k} of {len(todo)} places: ran out of time")
                    break
                url = r["link"] + f"&hl={d['lang']}" + (f"&gl={d['country']}" if d.get("country") else "")
                body = maps.place_panel(page, url)
                if body == "blocked":
                    if notes is not None:
                        notes.append(f"reviews read for {k} of {len(todo)} places: Google's unusual-traffic page")
                    break
                got = details.parse_place(body or "", d["reviews_max"])
                for k, v in got.items():
                    if k in ("user_reviews", "reviews_per_rating") or not r.get(k):
                        r[k] = str(v)
                n += "user_reviews" in got
                save()  # elapsed time, for a resume after a crash
        except (CDPError, OSError) as e:  # Chrome or the proxy went away: the reviews so far are kept
            if notes is not None:
                notes.append(f"the reviews pass stopped early: {e}")
        finally:
            if auth:
                auth.close()
            if page and not self._browser.cdp.closed:
                page.close()
            if ctx and not self._browser.cdp.closed:
                self._browser.dispose(ctx)
        return n


# ── Website pass (email, socials, verify_emails): each business homepage over plain HTTP ──
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")
JUNK_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
JUNK_DOMAIN = ("example.", "sentry", "wixpress.com", "domain.com", "yourdomain", "email.com")
UA = "Mozilla/5.0 (compatible; google-maps-scraper-kit; +https://github.com/Mahanaicoach/google-maps-scraper-kit)"
# The same patterns as scripts/scrape.py --socials (a test keeps them in step).
SOCIAL_RE = {
    "instagram": re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)", re.I),
    "facebook":  re.compile(r"https?://(?:www\.|m\.|web\.)?facebook\.com/([A-Za-z0-9_.\-]+)", re.I),
    "linkedin":  re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:company|in|school)/([A-Za-z0-9_.\-%]+)", re.I),
    "tiktok":    re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]+)", re.I),
    "youtube":   re.compile(r"https?://(?:www\.)?youtube\.com/(?:@|channel/|c/|user/)([A-Za-z0-9_.\-]+)", re.I),
    "x":         re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})\b", re.I),
}
SKIP_HANDLE = {"", "home", "pages", "people", "help", "about", "policies", "policy", "legal", "tos", "privacy",
               "settings", "sharer", "tr", "profile.php", "plugins", "dialog", "intent", "login", "share.php",
               "permalink.php", "p", "reel", "reels", "explore", "stories", "tv", "watch", "events", "groups",
               "marketplace", "gaming", "photo", "hashtag", "search", "share", "i"}
DOH = "https://dns.google/resolve?"  # Google's public DNS over HTTPS: no key, no account


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


def find_socials(text):
    """The first profile link per network on a page, share and login links skipped."""
    out = dict.fromkeys(SOCIALS, "")
    for net, rx in SOCIAL_RE.items():
        for m in rx.finditer(text or ""):
            h = m.group(1).lower()
            if h in SKIP_HANDLE or (net == "facebook" and (h.isdigit() or len(h) < 3)):
                continue
            out[net] = m.group(0).rstrip("\"'/").replace("\\", "")
            break
    return out


_MX = {}  # domain -> answer; a failed lookup is not kept, so the next job asks again


def mx_host(domain):
    """'nxdomain' when the domain does not exist, 'none' when it has no MX record, else who runs its
    mail: 'google_workspace', 'microsoft_365' or 'other'; '' when the lookup failed. Like scrape.py."""
    if domain in _MX:
        return _MX[domain]
    try:
        with _OPENER.open(urllib.request.Request(DOH + urllib.parse.urlencode({"name": domain, "type": "MX"}),
                                                 headers={"User-Agent": UA}), timeout=8) as r:
            ans = json.load(r)
    except Exception:  # noqa: BLE001 - no answer is not a verdict
        return ""
    mx = " ".join(a.get("data", "") for a in ans.get("Answer") or [] if a.get("type") == 15).lower()
    if ans.get("Status") == 3:
        host = "nxdomain"
    elif not mx:
        host = "none" if ans.get("Status") == 0 else ""
    elif "google.com" in mx or "googlemail.com" in mx:
        host = "google_workspace"
    else:
        host = "microsoft_365" if "outlook.com" in mx else "other"
    if host:
        if len(_MX) > 4096:  # ponytail: forget all at the cap; an LRU would keep the busy domains
            _MX.clear()
        _MX[domain] = host
    return host


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


def add_site_data(rows, seconds, email=True, socials=False, verify=False, get=None, stop=lambda: False):
    """One homepage fetch per row with a website, SITE_WORKERS at a time, for at most `seconds` or
    until stop() is true: `emails` (joined by ", ", like the old engine), the social columns, and
    with verify an MX check of each address's domain (dns.google) that drops domains that do not
    exist and fills `email_host`. (rows that got something, rows not checked in time)."""
    get = get or fetch

    def work(r):
        page, out = get(r["website"]), {}
        if email and not r.get("emails"):
            found = find_emails(page, site_host(r["website"]))
            if verify:
                hosts = [(e, mx_host(e.rpartition("@")[2])) for e in found]
                found = [e for e, h in hosts if h != "nxdomain"]
                out["email_host"] = next((h for e, h in hosts if h != "nxdomain"), "")
            out["emails"] = ", ".join(found)
        if socials:
            out.update(find_socials(page))
        return out

    todo = [r for r in rows if r.get("website")]
    pool = concurrent.futures.ThreadPoolExecutor(SITE_WORKERS)
    futs = {pool.submit(work, r): r for r in todo}
    end, pending = time.monotonic() + seconds, set(futs)
    while pending and not stop() and time.monotonic() < end:
        _, pending = concurrent.futures.wait(pending, timeout=min(1, max(0, end - time.monotonic())))
    done = set(futs) - pending
    pool.shutdown(wait=False, cancel_futures=True)
    n = 0
    for f in done:
        if not f.exception():
            got = {k: v for k, v in f.result().items() if v}
            futs[f].update(got)
            n += bool(got)
    return n, len(pending)
