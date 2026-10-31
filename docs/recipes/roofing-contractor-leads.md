# Scrape roofing contractors from Google Maps: a lead list recipe

This recipe shows how to scrape roofing contractors from Google Maps for one city with the Google Maps Scraper Kit, using one batch job for several search terms.
You get a CSV of roofers with phone, email, website, address, rating and review count, free and local, with no API key.

## Step 1: Run one search

Start the engine, then test with one search at the default depth:

```bash
docker compose up -d
python3 scripts/scrape.py "roofing contractors in Phoenix AZ" --city "Phoenix, AZ" --depth 5
```

Check the preview and the email found-rate line before you scale up.

## Step 2: Batch the roofing variants

Roofers list themselves under different names on Google Maps. Put each variant on its own line in a keywords file. Blank lines and lines starting with `#` are ignored.

`roofing.txt`:

```text
# One keyword per line. All lines run as one job and one CSV.
roofing contractor in Phoenix AZ
roof repair in Phoenix AZ
roofers in Phoenix AZ
```

Run them as one job:

```bash
python3 scripts/scrape.py --keywords-file roofing.txt --city "Phoenix, AZ"
```

## Step 3: Cover the whole city

Each search stops at about 20 listings (about 100 after the automatic split). Add `--grid 3` to split the city into 9 map tiles, run one at a time, with duplicates removed on `place_id`:

```bash
python3 scripts/scrape.py --keywords-file roofing.txt --city "Phoenix, AZ" --grid 3
```

Add `--socials` to any of these commands for Instagram, Facebook and LinkedIn links found on each website.

## Example output

Illustrative rows only:

| title | phone | emails | website | category | review_count |
|---|---|---|---|---|---|
| Example Roofing Co | +1 602-555-0101 | estimates@example.com | example.com | Roofing contractor | 245 |
| Sample Roof Repair | +1 602-555-0102 | info@example.org | example.org | Roofing contractor | 58 |
| Demo Roofers | +1 602-555-0103 |  | example.net | Roofer | 19 |

## Columns that matter for roofing outreach

- `phone`: the main contact channel for most trade businesses.
- `emails`: filled when the roofer publishes one. The email crawl reads the homepage, then contact and about pages.
- `website`: an empty cell here also means no email or socials could be found.
- `category`: shows how each business describes itself.
- `review_rating` and `review_count`: a quick read on size and reputation.

## What to expect

- Overlapping terms find the same business more than once. The engine lists each place once per job, and the script dedupes on `place_id` as well, so the CSV has no repeats.
- Emails appear only when a business publishes one on its website.
- A grid with three keywords is a large job. The script prints a rate-limit warning and proceeds.
- Add proxies for large or repeated runs. Heavy use without them can get your IP temporarily rate-limited.
- Phones and emails are personal data, so read [Responsible use](../../README.md#responsible-use) before outreach.

## Related

- [Dentist leads recipe](dentist-leads.md)
- [Restaurant leads recipe](restaurant-leads.md)
- [Kit README](../../README.md)
