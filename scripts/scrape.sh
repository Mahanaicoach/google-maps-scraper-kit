#!/usr/bin/env bash
# One-shot Google Maps scrape: create job -> poll -> download -> summarize.
# Usage:   ./scripts/scrape.sh "<keyword>" <lat> <lon> [depth]
# Example: ./scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5
set -euo pipefail

BASE="${SCRAPER_BASE_URL:-http://localhost:8080}"
KEY="${SCRAPER_API_KEY:-}"
# NOTE: empty-array expansion needs the ${a[@]+...} guard — plain "${AUTH[@]}" is an
# unbound-variable error under 'set -u' on bash 3.2 (stock macOS), which is the
# common case here since local use needs no API key.
AUTH=(); [ -n "$KEY" ] && AUTH=(-H "X-API-Key: $KEY")

KEYWORD="${1:-}"; LAT="${2:-}"; LON="${3:-}"; DEPTH="${4:-5}"
if [ -z "$KEYWORD" ] || [ -z "$LAT" ] || [ -z "$LON" ]; then
  echo "Usage: $0 \"<keyword>\" <lat> <lon> [depth]" >&2
  echo "Example: $0 \"coffee shops in Austin TX\" 30.2672 -97.7431 5" >&2
  exit 1
fi

# Is the scraper up?
if ! curl -s -m 5 ${AUTH[@]+"${AUTH[@]}"} "$BASE/api/v1/jobs" >/dev/null 2>&1; then
  echo "✗ Scraper not reachable at $BASE — run 'docker compose up -d' first." >&2
  exit 1
fi

echo "▶ Creating job: \"$KEYWORD\" @ $LAT,$LON depth=$DEPTH (email extraction ON)"
# email:true → the scraper visits each business website to pull emails (the key lead field). A bit slower.
BODY=$(printf '{"name":"scrape-cli","keywords":["%s"],"lang":"en","zoom":15,"lat":"%s","lon":"%s","fast_mode":false,"radius":10000,"depth":%s,"email":true,"max_time":600}' \
  "$KEYWORD" "$LAT" "$LON" "$DEPTH")
ID=$(curl -s -X POST "$BASE/api/v1/jobs" ${AUTH[@]+"${AUTH[@]}"} -H "Content-Type: application/json" -d "$BODY" \
     | grep -oE '[a-f0-9-]{36}' | head -1 || true)
[ -n "$ID" ] || { echo "✗ Failed to create job (check required fields)." >&2; exit 1; }
echo "  job id: $ID"

echo "▶ Polling (up to ~10 min)…"
for i in $(seq 1 60); do
  # '|| true' keeps the script alive under 'set -euo pipefail' when grep finds no match yet
  S=$(curl -s ${AUTH[@]+"${AUTH[@]}"} "$BASE/api/v1/jobs/$ID" | grep -oE '"Status":"[^"]*"' | head -1 | cut -d'"' -f4 || true)
  printf '\r  status: %-10s (attempt %d)' "${S:-?}" "$i"
  [ "$S" = ok ] && { echo; break; }
  [ "$S" = failed ] && { echo; echo "✗ Job failed." >&2; exit 1; }
  sleep 10
done
[ "$S" = ok ] || { echo; echo "✗ Timed out waiting for job." >&2; exit 1; }

OUT="results-$(echo "$KEYWORD" | tr ' /' '__' | tr -cd 'A-Za-z0-9_-').csv"
RAW="$(mktemp)"
echo "▶ Downloading…"
curl -s ${AUTH[@]+"${AUTH[@]}"} "$BASE/api/v1/jobs/$ID/download" -o "$RAW"

# Trim the raw 34-column dump down to money-useful LEAD fields only (drops geo coords, IDs, hours, images…).
if command -v python3 >/dev/null 2>&1; then
  python3 - "$RAW" "$OUT" <<'PY'
import csv, sys
LEAD = ["title", "phone", "emails", "website", "category", "address", "review_rating", "review_count"]
with open(sys.argv[1], newline='') as f:
    rows = list(csv.DictReader(f))
with open(sys.argv[2], "w", newline='') as f:
    w = csv.DictWriter(f, fieldnames=LEAD); w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in LEAD})
print(f"✓ Done — {len(rows)} leads saved to {sys.argv[2]}")
print("  (name, phone, email, website, category, address, rating, reviews)\n")
print("Preview (name | phone | email | website):")
for r in rows[:6]:
    print(f"  • {r.get('title','')} | {r.get('phone','')} | {r.get('emails','')} | {r.get('website','')}")
PY
  rm -f "$RAW"
else
  mv "$RAW" "$OUT"
  echo "✓ Done — raw CSV saved to $OUT (install python3 to auto-trim to lead fields only)"
fi
