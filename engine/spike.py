#!/usr/bin/env python3
"""Feasibility spike for the kit's own engine: one Google Maps search in a local headless Chrome,
driven over the DevTools protocol with the standard library; places come from the result batches
the page downloads while the list scrolls, so no place page is opened.

    python3 engine/spike.py "coffee shops in Austin TX" 30.2672 -97.7431 --depth 3

Prints counts and fill rates only, never names, phones or emails. Exit 0 when places came back.
"""
import argparse, collections, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import details, maps  # noqa: E402
from browser import Browser  # noqa: E402
from cdp import Page  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("query")
    ap.add_argument("lat", nargs="?", type=float)
    ap.add_argument("lon", nargs="?", type=float)
    ap.add_argument("--depth", type=int, default=3, help="result batches to read, about 20 places each (default 3)")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    ap.add_argument("--json", help="also write the counts to this file")
    a = ap.parse_args()

    t0 = time.time()
    url = maps.search_url(a.query, a.lat, a.lon, lang=a.lang)
    print(f"▶ {url}")
    with Browser(headless=not a.headful) as b:
        print(f"  browser: {b.version()} ({b.path})")
        page = Page(b.cdp)
        places, reason, stats = maps.collect(page, url, depth=a.depth)
        page.close()
    took = time.time() - t0
    fill = collections.Counter(k for p in places for k in p)
    print(f"✓ {len(places)} places in {took:.1f} s from {stats['batches']} batches "
          f"(stopped: {reason}; the page showed {stats['dom_links']} result links)")
    for k in list(details.FIELDS) + list(details.LISTS) + ["cid"]:
        print(f"    {k:14} {fill[k]:>3}/{len(places)}")
    summary = {"query_words": len(a.query.split()), "depth": a.depth, "places": len(places),
               "batches": stats["batches"], "dom_links": stats["dom_links"], "stop": reason,
               "seconds": round(took, 1), "filled": {k: fill[k] for k in fill}}
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1)
    sys.exit(0 if places else 1)


if __name__ == "__main__":
    main()
