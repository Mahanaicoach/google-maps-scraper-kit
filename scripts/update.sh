#!/usr/bin/env bash
# Update this kit to the latest version from github.com/Mahanaicoach/google-maps-scraper-kit.
# Works whether you cloned the original repo or your own fork. Your own commits
# are kept: git merges the update into them.
#
#   bash scripts/update.sh
set -euo pipefail

UPSTREAM="https://github.com/Mahanaicoach/google-maps-scraper-kit.git"
BRANCH="master"

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$KIT_DIR"

# A ZIP download has no git history. Check the top level too, so a copy inside
# another repo (for example a git-tracked ~/.claude) is never merged into it.
if [ "$(git rev-parse --show-toplevel 2>/dev/null)" != "$KIT_DIR" ]; then
  echo "This folder is not a git clone of the kit, so it can't update itself."
  echo "Clone it again: git clone $UPSTREAM"
  exit 1
fi

if ! git diff --quiet HEAD --; then
  echo "You have uncommitted changes. Save them with 'git stash' (or commit them), then run this again."
  exit 1
fi

git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$UPSTREAM"
git fetch --quiet upstream "$BRANCH"

if ! git merge-base HEAD "upstream/$BRANCH" >/dev/null; then
  echo "This copy shares no history with the original (it was made with 'Use this template',"
  echo "or it is a shallow clone), so git can't merge updates into it."
  echo "See what changed: https://github.com/Mahanaicoach/google-maps-scraper-kit/commits/$BRANCH"
  exit 1
fi

BEFORE="$(git rev-parse HEAD)"
if ! git merge --no-edit --quiet "upstream/$BRANCH"; then
  echo
  echo "The update conflicts with your own changes in the files listed above."
  echo "Fix them, then run: git add -A && git commit --no-edit"
  echo "Or undo the update: git merge --abort"
  exit 1
fi

if [ "$BEFORE" = "$(git rev-parse HEAD)" ]; then
  echo "Already up to date."
else
  echo "Updated. What's new:"
  git log --oneline --no-merges -n 20 "$BEFORE..HEAD"
  if ! git remote get-url origin 2>/dev/null | grep -qi "mahanaicoach/google-maps-scraper-kit"; then
    echo "Your fork on GitHub is still on the old version. Run 'git push' to update it too."
  fi
fi

# A new kit version can change the engine. Rebuild and restart a running one so it takes effect.
# --remove-orphans drops the nginx container of kit 1.x, which would otherwise keep the port.
if [ "$BEFORE" != "$(git rev-parse HEAD)" ] && command -v docker >/dev/null 2>&1 \
  && [ -n "$(docker compose ps -q 2>/dev/null)" ]; then
  docker compose up -d --build --remove-orphans
fi
if [ "$BEFORE" != "$(git rev-parse HEAD)" ] && [ -f .engine/state.json ]; then
  echo "Restart the engine so the update takes effect: python3 scripts/engine.py restart"
fi
