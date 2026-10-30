# Google Maps scraper in Python: the kit's dependency-free CLI

_Last updated 2026-10-30. Tested with engine v1.18.1._

`scripts/scrape.py` is a free Google Maps scraper Python CLI that turns a keyword and a place into a lead CSV, using only the standard library.
It drives the engine running locally in Docker, so there is no API key, no pip install and no data leaving your machine.

## How do I run it?

Start the engine, which is the gosom/google-maps-scraper official Docker image, then call the script:

```bash
docker compose up -d
python3 scripts/scrape.py "gyms in Miami FL" 25.7617 -80.1918 --depth 5
python3 scripts/scrape.py "cafes in Austin TX" --city "Austin, TX" --depth 5
python3 scripts/scrape.py --keywords-file examples/queries.example.txt --city "Denver, CO"
```

On Windows, run the same commands with `py` or `python3`. Python is only the client. The engine always runs in Docker.

## Every flag

| Flag | What it does |
|---|---|
| `keyword` | The search, with the place baked in, e.g. `"coffee shops in Austin TX"`. |
| `lat`, `lon` | Coordinates. Optional when you pass `--city`. |
| `--city` | Geocodes a city name to coordinates via OpenStreetMap. |
| `--depth` | How far to scroll each search. Default `5`. Higher is slower and more likely to be blocked. |
| `--no-email` | Skips email extraction for a faster run. Emails are on by default. |
| `--no-deep-emails` | Checks homepages only for emails, not contact and about pages. |
| `--grid N` | Splits the `--city` area into N x N map tiles, one job each, deduped. Needs `--city`. Max 10. |
| `--max-time` | Job time limit in seconds. Default `600`. |
| `--out` | Output path. Default `results-<id>.csv`. A `.json` extension writes JSON. |
| `--json` | Writes JSON instead of CSV. |
| `--full` | Keeps all 36 raw columns instead of the 8 lead fields. |
| `--fields` | Comma-separated columns to keep, overriding the lead fields. |
| `--socials` | Adds Instagram, Facebook and LinkedIn from each website. Slower. Zero AI tokens. |
| `--tech` | Adds `fb_pixel`, `google_ads_tag`, `analytics` (yes/no, blank if the site did not load), `cms`, `mobile_friendly` and a 0-100 `intent_score`, and sorts by that score. Shares the homepage fetch with `--socials`. Zero AI tokens. |
| `--keyword` | Adds an extra keyword to the same job. Repeatable. |
| `--lang` | Two-letter language the engine asks Google for, e.g. `pt`, `es`, `de`. Default `en`. Use the market's language so category names come back in it. |
| `--keywords-file` | Reads one keyword per line from a file. One job, one CSV. |

The script creates the job, polls until it finishes, downloads the CSV and keeps the 8 lead fields. It prints a short preview and saves the full results to a file.

## Example output

Illustrative rows only:

| title | phone | emails | website | category | review_rating |
|---|---|---|---|---|---|
| Example Fitness Club | +1 305-555-0101 | hello@example.com | example.com | Gym | 4.6 |
| Sample Strength Studio | +1 305-555-0102 | info@example.org | example.org | Gym | 4.8 |
| Demo Boxing Gym | +1 305-555-0103 |  | example.net | Boxing gym | 4.4 |

## What to expect

- Google Maps returns about 120 listings per search. Use `--grid 3` to cover a whole city past that cap.
- Grid tiles run one at a time, so a grid is slower and more likely to trip rate limits than one search.
- Emails appear only when a business publishes one on its website. Many rows will have an empty `emails` cell.
- Heavy use without proxies can get your IP temporarily rate-limited by Google. Your Google account is not affected.
- Geocoding uses the free OpenStreetMap Nominatim service, which allows about 1 request per second.
- Scraping Google Maps is against Google's Terms of Service, so read [Responsible use](../README.md#responsible-use) first.

## Related

- [Google Maps email scraper](google-maps-email-scraper.md)
- [Google Maps scraper without an API key](google-maps-scraper-no-api-key.md)
- [Kit README](../README.md)
