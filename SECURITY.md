# Security policy

This page tells you what to report, where to report it, and what belongs elsewhere.

## What to report

Report any way the kit could expose the engine, leak data or run code it should not. Examples: a
change that binds the API beyond `127.0.0.1`, a script that leaks `SCRAPER_API_KEY`, or a
command in `.claude/settings.json` that grants more than local access.

The engine (`engine/serve.py`) listens on `127.0.0.1` only, and in Docker the port is published on
the host's `127.0.0.1` only. That localhost binding is the security boundary: an API key is optional
(`SCRAPER_API_KEY` in `.env` makes the engine require an `X-API-Key` header). Do not expose port
8080 to a network.

The engine also answers only `localhost`, `127.0.0.1` and `[::1]` Host headers (421 otherwise) and
refuses requests whose `Origin` is another site (403), so a web page in your browser cannot reach it
through DNS rebinding or a cross-site post. The engine and `scripts/scrape.py` fetch business
websites only when they resolve, redirect and connect to a public address. Proxy passwords show as
`***` in the API and the web UI and never reach the log. `scripts/engine.py start`
runs a downloaded Chrome only when its SHA-256 matches the one pinned in `engine/browser.py`. A way
around any of these checks is in scope.

The engine sends no telemetry. It contacts Google Maps; for email or socials jobs, the businesses'
own websites; and for `verify_emails` jobs, Google's public DNS (`dns.google`). The web UI page in
your browser also loads two files from `raw.githubusercontent.com`: the sponsor list
(`sponsors.json`) and the business category list. A request to anywhere else is in scope.

## Where to report

Use GitHub's private
[security advisory form](https://github.com/Mahanaicoach/google-maps-scraper-kit/security/advisories/new).
Do not open a public issue for security problems.

## Out of scope

- Problems that only occur after you expose the API to a network yourself.
- Bugs in Chrome, Chromium or the Debian base image. Report those to their projects.
- Google rate-limiting your IP. That is expected behavior, covered in [SETUP.md](SETUP.md).
