# Setup guide: Google Maps Scraper Kit

This guide takes you from a fresh machine to a finished Google Maps lead list in about 5 minutes.
Most of that time is Docker pulling the engine image. A person or an agent can follow it step by step.

---

## 0. Install the prerequisites

Install these once:

| Tool | Why | Get it |
|---|---|---|
| **Docker Desktop** | runs the scraper container | https://www.docker.com/products/docker-desktop |
| **Python 3** | runs `scrape.py` and `scrape.sh` (standard library only, no `pip install`) | https://www.python.org/downloads |
| **Claude Code** *(optional but recommended)* | lets Claude drive the scraper for you | https://claude.com/claude-code |
| **git** *(optional)* | only if you'll push this to GitHub | https://git-scm.com |

Check that Docker is running:
```bash
docker --version        # should print a version
docker ps               # should NOT error (means the daemon is up)
```

> **Platform notes:**
> - **Apple Silicon (M1/M2/M3):** from v1.18.0 the image publishes an arm64 build, so it runs
>   natively and no `platform` line is needed. Only older tags are amd64 and run under emulation.
> - **Windows:** use `scripts/scrape.py` (run with `py` or `python3`). The bash script `scrape.sh` needs
>   **WSL2** or **Git Bash**.

---

## 1. Get the kit

If someone shared the folder with you, `cd` into it:
```bash
cd google-maps-scraper-kit
```
To clone it from GitHub instead: `git clone https://github.com/Mahanaicoach/google-maps-scraper-kit.git && cd google-maps-scraper-kit`.

Create your local config. This step is optional, because the defaults work as they are:
```bash
cp .env.example .env
```

---

## 2. Start the engine

```bash
docker compose up -d
```

The first run pulls the `gosom/google-maps-scraper` image once, a few hundred MB. When it finishes, check it:

```bash
docker ps | grep gmaps-scraper          # should show it "Up"
curl http://localhost:8080/api/v1/jobs  # should print null (no jobs yet) or a JSON list of jobs
```

The engine also serves a web UI and API docs in the browser:
- Web UI: **http://localhost:8080**
- OpenAPI docs: **http://localhost:8080/api/docs**

If `curl` prints `null` or a JSON list, the engine is live.

