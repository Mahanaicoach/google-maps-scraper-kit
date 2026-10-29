#!/usr/bin/env python3
"""Simple web UI for the kit: type a business and a place, get a lead table and a CSV.

Run it, then open http://localhost:8090 in your browser:
    python3 scripts/ui.py

It sits in front of the engine on localhost:8080 and reuses scrape.py (geocoding, the 8 lead fields,
the deep email pass). Standard library only. Bound to 127.0.0.1, like the engine; never expose it.
"""
import csv, io, json, os, sys, threading, time, uuid, urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scrape  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PORT = int(os.environ.get("UI_PORT") or 8090)
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")

# How many results the user wants -> engine depth and wall-clock limit (seconds).
SIZES = {"few": (5, 600), "more": (10, 1200), "max": (15, 1800)}

TASKS = {}
LOCK = threading.Lock()  # one search at a time: parallel jobs without proxies get throttled


def update(task, **kw):
    with LOCK:
        task.update(kw)


def lead_rows(rows):
    return [{k: r.get(k, "") for k in scrape.LEAD} for r in rows]


def find_emails(task, results):
    """Same pass as scrape.enrich_emails, but reports progress to the page."""
    todo = [r for r in results if r.get("website") and not r.get("emails")]
    def work(r):
        site = r["website"]
        home = scrape._fetch_cached(site)
        found = scrape.extract_emails(home, site)
        if home and not found:
            pages = "".join(scrape._fetch_html(u, timeout=8) for u in scrape.contact_links(home, site))
            found = scrape.extract_emails(pages, site)
        r["emails"] = ", ".join(found)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, _ in enumerate(ex.map(work, todo), 1):
            update(task, message=f"Checking business websites for emails ({i} of {len(todo)})")


def run_search(task, businesses, place, size, emails):
    try:
        update(task, stage="locating", message=f"Finding {place} on the map")
        coords = scrape.geocode(place)
        if not coords:
            raise RuntimeError(f"Couldn't find \"{place}\" on the map. Try adding the city or country, "
                               "e.g. \"Rawat, Pakistan\".")
        lat, lon = coords
        depth, max_time = SIZES[size]
        keywords = [f"{b} in {place}" for b in businesses]
        body = {"name": f"{', '.join(businesses)} in {place}", "keywords": keywords, "lang": "en",
                "zoom": 15, "lat": lat, "lon": lon, "fast_mode": False, "radius": 10000,
                "depth": depth, "email": emails, "max_time": max_time}
        try:
            _, raw = scrape.req("POST", "/api/v1/jobs", body)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"The scraper refused the search (HTTP {e.code}).")
        job_id = json.loads(raw)["id"]
        update(task, stage="searching", job_id=job_id, started=time.time(),
               message="Searching Google Maps")

        while True:
            _, raw = scrape.req("GET", f"/api/v1/jobs/{job_id}")
            status = json.loads(raw).get("Status")
            if status == "ok":
                break
            if status == "failed":
                raise RuntimeError("Google Maps stopped answering. You may be rate-limited: "
                                   "wait 30–60 minutes and try again.")
            time.sleep(5)

        _, raw = scrape.req("GET", f"/api/v1/jobs/{job_id}/download")
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
        results = lead_rows(scrape.dedupe(rows))
        if emails and results:
            update(task, stage="emails", message="Checking business websites for emails")
            find_emails(task, results)
        update(task, stage="done", results=results,
               message=f"Found {len(results)} businesses")
    except Exception as e:
        msg = str(e) if isinstance(e, RuntimeError) else f"Something went wrong: {e}"
        update(task, stage="error", message=msg)


def csv_bytes(results):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=scrape.LEAD)
    w.writeheader()
    w.writerows(results)
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel shows Urdu/Arabic text correctly


def safe_name(text):
    keep = "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")
    return "-".join(p for p in keep.split("-") if p)[:60] or "results"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # keep the console quiet
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def download(self, results, name):
        self.send(200, csv_bytes(results), "text/csv; charset=utf-8",
                  {"Content-Disposition": f'attachment; filename="{safe_name(name)}.csv"'})

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            with open(PAGE, "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if p == "/api/health":
            try:
                scrape.req("GET", "/api/v1/jobs")
                return self.send(200, {"engine": True})
            except Exception:
                return self.send(200, {"engine": False})
        if p.startswith("/api/task/"):
            tid, _, rest = p[len("/api/task/"):].partition("/")
            task = TASKS.get(tid)
            if not task:
                return self.send(404, {"error": "unknown search"})
            if rest == "csv":
                return self.download(task.get("results") or [], task["name"])
            with LOCK:
                out = {k: v for k, v in task.items() if k != "thread"}
            if out.get("started"):
                out["elapsed"] = int(time.time() - out["started"])
            return self.send(200, out)
        if p == "/api/history":
            try:
                _, raw = scrape.req("GET", "/api/v1/jobs")
            except Exception:
                return self.send(200, [])
            jobs = json.loads(raw) or []
            out = []
            for j in jobs:
                kws = (j.get("Data") or {}).get("keywords") or []
                name = ", ".join(kws[:3]) + (f" + {len(kws) - 3} more" if len(kws) > 3 else "")
                out.append({"id": j.get("ID"), "name": name or j.get("Name"),
                            "date": j.get("Date"), "status": j.get("Status")})
            return self.send(200, sorted(out, key=lambda j: j["date"] or "", reverse=True))
        if p.startswith("/api/history/") and p.endswith("/csv"):
            jid = p[len("/api/history/"):-len("/csv")]
            try:
                _, raw = scrape.req("GET", f"/api/v1/jobs/{jid}/download")
            except Exception:
                return self.send(404, {"error": "results not available"})
            rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
            return self.download(lead_rows(scrape.dedupe(rows)), f"results-{jid[:8]}")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/search":
            return self.send(404, {"error": "not found"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError:
            return self.send(400, {"error": "bad request"})
        businesses = [b.strip() for b in str(data.get("business", "")).replace("\n", ",").split(",") if b.strip()]
        place = str(data.get("place", "")).strip()
        size = data.get("size") if data.get("size") in SIZES else "few"
        if not businesses or not place:
            return self.send(400, {"error": "Please fill in both the business type and the place."})
        with LOCK:
            if any(t["stage"] not in ("done", "error") for t in TASKS.values()):
                return self.send(409, {"error": "A search is already running. Wait for it to finish."})
            tid = uuid.uuid4().hex[:12]
            task = TASKS[tid] = {"id": tid, "stage": "starting", "message": "Starting",
                                 "name": f"{businesses[0]} in {place}", "businesses": businesses,
                                 "place": place, "size": size}
        threading.Thread(target=run_search, daemon=True,
                         args=(task, businesses, place, size, bool(data.get("emails", True)))).start()
        self.send(200, {"id": tid})

    def do_DELETE(self):
        if self.path.startswith("/api/history/"):
            jid = self.path[len("/api/history/"):]
            try:
                scrape.req("DELETE", f"/api/v1/jobs/{jid}")
            except Exception:
                return self.send(500, {"error": "couldn't delete"})
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Simple scraper UI running: open http://localhost:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
