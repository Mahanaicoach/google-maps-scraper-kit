# Contributing

This page tells you how to get a bug fixed or a change merged into the Google Maps Scraper Kit.

- **Report bugs** with the bug report issue template. Include the exact command and the full error.
- **Keep pull requests small**, one change each. Test with a `depth 1` scrape and run
  `python3 scripts/test_scrape.py` before you open the PR. CI runs the same suite, a bash syntax
  check and `docker compose config` on every PR.
- **Keep the scripts portable.** `scripts/scrape.py` uses the Python standard library only, so do not
  add pip dependencies. `scripts/scrape.sh` must run on macOS's default bash 3.2.
- **Send engine changes upstream.** Changes to the engine itself belong in
  [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper).
- **Never commit scrape output.** CSV and result files are git-ignored because they hold personal data.
