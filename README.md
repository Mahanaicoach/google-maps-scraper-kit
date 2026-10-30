# Google Maps Scraper Kit

<p align="center">
  <img src="assets/banner.svg" alt="Google Maps Scraper Kit: free local Google Maps scraper for lead generation, driven by Claude Code" width="100%">
</p>

<p align="center">
  <a href="https://github.com/Mahanaicoach/google-maps-scraper-kit/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/Mahanaicoach/google-maps-scraper-kit?style=flat-square&color=f59e0b"></a>
  <a href="https://github.com/Mahanaicoach/google-maps-scraper-kit/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/Mahanaicoach/google-maps-scraper-kit?style=flat-square&color=0b1220"></a>
  <a href="https://github.com/Mahanaicoach/google-maps-scraper-kit/commits/master"><img alt="Last commit" src="https://img.shields.io/github/last-commit/Mahanaicoach/google-maps-scraper-kit?style=flat-square&color=334155"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-0b1220?style=flat-square"></a>
  <img alt="Runs 100% locally" src="https://img.shields.io/badge/runs-100%25%20local-059669?style=flat-square">
  <img alt="No API keys" src="https://img.shields.io/badge/API%20keys-none-334155?style=flat-square">
</p>

<p align="center"><sub>Last verified working: 2026-10-30 with engine v1.18.1 on macOS Apple Silicon.</sub></p>

**A free, open-source Google Maps scraper for lead generation that runs on your own machine.**
Name a business type and a city. Get back an outreach-ready CSV with name, phone, email, website,
category, address, rating and review count. Add `--socials` for Instagram, Facebook and LinkedIn.

No Google Maps API key, no SaaS credits, no data leaving your computer. One Docker command starts the
engine. Then run it from the terminal, or let Claude Code, Codex or Cursor drive it for you in plain
English.

