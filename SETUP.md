# Setup guide: Google Maps Scraper Kit

This guide takes you from a fresh machine to a finished Google Maps lead list in about 10 minutes.
The engine starts in seconds; most of the time is the first download of Python, Chrome or Docker if
you have none. A person or an agent can follow it step by step.

---

## 0. Install the prerequisites

Install these once:

| Tool | Why | Get it |
|---|---|---|
| **Python 3.9 or newer** | runs the engine and the scripts (standard library only, no `pip install`) | https://www.python.org/downloads |
| **Google Chrome** *(or Chromium, Edge or Brave)* | the engine drives it, headless | https://www.google.com/chrome |
| **Docker Desktop** *(optional)* | runs the engine in a container instead, with its own Chromium | https://www.docker.com/products/docker-desktop |
| **Claude Code** *(optional but recommended)* | lets Claude drive the scraper for you | https://claude.com/claude-code |
| **git** | clones the kit and runs `scripts/update.sh` later | https://git-scm.com |

No browser and no wish to install one? Skip it: the first `python3 scripts/engine.py start` downloads
Google's Chrome for Testing once, about 100 MB, into the kit's `.engine/` folder, and runs it only if
it matches the SHA-256 pinned in `engine/browser.py`.

Check Python:
```bash
python3 --version       # Windows: py --version. Should print 3.9 or newer
```

> **Platform notes:**
> - **Windows:** when you install Python, tick **Add python.exe to PATH**, and type `py` wherever this
>   guide says `python3`. The bash script `scrape.sh` needs **WSL2** or **Git Bash**. Windows on Arm
>   runs the x64 Chrome for Testing under emulation.
> - **macOS:** the first `git` or `python3` command may ask to install the Command Line Tools. Click
>   Install, wait, then run the command again. Apple Silicon and Intel both run natively.
> - **Linux:** Ubuntu's snap Chromium cannot be driven this way, so the engine skips it and uses
>   Google Chrome, a non-snap Chromium or the Chrome for Testing download. On a bare server the engine
>   prints the one `apt-get install` line Chrome's libraries need. Ubuntu 23.10 and newer block
>   Chrome's sandbox for a browser outside `/opt` (AppArmor); the engine then runs it without one.

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

Pick one way. Both run the same engine on the same port, with the same API and web UI.

**A. Without Docker** (Python and Chrome on this machine):

```bash
python3 scripts/engine.py start
```

It prints the browser it found and `✓ Engine running: http://localhost:8080`. The engine keeps its
jobs, results and log in `.engine/` inside the kit folder. The other commands:

```bash
python3 scripts/engine.py status    # is it running, and with which browser
python3 scripts/engine.py restart   # after an update, or a change in .env
python3 scripts/engine.py stop
```

It does not start again after a reboot by itself: run `start` when you want to scrape.

A job the engine was stopped in the middle of (`stop` or `restart`, `docker compose restart` or
`stop`, a crash, a reboot) carries on from its last finished keyword when the engine starts again,
with the rest of its `max_time`. The keyword that was cut short is searched again. If the engine
crashes or is killed during the same job more than 3 times, the job is marked failed instead, with
its rows kept. `scripts/scrape.py` keeps polling through a short engine restart (about 1 minute).

**B. With Docker** (Docker Desktop installed and open):

```bash
docker compose up -d
```

The first run builds the engine image from the `Dockerfile`: Python plus Debian's Chromium, about
300 MB to download and 1.2 GB on disk, about a minute. Later starts take seconds, and the engine
restarts with Docker. Check it with `docker compose ps`.

**Then check it**, either way:

```bash
curl http://localhost:8080/api/v1/jobs  # should print null (no jobs yet) or a JSON list of jobs
```

If `curl` prints `null` or a JSON list, the engine is live. It also serves a web UI and API docs in the
browser:
- Web UI: **http://localhost:8080**
- API docs: **http://localhost:8080/api/docs** (a plain page; the OpenAPI JSON is at
  http://localhost:8080/api/openapi.json). Neither needs the API key.

The page has a form (keywords, latitude, longitude, depth, language, country, time limit, emails)
and a job list with each job's progress and CSV, JSON and Delete buttons. A job's error, if any,
shows under its name. Its "Add a business type" box suggests Google's business category names
(4,053, US English); pick one and it is added as a keyword line. Your browser loads that list from
GitHub and keeps it for 30 days. The page downloads the engine's raw rows, the rows so far while a
job works. The scripts give you the cleaned 8 lead fields and find more emails.

