---
description: Scrape many Google Maps keywords from a file in one job and return one lead CSV.
argument-hint: <keywords-file> [--city "City, ST"]
---
The user wants a **batch** Google Maps scrape. Input: **$ARGUMENTS** (a keywords file, and optionally a city).

Use the `google-maps-scraper` skill. The API scrapes **many keywords in ONE job** through the `keywords`
array, so a batch is one job with all the terms.

1. Check the engine is up with `python3 scripts/engine.py status`. It prints `✓ Running on http://localhost:<port>` only when the kit's own engine answers; use that URL as the base (the API examples show 8080). If it is not running, it names the browser the engine will use: with one, run `python3 scripts/engine.py start`; with none, ask the user first, or use `docker compose up -d` if they run Docker. With no `scripts/` folder, ask the user to install the kit.
2. Check the keyword list reads as one search per line (blank and `#` lines are ignored). With `--city` the
   script geocodes the city once; without it, each keyword must name its place.
3. **One notice:** a batch with many keywords is high-volume. Print ONE short notice that it can get the IP
   temporarily rate-limited and suggest proxies for large or repeated runs; the user's request is the
   authorisation to run it.
4. Run `python3 scripts/scrape.py --keywords-file <file> --city "<City, ST>"` as a background Bash command
   (`run_in_background: true`): one job with the full `keywords` array, emails and socials crawled by the
   script. Many keywords take longer, so add `--max-time 900` or more. Read its output file when notified.
5. Present the 8 lead fields (plus the social columns, which come by default) in a clean table and give the
   path of the file the script wrote. Append `--no-socials` only if the user says they do not want socials.
