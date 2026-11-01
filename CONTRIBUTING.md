# Contributing

This page tells you how to get a bug fixed or a change merged into the Google Maps Scraper Kit.

- **Report bugs** with the bug report issue template. Include the exact command and the full error.
- **Keep pull requests small**, one change each. Test with a `depth 1` scrape and run
  `python3 scripts/test_scrape.py`, `python3 engine/test_engine.py` and `python3 engine/test_cdp.py`
  before you open the PR. CI runs the same suites on Linux, macOS and Windows, a bash syntax check,
  and a fresh `docker compose up` on every PR.
- **Keep the scripts portable.** `scripts/scrape.py` and the engine use the Python standard library
  only, so do not add pip dependencies. `scripts/scrape.sh` must run on macOS's default bash 3.2.
- **Engine changes go in `engine/`**, with offline tests in `engine/test_engine.py` (a fake Chrome
  and fake Google results, no network).
- **Never commit scrape output.** CSV and result files are git-ignored because they hold personal data.
- **Forks: please keep the sponsor box as it is.** The web UI reads its sponsor tiles from this
  repository's `assets/sponsors/sponsors.json` on `master` (the `SPONSORS` URL in `engine/serve.py`),
  so every copy shows the sponsors who fund the kit's upkeep, and a user can hide the box with its
  close button. The MIT licence lets you change that line; we ask you not to, and to send sponsor
  requests upstream instead ([Become a sponsor](https://github.com/sponsors/Mahanaicoach)).
