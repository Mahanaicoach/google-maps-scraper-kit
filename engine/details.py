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
import json, math, urllib.parse

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
    "place_id": (78,),
    "data_id": (10,),
    "open_state": (203, 1, 4, 0),
    "thumbnail": (72, 0, 0, 6, 0),
    "timezone": (30,),
    "price_range": (4, 2),
    "description": (32, 1, 1),
}
HOURS = (203, 0)  # [[day name, 1-7 (Monday-Sunday), date, [[text, ...], ...], ...], ...], from today on
# The same layouts the old engine read from the place page, checked on live batches (2026-10-31):
# address parts 20/20 entities; the about list 20/20, but only its accessibility group, in batches and
# in the place panel alike (payments, service options... load with the place's About tab, which the
# engine does not open, so credit_cards_accepted is filled only if Google sends them); reservations 16/20
# restaurants; order links 2/20 coffee shops, 1/20 restaurants; menu 0/40. Index 183 has 2 items in
# batches, so the plus code is computed instead (plus_code).
ADDRESS = (183, 1)  # [borough, street, ?, city, postal code, state, country]
ADDRESS_PARTS = {"borough": 0, "street": 1, "city": 3, "postal_code": 4, "state": 5, "country": 6}
ABOUT = (100, 1)  # [[group id, group name, [[?, option name, [?, [[1 if it applies, ...]], ...]], ...]], ...]
RESERVATIONS = (46,)  # [[link, source, ...], ...]
# (75, 0): the listing's action groups, [type, ?, items, ...]. Type 1 is "Reserve a table" (the same
# links as RESERVATIONS), 3 the order links of delivery and ordering sites, 4 Google's own order flow
# (no items). The old engine also read the first group, which lists reservations as order links.
ACTIONS, ORDER_GROUP = (75, 0), 3
ORDER_ITEM = ((1, 2, 0), (0, 0))  # link, source of each [[source, ...], [?, ?, [link]], ...] item
MENU = (38,)  # [link, source]
# What the reviews pass takes from a place's panel for a row that lacks it (a DOM-fallback row).
PANEL_FIELDS = ("review_count", "reviews_link", "open_hours", "complete_address", "about", "credit_cards_accepted",
                "reservations", "order_online", "menu")
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


def as_list(node, path):
    """dig(node, path) when it is a list, else []: something to loop over whatever Google sent."""
    v = dig(node, path)
    return v if isinstance(v, list) else []


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
    groups, cards = about(entity)
    orders = next((dig(g, (2,)) for g in as_list(entity, ACTIONS) if dig(g, (0,)) == ORDER_GROUP), None)
    found = {"complete_address": complete_address(entity), "about": groups, "credit_cards_accepted": cards,
             "reservations": link_sources(dig(entity, RESERVATIONS), (0,), (1,)),
             "order_online": link_sources(orders, *ORDER_ITEM), "menu": menu(entity)}
    out.update({k: v for k, v in found.items() if v})
    if isinstance(out.get("data_id"), str) and ":" in out["data_id"]:
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
        spans = [_text(x[0]) for x in as_list(d, (3,)) if isinstance(x, list) and x and _text(x[0])]
        week[d[1]] = (d[0], spans)
    if len(week) < 7:
        return ""
    return json.dumps({name: spans for _, (name, spans) in sorted(week.items())}, ensure_ascii=False)


IMAGES_MAX, IMAGE_URL_MAX = 20, 1000  # keeps the cell far below csv's 128 KB field limit


def images(entity):
    """Photo URLs already supplied by Maps, as the existing images column's JSON objects: plain https
    links (no login, no whitespace), at most IMAGES_MAX of them."""
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
            if not plain_url(url) or url in seen:  # plain_url first: a list here is not hashable
                continue
            seen.add(url)
            title = _text(dig(item, title_path)) if title_path else ""
            found.append({"title": title, "image": url})
            if len(found) == IMAGES_MAX:
                return json.dumps(found, ensure_ascii=False)
    return json.dumps(found, ensure_ascii=False) if found else ""


def plain_url(url, schemes=("https",)):
    """True for a plain link a spreadsheet or browser can open as is: no login, no whitespace or
    control characters, at most IMAGE_URL_MAX characters."""
    if not isinstance(url, str) or len(url) > IMAGE_URL_MAX or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        p = urllib.parse.urlsplit(url)
        return p.scheme in schemes and bool(p.hostname) and not (p.username or p.password)
    except ValueError:
        return False


