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

# Git Bash on Windows often has only `python`.
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "✗ Python 3 is required (standard library only, no pip installs)." >&2; exit 1; }

OUT="results-$(echo "$KEYWORD" | tr ' /' '__' | tr -cd 'A-Za-z0-9_-').csv"
exec "$PY" "$(dirname "$0")/scrape.py" "$KEYWORD" "$LAT" "$LON" --depth "$DEPTH" --out "$OUT"
