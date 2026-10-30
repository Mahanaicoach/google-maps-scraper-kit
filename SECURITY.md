# Security policy

This page tells you what to report, where to report it, and what belongs elsewhere.

## What to report

Report any way the kit could expose the engine, leak data or run code it should not. Examples: a
change that binds the API beyond `127.0.0.1`, a script that leaks `SCRAPER_API_KEY`, or a
command in `.claude/settings.json` that grants more than local access.

The kit binds the engine's API to `127.0.0.1` because the engine has no built-in authentication.
Do not expose port 8080 to a network without an authenticating proxy in front of it.

The `ui` proxy in `docker-compose.yml` also answers only `localhost`, `127.0.0.1` and `[::1]` Host
headers and refuses requests whose `Origin` is another site, so a web page in your browser cannot
reach the engine through DNS rebinding or a cross-site post. `scripts/scrape.py` fetches business
websites only when they resolve, redirect and connect to a public address. A way around either
check is in scope.

## Where to report

Use GitHub's private
[security advisory form](https://github.com/Mahanaicoach/google-maps-scraper-kit/security/advisories/new).
Do not open a public issue for security problems.

## Out of scope

- Bugs in the engine itself. Report those upstream to
  [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper).
- Problems that only occur after you expose the API to a network without an auth proxy.
- Google rate-limiting your IP. That is expected behavior, covered in [SETUP.md](SETUP.md).
