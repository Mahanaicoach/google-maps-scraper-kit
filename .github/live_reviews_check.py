"""engine-live.yml: one depth-1 job with reviews_max=1 against the engine on localhost (SCRAPER_PORT, default 8080); exits 1
unless Google's full view came back (review counts and reviews filled). Prints counts only."""
import json, os, sys, time, urllib.request

BASE = f"http://localhost:{os.environ.get('SCRAPER_PORT', '8080')}/api/v1/jobs"


def call(method, url, body=None):
    r = urllib.request.Request(url, json.dumps(body).encode() if body else None,
                               {"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(r, timeout=30) as f:
        return json.loads(f.read())


jid = call("POST", BASE, {"name": sys.argv[1], "keywords": ["coffee shops in Austin TX"], "lang": "en",
                          "country": "us", "lat": "30.2672", "lon": "-97.7431", "depth": 1,
                          "max_time": 90, "reviews_max": 1})["id"]
for _ in range(200):
    job = call("GET", f"{BASE}/{jid}")
    if job["Status"] in ("ok", "failed"):
        break
    time.sleep(2)
rows = json.loads(urllib.request.urlopen(f"{BASE}/{jid}/download?format=json").read())
fill = {k: sum(1 for r in rows if r.get(k)) for k in ("review_count", "open_hours", "user_reviews")}
print(f"{sys.argv[1]}: status={job['Status']} places={len(rows)}", fill, job["Progress"])
sys.exit(0 if rows and fill["review_count"] and fill["user_reviews"] else 1)
