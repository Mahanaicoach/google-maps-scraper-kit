---
description: Start the local Google Maps scraper engine in Docker and run a health check.
---
Set up the kit's engine on this computer:

1. Confirm Docker is installed and running: `docker ps`. If it errors, tell the user to install or start **Docker Desktop** (link in `SETUP.md` §0) and stop here; do not set the engine up another way.
2. From the kit root, start the engine: `docker compose up -d`.
3. Wait ~10s, then verify it is live: `curl http://localhost:8080/api/v1/jobs` should return `null` (no jobs yet) or a JSON array of jobs.
4. Report success. Tell the user they can now run `/scrape <business> in <city>`, or ask in plain language (e.g. "scrape gyms in Miami").
5. **Show this warning once setup succeeds:**
   > **Before you scrape:** the engine sends real requests to Google Maps. Light use is fine. Big jobs
   > back to back, high `depth`, many keywords or scheduled runs without proxies can get your IP
   > temporarily rate-limited by Google, usually for minutes to hours. Your Google account is not
   > affected. Run one job at a time, start at `depth 5`, and add proxies for large or repeated runs.
   > Scraping Google Maps is against Google's Terms of Service, and contact details are personal data,
   > so follow GDPR, CCPA and CAN-SPAM.

If a step fails, see `SETUP.md` §6 (Troubleshooting).
