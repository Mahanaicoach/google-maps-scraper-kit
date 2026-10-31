# Scrape dentists from Google Maps: a lead list recipe

This recipe shows how to scrape dentists from Google Maps for one city with the Google Maps Scraper Kit, from a quick test to the whole city.
You get a CSV of dental practices with phone, email, website, address, rating and review count, free and local, with no API key.

## Step 1: Run one search

Start the engine, then test with one search at the default depth:

```bash
docker compose up -d
python3 scripts/scrape.py "dentists in Denver CO" --city "Denver, CO" --depth 5
```

Check the preview and the found-rate line for emails. If the rows look right, widen the run.

## Step 2: Cover the whole city

One search stops at about 20 listings (about 100 after the automatic split). Split the city into a 3 x 3 grid of map tiles:

```bash
python3 scripts/scrape.py "dentists in Denver CO" --city "Denver, CO" --grid 3
```

That is 9 jobs, run one at a time. Results are deduped on `place_id`, and rows outside the city box are dropped.

## Step 3: Add social profiles

Add `--socials` for Instagram, Facebook and LinkedIn links found on each practice website:

```bash
python3 scripts/scrape.py "dentists in Denver CO" --city "Denver, CO" --grid 3 --socials
```

Want specialists too? The kit ships a ready batch file with dentists, orthodontists, dental hygienists and pediatric dentists in Denver:

```bash
python3 scripts/scrape.py --keywords-file examples/queries.example.txt --city "Denver, CO"
```

## Example output

Illustrative rows only:

| title | phone | emails | website | review_rating | review_count | instagram |
|---|---|---|---|---|---|---|
| Example Dental Studio | +1 303-555-0101 | hello@example.com | example.com | 4.9 | 312 | instagram.com/exampledental |
| Sample Family Dentistry | +1 303-555-0102 | front@example.org | example.org | 4.6 | 128 | |
| Demo Orthodontics | +1 303-555-0103 |  | example.net | 4.8 | 64 | instagram.com/demoortho |

## Columns that matter for dental outreach

- `title` and `phone`: who to call, and the number to call.
- `emails`: the published practice inbox. The email crawl reads the homepage, then contact and about pages.
- `website`: the site you will review before you reach out.
- `category`: separates general dentists from orthodontists and other specialists.
- `review_rating` and `review_count`: a quick read on how established the practice is.
- `instagram`, `facebook`, `linkedin`: added by `--socials` when the site links them.

## What to expect

- Emails appear only when a practice publishes one on its website. Expect some empty cells.
- A grid is slower than one search and more likely to trip rate limits. Run one job at a time.
- Add proxies for large or repeated runs. Heavy use without them can get your IP temporarily rate-limited.
- Socials appear only when the practice links them on its own site.
- Phones and emails are personal data, so read [Responsible use](../../README.md#responsible-use) before outreach.

## Related

- [Roofing contractor leads recipe](roofing-contractor-leads.md)
- [Google Maps email scraper](../google-maps-email-scraper.md)
- [Kit README](../../README.md)
