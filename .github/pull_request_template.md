## What this changes

<!-- One or two sentences. Keep the PR to one change. -->

## Why

<!-- The problem it solves. Link the issue if there is one: Fixes #123 -->

## How I tested it

<!-- The exact command you ran, e.g. a `depth 1` scrape, and what you got back. -->

## Checklist

- [ ] Tested with a `depth 1` scrape
- [ ] `scripts/scrape.py` still uses the Python standard library only (no pip dependencies)
- [ ] `scripts/scrape.sh` still runs on macOS's default bash 3.2
- [ ] No scrape output (CSV or result files) committed
- [ ] `python3 scripts/test_scrape.py` passes, and `python3 engine/test_engine.py` and `python3 engine/test_cdp.py` for engine changes
- [ ] Docs updated (README, SETUP or the skill) if behavior changed
