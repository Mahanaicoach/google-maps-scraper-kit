# Best free Google Maps scrapers in 2026, compared

_Last updated 2026-10-30. Every competitor fact below comes from the vendor's own page, checked that day._

The best free Google Maps scraper depends on one question: will you install anything? If you can run
one Docker command, Google Maps Scraper Kit is free, runs on your machine, needs no API key and returns
emails. If you want nothing to install, a cloud scraper such as Apify or Outscraper is easier and
charges per result. If you need data inside Google's terms, only the official Places API qualifies.

_Disclosure: this page is written by the maintainer of Google Maps Scraper Kit. Where another tool is
the better pick, it says so._

## How do the options compare?

| | Google Maps Scraper Kit | gosom/google-maps-scraper | Apify Google Maps Scraper | Outscraper | Instant Data Scraper | Google Places API |
|---|---|---|---|---|---|---|
| Price | Free (MIT) | Free (MIT) | From $1.50 per 1,000 places; $4.00 on the free plan, which includes $5 of usage a month | First 500 places free every 30 days, then $3 per 1,000 records | Free | Text Search with phone and website: $35 per 1,000 requests, 1,000 free a month |
| Setup | Docker plus Python 3 | Docker, CLI or web UI | Cloud, no install | Cloud, no install | Chrome extension | Cloud API, key and billing account |
| Emails | Built in, on by default | Built in, `-email` flag, off by default | Paid add-on: $2.00 per 1,000 places on the free plan | Paid add-on: $3 per 1,000 domains | No dedicated feature | No email field |
| Results per search | About 120 (Google's list limit); whole city with `--grid` | About 120 (Google's list limit) | Whole area with a location input | Not stated | What the page shows | 20 per page, 60 at most |
| Where data is processed | Your machine | Your machine | Apify cloud | Outscraper servers | Your browser | Google cloud |
| AI agent support | Claude Code skill and slash commands; Codex and Cursor via `npx skills add` | REST API | REST API and a hosted MCP server | API on paid plans; an MCP server on its GitHub org | None | REST API |

## Which one should I pick?

- **Google Maps Scraper Kit**: pick it when you want a free, clean lead CSV with emails and socials,
  and you are fine running Docker. In a measured run, 20 Denver dentists with emails took 48 s
  ([PR #31](https://github.com/Mahanaicoach/google-maps-scraper-kit/pull/31)). It is the only option
  here that ships an Agent Skill for Claude Code. [Quick start](../README.md#quick-start).
- **gosom/google-maps-scraper**: pick the raw engine when you build your own pipeline in Go or want
  all of its output formats. The kit runs this engine and adds the lead layer on top.
- **Apify Google Maps Scraper**: pick it when you want no install, scheduled runs or large jobs
  without using your own IP. You pay per place, and contact enrichment costs extra.
- **Outscraper**: pick it for an occasional small pull with no install. The 500 free places every
  30 days cover a test or two.
- **Instant Data Scraper**: pick it for a one-off copy of a visible result list. It needs no setup and
  finds no emails.
- **Google Places API**: pick it when the data goes into an app and must stay within Google's terms.
  It returns no emails and at most 60 results per search.

## Is scraping Google Maps allowed?

Scraping Google Maps is against Google's Terms of Service, whichever scraper runs it. The Places API
is the only option above that Google permits. Phones and emails are personal data, so GDPR, CCPA and
CAN-SPAM apply to outreach. This is not legal advice. See
[Responsible use](../README.md#responsible-use).

## Sources

Checked 2026-10-30.

- Apify: [actor page](https://apify.com/compass/crawler-google-places), [pricing](https://apify.com/pricing), [MCP](https://docs.apify.com/platform/integrations/mcp)
- Outscraper: [pricing](https://outscraper.com/pricing/), [Google Maps scraper](https://outscraper.com/google-maps-scraper/), [MCP server](https://github.com/outscraper/outscraper-mcp-server)
- Google: [Places API pricing](https://developers.google.com/maps/billing-and-pricing/pricing), [Text Search](https://developers.google.com/maps/documentation/places/web-service/text-search), [data fields](https://developers.google.com/maps/documentation/places/web-service/data-fields)
- Instant Data Scraper: [Chrome Web Store listing](https://chromewebstore.google.com/detail/instant-data-scraper/ofaokhiedipichpaobibbnahnkdoiiah)
- gosom/google-maps-scraper: [README](https://github.com/gosom/google-maps-scraper), latest release v1.18.1 (2026-09-20)

## Related

- [Google Maps scraper without an API key](google-maps-scraper-no-api-key.md)
- [Google Maps email scraper](google-maps-email-scraper.md)
- [Scrape Google Maps leads with Claude Code](scrape-google-maps-leads-with-claude-code.md)