| Goal | Start here |
|---|---|
| Get a lead list in the next 5 minutes | [Quick start](#quick-start) |
| Let an AI coding agent run the scrape | [Use it with Claude Code or any agent](#use-it-with-claude-code-or-any-agent) |
| Scrape many keywords or cities in one job | [Batch jobs](#batch-jobs-and-the-python-cli) |
| Know what you get and where it stops | [What you get](#what-you-get) and [Honest limits](#honest-limits) |
| Compare it with Apify, Outscraper and chat AIs | [How it compares](#how-it-compares) |
| Follow a worked recipe for dentists, roofers or restaurants | [Guides and recipes](docs/README.md) |

## Sponsored by

<sub>Paid placements: these companies pay to appear here, and their links carry referral tracking.</sub>

### [Swiftproxy](https://www.swiftproxy.net/?ref=googlemapsscraperkit) - Residential proxies for Google Maps scraping

<a href="https://www.swiftproxy.net/?ref=googlemapsscraperkit"><img src="assets/sponsors/swiftproxy.webp" alt="Swiftproxy: outreach-ready residential IPs for Google Maps lead generation" width="600"></a>

90M+ residential IPs across 220+ locations, rotating or static, over HTTP(S) and SOCKS5, so big
Google Maps and multi-city jobs keep running. Use code `PROXY90` for 10% off.
[**Try Swiftproxy →**](https://www.swiftproxy.net/?ref=googlemapsscraperkit)

---

### [SerpApi](https://serpapi.com/maps-scraper-kit-github) - Google Maps API in JSON and Markdown

<a href="https://serpapi.com/maps-scraper-kit-github"><img src="assets/sponsors/serpapi.png" alt="SerpApi: complete Maps API, available in JSON and Markdown" width="600"></a>

Access Google Maps listings, reviews, directions, and other public search data with SerpApi's Google
Maps API, delivered as clean, structured JSON and Markdown.
[**Try SerpApi →**](https://serpapi.com/maps-scraper-kit-github)

<p align="center"><sub><a href="https://github.com/sponsors/Mahanaicoach">Become a sponsor</a></sub></p>

---

## Quick start

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop) and Python 3. The scripts
use the standard library only, so there is nothing to `pip install`.

```bash
git clone https://github.com/Mahanaicoach/google-maps-scraper-kit.git
cd google-maps-scraper-kit
docker compose up -d
./scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5
```

No coordinates handy? Let the Python CLI geocode the city:

```bash
python3 scripts/scrape.py "dentists in Denver CO" --city "Denver, CO" --depth 5 --socials
```

A search at `--depth 5` usually returns 20 to 40 places. A higher `--depth` scrolls further, up to
Google's cap of about 120 per search. Need the whole city? Add `--grid 3`:

```bash
python3 scripts/scrape.py "dentists in Denver CO" --city "Denver, CO" --grid 3
```

### Example output

`results-coffee_shops_in_Austin_TX.csv` from the first command above (`scrape.py` names its file
`results-<job id>.csv` unless you pass `--out`). Illustrative rows:

| title | phone | emails | website | category | address | review_rating | review_count |
|---|---|---|---|---|---|---|---|
| Example Roasters | +1 512-555-0101 | hello@example.com | example.com | Coffee shop | 100 Congress Ave, Austin, TX | 4.7 | 812 |
| Sample Espresso Bar | +1 512-555-0102 | info@example.org | example.org | Cafe | 200 S Lamar Blvd, Austin, TX | 4.5 | 356 |

`--socials` adds `instagram`, `facebook` and `linkedin` columns. `--tech` adds `fb_pixel`,
`google_ads_tag`, `analytics`, `cms`, `mobile_friendly` and `intent_score`, and sorts the hottest
leads first. `--full` keeps all 36 raw fields, including opening hours, coordinates, price range and
reviews.

Full walkthrough, platform notes and troubleshooting: [SETUP.md](SETUP.md).

---

## Use it with Claude Code or any agent

The kit ships a [Claude Code skill](.claude/skills/google-maps-scraper/SKILL.md) that teaches the
agent the exact job API, the required fields, polling, CSV cleanup and the rate-limit guardrails.
It also works with Codex, Cursor and any other [Agent Skills](https://agentskills.io)-compatible tool.

**Option A: open the folder in Claude Code.** The skill and four slash commands load automatically.

```bash
cd google-maps-scraper-kit
claude
```

**Option B: install the skill into an agent you already use.**

```bash
npx skills add mahanaicoach/google-maps-scraper-kit
```

This installs the skill file only. The agent starts the engine with one `docker run` and calls its
API directly. The scripts' extras (`--socials`, `--tech`, `--grid`, geocoding, the email crawl and the
8-column cleanup) and the slash commands come with Option A.

Then ask in plain language:

> Scrape gyms in Miami and give me phones, emails and websites.

The agent checks the engine is up and starts it if not, builds the job, polls in the background,
downloads the CSV, keeps the 8 lead fields, and hands you a clean table plus the file.

| Slash command (Claude Code) | Action |
|---|---|
| `/scrape <business> in <city, ST> [depth]` | Run one scrape, return a clean table and CSV |
| `/scrape-batch <keywords-file> [--city "City, ST"]` | Run many queries as one job |
| `/scrape-setup` | Start the engine and run a health check |
| `/scrape-jobs [list \| delete <id>]` | List or delete jobs |

Plain requests trigger the skill too: *"build me a lead list of plumbers in Phoenix"*. The skill
scrapes Google Maps business listings only. It does not scrape social platforms directly.

---

## What you get

| | |
|---|---|
| **8 lead fields by default** | name, phone, email, website, category, address, rating, review count. The engine captures 36 raw fields; `--full` keeps them all. |
| **Email extraction, on by default** | After the Maps job, the script crawls every business website, 16 at a time: the homepage, then its contact and about pages if the homepage shows no email. It prints your found rate. |
| **Beyond the 120 cap** | `--grid 3` splits the city into 3 x 3 map tiles, runs them one at a time and dedupes on `place_id`. |
| **Social enrichment** | `--socials` finds Instagram, Facebook and LinkedIn profiles in code (HTTP + regex). Zero AI tokens. |
| **Who already advertises** | `--tech` spots a Meta Pixel, Google Ads tag and analytics on each website, plus its platform (WordPress, Shopify, Wix…) and mobile support, then ranks leads by an `intent_score` from 0 to 100. Zero AI tokens. |
| **Auto-geocoding** | Type a city name. Coordinates are resolved for you via OpenStreetMap. |
| **Batch jobs** | Many keywords, one job, one CSV. The engine lists a place found by several keywords once. |
| **Proxy rotation** | Built into the engine for large or repeated runs. `socks5`, `socks5h`, `http`, `https`. |
| **Agent-native** | A skill plus four slash commands. The agent handles create, poll, download, clean and present. |
| **Safe defaults** | API bound to `127.0.0.1`, and the proxy refuses requests from other websites in your browser (DNS rebinding, cross-site posts). Results git-ignored; website fetches skip private addresses; rate-limit and data-law guardrails built into the skill. |

### Batch jobs and the Python CLI

`scripts/scrape.py` is a standalone, dependency-free Python CLI: single queries, batches, geocoding,
socials and CSV output. It works on macOS, Linux and Windows.

```bash
python3 scripts/scrape.py --keywords-file examples/queries.example.txt --city "Denver, CO"
```

One keyword per line. One job. One CSV.

---

## Honest limits

- **About 120 listings per search.** Google Maps stops scrolling at roughly 120 results per search.
  That is Google's cap, not the kit's. `--grid` is the fix: it splits the city into N x N map tiles,
  runs one search per tile in a single batch, and dedupes the results on `place_id`. Tiles run one at a
  time, so a grid is slower and more likely to trip rate limits than a single search. A first search
  at depth 5 usually returns 20 to 40.
- **Emails only when a business publishes one.** The script reads the business homepage, then its
  contact and about pages. Listings with no site, or a site with no visible
  email, return an empty `emails` cell. Instagram bios are not read. Expect emails on a fraction of
  rows, not all of them.
- **Rate limits are real.** Heavy use without proxies can get your IP temporarily rate-limited by
  Google for minutes to hours. Your Google account is not affected. See [Responsible use](#responsible-use).
- **The web UI is the engine's own page.** It downloads the raw 36-column CSV and uses the engine's
  slower email option, whose `emails` column keeps junk such as `sentry.wixpress.com` and image
  names. For a clean outreach list, use the scripts. The page also loads its scripts,
  fonts and map tiles from public CDNs and OpenStreetMap, so it needs internet. Scraped data stays on
  your machine.
- **Apple Silicon runs natively from v1.18.0 on.** The image publishes arm64 builds, so M-series
  Macs need no emulation. Older tags are amd64 only and run under emulation.

---

## How it compares

| | This kit | Asking a chat AI | Apify, Outscraper and other cloud scrapers | Copying by hand |
|---|---|---|---|---|
| Cost | Free (MIT) | Free | Per-result credits or subscription | Free |
| API key or account | None | Account | Required | None |
| Where data goes | Stays on your machine | Provider's servers | Provider's servers | Your machine |
| Rows per query | Whole city with --grid, deduped | Close to zero | Up to ~120 per keyword, unlimited keywords | A handful |
| Emails and socials | Built in | No | Often a paid add-on | Manual |
| Output | Clean CSV, 8 lead fields | Chat text | CSV or JSON | Spreadsheet |
| Driven by an AI agent | Yes, skill included | n/a | No | No |

Ask a chat AI to "scrape Google Maps" and it hits a consent wall and JavaScript-rendered pages, so
you get close to zero structured rows. This kit runs a real headless scraping engine locally and gives
the agent a precise operating manual for it.

---

## Guides and recipes

One page per job, with the exact commands. Index: [docs/README.md](docs/README.md).

- [Google Maps email scraper: how the kit finds business emails](docs/google-maps-email-scraper.md)
- [Google Maps scraper in Python: every CLI flag](docs/google-maps-scraper-python.md)
- [Google Maps scraper without an API key](docs/google-maps-scraper-no-api-key.md)
- [Scrape Google Maps leads with Claude Code](docs/scrape-google-maps-leads-with-claude-code.md)
- Recipes: [dentists](docs/recipes/dentist-leads.md), [roofing contractors](docs/recipes/roofing-contractor-leads.md), [restaurants](docs/recipes/restaurant-leads.md)

---

## FAQ

**Is this Google Maps scraper free?**
Yes. The kit is MIT licensed and the engine it runs is open source. There are no credits, subscriptions
or API keys. You only need Docker on your own machine.

**Do I need a Google Maps API key or the Google Places API?**
No. The kit drives a headless browser against Google Maps directly, so there is no API quota or billing.

**How is this different from gosom/google-maps-scraper?**
[gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) is the scraping engine, and
this kit runs its official Docker image. The kit adds the lead-focused layer on top: a one-command
local setup, a dependency-free Python CLI with geocoding and batching, email and social enrichment to a
clean 8-column CSV, and a Claude Code skill with slash commands and safety guardrails. See
[CREDITS.md](CREDITS.md).

**Do I need Claude Code to use it?**
No. Claude Code makes it hands-free through a skill and slash commands, but `scripts/scrape.py` and
`scripts/scrape.sh` run on their own from any terminal.

**Does it work with Codex, Cursor or other agents?**
Yes. `npx skills add mahanaicoach/google-maps-scraper-kit` installs the skill into any Agent
Skills-compatible tool. The `/scrape` slash commands are Claude Code only.

**Why does `npx skills add` show "High Risk" or "Med Risk"?**
skills.sh runs automated audits on every skill. On Oct 14, 2026 they flagged that this skill runs shell
commands (curl and Python against `localhost:8080`) and reads untrusted web content. Both are what a
local scraper does. Gen Agent Trust Hub's own summary found no malicious patterns and called its
remote-code alert a false positive, because it targets localhost. The skill now treats scraped data
as untrusted, and a re-audit is requested in
[vercel-labs/skills#2624](https://github.com/vercel-labs/skills/issues/2624).

**What data do I get for each business?**
Name, phone, email, website, category, address, rating and review count by default. Add `--socials`
for Instagram, Facebook and LinkedIn, or `--full` for all 36 raw fields.

**How many leads can I scrape?**
Up to about 120 per search. Add `--grid 3` to split the city into 9 map tiles and get past that cap,
with duplicates removed. Batch as many keywords as you like in one job. For large or repeated runs,
add proxies (see [Responsible use](#responsible-use)).

**Does it work on Windows, Linux and Apple Silicon Macs?**
Yes. Anywhere Docker runs. On Windows, use `scripts/scrape.py`, or run `scrape.sh` under WSL2 or Git
Bash. [SETUP.md](SETUP.md) covers the Apple Silicon note.

**Is scraping Google Maps legal?**
It is against Google's Terms of Service, and phones and emails are personal data. Read
[Responsible use](#responsible-use) and follow the law where the data is used: GDPR and CCPA
abroad, and LGPD in Brazil. This is not legal advice.

---

## Responsible use

This drives a real scraper against Google Maps. Light use is fine. Heavy use without proxies can get
your IP **temporarily rate-limited** (minutes to hours). Your Google account is not affected.

- Run one job at a time. Start at `depth 5` and raise only when needed.
- Watch for block signals: `failed` jobs, empty results, or far fewer rows than an identical earlier run.
- Add proxies for large or repeated runs. The engine rotates them automatically. Sponsor
  [Swiftproxy](https://www.swiftproxy.net/?ref=googlemapsscraperkit) takes 10% off with code `PROXY90`:
  ```json
  "proxies": ["socks5://user:pass@host:port", "http://host2:port2"]
  ```
- Treat output as leads to verify, not a dataset to resell. Phones and emails are personal data:
  follow GDPR, CCPA, CAN-SPAM and, in Brazil, LGPD — including the lawful basis, the purpose and an
  opt-out before any outreach.

For large requests, the agent flags the risk and suggests proxies, then proceeds. It refuses only
clearly abusive use.

---

## Featured in

- [Best Google Maps Scrapers on GitHub in 2026 (Tested)](https://scrap.io/google-maps-scraper-github-open-source-guide) by Scrap.io

## Star history

If this kit saved you an afternoon of copy-pasting, a star helps the next person find it.

<a href="https://www.star-history.com/#Mahanaicoach/google-maps-scraper-kit&Date">
  <img src="https://api.star-history.com/svg?repos=Mahanaicoach/google-maps-scraper-kit&type=Date" alt="Star history chart for Mahanaicoach/google-maps-scraper-kit" width="600">
</a>

<details>
<summary><strong>Project layout</strong></summary>

```
google-maps-scraper-kit/
├── docker-compose.yml      local engine on 127.0.0.1:8080
├── ui-branding.conf        nginx proxy: web UI links to this kit and its sponsors
├── .env.example            config template
├── scripts/
│   ├── scrape.py           Python CLI: single, batch, geocoding, socials, CSV
│   └── scrape.sh           bash CLI: single query
├── examples/               sample job body and batch keyword list
├── .claude/
│   ├── settings.json       pre-approved local commands for hands-free runs
│   ├── commands/           /scrape, /scrape-batch, /scrape-setup, /scrape-jobs
│   └── skills/google-maps-scraper/SKILL.md
├── CLAUDE.md               project instructions for Claude Code
├── CREDITS.md              scraping engine attribution and license
└── SETUP.md                full setup guide
```

</details>

## Updating

Pull the latest fixes with one command. It works in a clone of this repo or of your fork, and it
keeps your own commits:

```bash
bash scripts/update.sh
```

If the engine is running, the script restarts it so a new engine version takes effect.
Forked it on GitHub? Run `git push` afterwards, or click **Sync fork** on your fork's
page.

On Windows without Git Bash, run the same update by hand:

```bash
git pull https://github.com/Mahanaicoach/google-maps-scraper-kit.git master
docker compose up -d
```

## Support

Found a bug? [Open an issue](https://github.com/Mahanaicoach/google-maps-scraper-kit/issues/new/choose) with
the command you ran and the error. Issues are for problems with the kit. Run your own scrapes with the
quick start above; requests like "find me hotels in Hamburg" are closed without action.
Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

Built by [Mahan](https://github.com/Mahanaicoach). MIT licensed. Third-party components: [CREDITS.md](CREDITS.md).
