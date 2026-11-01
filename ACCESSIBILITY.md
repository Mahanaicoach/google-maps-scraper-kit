# Accessibility

This page tells you how the Google Maps Scraper Kit works with assistive technology, and how to report a barrier.
The kit aims to be usable by everyone, including people who use screen readers, keyboard-only input,
or high-contrast and magnified displays.

## Commitment

- Every step works from the keyboard. The kit is driven by text: agent commands, a terminal,
  and plain files. There is no mouse-only interface.
- Output is plain text and CSV, which opens in any spreadsheet app or screen reader.
- Documentation is Markdown with real headings, lists, and tables, so it reads in order with
  assistive technology.
- Images in the docs carry descriptive alt text. No information is given by image or color alone.

## Supported environments

- **Operating systems:** macOS, Linux, and Windows (`scrape.py` directly, or `scrape.sh` under WSL2 or Git Bash), with Python and Chrome, or anywhere Docker runs.
- **Terminals:** any terminal, including those used with VoiceOver, NVDA, JAWS, and Orca.
- **Results:** any app that opens CSV, such as Excel, Numbers, LibreOffice Calc, or Google Sheets.

## Known limitations

- Docker Desktop's own interface is outside this project's control and may have accessibility gaps.
  Docker is optional (`python3 scripts/engine.py start` runs the engine without it), and every Docker
  step in [SETUP.md](SETUP.md) can be done from the command line instead.
- Scraped data comes from Google Maps as listed. The kit does not fix missing or unclear fields in
  the source listings.
- The README banner and badges are decorative. Their content is repeated in the text below them.

## Report a barrier

If something in the kit or its docs is hard to use with your setup,
[open an issue](https://github.com/Mahanaicoach/google-maps-scraper-kit/issues/new/choose).
Describe what you were trying to do, the assistive technology you use, and where you got stuck.
Accessibility reports are treated as bugs.
