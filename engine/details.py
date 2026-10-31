"""Place fields from Google Maps' own result data.

While the result list scrolls, the page fetches batches of about 20 places from
`https://www.google.com/search?tbm=map…` as `)]}'\\n[...]` JSON; each batch carries the full place
entity (name, address, phone, website, rating, coordinates, ids…). The engine captures those
responses in the browser (cdp Network events) and parses them here, so no place page is opened for
them, and business photo URLs come with them too. Review texts are the one thing the batches lack:
they come from the `/maps/preview/place` response a place's panel downloads (parse_place), only for
jobs that ask for them.
Standard library only.
"""
import json, urllib.parse

# Index path of each field inside a place entity. Checked on live batches in the spike
# (2026-10-31): title, category, address, rating, lat/lon, place_id, data_id, timezone, thumbnail
# filled on 60/60 entities, website 55/60, phone 46/60 (listings without one).
# review_count (4, 8), reviews_link (4, 3, 0) and the week of opening hours come only with Google's
# full view: a browser whose user agent says HeadlessChrome gets its "limited view", which leaves them
# empty and lists today's hours only (browser.py sends Chrome/ instead, see there). Full view
# (2026-10-31): count on 20/20 entities, 7 days of hours on 19/20. (37, 1) is the photo count.
FIELDS = {
    "title": (11,),
    "category": (13, 0),
    "address": (18,),
    "website": (7, 0),
    "phone": (178, 0, 0),
    "review_rating": (4, 7),
    "review_count": (4, 8),
    "reviews_link": (4, 3, 0),
    "latitude": (9, 2),
    "longitude": (9, 3),
    "plus_code": (183, 2, 2, 0),
    "place_id": (78,),
    "data_id": (10,),
    "open_state": (203, 1, 4, 0),
    "thumbnail": (72, 0, 0, 6, 0),
    "timezone": (30,),
    "price_range": (4, 2),
    "description": (32, 1, 1),
}
HOURS = (203, 0)  # [[day name, 1-7 (Monday-Sunday), date, [[text, ...], ...], ...], ...], from today on
HISTOGRAM = (175, 3)  # place panel: [one-star count, ..., five-star count]
REVIEWS = (175, 9, 0, 0)  # place panel: the reviews it shows first (about 5)
LISTS = {"categories": (13,), "address_lines": (2,)}
ENTRIES = 64  # position of the result list inside a batch


def dig(node, path):
    for i in path:
        if not isinstance(node, list) or i >= len(node) or node[i] is None:
            return None
        node = node[i]
    return node


def parse_entity(entity):
    """{field: value} for every field that resolves to a string or number; lists joined with ' | '."""
    out = {}
    for name, path in FIELDS.items():
        v = dig(entity, path)
        if isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != "":
            out[name] = v
    for name, path in LISTS.items():
        v = dig(entity, path)
        if isinstance(v, list):
            items = [x for x in v if isinstance(x, str) and x]
            if items:
                out[name] = " | ".join(items)
    hours = open_hours(dig(entity, HOURS))
    if hours:
        out["open_hours"] = hours
    photos = images(entity)
    if photos:
        out["images"] = photos
    if "data_id" in out and ":" in out["data_id"]:
        try:
            out["cid"] = str(int(out["data_id"].split(":")[1], 16))
        except ValueError:
            pass
    return out


def _text(v):
    return v.replace("\u202f", " ").strip() if isinstance(v, str) else ""


def open_hours(days):
    """The week's hours as old-engine JSON ({"Monday": ["7 AM–9 PM"], ...}, Monday first), or "" when
    Google sent fewer than 7 days (the limited view sends today only, which would read as a week)."""
    if not isinstance(days, list):
        return ""
    week = {}
    for d in days:
        if not (isinstance(d, list) and len(d) > 3 and isinstance(d[0], str) and isinstance(d[1], int)):
            continue
        spans = [_text(x[0]) for x in d[3] or [] if isinstance(x, list) and x and _text(x[0])]
        week[d[1]] = (d[0], spans)
    if len(week) < 7:
        return ""
    return json.dumps({name: spans for _, (name, spans) in sorted(week.items())}, ensure_ascii=False)


def images(entity):
    """Photo URLs already supplied by Maps, as the existing images column's JSON objects."""
    found, seen = [], set()
    # Search cards and panels use different gallery layouts. Never include reviewer profiles.
    groups = [(dig(entity, (171, 0)), (3, 0, 6, 0), (2,)),
              (dig(entity, (37, 0)), (6, 0), ()),
              (dig(entity, (72, 0)), (6, 0), ())]
    for items, path, title_path in groups:
        if not isinstance(items, list):
            continue
        for item in items:
            url = dig(item, path)
            if not isinstance(url, str) or url in seen:
                continue
            try:
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname:
                    continue
            except ValueError:
                continue
            seen.add(url)
            title = _text(dig(item, title_path)) if title_path else ""
            found.append({"title": title, "image": url})
    return json.dumps(found, ensure_ascii=False) if found else ""


def parse_place(body, limit=5):
    """review_count, reviews_per_rating and up to `limit` user_reviews from one `/maps/preview/place`
    response. Reviewer names and photos are left out: the kit collects business data, not people."""
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[-1]
    try:
        entity = dig(json.loads(body), (6,))
    except ValueError:
        return {}
    if not isinstance(entity, list):
        return {}
    out = {k: v for k, v in parse_entity(entity).items() if k in ("review_count", "reviews_link", "open_hours")}
    hist = dig(entity, HISTOGRAM)
    if isinstance(hist, list) and len(hist) == 5 and all(isinstance(n, int) for n in hist):
        out["reviews_per_rating"] = json.dumps({str(i): n for i, n in enumerate(hist, 1)})
    reviews = []
    for r in dig(entity, REVIEWS) or []:
        rating, text = dig(r, (0, 2, 0, 0)), _text(dig(r, (0, 2, 15, 0, 0)))
        if isinstance(rating, int) and not isinstance(rating, bool):
            reviews.append({"Rating": rating, "When": _text(dig(r, (0, 1, 6))), "Description": text})
        if len(reviews) >= limit:
            break
    if reviews:
        out["user_reviews"] = json.dumps(reviews, ensure_ascii=False)
    return out


def parse_batch(body):
    """The places in one `/search?tbm=map` response body (with or without the `)]}'` prefix)."""
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[-1]
    try:
        data = json.loads(body)
    except ValueError:
        return []
    entries = dig(data, (ENTRIES,))
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        entity = dig(e, (1,))
        if isinstance(entity, list):
            row = parse_entity(entity)
            if row.get("title"):
                out.append(row)
    return out
