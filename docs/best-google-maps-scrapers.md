# Best open-source Google Maps scrapers on GitHub (compared, November 2026)

*Last checked: 2026-11-01. Star counts and feature claims come from each project's own GitHub page and README on that date. The kit's numbers come from a fresh install of v2.1.0 on a MacBook with an installed Chrome, no Docker, no proxies, home internet.*

**Short answer:** if you want a free Google Maps scraper that gives you phones, emails and social profiles in one CSV, with nothing to install beyond Python and Chrome, the [Google Maps Scraper Kit](../README.md) is the only open-source option that does all of that by default. If you need PostgreSQL-backed distributed crawling or hundreds of reviews per place, [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) is the stronger pick. If you want a desktop app and are fine paying per enrichment, [omkarcloud](https://github.com/omkarcloud/google-maps-scraper) is the commercial option.

## The repos people actually find

| Repo | Stars | Language, licence | Needs | Emails | Social profiles | Reviews | Past Google's ~120 per search | Web UI and API | Agent support |
|---|---|---|---|---|---|---|---|---|---|
| [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) | 7,151 | Go, MIT | Docker, or a Go toolchain | `-email` flag, off by default | No | Yes, `-extra-reviews` up to about 300 | Manual `-grid-bbox` and `-grid-cell` | Yes, `-web` on :8080, OpenAPI | Skill for Claude Code, Codex, Cursor (needs Docker and Node) |
| [omkarcloud/google-maps-scraper](https://github.com/omkarcloud/google-maps-scraper) | 3,855 | Docs only, no scraper source in the repo | Their desktop app, account for paid features | Paid credits (0.2 credit per business) | Paid credits, 9 networks | Separate product | Not stated | 13-endpoint API in the app | No |
| **[Google Maps Scraper Kit](https://github.com/Mahanaicoach/google-maps-scraper-kit)** | 2,117 | Python standard library, MIT | Python 3.9+ and Chrome (or Docker) | **On by default**: homepage, contact and about pages, 16 sites at a time; `--verify-emails` MX check | **On by default**: Instagram, Facebook, LinkedIn, TikTok, YouTube, X | `--reviews 1-5` plus the star histogram | `--grid N`: auto bounding box, N×N tiles, deduped on `place_id` | Yes, form UI and REST API with OpenAPI, localhost only | Skill plus four slash commands; `npx skills add` |
| [zohaibbashir/Google-Maps-Scrapper](https://github.com/zohaibbashir/Google-Maps-Scrapper) | 1,609 | Python, MIT | pip, Playwright, visible browser | No | No | Count only | No | No | No |
| [gaspa93/googlemaps-scraper](https://github.com/gaspa93/googlemaps-scraper) | 530 | Python, GPL-3.0 | chromedriver | No | No | Reviews only | n/a | No | No |
| [conor-is-my-name/google-maps-scraper](https://github.com/conor-is-my-name/google-maps-scraper) | 329 | Python, MIT | Docker or uvicorn | No | No | Count only | No | REST `/scrape` | No |

Several small MCP servers (all under 10 stars on 2026-11-01) also scrape Google Maps; none of the repos above ships one.

## What only the kit does, out of the box

- **Enrichment without credits.** Emails, six social networks, an MX check of each email domain (`--verify-emails`, via dns.google, no key), and `--tech`: Meta Pixel, Google Ads tag, analytics, Bing, LinkedIn and TikTok pixels, the site platform (WordPress, Shopify, Wix, Squarespace, Webflow), mobile support and an `intent_score` from 0 to 100. All in code, zero AI tokens, zero paid credits.
- **No Docker, no Go, no pip.** `python3 scripts/engine.py start` runs the engine with the Chrome you already have. Docker stays optional.
- **Lead-list workflow flags.** `--no-website` (businesses that still need a site), `--exclude earlier.csv` (a weekly re-run returns only new leads), `--min-rating`, `--min-reviews`, `--has-phone`, `--has-email`, `--limit`, `--fields`.
- **Hardened local API.** Bound to 127.0.0.1, refuses foreign `Host` (421) and `Origin` (403) headers, 3 MB body cap, strict validation of every job field, CSV formula guard, proxy passwords masked.

## What the others do better

- **gosom**: PostgreSQL job queue for many machines (`-dsn`), S3 and AWS Lambda runners, up to about 300 reviews per place, Go plugin writers, and a community five times larger with a longer track record.
- **omkarcloud**: about 55 fields including popular times, owner posts, "is spending on ads", gas prices and more social networks, plus phone-carrier and email-verification add-ons, if you pay for credits.
- **Kit gaps**: `popular_times`, `owner`, `price_range` and `street_view_url` stay empty (Google does not send them to any scraper we tested); the MCP server does not exist yet; proxies with a login work on HTTP(S) only.

## Measured on 2026-11-01 (kit v2.1.0, fresh install)

| Run | Result | Wall time |
|---|---|---|
| Quick start: coffee shops in Austin, depth 2, emails and socials on | 40 places, 11 emails, 16 with a social profile | 21 s |
| Dentists in Denver, `--city`, depth 5, emails and socials on | 100 places, 98 phones, 42 emails, 63 with a social profile | 44 s |
| Restaurants in Manhattan, depth 20, no crawl | 119 places (Google's cap) | 34 s |
| Restaurants in Manhattan, `--grid 3`, depth 6 | 471 unique places, 9 tiles | 5 min 21 s |
| Restaurants in Salt Lake City, `--grid 2`, depth 5 | 225 unique places, 4 tiles | 87 s |
| Roofers in Phoenix, `--tech --verify-emails --min-rating 4 --has-phone` | 59 places, 38 emails, pixels found on 11 sites, sorted by intent | 29 s |
| Lisbon cafés, `--lang pt --country pt --reviews 3 --full` | 40 places, 3 reviews and a star histogram for each, hours, photos | 37 s |
| Whole session | 2,068 places in 27 jobs from one home IP | 21 min, zero rate-limit pages |

gosom's README estimates "~120 places/minute (with -c 8 -depth 1)" for its engine without email crawling; we did not re-run gosom for this page. No independent benchmark of open-source Google Maps scrapers exists as of this date (AIMultiple, Scrapingdog and Scrap.io compare paid APIs or quote vendors).

## Not open source, but often compared

- **Apify, Outscraper, Scrap.io, SerpApi, HasData**: cloud services billed per result or per request; no install, data leaves your machine, and reviews, images and popular times are usually included. Right when you need thousands of places per day with support, or an official API contract.
- **Google Places API**: the sanctioned route. Billing per request, no emails, no social links, and the terms forbid storing most of it.
- **Instant Data Scraper and browser extensions**: free, click-driven, one page at a time, no emails, no grid.

## Which to pick

- Lead lists with contact data for outreach, on your own machine, free: **the kit**.
- Massive distributed crawls into PostgreSQL, or hundreds of reviews per place: **gosom**.
- A desktop app and you accept paid credits for enrichment: **omkarcloud**.
- Scraping reviews only: **gaspa93**.
- Zero setup and a budget: **Apify or Outscraper**.

Scraping Google Maps is against Google's Terms of Service, and phones and emails are personal data under GDPR, CCPA and LGPD. Every tool on this page carries that risk; read the kit's [Responsible use](../README.md#responsible-use) before any outreach.
