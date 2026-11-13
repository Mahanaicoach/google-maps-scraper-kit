#!/usr/bin/env python3
"""Answer questions about a saved results file in code, so an agent never reads the scraped rows.

  python3 scripts/leads.py results-abc.csv                              # counts only
  python3 scripts/leads.py results-abc.csv --no-website --min-rating 4.5 # a new filtered file
  python3 scripts/leads.py results-abc.csv --sort reviews --limit 10 --open

It prints numbers and the saved path, never a scraped value. In Claude Code the kit's preview hook
then shows the user the rows of the new file."""
import argparse, csv, importlib.util, json, os, subprocess, sys

_spec = importlib.util.spec_from_file_location("scrape", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scrape.py"))
scrape = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scrape)
SORT = {"rating": "review_rating", "reviews": "review_count"}


def load(path):
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def counts(rows):
    n = len(rows)
    rated = [scrape._num(r.get("review_rating")) for r in rows if scrape._num(r.get("review_rating"))]
    line = (f"  {n} rows · phone {sum(1 for r in rows if r.get('phone'))} · "
            f"email {sum(1 for r in rows if r.get('emails'))} · "
            f"website {sum(1 for r in rows if r.get('website'))} · "
            f"no website {sum(1 for r in rows if not r.get('website'))} · "
            f"rated 4.5+ {sum(1 for x in rated if x >= 4.5)}")
    return line + (f" · average rating {sum(rated) / len(rated):.1f}" if rated else "")


def open_file(path):
    if sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    elif os.name == "nt":
        os.startfile(path)  # noqa: the default app for .csv (Excel) on Windows
    else:
        subprocess.run(["xdg-open", path], check=False)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Counts, filters and sorting for a saved results file.")
    ap.add_argument("file")
    ap.add_argument("--min-rating", type=float, metavar="X")
    ap.add_argument("--min-reviews", type=int, metavar="N")
    ap.add_argument("--no-website", action="store_true")
    ap.add_argument("--has-phone", action="store_true")
    ap.add_argument("--has-email", action="store_true")
    ap.add_argument("--sort", choices=SORT, help="highest first")
    ap.add_argument("--limit", type=int, metavar="N")
    ap.add_argument("--out", help="output path (default: <file>-filtered.csv)")
    ap.add_argument("--open", action="store_true", help="open the file in the default app (Excel, Numbers…)")
    a = ap.parse_args(argv)

    rows = load(a.file)
    print(f"▶ {os.path.basename(a.file)}\n{counts(rows)}")
    if not (a.min_rating or a.min_reviews or a.no_website or a.has_phone or a.has_email or a.sort or a.limit):
        if a.open:
            open_file(a.file)
        return
    kept = [r for r in rows if scrape.keep_row(r, a) and (not a.has_email or r.get("emails"))]
    if a.sort:
        kept.sort(key=lambda r: scrape._num(r.get(SORT[a.sort])), reverse=True)
    kept = kept[:a.limit or None]
    out = a.out or os.path.splitext(a.file)[0] + "-filtered.csv"
    fields = list(rows[0].keys()) if rows else scrape.LEAD
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows([{k: scrape.csv_safe(r.get(k, "")) for k in fields} for r in kept])
    print(f"  kept {len(kept)} of {len(rows)}\n{counts(kept)}")
    print(f"  saved → {os.path.abspath(out)}  (opens in Excel or Google Sheets)")
    if a.open:
        open_file(out)


if __name__ == "__main__":
    main()