def complete_address(entity):
    """The address in parts, as the old engine's JSON ({"borough": ..., "street": ..., "city": ...,
    "postal_code": ..., "state": ..., "country": ...}), or "" when Google sent none."""
    parts = dig(entity, ADDRESS)
    if not isinstance(parts, list):
        return ""
    out = {k: _text(parts[i]) if i < len(parts) else "" for k, i in ADDRESS_PARTS.items()}
    return json.dumps(out, ensure_ascii=False) if any(out.values()) else ""


def about(entity):
    """(about, credit_cards_accepted): the listing's attribute groups as the old engine's JSON
    ([{"id", "name", "options": [{"name", "enabled", "values"?}]}]), and the cards named in its
    payments group, joined with ", ". "" for each when absent."""
    groups, cards = [], []
    for g in as_list(entity, ABOUT):
        if not isinstance(g, list):
            continue
        gid, opts = _text(dig(g, (0,))), []
        for o in as_list(g, (2,)):
            name = _text(dig(o, (1,)))
            if not name:
                continue
            values = [v for v in (_text(dig(x, (2,))) or _text(dig(x, (3,))) for x in as_list(o, (2, 4, 1, 0, 0))) if v]
            on = dig(o, (2, 1, 0, 0)) == 1
            same = next((x for x in opts if x["name"] == name), None)
            if same:  # one option listed twice: it applies if either says so
                same["enabled"] = same["enabled"] or on
                values = [v for v in values if v not in same.get("values", [])]
                if values:
                    same["values"] = same.get("values", []) + values
            else:
                opts.append({"name": name, "enabled": on, **({"values": values} if values else {})})
            if gid == "payments" and name == "Credit cards":
                cards += [v for v in values if v not in cards]
        if gid or opts:
            groups.append({"id": gid, "name": _text(dig(g, (1,))), "options": opts})
    return (json.dumps(groups, ensure_ascii=False) if groups else ""), ", ".join(cards)


def link_sources(nodes, link, source):
    """[{"link", "source"}, ...] as JSON (reservations, order_online): http(s) links only, "" when none."""
    out = []
    for it in nodes if isinstance(nodes, list) else []:
        url, src = dig(it, link), _text(dig(it, source))
        if src and plain_url(url, ("https", "http")) and all(x["link"] != url for x in out):
            out.append({"link": url, "source": src})
    return json.dumps(out, ensure_ascii=False) if out else ""


def menu(entity):
    """{"link", "source"} as JSON, or "" when the listing links no menu."""
    url, src = dig(entity, MENU + (0,)), _text(dig(entity, MENU + (1,)))
    return json.dumps({"link": url, "source": src}, ensure_ascii=False) if plain_url(url, ("https", "http")) else ""


OLC = "23456789CFGHJMPQRVWX"  # Open Location Code (Plus Code) digits


def plus_code(lat, lon):
    """The 10-digit global Plus Code of a point (Open Location Code, a cell about 14 x 14 m), or ""
    when lat/lon are not numbers. Google's search batches carry no plus code, so it is computed."""
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return ""
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return ""
    y = min(math.floor(round((min(max(lat, -90.0), 90.0) + 90) * 8000, 6)), 180 * 8000 - 1)  # 1/8000° cells
    x = math.floor(round(((lon + 180) % 360) * 8000, 6)) % (360 * 8000)
    code = ""
    for _ in range(5):  # base 20, least significant pair first
        code = OLC[y % 20] + OLC[x % 20] + code
        y, x = y // 20, x // 20
    return code[:8] + "+" + code[8:]


def parse_place(body, limit=5):
    """review_count, reviews_per_rating, up to `limit` user_reviews and the PANEL_FIELDS from one
    `/maps/preview/place` response. Reviewer names and photos are left out: the kit collects business
    data, not people."""
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[-1]
    try:
        entity = dig(json.loads(body), (6,))
    except ValueError:
        return {}
    if not isinstance(entity, list):
        return {}
    out = {k: v for k, v in parse_entity(entity).items() if k in PANEL_FIELDS}
    hist = dig(entity, HISTOGRAM)
    if isinstance(hist, list) and len(hist) == 5 and all(isinstance(n, int) for n in hist):
        out["reviews_per_rating"] = json.dumps({str(i): n for i, n in enumerate(hist, 1)})
    reviews = []
    for r in as_list(entity, REVIEWS):
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
            try:
                row = parse_entity(entity)
            except (TypeError, ValueError, IndexError, AttributeError):  # one odd entity, not the whole batch
                continue
            if row.get("title"):
                out.append(row)
    return out
