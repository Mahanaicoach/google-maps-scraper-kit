---
description: List, download or delete Google Maps scrape jobs on the local engine.
argument-hint: "[list | delete <job-id>]"
---
Manage scrape jobs through the engine's API. Request: **$ARGUMENTS** (default to "list" if empty).

- **list**: `GET http://localhost:8080/api/v1/jobs`. Show a tidy table of `ID`, `Name`, `Status`, `Date` (newest first).
- **delete <job-id>**: `DELETE http://localhost:8080/api/v1/jobs/<job-id>` to free disk space, then confirm it returned HTTP 200.
- **download <job-id>**: `GET .../jobs/<job-id>/download`, then save the CSV and report the row count.

Only use a job id that is a UUID (hex digits and dashes) taken from the job list; refuse anything else.
The `google-maps-scraper` skill has the full API reference.
