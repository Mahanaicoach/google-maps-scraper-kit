---
description: Scrape many Google Maps keywords from a file in one job and return one lead CSV.
argument-hint: <keywords-file> [--city "City, ST"]
---
The user wants a **batch** Google Maps scrape. Input: **$ARGUMENTS** (a keywords file, and optionally a city).

Use the `google-maps-scraper` skill. The API scrapes **many keywords in ONE job** through the `keywords`
array, so a batch is one job with all the terms.

1. Check the engine is up (`curl -s http://localhost:8080/api/v1/jobs`; if not, `python3 scripts/engine.py status` names the browser the engine will use: with one, run `python3 scripts/engine.py start`; with none, ask the user first, or use `docker compose up -d` if they run Docker).
2. Read the keyword list (one per line; ignore blank and `#` lines). If a `--city` was given, geocode it once
   for `lat`/`lon`. Otherwise expect the location baked into each keyword, and geocode the first one.
3. **Warn, don't block:** a batch with many keywords is high-volume. Print ONE short warning that it can
   get the IP temporarily rate-limited and suggest proxies for large or repeated runs, then **proceed**.
4. Easiest: `python3 scripts/scrape.py --keywords-file <file> --city "<City, ST>"` (one job, emails crawled
   by the script). Raw API instead: POST a single job with the full `keywords` array, the required fields (`name`, `lang` `"en"`, `max_time` seconds), string `lat`/`lon` (send both, or neither:
   with neither Google places each search by the place in its keyword), **`"email": true`** (emails are the key
   lead field) and **`"socials": true`** (opt-in on the raw API). Use a generous `max_time` (e.g. `900`), because many
   keywords plus email extraction take longer. Poll in the background, then download.
5. Present the 8 lead fields (plus the social columns, which come by default) in a clean table and save the full file to disk.

Tip: `scripts/scrape.py --keywords-file <file> --city "<City, ST>"` already does all of this, with emails on by
default, and social profiles too. Append `--no-socials` only if the user says they do not want them.
