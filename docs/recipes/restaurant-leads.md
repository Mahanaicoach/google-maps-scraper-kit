# Scrape restaurants from Google Maps: a lead list recipe

This recipe shows how to scrape restaurants from Google Maps for one city with the Google Maps Scraper Kit, including cafes and pizza places in the same run.
You get a CSV of food businesses with phone, email, website, address and rating, free and local, with no API key.

## Step 1: Run one search

Start the engine, then test with one search at the default depth:

```bash
python3 scripts/engine.py start
python3 scripts/scrape.py "restaurants in Austin TX" --city "Austin, TX" --depth 5
```

With Docker, `docker compose up -d` starts the same engine instead of the first line.

Check the preview and the email found-rate line before you scale up.

## Step 2: Cover the food categories you sell to

Food businesses sit in many Google Maps categories. A search for restaurants may miss cafes and pizza places. Add each term with `--keyword` to run them as one job and one CSV:

```bash
python3 scripts/scrape.py "restaurants in Austin TX" --keyword "cafes in Austin TX" \
  --keyword "pizza in Austin TX" --city "Austin, TX"
```

For a whole city past the ~120 per search cap, add `--grid 3`. It splits the city into 9 map tiles, runs them one at a time and dedupes on `place_id`.

## Step 3: Add price range for qualification

`--full` keeps all raw columns, including `price_range` and `status` (open or closed now). Weekly opening hours work [headless](../../SETUP.md#review-counts-hours-and-reviews) when Google supplies them:

```bash
python3 scripts/scrape.py "restaurants in Austin TX" --city "Austin, TX" --full
```

To keep the file lean, pick your own columns with `--fields`:

```bash
python3 scripts/scrape.py "restaurants in Austin TX" --city "Austin, TX" \
  --fields "title,phone,emails,website,category,price_range,review_rating"
```

## Example output

Illustrative rows only:

| title | phone | emails | category | price_range | review_rating |
|---|---|---|---|---|---|
| Example Kitchen | +1 512-555-0101 | hello@example.com | Restaurant | $$ | 4.6 |
| Sample Slice | +1 512-555-0102 | orders@example.org | Pizza restaurant | $ | 4.4 |
| Demo Coffee House | +1 512-555-0103 |  | Cafe | $ | 4.7 |

## Columns that matter for restaurant outreach

- `category`: splits restaurants, cafes and pizza places so you can tailor each message.
- `price_range`: a quick read on the kind of venue. Needs `--full` or `--fields`.
- `status`: whether the venue is open right now, so you can time a call. Needs `--full` or `--fields`.
- `review_rating`: a signal of reputation. `review_count` works [headless](../../SETUP.md#review-counts-hours-and-reviews) and supports `--min-reviews` filtering.
- `phone`, `emails` and `website`: the contact channels.

## What to expect

- Emails appear only when a business publishes one on its website. Many venues will have an empty cell.
- Not every listing has a price range, so that cell can be empty too.
- A grid or many keywords is a large job. Run one job at a time and add proxies for repeated runs.
- Heavy use without proxies can get your IP temporarily rate-limited by Google for minutes to hours.
- Phones and emails are personal data, so read [Responsible use](../../README.md#responsible-use) before outreach.

## Related

- [Roofing contractor leads recipe](roofing-contractor-leads.md)
- [Google Maps scraper in Python](../google-maps-scraper-python.md)
- [Kit README](../../README.md)
