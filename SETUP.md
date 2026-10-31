# Setup guide: Google Maps Scraper Kit

This guide takes you from a fresh machine to a finished Google Maps lead list in about 10 minutes:
about a minute for Docker to download the engine, then a few minutes for the first scrape. A person or an agent can follow it step by step.

---

## 0. Install the prerequisites

Install these once:

| Tool | Why | Get it |
|---|---|---|
| **Docker Desktop** *(or skip it, see [Running without Docker](#running-without-docker))* | runs the scraper container | https://www.docker.com/products/docker-desktop |
| **Python 3** | runs `scrape.py` and `scrape.sh` (standard library only, no `pip install`) | https://www.python.org/downloads |
| **Claude Code** *(optional but recommended)* | lets Claude drive the scraper for you | https://claude.com/claude-code |
| **git** | clones the kit and runs `scripts/update.sh` later | https://git-scm.com |

Check that Docker is running:
```bash
docker --version        # should print a version
docker ps               # should NOT error (means the daemon is up)
```

> **Platform notes:**
> - **Apple Silicon (M1/M2/M3):** from v1.18.0 the image publishes an arm64 build, so it runs
>   natively and no `platform` line is needed. Only older tags are amd64 and run under emulation.
> - **Windows:** when you install Python, tick **Add python.exe to PATH**. Then use `scripts/scrape.py`
>   (run with `py`). The bash script `scrape.sh` needs **WSL2** or **Git Bash**.
> - **macOS:** the first `git` or `python3` command may ask to install the Command Line Tools. Click
>   Install, wait, then run the command again.

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

The first run pulls the `gosom/google-maps-scraper` image once: about 600 MB to download, 2.4 GB on disk. When it finishes, check it:

```bash
docker compose ps                       # both services should show "Up" or "running"
curl http://localhost:8080/api/v1/jobs  # should print null (no jobs yet) or a JSON list of jobs
```

The engine also serves a web UI and API docs in the browser:
- Web UI: **http://localhost:8080**
- OpenAPI docs: **http://localhost:8080/api/docs**

The page starts with emails on and depth 5, like the scripts. Type a keyword with its city, set the
latitude and longitude, and click Start Scraping. It downloads the engine's raw 36-column CSV. The
scripts give you the cleaned 8 lead fields and find more emails.

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
bash scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5
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

**Go past Google's ~20 results per search.** Signed-out Google Maps shows about 20 places per search,
so a search that comes back full is re-run as 4 closer searches on its own (5 jobs; `--no-split` keeps
one). `--grid 3` splits the city into a 3 x 3 grid of map tiles instead. That is 9 jobs, run one at a
time, with duplicates removed:
```bash
python3 scripts/scrape.py "plumbers in Phoenix AZ" --city "Phoenix, AZ" --grid 3
```

---

## 4. Run it from Claude Code or another agent

The kit ships a Claude Code skill that teaches the agent the job API, the required fields, polling
and the guardrails. It works with Claude Code, Codex, Cursor and any other
[Agent Skills](https://agentskills.io)-compatible tool. In this folder (Option A) two files do the work;
`npx skills add` (Option B) installs only the second:

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
`name`, `keywords`, `lang`, `lat`, `lon` and `max_time`, polls in the background, downloads the CSV and hands you clean rows. It
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
| `Cannot connect to the Docker daemon` / `docker: command not found` | Docker Desktop is not running (or not installed). Open Docker Desktop, wait until its whale icon stops animating, then run `docker compose up -d` again. |
| `502 Bad Gateway` right after `docker compose up -d` | The engine is still starting, about 10 seconds. The scripts wait for it; in the browser, reload the page. |
| `Permission denied` running `./scripts/scrape.sh` | The file lost its execute bit (a ZIP download does that). Run `bash scripts/scrape.sh …` instead. |
| `port is already allocated` / something else already uses 8080 | Set `SCRAPER_PORT=8081` in `.env`, then `docker compose up -d`. Compose publishes the scraper on that host port and `scripts/scrape.sh` and `scripts/scrape.py` read the same file, so nothing else needs editing. The port inside the container stays 8080 (do not change the right-hand side of the mapping). If your `.env` still has a `SCRAPER_BASE_URL` line, delete it or update its port: it overrides `SCRAPER_PORT`. The slash commands and the skill show `localhost:8080` in their examples, so tell Claude the port you picked; its pre-approved `curl` rules are pinned to 8080 too, so add `Bash(curl http://localhost:8081/:*)` to `.claude/settings.json` if you want it to stop asking. **A second copy of the kit on the same machine** (a fork next to the original, a test clone): change `SCRAPER_PORT` and set `COMPOSE_PROJECT_NAME` and `SCRAPER_CONTAINER` in `.env` to names nobody else uses on the machine (`docker ps` lists the names in use), as `.env.example` shows; with the defaults, `docker compose up -d` adopts the other copy's containers. |
| `422 missing max time` | Job body needs `max_time` (in **seconds**, e.g. `300`). |
| `422 missing geo coordinates` | Job body needs `lat` and `lon` as **strings**, e.g. `"30.2672"`. |
| `422 missing name` / `422 missing lang` | Job body needs `name` (any label) and `lang` (`"en"`). All six required fields: `name`, `keywords`, `lang`, `lat`, `lon`, `max_time`. |
| Job stuck `working` forever, or every new job stays `pending` | Engine v1.18.x sometimes finishes a job without marking it done ([gosom/google-maps-scraper#346](https://github.com/gosom/google-maps-scraper/issues/346)) and then holds every later job on `pending`. It is common: in our depth-1 tests it hit between one job in four and every other job. `scripts/scrape.py` spots it in the engine log within about 30 seconds, keeps the rows, restarts the engine and waits for it to come back. It also deletes the stuck job from the engine's list, because the web UI shows no Download button for a `working` job. If you hit this from the web UI or the API, fetch the rows with `curl -o results.csv http://localhost:8080/api/v1/jobs/<id>/download`, delete the job, then run `docker restart gmaps-scraper` (or the name you set in `SCRAPER_CONTAINER`). A new job that sits on `pending` for about 50 s while the engine shows no activity gets the same restart from `scripts/scrape.py`, which then re-creates the job, and a `failed` job is retried once after 60 s. A job that stays `working` with no rows can also mean Google is throttling your IP: wait, or add proxies. |
| Scrapes feel slow | One search with emails takes about a minute, and a search that comes back full runs 4 more (about 4 minutes in all; `--no-split` keeps it to one): the engine reads Google Maps with 4 workers (`-c 4` in `docker-compose.yml`; the engine's own default is 1), then the script checks the business websites for emails, 16 at a time. `--no-email` skips the website check. The web UI uses the engine's own email option, which is several times slower. |
| Empty CSV | Keyword too narrow or wrong geo — widen `radius` or fix `lat`/`lon`. |
| Docker pull is slow | Normal on first run; it caches after that. |
| `docker: 'compose' is not a docker command` / `unknown shorthand flag: 'd'` | The Compose plugin is missing. It ships with Docker Desktop — uninstalling Desktop leaves a **dangling symlink** in `~/.docker/cli-plugins/` that looks installed but isn't (`ls -l ~/.docker/cli-plugins/docker-compose`). Install it (`brew install docker-compose`), or skip Compose entirely — see *No Compose?* below. |
| `no space left on device` while pulling | The **VM's** disk is full, not your Mac's — Colima/Lima/Docker Desktop each run a fixed-size virtual disk. Check with `docker system df`, reclaim with `docker image prune -a`, or grow the disk (Colima: `colima stop && colima start --disk 60`). The image bundles Chromium, so budget a few GB. |
| `could not install driver: … 404 … playwright.azureedge.net` | In Docker, you are on an old image: `v1.15.0` and earlier download a driver build that no longer exists. Use the pinned `v1.18.1` in `docker-compose.yml`, then `docker compose pull && docker compose up -d`. Without Docker, `scripts/engine.py` uses the same v1.18.1 release, whose driver downloads normally. |


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

### Running without Docker

The engine also ships as a single program for macOS, Windows and Linux. `scripts/engine.py` runs it
for you, with nothing to install but Python:

```bash
python3 scripts/engine.py start     # instead of docker compose up -d
python3 scripts/engine.py status
python3 scripts/engine.py stop      # instead of docker compose stop
```

- **What it does.** The first `start` downloads the engine's v1.18.1 release build from its GitHub
  page (about 60 MB) and checks it against the SHA-256 pinned in `engine.py`; a mismatch is deleted,
  never run. It starts the engine on a private port and serves http://localhost:8080 through a small
  Python proxy with the same Host and Origin checks and page fixes as the Docker setup (it reads them
  from `ui-branding.conf`). Scripts, slash commands and the web UI use it the same way.
- **One difference from Docker.** Docker hides the engine inside its own network; here its private
  port is open on this machine (bound to 127.0.0.1 only, so not to your network). The engine has no
  password, so other programs on this computer, or a web page that finds the port, could reach it
  without the proxy's checks. Stop it when you are not scraping: `python3 scripts/engine.py stop`.
- **After a reboot** run `start` again; it cleans up after itself.
- **First scrape.** The engine downloads Chromium once, about 170 MB, into Playwright's cache
  (macOS `~/Library/Caches/ms-playwright`, Linux `~/.cache/ms-playwright`, Windows
  `%LOCALAPPDATA%\ms-playwright`).
- **Apple Silicon** runs the Intel build through Rosetta (`softwareupdate --install-rosetta` if
  macOS asks). On macOS the engine loses its browser at depth 3 or more
  ([gosom/google-maps-scraper#338](https://github.com/gosom/google-maps-scraper/issues/338)), so
  `scrape.py` uses depth 2 there. Google shows about 20 places per search either way; the kit's split
  searches do the rest.
- **Not supported:** Linux on arm64 (no engine build). Use Docker there.
- **Stuck jobs** ([gosom/google-maps-scraper#346](https://github.com/gosom/google-maps-scraper/issues/346))
  are spotted in `.engine/engine.log` and fixed with `python3 scripts/engine.py restart`, the same way
  the Docker setup uses `docker restart`.
- **Remove it:** `python3 scripts/engine.py stop`, then delete the `.engine` folder (engine, logs, job
  data) and, if you like, the Playwright cache above.
- **Port 8080 taken by the Docker engine?** Stop one of them, or set `SCRAPER_PORT` in `.env`.

---

## 7. Best practices

The skill holds the full rules. These are the essentials:
- Always send `max_time` (seconds) and `lat`/`lon` (strings).
- Start at `depth 5` and run one job at a time. Raise depth only when needed.
- Keep email extraction on, the default, because emails are the key lead field. Use `--no-email` for a
  faster run without them.
- Add proxies for big jobs. The engine rotates them for you.
- Never commit result files. They are git-ignored for you.
- Changed something? `python3 scripts/test_scrape.py` runs the offline checks in about a second, no engine needed.
