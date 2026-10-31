---
description: Start the local Google Maps scraper engine and run a health check.
---
Set up the kit's engine on this computer:

1. Pick how the engine runs. By default, without Docker: from the kit root, `python3 scripts/engine.py start` (Windows: `py scripts/engine.py start`). It needs Python 3.9 or newer and an installed Chrome, Chromium, Edge or Brave. On a machine with none, it downloads Chrome for Testing once (about 100 MB, pinned by SHA-256): tell the user before you run it. If the user runs Docker instead, confirm it is up with `docker ps` (if it errors, point them to **Docker Desktop**, link in `SETUP.md` §0, and stop here), then run `docker compose up -d`; the first run builds the image, about a minute.
2. `python3 scripts/engine.py status` prints the browser the engine uses (skip it for Docker).
3. Verify it is live: `curl http://localhost:8080/api/v1/jobs` should return `null` (no jobs yet) or a JSON array of jobs.
4. Report success. Tell the user they can now run `/scrape <business> in <city>`, or ask in plain language (e.g. "scrape gyms in Miami").
5. **Show this warning once setup succeeds:**
   > **Before you scrape:** the engine sends real requests to Google Maps. Light use is fine. Big jobs
   > back to back, high `depth`, many keywords or scheduled runs without proxies can get your IP
   > temporarily rate-limited by Google, usually for minutes to hours. Your Google account is not
   > affected. Run one job at a time, start at `depth 5`, and add proxies for large or repeated runs.
   > Scraping Google Maps is against Google's Terms of Service, and contact details are personal data,
   > so follow GDPR, CCPA and CAN-SPAM.

If a step fails, see `SETUP.md` §6 (Troubleshooting).