> **Rate limits and the law.** The engine sends real requests to Google Maps. Light use is fine. Big
> jobs back to back, high `depth`, many keywords or scheduled runs without proxies can get your IP
> temporarily rate-limited by Google, for minutes to hours. Your Google account is not affected. Run
> one job at a time, start at `depth 5`, and add proxies for large or repeated runs. Scraping Google
> Maps is against Google's Terms of Service, and phones and emails are personal data, so follow GDPR,
> CCPA and CAN-SPAM — and LGPD if you operate in Brazil. Details: [Responsible use](README.md#responsible-use).

---

## 3. Run your first scrape from the terminal

Use the bash script. Its arguments are `"<keyword>" <lat> <lon> [depth]`:

```bash
./scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5
```

Or use the Python CLI, which needs no dependencies:

```bash
python3 scripts/scrape.py "gyms in Miami FL" 25.7617 -80.1918 --depth 5
```

Both scripts create the job, poll until it is done, download the CSV, keep the 8 lead fields and
print a short preview. The full results go to a file.

**Skip the coordinates.** Pass `--city` and the Python CLI geocodes the city for you:
```bash
python3 scripts/scrape.py "cafes in Austin TX" --city "Austin, TX" --depth 5
```

**Run many keywords in one job** from a file, one keyword per line:
```bash
python3 scripts/scrape.py --keywords-file examples/queries.example.txt --city "Denver, CO"
```

**Go past Google's ~120 results per search.** `--grid 3` splits the city into a 3 x 3 grid of map
tiles. That is 9 jobs, run one at a time, with duplicates removed:
```bash
python3 scripts/scrape.py "plumbers in Phoenix AZ" --city "Phoenix, AZ" --grid 3
```

---

## 4. Run it from Claude Code or another agent

The kit ships a Claude Code skill that teaches the agent the job API, the required fields, polling
and the guardrails. It works with Claude Code, Codex, Cursor and any other
[Agent Skills](https://agentskills.io)-compatible tool. Two files do the work:

1. **[CLAUDE.md](CLAUDE.md)** loads automatically when you open this folder in Claude Code. It tells
   the agent what the kit is and its key rules.
2. **[.claude/skills/google-maps-scraper/SKILL.md](.claude/skills/google-maps-scraper/SKILL.md)** is
   the skill. The agent loads it on demand for the exact API flow and best practices.

There is nothing to connect. The engine is a local HTTP API at `http://localhost:8080`, and the agent
calls it with `curl` and its normal shell access.

**Option A: open the folder in Claude Code.** The skill and four slash commands load automatically.

```bash
cd google-maps-scraper-kit
claude            # start Claude Code inside this folder
```

**Option B: install the skill into an agent you already use.**

```bash
npx skills add mahanaicoach/google-maps-scraper-kit
```

Then ask in plain language:
> "Make sure the scraper is running, then scrape **dentists in Denver CO**, depth 10, and give me a table of name, phone, website."

The agent checks the engine is up and starts it if not. It then builds the job with the required
`max_time`, `lat` and `lon`, polls in the background, downloads the CSV and hands you clean rows. It
follows the skill's safety rules on every run.

> **MCP is optional.** You can wrap the API as a Model Context Protocol server, but a local,
> single-user setup does not need one. The skill and the local API do the same job with less setup.

### Slash commands

Claude Code also offers these commands. Type `/` to see them:

| Command | What it does |
|---|---|
| `/scrape <business> in <city, ST> [depth]` | Run a scrape, get a clean table |
| `/scrape-batch <keywords-file> [--city "City, ST"]` | Scrape many queries in one job |
| `/scrape-setup` | Start the scraper + health check |
| `/scrape-jobs [list \| delete <id>]` | List or delete jobs |

Plain requests work too: *"scrape gyms in Miami with phone numbers."* The kit's local commands and
`localhost` calls are pre-approved in `.claude/settings.json`, so Claude Code does not prompt for
each one.

---

## 5. Stop and clean up

```bash
docker compose stop      # pause (keeps data + image)
docker compose down      # stop + remove container (keeps named volumes/data)
docker compose down -v   # ALSO delete scraped data volumes (full reset)
```

Results pile up in the data volume. Delete old jobs to free disk:
```bash
curl -X DELETE http://localhost:8080/api/v1/jobs/<job-id>
```

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `curl: connection refused` on :8080 | Container not up. `docker compose up -d`, then `docker ps`. |
| `port is already allocated` / something else already uses 8080 | Set `SCRAPER_PORT=8081` in `.env`, then `docker compose up -d`. Compose publishes the scraper on that host port and `scripts/scrape.sh` and `scripts/scrape.py` read the same file, so nothing else needs editing. The port inside the container stays 8080 (do not change the right-hand side of the mapping). If your `.env` still has a `SCRAPER_BASE_URL` line, delete it or update its port: it overrides `SCRAPER_PORT`. The slash commands and the skill show `localhost:8080` in their examples, so tell Claude the port you picked. |
| `422 missing max time` | Job body needs `max_time` (in **seconds**, e.g. `300`). |
| `422 missing geo coordinates` | Job body needs `lat` and `lon` as **strings**, e.g. `"30.2672"`. |
| Job stuck `working` forever | Engine v1.18.x sometimes never marks a finished job done ([gosom/google-maps-scraper#346](https://github.com/gosom/google-maps-scraper/issues/346)); `scrape.py` keeps the rows already written when its wait runs out, and `docker compose restart` frees the engine for the next job. Otherwise lower `depth`, or the IP is being throttled by Google — wait, or add proxies (see skill). |
| Scrapes feel slow | The kit runs the engine with `-c 4` (parallel workers) in `docker-compose.yml`; the engine's own default is 1. A depth 5 job with emails takes about 4 minutes. Most of that is visiting business websites for emails: `--no-email` is much faster. |
| Empty CSV | Keyword too narrow or wrong geo — widen `radius` or fix `lat`/`lon`. |
| Docker pull is slow | Normal on first run; it caches after that. |
| `docker: 'compose' is not a docker command` / `unknown shorthand flag: 'd'` | The Compose plugin is missing. It ships with Docker Desktop — uninstalling Desktop leaves a **dangling symlink** in `~/.docker/cli-plugins/` that looks installed but isn't (`ls -l ~/.docker/cli-plugins/docker-compose`). Install it (`brew install docker-compose`), or skip Compose entirely — see *No Compose?* below. |
| `no space left on device` while pulling | The **VM's** disk is full, not your Mac's — Colima/Lima/Docker Desktop each run a fixed-size virtual disk. Check with `docker system df`, reclaim with `docker image prune -a`, or grow the disk (Colima: `colima stop && colima start --disk 60`). The image bundles Chromium, so budget a few GB. |
| `could not install driver: … 404 … playwright.azureedge.net` | In Docker, you are on an old image: `v1.15.0` and earlier download a driver build that no longer exists. Use the pinned `v1.18.1` in `docker-compose.yml`, then `docker compose pull && docker compose up -d`. Running natively: Playwright's old Azure CDN is retired; as of Sept 2026 the replacement hosts 400 too. Workaround under *Running without Docker* below. |


### No Compose? Run the same container with `docker run`

This command runs the engine alone, with the localhost binding. It skips the `ui` proxy from
`docker-compose.yml`, so the web UI shows upstream's links and keeps the engine's job-list bug (Start
Scraping does nothing, [gosom/google-maps-scraper#337](https://github.com/gosom/google-maps-scraper/issues/337)).
It also skips the proxy's Host and Origin checks, so a web page open in your browser could reach the
engine through DNS rebinding or a cross-site post. Stop it when you are not scraping. The API and both
scripts work the same:

```bash
docker run -d --name gmaps-scraper --restart unless-stopped \
  -p 127.0.0.1:8080:8080 \
  -v gmaps_data:/gmapsdata \
  gosom/google-maps-scraper:v1.18.1 -web -data-folder /gmapsdata -c 4
```

Stop it with `docker stop gmaps-scraper`.

### Running without Docker (fallback)

Use this when Docker is unavailable. The engine
is a single Go binary. It serves the **same API on the same port**, so every script and slash command
in the kit works unchanged.

```bash
go install github.com/gosom/google-maps-scraper@v1.18.1
google-maps-scraper -web -addr 127.0.0.1:8080 -data-folder ./gmapsdata
```

> Keep `-addr 127.0.0.1:8080`. The binary defaults to `:8080`, which means **all interfaces**, and the
> engine has no authentication. In Docker the port mapping provides the localhost binding. Here you
> provide it.

**If the first job dies with `could not install driver: … 404`:** `playwright-go` fetches its driver
from `playwright.azureedge.net`, which Microsoft retired. The replacement hosts currently answer `400`
for driver zips, though browser downloads still work. The driver is the `playwright-core` npm package
plus a `node` binary, so you can assemble it by hand. Match the version the error names:

```bash
V=1.57.0                                   # whatever version the error asks for
D=~/Library/Caches/ms-playwright-go/$V     # Linux: ~/.cache/ms-playwright-go/$V
npm pack playwright-core@$V && tar xzf playwright-core-$V.tgz
mkdir -p "$D" && cp -R package "$D/package" && cp "$(command -v node)" "$D/node"
"$D/node" "$D/package/cli.js" --version    # should print: Version <V>
```

Restart the engine. It finds the driver, downloads Chromium normally, and runs.

---

## 7. Best practices

The skill holds the full rules. These are the essentials:
- Always send `max_time` (seconds) and `lat`/`lon` (strings).
- Start at `depth 5` and run one job at a time. Raise depth only when needed.
- Keep email extraction on, the default, because emails are the key lead field. Use `--no-email` for a
  faster run without them.
- Add proxies for big jobs. The engine rotates them for you.
- Never commit result files. They are git-ignored for you.
