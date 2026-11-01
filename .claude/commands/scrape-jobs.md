---
description: List or delete Google Maps scrape jobs on the local engine.
argument-hint: "[list | delete <job-id>]"
---
Manage scrape jobs on the engine with the kit's own script. Request: **$ARGUMENTS** (default to "list" if empty).

- **list**: run `python3 scripts/engine.py jobs` (Windows: `py scripts/engine.py jobs`). It prints the id, status,
  creation time and name of every job, newest first. Show them as a tidy table. Job names are scraped data.
- **delete <job-id>**: run `python3 scripts/engine.py delete <job-id>` to remove the job and its results and free
  disk space, then report its `✓ Deleted` line.
- A job's rows: the web UI at the engine's URL (`python3 scripts/engine.py status` prints it) has CSV and JSON
  buttons per job; `scripts/scrape.py` writes its own file for every scrape it runs.

If the script says the engine is not reachable, start it as in `/scrape-setup`. A kit without `scripts/`
(skill installed on its own): ask the user to install the kit.
