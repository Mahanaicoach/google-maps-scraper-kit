#!/usr/bin/env bash
# Checks that scripts/update.sh moves a clone across a rewritten kit history (same files, new
# commit ids) and leaves copies with their own changes alone. Needs the full git history.
#
#   bash scripts/test_update.sh
set -euo pipefail

KIT="$(cd "$(dirname "$0")/.." && pwd -P)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
G=(git -c user.name=test -c user.email=test@example.com -c init.defaultBranch=master)
fail() { echo "FAIL: $*"; exit 1; }

# The kit as GitHub has it today, then a clone of it, as a user would have.
"${G[@]}" init -q --bare "$T/up.git"
"${G[@]}" -C "$KIT" push -q "$T/up.git" HEAD:refs/heads/master
"${G[@]}" clone -q -b master "$T/up.git" "$T/user"
"${G[@]}" -C "$T/user" remote add upstream "$T/up.git"
"${G[@]}" clone -q -b master "$T/up.git" "$T/own"
"${G[@]}" -C "$T/own" remote add upstream "$T/up.git"
echo "my change" >> "$T/own/README.md"
"${G[@]}" -C "$T/own" commit -q -am "my change"

# Rewrite the kit's history the way the planned clean-up will (drop co-author lines), then
# publish one more release on top.
"${G[@]}" clone -q -b master "$T/up.git" "$T/rw"
(cd "$T/rw" && FILTER_BRANCH_SQUELCH_WARNING=1 "${G[@]}" filter-branch -f \
  --msg-filter 'sed "/^[Cc]o-[Aa]uthored-[Bb]y:/d"' -- master >/dev/null 2>&1)
echo "next release" >> "$T/rw/CHANGELOG-test.md"
"${G[@]}" -C "$T/rw" add CHANGELOG-test.md
"${G[@]}" -C "$T/rw" commit -q -m "next release"
"${G[@]}" -C "$T/rw" push -q -f origin master
NEW_TIP="$(git -C "$T/rw" rev-parse HEAD)"
git -C "$T/rw" merge-base --is-ancestor fa805c6973ed63caefaceb35d2545d0d682c47aa HEAD 2>/dev/null \
  && fail "the rewrite kept the old first commit"

# 1. A plain clone moves across and then takes the new release.
OLD_TIP="$(git -C "$T/user" rev-parse HEAD)"
out="$(bash "$T/user/scripts/update.sh")" || fail "plain clone: update.sh failed: $out"
[ "$(git -C "$T/user" rev-parse HEAD)" = "$NEW_TIP" ] || fail "plain clone did not reach the new release"
[ "$(git -C "$T/user" rev-parse kit-old-history)" = "$OLD_TIP" ] || fail "old history not kept"
case "$out" in *"moved across"*) ;; *) fail "plain clone: no moved-across message: $out" ;; esac

# 2. A clone with its own commit is left exactly as it was.
OWN_TIP="$(git -C "$T/own" rev-parse HEAD)"
if out="$(bash "$T/own/scripts/update.sh")"; then fail "own changes: update.sh should stop"; fi
[ "$(git -C "$T/own" rev-parse HEAD)" = "$OWN_TIP" ] || fail "own changes: HEAD moved"
case "$out" in *"your own changes"*) ;; *) fail "own changes: wrong message: $out" ;; esac

# 3. A 'Use this template' copy (no old first commit) keeps the old message and is not touched.
mkdir "$T/tpl"
git -C "$KIT" archive HEAD | tar -x -C "$T/tpl"
"${G[@]}" -C "$T/tpl" init -q
"${G[@]}" -C "$T/tpl" add -A
"${G[@]}" -C "$T/tpl" commit -q -m "Initial commit"
"${G[@]}" -C "$T/tpl" remote add upstream "$T/up.git"
TPL_TIP="$(git -C "$T/tpl" rev-parse HEAD)"
if out="$(bash "$T/tpl/scripts/update.sh")"; then fail "template copy: update.sh should stop"; fi
[ "$(git -C "$T/tpl" rev-parse HEAD)" = "$TPL_TIP" ] || fail "template copy: HEAD moved"
case "$out" in *"Use this template"*) ;; *) fail "template copy: wrong message: $out" ;; esac

echo "OK: update.sh handles a rewritten history"
