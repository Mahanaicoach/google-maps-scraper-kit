#!/usr/bin/env bash
# Google Maps scrape for one keyword: creates the job, polls, downloads, and saves the 8 lead fields to a CSV.
# Usage:   ./scripts/scrape.sh "<keyword>" <lat> <lon> [depth]
# Example: ./scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5
#
# A thin wrapper over scrape.py, so both entry points share one job flow: the same .env handling,
# polling budget, email cleanup and UTF-8 output. Fix the flow there, not here.
set -euo pipefail

KEYWORD="${1:-}"; LAT="${2:-}"; LON="${3:-}"; DEPTH="${4:-5}"
if [ -z "$KEYWORD" ] || [ -z "$LAT" ] || [ -z "$LON" ]; then
  echo "Usage: $0 \"<keyword>\" <lat> <lon> [depth]" >&2
  echo "Example: $0 \"coffee shops in Austin TX\" 30.2672 -97.7431 5" >&2
  exit 1
fi

# Git Bash on Windows often has only `python`, and `python3` there can be the Microsoft Store stub
# that only prints a message, so take the first name that really runs.
PY=""
for p in python3 python py; do
  command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys' >/dev/null 2>&1 && { PY="$p"; break; }
done
[ -n "$PY" ] || { echo "✗ Python 3 is required (standard library only, no pip installs)." >&2; exit 1; }

# File name from the keyword; a keyword with no ASCII letters (寿司 in 東京) falls back to a timestamp
# so reruns never overwrite each other.
SLUG="$(printf '%s' "$KEYWORD" | tr ' /' '__' | tr -cd 'A-Za-z0-9_-')"
[ -n "$SLUG" ] || SLUG="$(date +%Y%m%d-%H%M%S)"
OUT="results-$SLUG.csv"
exec "$PY" "$(dirname "$0")/scrape.py" "$KEYWORD" "$LAT" "$LON" --depth "$DEPTH" --out "$OUT"
