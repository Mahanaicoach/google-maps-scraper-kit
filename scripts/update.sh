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

# The kit's history may be rewritten once to tidy commit credits: every commit gets a new id but
# keeps exactly the same files. A clone of the old history (it contains the old first commit)
# whose files match a kit version moves across to the new history; nothing on disk changes.
OLD_ROOT="fa805c6973ed63caefaceb35d2545d0d682c47aa"
MOVED=""
if ! git merge-base HEAD "upstream/$BRANCH" >/dev/null; then
  if git merge-base --is-ancestor "$OLD_ROOT" HEAD 2>/dev/null; then
    TREE="$(git rev-parse 'HEAD^{tree}')"
    MATCH="$(git log --format='%H %T' "upstream/$BRANCH" | awk -v t="$TREE" '$2 == t && !m { m = $1 } END { print m }')"
    if [ -z "$MATCH" ]; then
      echo "The kit's history was rewritten on GitHub, and this copy has your own changes, so it"
      echo "can't move across by itself. Nothing was changed. Clone the kit again, then copy your"
      echo "changes into it: git clone $UPSTREAM"
      exit 1
    fi
    git branch -f kit-old-history HEAD
    git reset --quiet --hard "$MATCH"
    MOVED=1
    echo "The kit's history was rewritten on GitHub; this copy moved across (your files are unchanged)."
    echo "The old history is kept in the branch kit-old-history."
    if ! git remote get-url origin 2>/dev/null | grep -qi "mahanaicoach/google-maps-scraper-kit"; then
      echo "Your fork on GitHub still has the old history. Update it with: git push --force origin HEAD:$BRANCH"
    fi
  else
    echo "This copy shares no history with the original (it was made with 'Use this template',"
    echo "or it is a shallow clone), so git can't merge updates into it."
    echo "See what changed: https://github.com/Mahanaicoach/google-maps-scraper-kit/commits/$BRANCH"
    exit 1
  fi
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
  if [ -z "$MOVED" ] && ! git remote get-url origin 2>/dev/null | grep -qi "mahanaicoach/google-maps-scraper-kit"; then
    echo "Your fork on GitHub is still on the old version. Run 'git push' to update it too."
  fi
fi

# A new kit version can pin a new engine image. Restart a running engine so it takes effect.
if [ "$BEFORE" != "$(git rev-parse HEAD)" ] && command -v docker >/dev/null 2>&1 \
  && [ -n "$(docker compose ps -q 2>/dev/null)" ]; then
  docker compose up -d
fi
