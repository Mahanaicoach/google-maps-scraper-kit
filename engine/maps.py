"""Google Maps search in a tab: open the search, pass the consent page, scroll the result feed,
and collect the places from the result batches the page itself downloads. Standard library only.
"""
import json, queue, re, time, urllib.parse

import details
from cdp import CDPError

# Google's consent interstitial (EU and some other regions) is skipped when these cookies exist.
CONSENT_COOKIES = {"CONSENT": "YES+cb.20210720-07-p0.en+FX+410",
                   "SOCS": "CAISHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiAo_CmBg"}
# The feed's last line once the list ends. The element's class has been HlvSq for years; the
# texts back it up in case Google renames the class (en, fr and es checked live on 2026-10-31).
END_CLASS = "HlvSq"
END_MARKERS = ("You've reached the end of the list", "reached the end",
               "Vous êtes arrivé à la fin de la liste", "Has llegado al final de la lista",
               "Du hast das Ende der Liste erreicht", "Hai raggiunto la fine dell'elenco",
               "Você chegou ao fim da lista", "Chegou ao fim da lista", "Je hebt het einde van de lijst bereikt",
               "Listenin sonuna ulaştınız", "Dotarłeś do końca listy", "Du har nått slutet av listan")
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
JS_END = f"""(() => {{
  const f = document.querySelector('div[role="feed"]');
  return f ? {{cls: !!f.querySelector('.{END_CLASS}'), text: f.innerText}} : null;
}})()"""
# Google's consent page in any language: its "Reject all" form posts set_eom=true to
# consent.google.com/save (the "Accept all" form posts false). The button texts are the fallback.
# Not seen live from the kit's test machines (Google skips it outside the EEA, and the cookies
# above usually skip it inside), so both ways stay.
CONSENT_WORDS = ("reject all", "accept all", "alle ablehnen", "alle akzeptieren", "tout refuser", "tout accepter",
                 "rechazar todo", "aceptar todo", "rifiuta tutto", "accetta tutto", "rejeitar tudo", "aceitar tudo",
                 "alles afwijzen", "alles accepteren", "tümünü reddet", "tümünü kabul et", "odrzuć wszystko",
                 "zaakceptuj wszystko", "avvisa alla", "godkänn alla", "afvis alle", "accepter alle")
JS_CONSENT = """(() => {
  const forms = [...document.querySelectorAll('form[action*="consent"]')];
  const reject = forms.find(f => f.querySelector('input[name="set_eom"][value="true"]'));
  let b = reject && reject.querySelector('button, input[type="submit"]');
  const words = %s;
  if (!b) b = [...document.querySelectorAll('button')].find(x => words.some(w => x.innerText.toLowerCase().includes(w)));
  if (b) { b.click(); return true; } return false;
})()""" % json.dumps(CONSENT_WORDS, ensure_ascii=False)


def search_url(query, lat=None, lon=None, zoom=15, lang="en", country=""):
    """The Maps search URL: hl is the interface language (category names, opening-hours text), gl
    the country Google ranks and formats results for (without it, the country of this IP)."""
    q = urllib.parse.quote(query, safe="")
    at = f"/@{lat},{lon},{zoom}z" if lat is not None and lon is not None else ""
    return f"https://www.google.com/maps/search/{q}{at}?hl={lang}" + (f"&gl={country}" if country else "")


def ended(feed):
    """True when the feed shows its end-of-list line (JS_END's answer)."""
    if not isinstance(feed, dict):
        return False
    return bool(feed.get("cls")) or any(m in (feed.get("text") or "") for m in END_MARKERS)


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
        self.limited = 0    # batch requests Google answered with 429 Too Many Requests

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
            if not BATCH_URL.match(r.get("url", "")):
                continue
            if r.get("status") == 429:
                self.limited += 1
            elif "json" in (r.get("mimeType") or ""):
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


def collect(page, url, depth=1, max_seconds=180, log=print, stop=lambda: False, on_batch=None):
    """Open `url`, scroll the result feed, and return (places, stop reason, stats).
    `depth` is the number of result batches to read (about 20 places each, like the old engine's
    depth); the list may end sooner. Places are de-duplicated on data_id. `stop()` returning True
    ends the scroll early (a deleted job). on_batch(new places, batches read) is called as the
    batches arrive, so a caller can save them before the keyword ends. Reason "blocked" means
    Google answered with its unusual-traffic page, or 429 to the result batches."""
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
            new = []
            for row in batches.drain():
                key = row.get("data_id") or row.get("place_id") or row["title"]
                if key not in seen:
                    seen[key] = row
                    new.append(row)
            if new and on_batch:
                on_batch(new, batches.count)
            return len(new)

        try:  # a Chrome error mid-scroll keeps the places already read
            end = time.time() + 10  # the first batch arrives with the page
            while time.time() < end and batches.count == 0 and not batches.limited:
                take()
                time.sleep(0.4)
            log(f"  batch {batches.count}: {len(seen)} places")
            while batches.count < depth and time.time() - started < max_seconds and not stop():
                if ended(page.eval(JS_END)):
                    reason = "end-of-list"
                    break
                if batches.limited or "/sorry/" in (page.url() or ""):
                    reason = "blocked"
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
                    if batches.limited:  # Google refused the next batch: slow down, do not keep asking
                        reason = "blocked"
                        break
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
                elif batches.count == 0 and batches.limited:
                    reason = "blocked"
            take()
            links = page.eval(JS_LINKS) or []
        except CDPError as e:
            if not seen:
                raise
            return list(seen.values()), f"error: {e}", {"batches": batches.count, "dom_links": 0}
        if not seen and reason != "blocked":  # the batch format changed under us: fall back to the page
            for a in links:
                p = parse_link(a["href"], a["label"])
                seen.setdefault(p["data_id"] or p["link"], p)
            reason = reason + "+dom-fallback"
        return list(seen.values()), reason, {"batches": batches.count, "dom_links": len(links)}
    finally:
        batches.close()


PLACE_URL = re.compile(r"^https://www\.google\.[a-z.]+/maps/preview/place\?")


def place_panel(page, url, timeout=10):
    """Open one place's panel and return the body of the `/maps/preview/place` response it downloads
    (details.parse_place reads it); "blocked" for Google's unusual-traffic page, "" when none came."""
    q = page.cdp.subscribe("Network.responseReceived", page.sid)
    try:
        page.navigate(url)
        if "/sorry/" in (page.url() or ""):
            return "blocked"
        end, rid = time.time() + timeout, None
        while time.time() < end:
            while rid is None:
                try:
                    p = q.get_nowait()
                except queue.Empty:
                    break
                if not isinstance(p, dict):
                    raise CDPError(f"connection lost: {page.cdp.closed}")
                r = p.get("response", {})
                if PLACE_URL.match(r.get("url", "")) and "json" in (r.get("mimeType") or ""):
                    rid = p["requestId"]
            if rid:
                try:
                    return page.send("Network.getResponseBody", {"requestId": rid})["body"]
                except CDPError:  # still downloading
                    if page.cdp.closed:
                        raise
            time.sleep(0.3)
        return ""
    finally:
        page.cdp.unsubscribe("Network.responseReceived", q, page.sid)
