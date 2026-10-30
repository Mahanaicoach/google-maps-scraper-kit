---
description: Scrape many Google Maps keywords from a file in one job and return one lead CSV.
argument-hint: <keywords-file> [--city "City, ST"]
---
The user wants a **batch** Google Maps scrape. Input: **$ARGUMENTS** (a keywords file, and optionally a city).

Use the `google-maps-scraper` skill. The API scrapes **many keywords in ONE job** through the `keywords`
array, so a batch is one job with all the terms.

1. Check the engine is up (`curl -s http://localhost:8080/api/v1/jobs`; `docker compose up -d` if not).
2. Read the keyword list (one per line; ignore blank and `#` lines). If a `--city` was given, geocode it once
   for `lat`/`lon`. Otherwise expect the location baked into each keyword, and geocode the first one.
3. **Ask about social profiles first.** Skip only if the user already said yes or no: *"Also grab social profiles
   (Instagram / Facebook / LinkedIn) for these? Emails are already included. (yes / no)"*. If yes, add `--socials`.
4. **Warn, don't block:** a batch with many keywords is high-volume. Print ONE short warning that it can
   get the IP temporarily rate-limited and suggest proxies for large or repeated runs, then **proceed**.
5. Easiest: `python3 scripts/scrape.py --keywords-file <file> --city "<City, ST>"` (one job, emails crawled
   by the script). Raw API instead: POST a single job with the full `keywords` array, the required fields (`name`, `lang` `"en"`, `max_time` seconds, string `lat`/`lon`)
   and **`"email": true`** (emails are the key lead field). Use a generous `max_time` (e.g. `900`), because many
   keywords plus email extraction take longer. Poll in the background, then download.
6. Present the 8 lead fields in a clean table and save the full file to disk.

Tip: `scripts/scrape.py --keywords-file <file> --city "<City, ST>"` already does all of this, with emails on by
default. Append `--socials` if the user said yes in step 3.