> **Rate limits and the law.** The engine sends real requests to Google Maps. Light use is fine. Big
> jobs back to back, high `depth`, many keywords or scheduled runs without proxies can get your IP
> temporarily rate-limited by Google, for minutes to hours. Your Google account is not affected. Run
> one job at a time, start at `depth 5`, and add proxies for large or repeated runs. Scraping Google
> Maps is against Google's Terms of Service, and phones and emails are personal data, so follow GDPR,
> CCPA and CAN-SPAM — and LGPD if you operate in Brazil. Details: [Responsible use](README.md#responsible-use).

---

## 3. Run your first scrape from the terminal

Use the Python CLI, which needs no dependencies:

```bash
python3 scripts/scrape.py "gyms in Miami FL" 25.7617 -80.1918 --depth 5
```

Or the bash script. Its arguments are `"<keyword>" <lat> <lon> [depth]`:

```bash
bash scripts/scrape.sh "coffee shops in Austin TX" 30.2672 -97.7431 5
```

Both scripts create the job, poll until it is done, download the CSV, keep the 8 lead fields and
print a short preview. The Python CLI also prints the job's progress while it polls, e.g.
`keyword 2/3, 41 places, 50%`. The full results go to a file. Each `depth` step loads one more batch of about
20 places, so depth 5 returns up to about 100, near Google's cap of about 120 per search.

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

**Scraping another country?** `--lang` sets the interface language (category names, hours text),
and `--country` the 2-letter country Google ranks and formats results for (default: the country of
your IP address): `--lang de --country de`.

### Review counts, hours and reviews: window mode

By default `review_count`, `open_hours` and `reviews_link` stay empty: signed out, Google shows a
headless browser its "limited view", with no review counts, and the kit does not disguise its
browser. `--min-reviews` is then ignored with a warning; `--min-rating` works.

To fill them, run Chrome as a normal window. Add this line to `.env` (or set it in the environment),
then restart the engine with `python3 scripts/engine.py restart`:

```bash
SCRAPER_HEADLESS=0
```

Chrome then opens as a window placed off-screen. Google gives a normal window its full view, so
`review_count`, `open_hours` (the week, as JSON like `{"Monday": ["7 AM–9 PM"], ...}`) and
`reviews_link` are filled, and `--min-reviews` works. No user agent is changed. The costs:

- It needs a desktop with a display. Docker's image stays headless, and a server without a display
  cannot run it.
- It needs an installed Chrome, Chromium, Edge or Brave. The downloaded headless shell has no window.
- On macOS a second Chrome icon shows in the Dock, and the window may take focus when a job starts.

Review texts come only through the raw API and in window mode: set `"reviews_max"` (0 to 5) in the
job body, and the engine opens each place's panel, one page load per place (about a second each).
It fills `user_reviews` (a JSON list of `{"Rating", "When", "Description"}`; reviewer names and
photos are left out on purpose) and `reviews_per_rating` (`{"1": n, ... "5": n}`). A headless Chrome
gets no reviews, and the job's `Error` says so. `scripts/scrape.py` has no flag for reviews.

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

The engine ships with the kit, so with Option B install the kit too (step 1) and start the engine
(step 2), or the agent asks you to.

Then ask in plain language:
> "Make sure the scraper is running, then scrape **dentists in Denver CO**, depth 5, and give me a table of name, phone, website."

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

Without Docker:
```bash
python3 scripts/engine.py stop   # jobs and results stay in .engine/data
```
To remove everything the engine kept (jobs, results, its log and a downloaded Chrome), stop it and
delete the `.engine` folder.

With Docker:
```bash
docker compose stop      # pause (keeps data + image)
docker compose down      # stop + remove container (keeps the data volume)
docker compose down -v   # ALSO delete the jobs and results volume (full reset)
```

Results pile up. Delete old jobs with the web UI's Delete button (it asks you to confirm), or:
```bash
curl -X DELETE http://localhost:8080/api/v1/jobs/<job-id>
```

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `curl: connection refused` on :8080, or `Scraper not reachable` | The engine is not running. `python3 scripts/engine.py start` (or `docker compose up -d`), then `python3 scripts/engine.py status`. |
| `✗ Port 8080 is in use` from `engine.py start` | Something else holds the port, often kit 1.x's Docker engine. Stop it (`docker compose stop` in that copy's folder), or set `SCRAPER_PORT=8081` in `.env` and start again. |
| `Cannot connect to the Docker daemon` / `docker: command not found` | Docker Desktop is not running (or not installed). Open Docker Desktop, wait until its whale icon stops animating, then run `docker compose up -d` again. Or use the engine without Docker (§2 A). |
| `Empty reply from server` or `Connection reset` right after `docker compose up -d` | The engine is still starting, a few seconds. The scripts wait for it; in the browser, reload the page. |
| `Permission denied` running `./scripts/scrape.sh` | The file lost its execute bit (a ZIP download does that). Run `bash scripts/scrape.sh …` instead. |
| `port is already allocated` / something else already uses 8080 | Just updated from kit 1.x? Its `ui` container still holds the port: run `docker compose up -d --remove-orphans`. Otherwise set `SCRAPER_PORT=8081` in `.env`, then start the engine again (`python3 scripts/engine.py restart` or `docker compose up -d`). The engine, Compose and both scripts read the same file, so nothing else needs editing. With Docker the port inside the container stays 8080 (do not change the right-hand side of the mapping). If your `.env` still has a `SCRAPER_BASE_URL` line, delete it or update its port: it overrides `SCRAPER_PORT`. The slash commands and the skill show `localhost:8080` in their examples, so tell Claude the port you picked; its pre-approved `curl` rules are pinned to 8080 too, so add `Bash(curl http://localhost:8081/:*)` to `.claude/settings.json` if you want it to stop asking. **A second copy of the kit on the same machine** (a fork next to the original, a test clone): change `SCRAPER_PORT` in its `.env`. Without Docker that is all, because each copy keeps its own `.engine/` folder. With Docker, also set `COMPOSE_PROJECT_NAME` to a name nobody else uses (`docker compose ls` lists them), as `.env.example` shows; with the default name, `docker compose up -d` takes over the other copy's container. |
| `422 missing max time` | Job body needs `max_time` (in **seconds**, e.g. `300`). |
| `422 missing geo coordinates` | Job body needs `lat` and `lon` as **strings**, e.g. `"30.2672"`. |
| `422 missing name` / `422 missing lang` | Job body needs `name` (any label) and `lang` (`"en"`). All six required fields: `name`, `keywords`, `lang`, `lat`, `lon`, `max_time`. |
| `422 invalid proxy: Chrome cannot log in to a SOCKS5 proxy` | Chrome takes no username and password on SOCKS5. Use the provider's `http://user:pass@host:port` address (URL-encode special characters), or a SOCKS5 proxy that allows your IP address. |
| A job `failed` | Its `Error` says why: the web UI shows it under the job name, and `curl http://localhost:8080/api/v1/jobs/<id>` returns it. `scripts/scrape.py` prints it, and retries once after 60 s unless a retry cannot help. `Chrome cannot start … error while loading shared libraries`: run the `apt-get install` line in the message. `No Chrome, Chromium, Edge or Brave found`: run `python3 scripts/engine.py start`, which downloads one. `unusual-traffic page`: Google is rate-limiting your IP. The engine already waited and retried the keyword (`Waited and retried N time(s)`) and kept the rows found before that; wait, or add proxies. An error with `ERR_PROXY`, `ERR_SOCKS` or `ERR_TUNNEL`: the proxy is wrong or down. `proxy login refused: the proxy answered 407 to its username and password`: the proxy's username or password is wrong; `scripts/scrape.py` does not retry it. `the engine stopped while this job ran`: the engine crashed or was killed during this job more than 3 times; the rows it found are kept. |
| `The engine says: stopped at max_time` | The job ran out of time and kept the rows it had. Raise `--max-time` (seconds, default 600) or lower `--depth`. |
| `website(s) not checked: ran out of time` or `reviews read for N of M places: ran out of time` | A raw-API job's website or reviews pass stopped at the job's time limit; the rows are complete, those columns are not. Raise `max_time`. |
| `--min-reviews ignored` | Expected by default: `review_count` is empty while Chrome runs headless. Use `--min-rating`, or window mode (§3) to fill `review_count`. |
| Scrapes feel slow | Google Maps itself is quick: a depth 2 search takes about 5 seconds. Most of a run is the email crawl, which checks every business website, 16 at a time. `--no-email` skips it. The web UI's email option reads homepages only. |
| Empty CSV | Keyword too narrow or wrong coordinates. Check `lat`/`lon` (latitude first) and try a broader keyword. |
| Docker build is slow | Normal on the first run: it downloads Python and Chromium once, then caches them. |
| `docker: 'compose' is not a docker command` / `unknown shorthand flag: 'd'` | The Compose plugin is missing. It ships with Docker Desktop — uninstalling Desktop leaves a **dangling symlink** in `~/.docker/cli-plugins/` that looks installed but isn't (`ls -l ~/.docker/cli-plugins/docker-compose`). Install it (`brew install docker-compose`), or skip Docker and use §2 A. |
| `no space left on device` while building | The **VM's** disk is full, not your Mac's — Colima/Lima/Docker Desktop each run a fixed-size virtual disk. Check with `docker system df`, reclaim with `docker image prune -a`, or grow the disk (Colima: `colima stop && colima start --disk 60`). The image bundles Chromium, so budget a few GB. |

### Upgrading from kit 1.x (the gosom engine)

The new engine cannot read 1.x jobs, so first download the CSVs you still need from the old web UI
at http://localhost:8080. Then `bash scripts/update.sh` pulls the new kit and, if the old Docker
engine is running, rebuilds and restarts it with `docker compose up -d --build --remove-orphans`. By
hand, run that same command in the kit folder. `--remove-orphans` removes 1.x's `ui` container, which
would otherwise keep port 8080. The old jobs stay in the old `gmaps_data` volume, which
`docker volume ls` lists with your project's name in front; remove it with `docker volume rm` when
you no longer need it.

---

## 7. Best practices

The skill holds the full rules. These are the essentials:
- Always send `max_time` (seconds) and `lat`/`lon` (strings).
- Start at `depth 5` and run one job at a time. Raise depth only when needed.
- Keep email extraction on, the default, because emails are the key lead field. Use `--no-email` for a
  faster run without them.
- Add proxies for big jobs. With several, the keywords take turns, and a keyword Google refuses is
  retried through the next one.
- Never commit result files. They are git-ignored for you.
- Changed something? `python3 scripts/test_scrape.py` and `python3 engine/test_engine.py` run the
  offline checks in seconds, with no engine, no network and no Chrome.
