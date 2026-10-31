# Scrape Google Maps leads with Claude Code

The Google Maps Scraper Kit lets you scrape Google Maps leads with Claude Code by asking in plain English, and get back a clean table plus a CSV.
It is free and runs locally: the agent drives the kit's own scraping engine on your machine, with no API key and no SaaS credits.

## Set it up

Install Python 3.9 or newer and Google Chrome (or Chromium, Edge or Brave), then pick one option. Docker Desktop works too, instead of the local Chrome.

Option A: open the kit folder in Claude Code. The skill and four slash commands load automatically.

```bash
git clone https://github.com/Mahanaicoach/google-maps-scraper-kit.git
cd google-maps-scraper-kit
claude
```

Option B: install the skill into an agent you already use.

```bash
npx skills add mahanaicoach/google-maps-scraper-kit
```

The skill works with Claude Code, Codex, Cursor and any other Agent Skills-compatible tool. The slash commands are Claude Code only. The engine ships with the kit, so the agent still needs the kit folder to start it.

## Ask in plain language

> Scrape gyms in Miami and give me phones, emails and websites.

Or use a slash command in Claude Code:

| Slash command | Action |
|---|---|
| `/scrape <business> in <city, ST> [depth]` | Run one scrape, return a clean table and CSV |
| `/scrape-batch <keywords-file> [--city "City, ST"]` | Run many queries as one job |
| `/scrape-setup` | Start the engine and run a health check |
| `/scrape-jobs [list \| delete <id>]` | List or delete jobs |

## What the agent does

1. Runs a health check on the engine at `localhost:8080`, and starts it with `python3 scripts/engine.py start` (or `docker compose up -d` if you run Docker) if it is down.
2. Asks once whether you also want Instagram, Facebook and LinkedIn profiles.
3. Builds the job with the required `max_time`, `lat` and `lon`, with emails on by default.
4. Polls the job in the background until it finishes.
5. Downloads the CSV.
6. Keeps the 8 lead fields: name, phone, email, website, category, address, rating and review count (empty for now).
7. Saves the file, then presents a short table and the CSV path instead of dumping every row into chat.

The kit's local commands and `localhost` calls are pre-approved in `.claude/settings.json`, so Claude Code does not prompt for each one.

## Example output

Illustrative rows only:

| title | phone | emails | website | category |
|---|---|---|---|---|
| Example Fitness Club | +1 305-555-0101 | hello@example.com | example.com | Gym |
| Sample Strength Studio | +1 305-555-0102 | info@example.org | example.org | Gym |
| Demo Boxing Gym | +1 305-555-0103 |  | example.net | Boxing gym |

## What to expect

- Google Maps returns about 120 listings per search. Ask for the whole city and the agent runs `--grid 3`, which splits it into 9 tiles and dedupes the results.
- Emails come back only when a business publishes one on its website. Expect some empty cells.
- For large jobs, the agent prints one short warning about temporary IP rate limits and suggests proxies, then proceeds.
- The skill scrapes Google Maps business listings only. It does not scrape social platforms directly.
- Scraping Google Maps is against Google's Terms of Service, so read [Responsible use](../README.md#responsible-use) before outreach.

## Related

- [Google Maps email scraper](google-maps-email-scraper.md)
- [Dentist leads recipe](recipes/dentist-leads.md)
- [Kit README](../README.md)
