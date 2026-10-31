# Credits and attribution

This page lists who built what in the Google Maps Scraper Kit, and the licenses of the parts it uses.

The kit, its Google Maps engine (`engine/`), the scripts, the skill and the slash commands are designed
and built by Mahan ([@Mahanaicoach](https://github.com/Mahanaicoach)), MIT licensed (see [LICENSE](LICENSE)).

## History: gosom/google-maps-scraper

Kit versions 1.x ran [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper) by
Georgios Komninos ([@gosom](https://github.com/gosom)), MIT License, Copyright (c) 2023 Georgios
Komninos, as its official Docker image, unmodified. That engine shaped this kit: the job API
(`/api/v1/jobs`, the job body, the `Status` values) and the 36 CSV column names are its design, kept
so that scripts and files made for kit 1.x keep working. The kit's own engine contains none of its
code. To support its author, star the repository: https://github.com/gosom/google-maps-scraper

## Chrome for Testing

On a machine with no Chrome, Chromium, Edge or Brave, `scripts/engine.py start` downloads Google's
[Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) headless shell, pinned by
version and SHA-256 in `engine/browser.py`. It is Google's build of Chrome, made from the open-source
Chromium project. This repository does not contain or redistribute it: it comes from Google's servers
straight into `.engine/` on your machine, under Google's terms for Chrome (its `ABOUT` file) and the
open-source licenses listed in its `LICENSE.headless_shell` file. The Docker image installs Debian's
`chromium` package instead, under Chromium's BSD-style license and the notices Debian ships with it.

## Website signals adapted from OpenLeadFinder

The `--tech` detection patterns, the intent score and the obfuscated-email patterns in
`scripts/scrape.py`, and its private-address guard, are adapted from OpenLeadFinder, Mahan's
self-hosted lead finder. It is released under the MIT License, Copyright (c) 2026 OpenLeadFinder
contributors, with the license text below.

## MIT License text (gosom/google-maps-scraper, OpenLeadFinder)

```
MIT License

Copyright (c) 2023 Georgios Komninos
Copyright (c) 2026 OpenLeadFinder contributors

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
