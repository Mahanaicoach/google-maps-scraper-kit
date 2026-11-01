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
