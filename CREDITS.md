# Credits and attribution

This page lists who built what in the Google Maps Scraper Kit, and the license that covers the engine.

The kit is designed and built by Mahan ([@Mahanaicoach](https://github.com/Mahanaicoach)).
One component, the Google Maps scraping engine, is open-source work by Georgios Komninos, credited below.

## Scraping engine

- **Project:** google-maps-scraper
- **Author:** **Georgios Komninos** ([@gosom](https://github.com/gosom))
- **Repository:** https://github.com/gosom/google-maps-scraper
- **License:** MIT License, Copyright (c) 2023 Georgios Komninos
- **Docker image used:** `gosom/google-maps-scraper`

The kit runs the engine's official Docker image, unmodified. Everything else in this repository is
original to the kit: local deployment, lead-field cleaning, CSV output, auto-geocoding, batch jobs,
social profile enrichment, and the Claude Code skill and slash commands.

## Website signals adapted from OpenLeadFinder

The `--tech` detection patterns, the intent score and the obfuscated-email patterns in
`scripts/scrape.py`, and its private-address guard, are adapted from OpenLeadFinder, Mahan's
self-hosted lead finder. It is released under the same MIT License, Copyright (c) 2026
OpenLeadFinder contributors; the license text is the one reproduced below.

## Engine license (reproduced as required by MIT)

```
MIT License

Copyright (c) 2023 Georgios Komninos

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

To support the engine's author, star the repository: https://github.com/gosom/google-maps-scraper
