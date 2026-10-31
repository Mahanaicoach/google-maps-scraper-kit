"""Google Maps search in a tab: open the search, pass the consent page, scroll the result feed,
and collect the places from the result batches the page itself downloads. Standard library only.
"""
import queue, re, time, urllib.parse

import details
from cdp import CDPError

# Google's consent interstitial (EU and some other regions) is skipped when these cookies exist.
CONSENT_COOKIES = {"CONSENT": "YES+cb.20210720-07-p0.en+FX+410",
                   "SOCS": "CAISHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg"}
END_MARKERS = ("You've reached the end of the list", "reached the end")
BATCH_URL = re.compile(r"^https://www\.google\.[a-z.]+/search\?.*tbm=map")
_CID = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)")
_LAT_LON = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_PLACE_ID = re.compile(r"!19s(ChIJ[\w-]+)")

# JavaScript run in the page. The feed is the scrollable results column.
JS_FEED = "!!document.querySelector('div[role=\"feed\"]')"
JS_LINKS = """(() => {
  const out = [];
  for (const a of document.querySelectorAll('div[role="feed"] a[href*="/maps/place/"]'))
    out.push({href: a.href, label: a.getAttribute('aria-label') || ''});
  return out;
})()"""
JS_SCROLL = """(() => {
  const f = document.querySelector('div[role="feed"]');
  if (!f) return null;
  f.scrollTo(0, f.scrollHeight);
  return f.scrollHeight;
})()"""
JS_END = "(() => { const f = document.querySelector('div[role=\"feed\"]'); return f ? f.innerText : ''; })()"
JS_CONSENT = """(() => {
  const b = [...document.querySelectorAll('button')].find(b => /accept all|reject all|alle akzeptieren|alle ablehnen/i.test(b.innerText));
  if (b) { b.click(); return true; } return false;
})()"""


def search_url(query, lat=None, lon=None, zoom=15, lang="en"):
    q = urllib.parse.quote(query, safe="")
    at = f"/@{lat},{lon},{zoom}z" if lat is not None and lon is not None else ""
    return f"https://www.google.com/maps/search/{q}{at}?hl={lang}"


def parse_link(href, label=""):
    """{link, title, data_id, latitude, longitude, place_id} from a /maps/place/ URL (the fallback
    when no result batch could be read)."""
    path = urllib.parse.urlparse(href).path
    name = ""
    if "/maps/place/" in path:
        name = urllib.parse.unquote_plus(path.split("/maps/place/", 1)[1].split("/", 1)[0])
    m = _CID.search(href)
    ll = _LAT_LON.search(href)
    pid = _PLACE_ID.search(href)
    return {"link": href, "title": label or name, "data_id": m.group(1) if m else "",
            "latitude": ll.group(1) if ll else "", "longitude": ll.group(2) if ll else "",
            "place_id": pid.group(1) if pid else ""}


class Batches:
    """Collects the result batches the page downloads: subscribe before navigating, then drain()."""

    def __init__(self, page):
        self.page = page
        self.q = page.cdp.subscribe("Network.responseReceived", page.sid)
        self.pending = {}   # requestId -> tries, for batches seen but not yet readable
        self.count = 0

    def drain(self):
        """New places from batches that finished since the last call."""
        while True:
            try:
                p = self.q.get_nowait()
            except queue.Empty:
                break
            if not isinstance(p, dict):  # the connection died (cdp puts a marker on every queue)
                raise CDPError(f"connection lost: {self.page.cdp.closed}")
            r = p.get("response", {})
            if BATCH_URL.match(r.get("url", "")) and "json" in (r.get("mimeType") or ""):
                self.pending[p["requestId"]] = 0
        rows = []
        for rid in list(self.pending):
            try:
                body = self.page.send("Network.getResponseBody", {"requestId": rid})["body"]
            except CDPError:
                if self.page.cdp.closed:
                    raise
                self.pending[rid] += 1  # not finished yet; try again next time, for about 10 s
                if self.pending[rid] > 25:  # failed or evicted: it never will be
                    del self.pending[rid]
                continue
            del self.pending[rid]
            self.count += 1
            rows += details.parse_batch(body)
        return rows

    def close(self):
        self.page.cdp.unsubscribe("Network.responseReceived", self.q, self.page.sid)


def collect(page, url, depth=1, max_seconds=180, log=print, stop=lambda: False):
    """Open `url`, scroll the result feed, and return (places, stop reason, stats).
    `depth` is the number of result batches to read (about 20 places each, like the old engine's
    depth); the list may end sooner. Places are de-duplicated on data_id. `stop()` returning True
    ends the scroll early (a deleted job). Reason "blocked" means Google answered with its
    unusual-traffic page."""
    for k, v in CONSENT_COOKIES.items():
        page.set_cookie(k, v, ".google.com")
    batches = Batches(page)
    seen, started, reason, stale = {}, time.time(), "depth", 0
    try:
        page.navigate(url)
        if "/sorry/" in page.url():
            return [], "blocked", {"batches": 0, "dom_links": 0}
        if "consent.google" in page.url():
            page.eval(JS_CONSENT)
            page.wait_for("!location.href.includes('consent.google')", timeout=15)
        if not page.wait_for(JS_FEED, timeout=15):
            if "/maps/place/" in page.url():  # the query resolved to a single place
                return [parse_link(page.url())], "single", {"batches": 0, "dom_links": 1}
            return [], "no-feed", {"batches": 0, "dom_links": 0}

        def take():
            new = 0
            for row in batches.drain():
                key = row.get("data_id") or row.get("place_id") or row["title"]
                if key not in seen:
                    seen[key] = row
                    new += 1
            return new

        try:  # a Chrome error mid-scroll keeps the places already read
            end = time.time() + 10  # the first batch arrives with the page
            while time.time() < end and batches.count == 0:
                take()
                time.sleep(0.4)
            log(f"  batch {batches.count}: {len(seen)} places")
            while batches.count < depth and time.time() - started < max_seconds and not stop():
                if any(m in (page.eval(JS_END) or "") for m in END_MARKERS):
                    reason = "end-of-list"
                    break
                before = batches.count
                page.eval(JS_SCROLL)
                end = time.time() + 10
                while time.time() < end and batches.count == before:
                    time.sleep(0.4)
                    take()
                    if batches.count == before:
                        page.eval(JS_SCROLL)  # the feed renders in small steps; keep nudging
                if batches.count == before:
                    stale += 1
                    if stale >= 2:
                        reason = "no-growth"
                        break
                else:
                    stale = 0
                    log(f"  batch {batches.count}: {len(seen)} places")
            else:
                if stop():
                    reason = "stopped"
                elif time.time() - started >= max_seconds:
                    reason = "timeout"
            take()
            links = page.eval(JS_LINKS) or []
        except CDPError as e:
            if not seen:
                raise
            return list(seen.values()), f"error: {e}", {"batches": batches.count, "dom_links": 0}
        if not seen:  # the batch format changed under us: fall back to what the page shows
            for a in links:
                p = parse_link(a["href"], a["label"])
                seen.setdefault(p["data_id"] or p["link"], p)
            reason = reason + "+dom-fallback"
        return list(seen.values()), reason, {"batches": batches.count, "dom_links": len(links)}
    finally:
        batches.close()
