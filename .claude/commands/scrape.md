---
description: Scrape Google Maps business listings for one business type in one city and return the 8 lead fields as a table and CSV.
argument-hint: <business type> in <city, ST> [depth]
---
The user wants a Google Maps lead list. Query: **$ARGUMENTS**

Use the `google-maps-scraper` skill and follow these steps:

1. **Check the engine:** `curl -s http://localhost:8080/api/v1/jobs`. If it is down, run `python3 scripts/engine.py start` from the kit root (or `docker compose up -d` if the user runs the engine in Docker), and retry.
2. **Parse** the business type and city from the query. Look up the city's **latitude/longitude** (as strings).
3. **Ask about social profiles before scraping, every time.** Ask the user ONE short question:
   *"Want me to also grab their social profiles (Instagram / Facebook / LinkedIn / TikTok / YouTube / X)? Emails are already included by default. (yes / no)"*
   Wait for the answer. **Skip the question only if** the user already said yes or no to socials in the request.
   If **yes**, add `--socials` in step 4. If **no**, leave it off. Socials cost no LLM tokens and one extra HTTP fetch per site (see the skill).
4. **Run the scrape with emails on.** Easiest and fastest: the script (below), which keeps the engine's homepage-only email option off and crawls each business website itself, contact and about pages included. Only on the raw API, set `"email": true` in the body. Required fields: `name` (any label), `keywords` (location baked into each term), `lang` (`"en"`), `lat`/`lon` (strings), `max_time` (seconds, default `300`); plus `depth` (the number in the query, else `5`; each unit is about 20 places). Easiest path: `python3 scripts/scrape.py "<keyword>" --city "<City, ST>" --depth <n>` (emails are already on by default; append `--socials` if the user said yes in step 3).
5. **Poll to completion in the background.** The Bash tool blocks foreground `sleep`, so use `run_in_background: true` and read the output file when notified. Then download the CSV.
6. **Save a CSV file.** `scripts/scrape.py` writes `results-<id>.csv` by default. If you use the raw API, save the parsed lead fields to a `.csv` yourself. Then **present only the 8 lead fields** in a clean markdown table: `name (title), phone, emails, website, category, address, review_rating, review_count` (`review_count` is empty with this engine), plus `instagram, facebook, linkedin, tiktok, youtube, x` if socials were requested. **Drop everything else**, especially `latitude`/`longitude`, IDs, hours, images and review blobs. Never dump the full raw CSV (36 columns plus `categories`) into chat. Show a few sample rows and give the CSV path. Include dropped fields only if the user explicitly asks.

**All, every or entire city:** add `--grid 3` (3 x 3 map tiles, one job at a time, deduped) to get past Google's ~120 results per search. Warn once that it is slower and more likely to trip rate limits, then proceed.

**Emails:** on by default. The script crawls each business website after the Maps job, 16 at a time. Some businesses publish no email, so coverage is partial. For a deliberately fast run without emails, pass `--no-email`.

**Filters:** when the user wants only businesses without a website, a minimum rating or review count, or only rows with a phone or email, add `--no-website`, `--min-rating X`, `--min-reviews N` (ignored with a warning while `review_count` is empty), `--has-phone`, `--has-email`. `--exclude <earlier.csv>` drops places from a previous run; `--limit N` caps the rows. Several cities: repeat `--city`.

**Socials:** when the user says yes, `scripts/scrape.py … --socials` adds Instagram, Facebook and LinkedIn. Extraction costs **0 LLM tokens** because it runs in code. It adds ~40–50 tokens per business only if you load the rows into chat. **Never** WebFetch each website yourself to find socials; that costs thousands of tokens.

**Warn, don't block:** if the request is high-volume (high `depth`, many keywords or repeated runs), print ONE short warning that heavy use can get the user's IP temporarily rate-limited by Google, and suggest proxies. Then **proceed with the scrape**. Do not gate or demand confirmation for a big job. Refuse outright only for clearly abusive use (surveilling individuals, spam, harassment). If the user will store or contact the data, remind them once that it is personal data and that scraping is against Google's Terms of Service.
