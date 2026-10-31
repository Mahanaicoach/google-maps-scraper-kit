---
description: List, download or delete Google Maps scrape jobs on the local engine.
argument-hint: "[list | download <job-id> | delete <job-id>]"
---
Manage scrape jobs through the engine's API. Request: **$ARGUMENTS** (default to "list" if empty).

First run `python3 scripts/engine.py status` (Windows: `py scripts/engine.py status`). It prints `✓ Running on http://localhost:<port>` only when the kit's own engine answers. Use that URL where the examples below show `http://localhost:8080`. With no `scripts/` folder, the base is `http://localhost:8080` (or `SCRAPER_PORT` from `.env`).

- **list**: `GET http://localhost:8080/api/v1/jobs`. Show a tidy table of `ID`, `Name`, `Status`, `Date` (newest first).
- **delete <job-id>**: `DELETE http://localhost:8080/api/v1/jobs/<job-id>` to free disk space, then confirm it returned HTTP 200.
- **download <job-id>**: `GET .../jobs/<job-id>/download`, then save the CSV and report the row count.

The `google-maps-scraper` skill has the full API reference.
