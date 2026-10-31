# Google Maps scraper without an API key

The Google Maps Scraper Kit is a free, local Google Maps scraper that needs no API key, no Places API and no Google Cloud account.
It drives a headless browser against the public Google Maps site and returns a CSV of business leads, with no quota and no billing.

## Why no API key is needed

The kit never calls the Google Maps Platform or the Places API. So there is no key to create and no API quota or billing to manage.

Instead, the engine opens Google Maps the way a browser does. The engine is the kit's own, in `engine/`: Python and a headless Chrome on your own machine. It searches, scrolls the result list and reads the listings Google Maps sends the page. The kit then trims the output to the 8 lead fields: name, phone, email, website, category, address, rating and review count. Review counts stay empty by default, because Google shows a signed-out headless browser none; window mode (`SCRAPER_HEADLESS=0`, see [SETUP §3](../SETUP.md#review-counts-hours-and-reviews-window-mode)) fills them.

City names are turned into coordinates by OpenStreetMap Nominatim, which is free and also needs no key.

## Run it

```bash
git clone https://github.com/Mahanaicoach/google-maps-scraper-kit.git
cd google-maps-scraper-kit
python3 scripts/engine.py start
python3 scripts/scrape.py "plumbers in Phoenix AZ" --city "Phoenix, AZ" --depth 5
```

You need Python 3.9 or newer (standard library only) and Google Chrome, Chromium, Edge or Brave. With no browser installed, `engine.py start` downloads Chrome for Testing once, about 100 MB, pinned by SHA-256. Prefer Docker? `docker compose up -d` runs the same engine in a container instead of the `engine.py` line. The engine API listens on `127.0.0.1` only, so it is reachable only from your machine.

## Example output

Illustrative rows only:

| title | phone | emails | website | address | review_rating |
|---|---|---|---|---|---|
| Example Plumbing Co | +1 602-555-0101 | service@example.com | example.com | 100 Main St, Phoenix, AZ | 4.8 |
| Sample Drain Pros | +1 602-555-0102 | info@example.org | example.org | 200 Central Ave, Phoenix, AZ | 4.5 |
| Demo Pipe Repair | +1 602-555-0103 |  | example.net | 300 Van Buren St, Phoenix, AZ | 4.2 |

## What no API key means for cost

- No quota. There is no daily request allowance to run out of.
- No billing. There are no credits, subscriptions or per-result charges. The kit and its engine are MIT licensed.
- No account. You do not sign in to Google or to any SaaS to run a scrape.
- No data leaving your machine. Results are saved as local files and are git-ignored.

## The trade-off

Skipping the API also skips its guarantees. These are the limits you accept instead.

- Google can rate-limit you. Heavy use without proxies can get your IP temporarily rate-limited for minutes to hours. Your Google account is not affected.
- Add proxies for large or repeated runs: `--proxy` takes `http`, `https`, `socks5` or `socks5h` URLs, with `user:pass@` on `http` and `https` only. Repeat it and the keywords take turns; a keyword Google refuses is retried through the next proxy.
- Google Maps stops at about 120 listings per search. Add `--grid 3` to split the city into 9 map tiles, with duplicates removed.
- Emails appear only when a business publishes one on its website.
- Scraping Google Maps is against Google's Terms of Service, so read [Responsible use](../README.md#responsible-use) before you run large jobs.

## Related

- [Google Maps scraper in Python](google-maps-scraper-python.md)
- [Scrape Google Maps leads with Claude Code](scrape-google-maps-leads-with-claude-code.md)
- [Kit README](../README.md)
