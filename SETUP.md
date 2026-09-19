# SETUP — Google Maps Scraper Kit

This guide sets up the whole system **on your computer** from scratch. It's written so that a person
*or* Claude can follow it exactly. Total time: ~5 minutes (most of it is Docker pulling the image).

---

## 0. Prerequisites

Install these first (one-time):

| Tool | Why | Get it |
|---|---|---|
| **Docker Desktop** | runs the scraper container | https://www.docker.com/products/docker-desktop |
| **Claude Code** *(optional but recommended)* | lets Claude drive the scraper for you | https://claude.com/claude-code |
| **git** *(optional)* | only if you'll push this to GitHub | https://git-scm.com |

Verify Docker is running:
```bash
docker --version        # should print a version
docker ps               # should NOT error (means the daemon is up)
```

> **Platform notes:**
> - **Apple Silicon (M1/M2/M3):** the scraper image is `linux/amd64` and runs under emulation. If the
>   container won't start, uncomment the `platform: linux/amd64` line in `docker-compose.yml`.
> - **Windows:** use `scripts/scrape.py` (run with `py` or `python3`). The bash script `scrape.sh` needs
>   **WSL2** or **Git Bash**.

---

## 1. Get the kit

If someone shared this folder with you, just `cd` into it:
```bash
cd google-maps-scraper-kit
```
(Or clone it from GitHub if it's hosted: `git clone <repo-url> && cd google-maps-scraper-kit`.)

Create your local config (optional — defaults work as-is):
```bash
cp .env.example .env
```

---

## 2. Start the scraper

```bash
docker compose up -d
```

First run pulls the `gosom/google-maps-scraper` image (~once, a few hundred MB). When it finishes:

```bash
docker ps | grep gmaps-scraper          # should show it "Up"
curl http://localhost:8080/api/v1/jobs  # should print [] (empty list) or existing jobs
```

You can also open the built-in UI and API docs in a browser:
- Web UI: **http://localhost:8080**
- OpenAPI docs: **http://localhost:8080/api/docs**

✅ If `curl` returns a JSON array, the system is live.

> ⚠️ **Now that it's running — read this once.** This hits Google Maps for real. Light use is fine, but
> big jobs back-to-back, very high `depth`, many keywords, or scheduled runs **without proxies** can get
> your **IP temporarily rate-limited/blocked by Google** (clears in minutes–hours; jobs return empty/failed
> meanwhile). It does **not** ban your Google account. **Stay safe:** one job at a time, start at `depth 5`,
> and add **proxies** for large or repeated runs. Scraping Maps is against Google's ToS — use responsibly
> and follow data law (GDPR/CCPA) for any contact data. (Full guidance in README → *Rate limits, bans &
> responsible use*.)

---

## 3. Run your first scrape (without Claude)

Use the included script. Arguments: `"<keyword>" <lat> <lon> [depth]`

```bash
./scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5
```

or the Python version (no dependencies needed):

```bash
python3 scripts/scrape.py "gyms in Miami FL" 25.7617 -80.1918 --depth 5
```

The script will **create the job → poll until done → download the CSV → print a summary** and save
the full results to a file.

**Don't want to look up coordinates?** Let it geocode the city for you:
```bash
python3 scripts/scrape.py "cafes in Austin TX" --city "Austin, TX" --depth 5
```

**Scrape many queries at once** (one job, many keywords) from a file:
```bash
python3 scripts/scrape.py --keywords-file examples/queries.example.txt --city "Denver, CO"
```

---

## 4. Use it with Claude (the autopilot way) 🤖

This is the point of the kit. Two parts make it work:

1. **[CLAUDE.md](CLAUDE.md)** — auto-loaded when you open this folder in Claude Code. It tells Claude
   what this project is and the key rules.
2. **[.claude/skills/google-maps-scraper/SKILL.md](.claude/skills/google-maps-scraper/SKILL.md)** — a
   **skill** Claude loads on demand. It teaches Claude the exact API flow + best practices.

### How to connect it to Claude
There's nothing to "connect" — the scraper is a local HTTP API at `http://localhost:8080`, and Claude
calls it with its normal `curl`/Bash ability. Just:

```bash
cd google-maps-scraper-kit
claude            # start Claude Code inside this folder
```

Then talk to it naturally:
> "Make sure the scraper is running, then scrape **dentists in Denver CO**, depth 10, and give me a table of name, phone, website."

Claude will: check the container is up (start it if not) → build the correct job (with the required
`max_time`, `lat`, `lon`) → poll in the background → download → hand you clean rows. It follows the
safety/best-practice rules in the skill automatically.

> ℹ️ **Advanced (optional):** prefer a Model Context Protocol server instead of curl? You can wrap the
> API as an MCP tool, but it's unnecessary for a local single-user setup — the skill + local API is
> simpler and just as capable.

---

### Slash-command shortcuts
Inside Claude Code you can also use these commands (type `/` to see them):

| Command | What it does |
|---|---|
| `/scrape <business> in <city, ST> [depth]` | Run a scrape, get a clean table |
| `/scrape-batch <keywords-file> [--city "City, ST"]` | Scrape many queries in one job |
| `/scrape-setup` | Start the scraper + health check |
| `/scrape-jobs [list \| delete <id>]` | List or delete jobs |

…or just ask in plain English: *"scrape gyms in Miami with phone numbers."* (The scraper's commands and
`localhost` calls are pre-approved in `.claude/settings.json`, so Claude won't prompt for each one.)

---

## 5. Stop / clean up

```bash
docker compose stop      # pause (keeps data + image)
docker compose down      # stop + remove container (keeps named volumes/data)
docker compose down -v   # ALSO delete scraped data volumes (full reset)
```

Delete old jobs to free disk (results accumulate in the data volume):
```bash
curl -X DELETE http://localhost:8080/api/v1/jobs/<job-id>
```

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `curl: connection refused` on :8080 | Container not up. `docker compose up -d`, then `docker ps`. |
| `422 missing max time` | Job body needs `max_time` (in **seconds**, e.g. `300`). |
| `422 missing geo coordinates` | Job body needs `lat` and `lon` as **strings**, e.g. `"30.2672"`. |
| Job stuck `working` forever | Lower `depth`, or the IP is being throttled by Google — wait, or add proxies (see skill). |
| Empty CSV | Keyword too narrow or wrong geo — widen `radius` or fix `lat`/`lon`. |
| Docker pull is slow | Normal on first run; it caches after that. |
| `docker: 'compose' is not a docker command` / `unknown shorthand flag: 'd'` | The Compose plugin is missing. It ships with Docker Desktop — uninstalling Desktop leaves a **dangling symlink** in `~/.docker/cli-plugins/` that looks installed but isn't (`ls -l ~/.docker/cli-plugins/docker-compose`). Install it (`brew install docker-compose`), or skip Compose entirely — see *No Compose?* below. |
| `no space left on device` while pulling | The **VM's** disk is full, not your Mac's — Colima/Lima/Docker Desktop each run a fixed-size virtual disk. Check with `docker system df`, reclaim with `docker image prune -a`, or grow the disk (Colima: `colima stop && colima start --disk 60`). The image bundles Chromium, so budget a few GB. |
| `could not install driver: … 404 … playwright.azureedge.net` | Only hits you when running the scraper **natively** (not in Docker) — the Docker image has the driver baked in. Playwright's old Azure CDN is retired; as of Sept 2026 the replacement hosts 400 too. Workaround under *Running without Docker* below. |


### No Compose? Run the same container with `docker run`

`docker-compose.yml` is thin — this is the exact equivalent, localhost binding and all:

```bash
docker run -d --name gmaps-scraper --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v gmaps_data:/gmapsdata -v gmaps_cache:/opt \
  gosom/google-maps-scraper:v1.15.0 -web -data-folder /gmapsdata
```

Add `--platform linux/amd64` on Apple Silicon. Stop it with `docker stop gmaps-scraper`.

### Running without Docker (fallback)

If Docker is unavailable — or the image's amd64 emulation is too slow on Apple Silicon — run the
scraper natively. It's a single Go binary, and it serves the **same API on the same port**, so every
script and slash command in this kit works unchanged.

```bash
go install github.com/gosom/google-maps-scraper@v1.15.0
google-maps-scraper -web -addr 127.0.0.1:8080 -data-folder ./gmapsdata
```

> Keep `-addr 127.0.0.1:8080`. The binary defaults to `:8080` — **all interfaces** — and the scraper
> has no authentication. In Docker the localhost binding comes from the port mapping; here it's on you.

**If the first job dies with `could not install driver: … 404`:** `playwright-go` fetches its driver
from `playwright.azureedge.net`, which Microsoft retired; the replacement hosts currently answer `400`
for driver zips (browser downloads still work). That driver is just the `playwright-core` npm package
plus a `node` binary, so you can assemble it by hand — match the version the error names:

```bash
V=1.57.0                                   # whatever version the error asks for
D=~/Library/Caches/ms-playwright-go/$V     # Linux: ~/.cache/ms-playwright-go/$V
npm pack playwright-core@$V && tar xzf playwright-core-$V.tgz
mkdir -p "$D" && cp -R package "$D/package" && cp "$(command -v node)" "$D/node"
"$D/node" "$D/package/cli.js" --version    # should print: Version <V>
```

Restart the scraper; it finds the driver, downloads Chromium normally, and runs.

---

## 7. Best practices & safety (summary)

The full rules live in the skill, but the essentials:
- **Always** send `max_time` (seconds) and `lat`/`lon` (strings).
- Start with **low `depth`** (5) and **one job at a time**. Raise only when needed.
- Turn on **email extraction** only when you need emails — it's much slower.
- For big jobs, add **proxies** (the scraper has built-in rotation) to avoid IP blocks.
- Scraped **phones/emails are personal data** — comply with GDPR/CCPA/CAN-SPAM, and respect Google's ToS.
- Never commit result files (they're git-ignored for you).
