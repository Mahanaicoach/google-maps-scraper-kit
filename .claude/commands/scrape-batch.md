---
description: Scrape many Google Maps keywords from a file in one job and return one lead CSV.
argument-hint: <keywords-file> [--city "City, ST"]
---
The user wants a **batch** Google Maps scrape. Input: **$ARGUMENTS** (a keywords file, and optionally a city).

Use the `google-maps-scraper` skill. The API scrapes **many keywords in ONE job** through the `keywords`
array, so a batch is one job with all the terms.

1. Check the engine is up with `python3 scripts/engine.py status`. It prints `✓ Running on http://localhost:<port>` only when the kit's own engine answers; use that URL as the base (the API examples show 8080). If it is not running, it names the browser the engine will use: with one, run `python3 scripts/engine.py start`; with none, ask the user first, or use `docker compose up -d` if they run Docker. With no `scripts/` folder, use `curl -s http://localhost:8080/api/v1/jobs`.
2. Read the keyword list (one per line; ignore blank and `#` lines). If a `--city` was given, geocode it once
   for `lat`/`lon`. Otherwise expect the location baked into each keyword, and geocode the first one.
3. **One notice:** a batch with many keywords is high-volume. Print ONE short notice that it can get the IP
   temporarily rate-limited and suggest proxies for large or repeated runs, then run it as the user asked.
4. Run `python3 scripts/scrape.py --keywords-file <file> --city "<City, ST>"` as a background Bash command
   (`run_in_background: true`): one job with the full `keywords` array, emails and socials crawled by the
   script. Many keywords take longer, so add `--max-time 900` or more. Read its output file when notified.
5. Present the 8 lead fields (plus the social columns, which come by default) in a clean table and save the full file to disk.

Tip: `scripts/scrape.py --keywords-file <file> --city "<City, ST>"` already does all of this, with emails on by
default, and social profiles too. Append `--no-socials` only if the user says they do not want them.
