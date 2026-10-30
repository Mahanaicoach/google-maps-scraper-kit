# Google Maps email scraper: how the kit finds business emails

_Last updated 2026-10-30. Tested with engine v1.18.1._

The Google Maps Scraper Kit is a free Google Maps email scraper that runs on your own machine with no API key.
Name a business type and a city, and you get a CSV of local businesses with their published contact emails next to phone, website and address.

## How does it find emails?

Emails are on by default. Google Maps listings carry no email field, so once the Maps job is done the script crawls each business website itself, 16 sites at a time:

1. It fetches the homepage and reads any email there.
2. If the homepage shows none, it fetches up to 3 contact and about pages on the same site.

The engine's own email option is left off: it visits every site in a browser, took most of each job's time, and found fewer emails than this crawl on the same listings.

The crawl reads `mailto:` links and plain text on those pages. It drops junk such as image file names, placeholder addresses and website-builder domains. It sorts addresses on the business's own domain first and keeps at most 3 per row. It runs in plain Python code, so it costs zero AI tokens.

When the crawl ends, the script prints one found-rate line: how many businesses got an email, and their share of all rows.

## How do I run it?

Start the engine once, then run a search with `--city` so you can skip coordinates:

```bash
docker compose up -d
python3 scripts/scrape.py "dentists in Denver CO" --city "Denver, CO" --depth 5
```

Check homepages only for a quicker run, or skip emails entirely for the fastest run:

```bash
python3 scripts/scrape.py "dentists in Denver CO" --city "Denver, CO" --no-deep-emails
python3 scripts/scrape.py "dentists in Denver CO" --city "Denver, CO" --no-email
```

The CSV lands in `results-<id>.csv` unless you pass `--out`.

## Example output

Illustrative rows only:

| title | phone | emails | website | category |
|---|---|---|---|---|
| Example Dental Studio | +1 303-555-0101 | hello@example.com | example.com | Dentist |
| Sample Family Dentistry | +1 303-555-0102 | front@example.org, info@example.org | example.org | Dentist |
| Demo Smile Center | +1 303-555-0103 |  | example.net | Dentist |

The empty `emails` cell on the last row is normal. That site published no email the kit could read.

## What to expect

- Expect emails on a fraction of rows, not all of them. A business with no website, or a site with no visible email, returns an empty cell.
- Instagram bios are not read. Add `--socials` to get Instagram, Facebook, LinkedIn, TikTok, YouTube and X profile links from each website instead.
- Add `--verify-emails` to drop addresses whose domain does not exist and to see who hosts each domain's mail (`email_host`: Google Workspace, Microsoft 365, other). Add `--has-email` to keep only rows with an address.
- Email extraction visits every website, so a run with emails is slower than one with `--no-email`.
- Google Maps returns about 120 listings per search. Add `--grid 3` to split the city into 9 map tiles and get past that cap, with duplicates removed.
- Heavy use without proxies can get your IP temporarily rate-limited by Google for minutes to hours. Run one job at a time and add proxies for large or repeated runs.
- Scraping Google Maps is against Google's Terms of Service and emails are personal data, so read [Responsible use](../README.md#responsible-use) before you send anything.

## Related

- [Scrape Google Maps leads with Claude Code](scrape-google-maps-leads-with-claude-code.md)
- [Google Maps scraper in Python](google-maps-scraper-python.md)
- [Kit README](../README.md)
