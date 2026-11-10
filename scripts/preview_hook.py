#!/usr/bin/env python3
"""Claude Code PostToolUse hook: after a scrape.py run, show the user the first rows of each saved file.

The preview goes out as the hook's `systemMessage`, which Claude Code shows to the user and does not
send to the model, so the leads appear in the chat while scraped text stays out of the agent's context
(skills.sh / Snyk W011). Registered in .claude/settings.json. Prints nothing for any other command,
and never fails the tool call."""
import csv, json, os, re, sys

COLUMNS = [("title", "name"), ("phone", "phone"), ("emails", "email"), ("website", "website"),
           ("review_rating", "rating")]
ROWS, WIDTH = 10, 30
SAVED = re.compile(r"saved → (.+?\.(?:csv|json))\s")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]+")


def cell(v):
    """One line of plain text: no control or bidi characters (no terminal escapes), at most WIDTH."""
    s = CONTROL.sub(" ", "" if v is None else str(v)).strip().lstrip("'")
    s = " ".join(s.split())
    return s if len(s) <= WIDTH else s[:WIDTH - 1] + "…"


def rows_of(path):
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def table(path):
    rows = rows_of(path)
    cols = [(k, h) for k, h in COLUMNS if rows and k in rows[0]]
    grid = [[h for _, h in cols]] + [[cell(r.get(k)) for k, _ in cols] for r in rows[:ROWS]]
    widths = [max(len(line[i]) for line in grid) for i in range(len(cols))]
    lines = ["  ".join(v.ljust(w) for v, w in zip(line, widths)).rstrip() for line in grid]
    head = f"Leads preview: first {min(ROWS, len(rows))} of {len(rows)} rows in {os.path.basename(path)}"
    return "\n".join([head] + lines)


def main():
    event = json.load(sys.stdin)
    if "scrape.py" not in str((event.get("tool_input") or {}).get("command", "")):
        return
    out = event.get("tool_response") or {}
    text = out.get("stdout", "") if isinstance(out, dict) else str(out)
    tables = [table(p) for p in dict.fromkeys(SAVED.findall(text + "\n")) if os.path.isfile(p)]
    if tables:
        note = "(shown to you only; Claude does not see these rows)"
        print(json.dumps({"systemMessage": "\n\n".join(tables) + "\n" + note}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a preview must never break or block the scrape the user just ran
        pass
