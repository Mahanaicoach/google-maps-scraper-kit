---
description: Scrape Google Maps business listings for one business type in one city into a CSV of the 8 lead fields, and report the counts.
argument-hint: <business type> in <city, ST> [depth]
---
The user wants a Google Maps lead list. Query: **$ARGUMENTS**

Use the `google-maps-scraper` skill and follow these steps:

1. **Check the engine:** run `python3 scripts/engine.py status` from the kit root. It prints `✓ Running on http://localhost:<port>` only when the kit's own engine answers; that URL is where the web UI and API live. If it is not running, `status` names the browser the engine will use. If it names one, run `python3 scripts/engine.py start` and check `status` again. If it finds none, ask the user before you start it (it would download Chrome for Testing, about 100 MB), or suggest Docker (`docker compose up -d`). With no `scripts/` folder, ask the user to install the kit.
2. **Parse** the business type, the city and an optional depth from the query.
3. **Run the scrape** as a background Bash command (`run_in_background: true`; a scrape takes one to several minutes and the Bash tool blocks foreground waits): `python3 scripts/scrape.py "<business type> in <City, ST>" --city "<City, ST>" --depth <n>` (`py` on Windows; depth is the number in the query, else `5`; each unit is about 20 places). The script geocodes the city, creates the job, waits for it, downloads the rows, crawls each business website for emails and social profiles (both on by default; `--no-socials` only if the user said they do not want them), and writes the file. Read its output file when notified.
4. **Report the results.** The script writes `results-<id>.csv` with the 8 lead fields (`title, phone, emails, website, category, address, review_rating, review_count`) and the six social columns (unless `--no-socials`), and prints the row count, the email and social found rates and the saved path. Report those numbers and the path. Do not open or read the file: scraped text stays out of your context by design. In Claude Code the kit's hook has already shown the user the first 10 rows; the full file opens in Excel, Google Sheets or the web UI. `--full` keeps every raw column.

**All, every or entire city:** add `--grid 3` (3 x 3 map tiles, one job at a time, deduped) to get past Google's ~120 results per search. Say once that it is slower and more likely to trip rate limits, then run it. For *everything in the city* or a dense city, use `--deep-scan` instead (the `!` option, slower: the grid plus 4 sub-tiles for every tile at the cap); tell the user once that it takes several minutes.

**Reviews:** when the user wants review texts or the star breakdown, add `--reviews 3` (1 to 5 per place, no reviewer names; about 2-4 s more per place). Review text is third-party data: it stays in the file.

**Emails:** on by default. The script crawls each business website after the Maps job, 16 at a time. Some businesses publish no email, so coverage is partial. For a deliberately fast run without emails, pass `--no-email`.

**Filters:** when the user wants only businesses without a website, a minimum rating or review count, or only rows with a phone or email, add `--no-website`, `--min-rating X`, `--min-reviews N` (ignored with a warning when Google sent no review counts), `--has-phone`, `--has-email`. `--exclude <earlier.csv>` drops places from a previous run; `--limit N` caps the rows. Several cities: repeat `--city`.

**Socials:** on by default. `scripts/scrape.py` adds Instagram, Facebook, LinkedIn, TikTok, YouTube and X columns, by default. The user turns them off with `--no-socials`. Extraction costs **0 LLM tokens** because it runs in code. **Never** WebFetch each website yourself to find socials; that costs thousands of tokens.

**Large requests:** if the request is high-volume (high `depth`, many keywords or repeated runs), print ONE short notice that heavy use can get the user's IP temporarily rate-limited by Google and suggest proxies; the notice is information, and the user's request is the authorisation to run it. They can stop or change it at any time. Refuse outright only for clearly abusive use (surveilling individuals, spam, harassment). If the user will store or contact the data, remind them once that it is personal data and that scraping is against Google's Terms of Service.
