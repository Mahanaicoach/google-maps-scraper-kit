#!/usr/bin/env python3
"""After-shell hook: when scrape.py or leads.py saves a file, show the user its first rows.

The preview goes out as the hook's `systemMessage`, which the agent app shows to the user and does not
send to the model, so the leads appear in the chat while scraped text stays out of the agent's context
(skills.sh / Snyk W011). Registered for Claude Code (.claude/settings.json and the skill's frontmatter),
Codex (.codex/hooks.json) and Gemini CLI (.gemini/settings.json); all three send the same event shape.
Prints nothing for any other command, and never fails the tool call."""
import csv, json, os, re, sys, unicodedata

ROWS, WIDTH = 10, 40
SAVED = re.compile(r"saved → (.+?\.(?:csv|json))\s")
SCRIPT = re.compile(r"\b(?:scrape|leads)\.py\b")


def cell(v):
    """One line of plain text: no control, format or bidi characters (no terminal escapes), at most WIDTH."""
    s = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in str(v or ""))
    s = " ".join(s.split()).lstrip("'")
    return s if len(s) <= WIDTH else s[:WIDTH - 1] + "…"


def rows_of(path):
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def line(i, r):
    """`3. Name · phone · email · website · 4.8★ (212)`: one row per line, readable in any font."""
    rating = cell(r.get("review_rating"))
    parts = [cell(r.get(k)) for k in ("title", "phone", "emails", "website")]
    if rating:
        parts.append(f"{rating}★ ({cell(r.get('review_count')) or 0})")
    return f"{i}. " + " · ".join(p for p in parts if p)


def preview(path):
    rows = rows_of(path)
    head = f"Leads preview: first {min(ROWS, len(rows))} of {len(rows)} rows in {os.path.basename(path)}"
    return "\n".join([head] + [line(i, r) for i, r in enumerate(rows[:ROWS], 1)])


def strings(v):
    """Every string inside the tool result: Claude Code puts stdout in `stdout`, Gemini CLI in
    `llmContent` / `returnDisplay`, and Codex's shape is not documented."""
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        v = list(v.values())
    return [s for x in v for s in strings(x)] if isinstance(v, list) else []


def main():
    event = json.load(sys.stdin)
    if not SCRIPT.search(json.dumps(event.get("tool_input") or "")):
        return
    text = "\n".join(strings(event.get("tool_response")))
    paths = [p if os.path.isabs(p) else os.path.join(event.get("cwd") or "", p)
             for p in dict.fromkeys(SAVED.findall(text + "\n"))]
    shown = [preview(p) for p in paths if os.path.isfile(p)]
    if shown:
        note = "(shown to you only; the AI agent does not see these rows)"
        print(json.dumps({"systemMessage": "\n\n".join(shown) + "\n" + note}))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a preview must never break or block the scrape the user just ran
        pass
