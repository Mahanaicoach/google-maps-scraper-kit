# Google Maps scraper in Python: the kit's dependency-free CLI

`scripts/scrape.py` is a free Google Maps scraper Python CLI that turns a keyword and a place into a lead CSV, using only the standard library.
It drives the engine running locally in Docker, so there is no API key, no pip install and no data leaving your machine.

## Run it

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
| `--city` | Geocodes a city name to coordinates via OpenStreetMap. Repeat it to run several cities, one after another, into one deduped file. |
| `--depth` | How far to scroll each search. Default `5`. Higher is slower and more likely to be blocked. |
| `--no-email` | Skips email extraction for a faster run. Emails are on by default. |
| `--no-deep-emails` | Checks homepages only for emails, not contact and about pages. |
| `--grid N` | Splits the `--city` area into N x N map tiles, one job each, deduped. Needs `--city`. Max 10. |
| `--no-split` | Keeps one job per search or tile. By default a search that returns 100 rows or more is re-run as 4 closer searches, up to 2 levels deep and 128 jobs per place. |
| `--max-time` | Job time limit in seconds. Default `600`. |
| `--out` | Output path. Default `results-<short job id>.csv`. A `.json` extension writes JSON. |
| `--json` | Writes JSON instead of CSV. |
| `--full` | Keeps all 36 raw columns instead of the 8 lead fields. |
| `--fields` | Comma-separated columns to keep, overriding the lead fields. |
| `--socials` | Adds Instagram, Facebook, LinkedIn, TikTok, YouTube and X from each website. Slower. Zero AI tokens. |
| `--tech` | Adds `fb_pixel`, `google_ads_tag`, `analytics`, `bing_ads`, `linkedin_insight`, `tiktok_pixel` (yes/no, blank if the site did not load), `cms`, `mobile_friendly` and a 0-100 `intent_score`, and sorts by that score. Shares the homepage fetch with `--socials`. Zero AI tokens. |
| `--keyword` | Adds an extra keyword to the same job. Repeatable. |
| `--lang` | Two-letter language the engine asks Google for, e.g. `pt`, `es`, `de`. Default `en`. Use the market's language so category names come back in it. |
| `--keywords-file` | Reads one keyword per line from a file. One job, one CSV. |
| `--proxy` | Proxy URL for the engine, repeatable: `http://`, `https://`, `socks5://` or `socks5h://`. This engine needs `user:pass@` for socks5. The engine rotates them; a wrong proxy fails the job and the script prints the engine's reason. |
| `--min-rating`, `--min-reviews` | Keep rows rated X or higher, or with N or more reviews. Unrated rows drop. |
| `--no-website` | Keep only businesses without a website: the classic web-design prospect list. |
| `--has-phone`, `--has-email` | Keep only rows with a phone, or rows where an email was found. |
| `--exclude FILE` | Drop places already in an earlier results file (`.csv` or `.json`), repeatable. Matches on `place_id`, `cid`, link, or name plus address, so the lean 8-column CSV works. |
| `--limit N` | Stop at N rows after filtering. |
| `--verify-emails` | Checks each email domain's MX record over DNS (dns.google, no key). Drops addresses on domains that do not exist and adds `email_host`: `google_workspace`, `microsoft_365`, `other` or `none` (blank when the row has no email). |

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
