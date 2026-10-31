"""Place fields from Google Maps' own result data.

While the result list scrolls, the page fetches batches of about 20 places from
`https://www.google.com/search?tbm=map…` as `)]}'\\n[...]` JSON; each batch carries the full place
entity (name, address, phone, website, rating, coordinates, ids…). The engine captures those
responses in the browser (cdp Network events) and parses them here, so no place page is ever
opened: the spike showed the place page itself, fetched by curl or by Chrome, holds no entity data
any more. Standard library only.
"""
import json

# Index path of each field inside a place entity. Checked on live batches in the spike
# (2026-10-31): title, category, address, rating, lat/lon, place_id, data_id, timezone, thumbnail
# filled on 60/60 entities, website 55/60, phone 46/60 (listings without one).
# review_count is the one unverified path: (37, 1) is an integer on every entity in the range a
# review count has (23 to 1998 on 40 coffee shops), but signed-out Google Maps shows no counts in
# the list or the place panel, so it could not be compared against the page. Verify it in P1
# against a few listings whose counts are known (a signed-in browser shows them).
FIELDS = {
    "title": (11,),
    "category": (13, 0),
    "address": (18,),
    "website": (7, 0),
    "phone": (178, 0, 0),
    "review_rating": (4, 7),
    "review_count": (37, 1),
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
    if "data_id" in out and ":" in out["data_id"]:
        try:
            out["cid"] = str(int(out["data_id"].split(":")[1], 16))
        except ValueError:
            pass
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
