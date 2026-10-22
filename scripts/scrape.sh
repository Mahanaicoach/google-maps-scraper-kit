#!/usr/bin/env bash
# Google Maps scrape for one keyword: creates the job, polls, downloads, and saves the 8 lead fields to a CSV.
# Usage:   ./scripts/scrape.sh "<keyword>" <lat> <lon> [depth]
# Example: ./scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5
set -euo pipefail

# Read SCRAPER_* from the kit's .env, the same file docker compose reads, so a remapped
# port needs one edit. Variables already exported in the shell win over the file.
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
if [ -f "$ENV_FILE" ]; then
  while IFS='=' read -r k v || [ -n "$k" ]; do
    case "$k" in SCRAPER_BASE_URL|SCRAPER_PORT|SCRAPER_API_KEY) ;; *) continue ;; esac
    v="${v%$'\r'}"; v="${v#\"}"; v="${v%\"}"; v="${v#\'}"; v="${v%\'}"
    [ -n "${!k+x}" ] || export "$k=$v"
  done < "$ENV_FILE"
fi

BASE="${SCRAPER_BASE_URL:-http://localhost:${SCRAPER_PORT:-8080}}"
KEY="${SCRAPER_API_KEY:-}"
# NOTE: empty-array expansion needs the ${a[@]+...} guard — plain "${AUTH[@]}" is an
# unbound-variable error under 'set -u' on bash 3.2 (stock macOS), which is the
# common case here since local use needs no API key.
AUTH=(); [ -n "$KEY" ] && AUTH=(-H "X-API-Key: $KEY")

# JSON-escape a string using bash expansions only: no jq, no python dependency, bash 3.2 safe.
# Without it, a keyword containing a quote or a backslash builds invalid JSON and the engine
# rejects the job with a generic "check required fields" error.
json_escape() {
  local s=${1//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\t'/\\t}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  printf '%s' "$s"
}

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
# email:true makes the engine visit each business website for emails (the key lead field). A little slower.
RESP=$(curl -s -X POST "$BASE/api/v1/jobs" ${AUTH[@]+"${AUTH[@]}"} -H "Content-Type: application/json" \
  -d "$(printf '{"name":"scrape-cli","keywords":["%s"],"lang":"%s","zoom":15,"lat":"%s","lon":"%s","fast_mode":false,"radius":10000,"depth":%s,"email":true,"max_time":600}' \
    "$(json_escape "$KEYWORD")" "en" "$LAT" "$LON" "$DEPTH")")
ID=$(printf '%s' "$RESP" | grep -oE '[a-f0-9-]{36}' | head -1 || true)
[ -n "$ID" ] || { echo "✗ Failed to create job. API said: ${RESP:-<empty response>}" >&2; exit 1; }
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

# Trim the raw 34-column CSV to the 8 lead fields (drops coordinates, IDs, hours, images).
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
