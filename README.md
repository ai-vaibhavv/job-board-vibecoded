# Germany Research Job Alerts

Finds **Research Assistant, HiWi, Werkstudent, Research Intern and Master-thesis
positions across Germany** and sends newly discovered ones to a Discord channel.

Built for a Master's student looking for AI/ML/Data Science/Software/Robotics
research roles. It runs on your own machine (macOS, Linux or Windows), searches
on a schedule, remembers what it already sent, and only pings you about jobs it
has not shown you before.

```
🎓 3 new research positions in Germany

┌─ STUDENTISCHE HILFSKRAFT im Bereich Softwareentwicklung für Simulationen
│  🏛️ Fraunhofer FKIE   📍 Wachtberg   ⭐ 55/100   🔎 fraunhofer
│  🏷️ Matched: studentische hilfskraft
└─ Open job →
```

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Compliance and limitations (read this)](#2-compliance-and-limitations-read-this)
3. [Install Python](#3-install-python)
4. [Create a virtual environment](#4-create-a-virtual-environment)
5. [Install dependencies](#5-install-dependencies)
6. [Create a Discord webhook](#6-create-a-discord-webhook)
7. [Set up `.env`](#7-set-up-env)
8. [Configure the YAML files](#8-configure-the-yaml-files)
9. [Run your first dry run](#9-run-your-first-dry-run)
10. [Send a test message](#10-send-a-test-message)
11. [Run a real search](#11-run-a-real-search)
12. [Scheduling on Linux, macOS and Windows](#12-scheduling-on-linux-macos-and-windows)
13. [Adding a new source](#13-adding-a-new-source)
14. [Running tests and linting](#14-running-tests-and-linting)
15. [Troubleshooting](#15-troubleshooting)
16. [Privacy and secret management](#16-privacy-and-secret-management)
17. [Docker](#17-docker)
18. [Source status: what actually works](#18-source-status-what-actually-works)
19. [How it works](#19-how-it-works)

---

## 1. What it does

On every run it:

1. Searches all enabled job sources (concurrently, each isolated from the others).
2. Normalizes results into one shared `Job` model.
3. Filters using configurable positive/negative keywords (German **and** English).
4. Scores each job 0–100 with a stored, human-readable explanation.
5. Deduplicates against previous runs *and* within the run.
6. Saves everything to SQLite.
7. Sends **only new, relevant** jobs to Discord.
8. Marks jobs notified **only after Discord confirms delivery**.
9. Prints a run summary.

Roles it looks for: Research Assistant · Student Research Assistant · Research
Intern · Wissenschaftliche/Studentische Hilfskraft · HiWi · Werkstudent
Forschung · Master Thesis / Masterarbeit · AI/ML/Data Science/Robotics research
positions.

---

## 2. Compliance and limitations (read this)

This tool is deliberately **conservative**. It will not do anything that could
get your accounts banned or break a website's terms.

### LinkedIn

**It never logs in to LinkedIn.** There is no automated login, no CAPTCHA
solving, no cookie/session reuse, no browser fingerprint evasion, no proxy
rotation, and no scraping of pages that require authentication.

LinkedIn is covered as a **discovery-link source**: a legitimate search API is
asked for public job URLs, e.g.

```
site:linkedin.com/jobs/view ("research assistant" OR "research intern") Germany
```

and the resulting public links are stored and sent to you. The application
never contacts `linkedin.com` itself. Because a search result only carries a
title, a URL and a snippet, LinkedIn jobs have thinner metadata than jobs from
a real feed — that is the honest trade-off for staying compliant.

### Everything else

- `robots.txt` is respected on every request (`http.respect_robots`, on by default).
- Requests are rate-limited per domain and honour `Crawl-delay` and `Retry-After`.
- Every request identifies the app via a real User-Agent — **put your own contact
  info in `http.user_agent`** in `settings.yaml`.
- **academics.de is deliberately disabled.** Its `robots.txt` states: *"The use
  of robots or other automated means to access academics.de or collect or mine
  data without the express permission of academics.de is strictly prohibited."*
  It ships as `forbidden: true`, which hard-blocks fetching even if you set
  `enabled: true`. Do not enable it without written permission.
- No browser automation (Selenium/Playwright). Sites that require JavaScript are
  simply out of scope — see [§18](#18-source-status-what-actually-works).

### Limitations

- Only jobs from configured sources are found. It is not a search engine.
- Sites change their HTML; a source can silently stop matching. The run logs a
  warning when a source parses zero jobs, and `check-source` tells you why.
- Search-engine discovery needs an API key (free tiers exist). Without one, the
  app still works on RSS/HTML/JSON sources.

---

## 3. Install Python

Python **3.12 or newer** is required.

```bash
python3 --version
```

- **Linux (Debian/Ubuntu):** `sudo apt install python3 python3-venv python3-pip`
- **Linux (Arch):** `sudo pacman -S python`
- **macOS:** `brew install python@3.12`
- **Windows:** install from [python.org](https://www.python.org/downloads/) and
  tick **"Add python.exe to PATH"** during setup.

---

## 4. Create a virtual environment

A virtual environment keeps this project's packages out of your system Python.

**Linux / macOS**
```bash
cd "/path/to/Job Board"
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell)**
```powershell
cd "C:\path\to\Job Board"
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Your prompt now starts with `(.venv)`. Run `deactivate` to exit.

---

## 5. Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Drop `[dev]` if you do not want the test/lint tools.

Verify:
```bash
python -m job_alerts --version
```

---

## 6. Create a Discord webhook

A webhook is a private URL that lets this app post to one channel. It is free
and needs no bot.

1. Open Discord (desktop or web).
2. Create a server if you have none: **+** in the left sidebar → *Create My Own*.
3. Create a channel, e.g. `#job-alerts`.
4. **Right-click the channel → Edit Channel → Integrations → Webhooks → New Webhook.**
5. Name it (e.g. *Job Alerts*), pick the channel, click **Copy Webhook URL**.

You now have a URL like:
```
https://discord.com/api/webhooks/1234567890/AbCdEf-your-secret-token
```

> ⚠️ **Treat this URL like a password.** Anyone who has it can post to your
> channel. Never commit it or paste it into a screenshot. If it leaks, click
> **Delete Webhook** and make a new one.

---

## 7. Set up `.env`

```bash
cp .env.example .env
```

Open `.env` and paste your webhook:

```ini
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890/AbCdEf-your-secret-token
```

`.env` is already in `.gitignore`.

### Optional: LLM relevance assessment (strongly recommended)

Keyword matching is blunt. It cannot tell *"PhD students are welcome"* from
*"PhD required"*, cannot see that a page titled *"Studentische Hilfskraft …
Jobs"* is a search page rather than a job, and cannot know that
*"Sensordatenfusion"* is signal processing unless you listed that exact word.
An LLM reads each posting and judges all three.

Set **either or both** keys — both are free tiers with **no credit card**:

| Provider | `.env` key | Where | Role |
|---|---|---|---|
| **Google Gemini** | `GEMINI_API_KEY` | <https://aistudio.google.com/apikey> | Tried first |
| **Groq** | `GROQ_API_KEY` | <https://console.groq.com/keys> | Automatic fallback |

```ini
GEMINI_API_KEY=your-gemini-key
GROQ_API_KEY=your-groq-key
```

**The LLM is never a dependency.** The fallback is layered:

1. **Gemini rate-limits or errors** → Groq assesses that batch instead.
2. **Both fail, or a reply is garbled** → those jobs are scored by the keyword
   scorer.
3. **No keys at all, or `llm.enabled: false`** → the keyword path runs exactly
   as it did before.

A job is never dropped because an LLM was unavailable. The run summary tells you
what happened:

```
  LLM assessed    : 5
      ! gemini: gemini HTTP 429: quota exceeded
```

Tune it under `llm:` in `settings.yaml` — model names, `batch_size`,
`max_concurrency`, and `prefilter_with_keywords` (leave on to save quota).
`llm.min_score_to_notify` lets LLM-scored jobs use a different threshold from
keyword-scored ones, since the two are calibrated differently.

### Optional: search-engine discovery (needed for LinkedIn coverage)

Pick **one** provider and add its key. All are optional.

| Provider | `SEARCH_API_PROVIDER` | Where to get a key | Notes |
|---|---|---|---|
| **Tavily** ← recommended | `tavily` | <https://app.tavily.com/> | Free tier (~1,000 credits/month), **no credit card** |
| Brave Search | `brave` | <https://brave.com/search/api/> | Free tier, but **requires a credit card** at signup |
| Google Programmable Search | `google_cse` | <https://developers.google.com/custom-search/v1/overview> | Free quota; also needs `GOOGLE_CSE_ID` |
| SerpAPI | `serpapi` | <https://serpapi.com/> | Paid |
| Bing Web Search | `bing` | Azure portal | Paid |

```ini
SEARCH_API_PROVIDER=tavily
SEARCH_API_KEY=tvly-your-key-here
```

Without a key, the `search_discovery` source reports itself as **skipped** and
everything else runs normally.

---

## 8. Configure the YAML files

```bash
cp config/settings.example.yaml config/settings.yaml
cp config/sources.example.yaml config/sources.yaml
```

Both files are heavily commented. The knobs you will actually touch:

| Setting | Meaning |
|---|---|
| `scoring.min_score_to_notify` | Notify threshold (default **55**). Lower = more jobs, more noise. |
| `keywords.positive` | A job must match one of these. |
| `keywords.negative` | Rejects a job (with PhD nuance, below). |
| `keywords.topics` | Your fields of interest. **Scoring only — never rejects.** |
| `locations.all_germany` | `true` = accept anywhere in Germany. |
| `notifications.max_per_run` | Cap on jobs sent per run (default 10). |
| `scheduler.run_at` | Times to search (default `08:00`, `18:00` Europe/Berlin). |
| `http.user_agent` | **Put your own contact details here.** |

Check what the app actually loaded:
```bash
python -m job_alerts show-config
```
(Secrets are masked in that output.)

---

## 9. Run your first dry run

**This sends nothing, writes nothing, and needs no webhook.** Safest first step:

```bash
python -m job_alerts search --dry-run
```

You will see exactly what *would* be sent, plus a summary:

```
══ Run summary ══════════════════════════════════════════
  Duration        : 0.8s   (DRY RUN — nothing sent)
  Sources OK      : 2 (mock, fraunhofer)
  Sources skipped : 1
      - search_discovery: no search API key configured
  Candidates      : 33
  After dedup     : 32
  Passed filter   : 27
  Above threshold : 6
  Notified        : 0
═════════════════════════════════════════════════════════
```

The `mock` source is offline demo data proving the pipeline works. Turn it off
in `sources.yaml` (`enabled: false`) once you trust the real sources.

---

## 10. Send a test message

```bash
python -m job_alerts send-test
```

Expect `✅ Test message sent.` and a message in your channel. If not, see
[Troubleshooting](#15-troubleshooting).

---

## 11. Run a real search

```bash
python -m job_alerts search
```

Then inspect what it found:

```bash
python -m job_alerts list --new             # not yet notified
python -m job_alerts list --min-score 70
python -m job_alerts list --explain         # WHY each job scored what it did
python -m job_alerts stats
python -m job_alerts export --format csv > jobs.csv
python -m job_alerts export --format json --output jobs.json
```

`--explain` prints the score breakdown:
```
🆕 [ 80] Student Research Assistant – Distributed Systems
        TU Darmstadt · Darmstadt · via mock
           +30 exact target title match: 'student research assistant'
           +15 Master's students eligible: "master's student"
           +10 location match: 'Darmstadt'
           +10 topic in description: distributed systems
           +5 English-language posting
           +5 published within 7 days
```

### All commands

| Command | Purpose |
|---|---|
| `search` | One complete search now |
| `search --dry-run` | Print what would be sent; no Discord call, no DB write |
| `send-test` | Test the Discord webhook |
| `list [--new] [--min-score N] [--explain]` | List stored jobs |
| `stats` | Database statistics |
| `run-scheduler` | Stay running; search on schedule |
| `export --format csv\|json` | Export stored jobs |
| `check-source <name>` | Fetch one source and show what it parsed |
| `show-config` | Effective config, secrets masked |

---

## 12. Scheduling on Linux, macOS and Windows

**How often does it actually discover jobs?** Only when a search runs. Out of
the box that is *never* — you must either run `search` by hand, or start one of
the options below. The default schedule is **twice a day, 08:00 and 18:00
Europe/Berlin**, which suits HiWi postings (they do not churn hourly) and keeps
you inside the free API tiers. Change it in `scheduler.run_at`.

### Option A — the built-in scheduler

```bash
python -m job_alerts run-scheduler
```

Runs at `scheduler.run_at` (default 08:00 and 18:00 Europe/Berlin) until you
press Ctrl+C. Overlapping runs are prevented by a lock file.

### Option B — cron (Linux/macOS)

```bash
crontab -e
```

Add (use **absolute paths**, and quote the path since it contains a space):

```cron
0 8,18 * * * cd "/path/to/Job Board" && .venv/bin/python -m job_alerts search >> "/path/to/Job Board/data/cron.log" 2>&1
```

Cron runs with a minimal environment — always `cd` into the project first so
`.env` and `config/` are found.

### Option C — systemd timer (Linux)

`~/.config/systemd/user/job-alerts.service`
```ini
[Unit]
Description=Germany Research Job Alerts

[Service]
Type=oneshot
WorkingDirectory=/path/to/Job Board
ExecStart=/path/to/Job Board/.venv/bin/python -m job_alerts search
```

`~/.config/systemd/user/job-alerts.timer`
```ini
[Unit]
Description=Run job alerts at 08:00 and 18:00

[Timer]
OnCalendar=*-*-* 08,18:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now job-alerts.timer
systemctl --user list-timers job-alerts
```

### Option D — Windows Task Scheduler

1. Press `Win`, type **Task Scheduler**, open it.
2. **Create Task…** (not "Basic Task").
3. **General:** name it *Job Alerts*; tick *Run whether user is logged on or not*.
4. **Triggers → New:** *Daily*, start `08:00`, then *Repeat task every* `12 hours`
   for a duration of `1 day`. (Or add two separate triggers, 08:00 and 18:00.)
5. **Actions → New:**
   - Action: *Start a program*
   - Program/script: `C:\path\to\Job Board\.venv\Scripts\python.exe`
   - Add arguments: `-m job_alerts search`
   - **Start in:** `C:\path\to\Job Board`  ← required, or `.env` is not found
6. **Conditions:** untick *Start the task only if the computer is on AC power*
   if you use a laptop.
7. OK, and enter your Windows password if prompted.

Test it: right-click the task → **Run**, then check `python -m job_alerts stats`.

---

## 13. Adding a new source

Edit `config/sources.yaml`. No Python needed for the common cases.

### An RSS/Atom feed

```yaml
  - name: my_university
    type: rss
    enabled: true
    url: "https://www.uni-example.de/jobs/feed.xml"
    defaults:
      organization: "University of Example"
      country: Germany
```

### An HTML career page

Open the page, right-click a job → **Inspect**, and find the repeating element.

```yaml
  - name: my_institute
    type: html
    enabled: true
    url: "https://institute.de/jobs"
    selectors:
      item: "div.job-listing"      # the repeated element, one per job
      title: "h3.job-title"
      url: "a.job-link@href"       # @attr reads an attribute
      location: "span.location"
      organization: "span.dept"
      description: "div.summary"
      published_at: "time@datetime"
```

Only `item`, `title` and `url` are required. Everything is resolved relative to
`item`.

### A JSON API

```yaml
  - name: my_board
    type: json_api
    enabled: true
    url: "https://board.de/api/jobs?country=DE"
    items_path: "data.results"     # where the list lives; "" = top level
    field_map:
      title: "title"               # dotted paths; `[]` walks a list
      url: "links.self"
      organization: "employer.name"
      location: "location.city"
      published_at: "posted_at"
```

### Always verify before trusting it

```bash
python -m job_alerts check-source my_institute
```

This fetches **only** that source, checks robots.txt, stores nothing, sends
nothing, and prints what it parsed. If it shows real jobs, set `enabled: true`.

---

## 14. Running tests and linting

```bash
python -m pytest              # 292 tests, all offline
python -m pytest -v
python -m pytest tests/test_scoring.py -v

python -m ruff format .       # format
python -m ruff check .        # lint
python -m ruff check --fix .
```

No test touches a live website — every HTTP call is mocked.

---

## 15. Troubleshooting

**`Configuration file not found: config/settings.yaml`**
→ `cp config/settings.example.yaml config/settings.yaml`

**`DISCORD_WEBHOOK_URL is not set`**
→ `cp .env.example .env` and paste your webhook. Confirm with `show-config`.
Note `--dry-run` needs no webhook.

**`send-test` fails / `Discord rejected the message (HTTP 401/404)`**
→ The webhook was deleted or mistyped. Make a new one and update `.env`. Check
for a stray trailing space or quote marks around the URL.

**No jobs are sent, but the summary shows candidates**

Check where they dropped out:
```bash
python -m job_alerts search --dry-run --log-level DEBUG
python -m job_alerts list --explain --limit 5
```
- *Passed filter is 0* → your `keywords.positive` do not match. Widen them.
- *Above threshold is 0* → scores are below `min_score_to_notify`. See the box below.
- *Everything is a duplicate* → they were already sent. `list` shows them.

> #### Why real jobs sometimes score just under the threshold
>
> Career **list pages** (like Fraunhofer's) expose only a title and a location —
> no description. A job with no description cannot earn the description-based
> points (topic-in-description +10, Master's-eligible +15, English +5,
> recent +5). Its ceiling is:
>
> `30 (exact title) + 15 (topic in title) + 10 (location) = 55`
>
> which is *exactly* the default threshold. So a title-only job clears the bar
> **only if its title matches one of your `keywords.topics`**. This is why the
> German topic synonyms in `settings.yaml` matter so much — "Softwareentwicklung"
> must be in your topic list or every German-titled job silently scores 40.
>
> If you are missing jobs: add topics in your field (German **and** English),
> or lower `min_score_to_notify` to ~40 and accept more noise.

**A source suddenly returns 0 jobs**
→ The site changed its HTML. `python -m job_alerts check-source <name>`, then
fix the selectors.

**`robots.txt disallows ...`**
→ Working as intended. Do not bypass it.

**`another run is in progress (pid N)`**
→ A run is already going. If you are sure it crashed, delete `data/scheduler.lock`.

**Cron/Task Scheduler runs but nothing happens**
→ Almost always a working-directory problem. Use absolute paths and set the
working directory to the project root.

---

## 16. Privacy and secret management

- **All data stays on your machine.** The SQLite database (`data/jobs.db`)
  never leaves it. There is no telemetry and no analytics.
- **Secrets live only in `.env`**, which is gitignored. They are never written to
  YAML, never stored in the database, and never sent to Discord.
- **Logs are scrubbed.** A redaction filter masks webhook URLs, API keys,
  `Authorization` headers and cookies from every log line — including logs from
  libraries like httpx. Safe to paste a log when asking for help; still, skim it.
- **Outbound traffic** goes only to: the job sources you enable, your chosen
  search API (if configured), and `discord.com`.
- **If a secret leaks:** delete the Discord webhook and create a new one; rotate
  the search API key in the provider's console. Both are instant and free.
- **Before pushing to a public repo:** confirm `git status` does not list `.env`,
  `data/`, or `config/*.yaml`. `.gitignore` covers all three.

---

## 17. Docker

Persist the database through a volume — it is what remembers which jobs were
already sent.

```bash
cp .env.example .env    # fill in your webhook
cp config/settings.example.yaml config/settings.yaml
cp config/sources.example.yaml config/sources.yaml
```

**One-off search:**
```bash
docker compose run --rm job-alerts search --dry-run
docker compose run --rm job-alerts send-test
docker compose run --rm job-alerts search
```

**Keep the scheduler running:**
```bash
docker compose up -d
docker compose logs -f
docker compose down
```

**Plain Docker:**
```bash
docker build -t germany-research-job-alerts .
docker run --rm --env-file .env \
  -v job-alerts-data:/data \
  -v "$(pwd)/config:/app/config:ro" \
  germany-research-job-alerts search --dry-run
```

The image runs as a non-root user, is read-only at runtime, and uses
`TZ=Europe/Berlin`.

---

## 18. Source status: what actually works

Verified by live probing on **2026-07-15**. Websites change — re-check with
`check-source`.

| Source | Type | Status | Needs |
|---|---|---|---|
| `mock` | mock | ✅ **Fully functional** | Nothing. Offline demo data. |
| `fraunhofer` | html | ✅ **Fully functional, verified live** | Nothing. Enabled by default. |
| `search_discovery` | search_api | ✅ **Fully functional** (adapter + all 5 providers tested) | **An API key.** Skips itself without one. |
| RSS adapter | rss | ✅ **Fully functional** | A real feed URL — you supply it. |
| JSON adapter | json_api | ✅ **Fully functional** | An endpoint + `field_map` — you supply them. |
| HTML adapter | html | ✅ **Fully functional** | Correct CSS selectors — you supply them. |
| `euraxess_germany` | rss | ⚠️ **Disabled — no working URL known** | A real feed URL. |
| `max_planck` | html | ⚠️ **Disabled — cannot work statically** | A browser engine (out of scope). |
| `daad` | html | ⚠️ **Disabled — UNVERIFIED** | Verified selectors. |
| `academics_de` | html | ⛔ **FORBIDDEN — do not enable** | Written permission from academics.de. |

### Honest notes on the unverified ones

- **Fraunhofer** was verified end-to-end: `tr.data-row` matched 25 live job rows
  and every configured field resolved. It is enabled by default and is the one
  real source that works out of the box.
- **EURAXESS**: two plausible RSS endpoints were probed and **both returned 404**.
  Rather than ship a URL known not to work, `url` is left empty. Find the real
  feed, put it in `sources.yaml`, and run `check-source`.
- **Max Planck**: `mpg.de/jobboard` returns HTTP 200, but the listings are
  rendered by JavaScript — the served HTML contains only filter checkboxes. **No
  CSS selector can work here.** Use `search_discovery` (it already queries
  `site:mpg.de`) or add individual institutes' static pages.
- **DAAD**: not verified; mostly scholarships rather than HiWi roles.
- Everything marked UNVERIFIED ships **disabled**. Nothing here claims to work
  that was not actually run.

---

## 19. How it works

```
germany-research-job-alerts/
├── README.md
├── pyproject.toml            # deps, ruff + pytest config
├── Dockerfile / docker-compose.yml / .dockerignore
├── .env.example              # secrets template (copy to .env)
├── .gitignore
├── config/
│   ├── settings.example.yaml # keywords, scoring, schedule, HTTP politeness
│   └── sources.example.yaml  # the sources, with status markers
├── src/job_alerts/
│   ├── __main__.py           # python -m job_alerts
│   ├── cli.py                # argparse commands
│   ├── config.py             # pydantic-settings (env) + YAML models
│   ├── models.py             # Job, JobCandidate, SearchQuery, RunSummary
│   ├── database.py           # SQLite + migrations + dedup
│   ├── normalization.py      # URL/date/language normalization, identity
│   ├── filtering.py          # DE/EN keywords, the PhD nuance rule
│   ├── scoring.py            # explainable 0–100 score
│   ├── http.py               # robots.txt, rate limits, retries, caching
│   ├── logging_setup.py      # structured logs + secret redaction
│   ├── pipeline.py           # the run, start to finish
│   ├── scheduler.py          # APScheduler + overlap lock
│   ├── llm/                  # base, prompt, providers (gemini/groq), chain
│   ├── sources/              # base, rss, generic_html, search_api,
│   │                         #   research_sources (json), mock
│   └── notifications/        # base, discord
└── tests/                    # offline tests
```

`http.py` and `logging_setup.py` are additions to the structure in the brief.
They exist so that politeness (robots, rate limiting, retries) and secret
redaction are enforced **centrally** rather than re-implemented — correctly —
in every adapter.

### Deduplication

The thing that decides whether this tool is pleasant or spammy.

Identity is `source:source_job_id` when a source provides an id, otherwise a
stable hash of the normalized URL + title + organization + location. URLs are
normalized by stripping tracking parameters (`utm_*`, `trk`, `refId`, `gclid`,
…), unifying scheme/`www`/trailing slashes, and collapsing LinkedIn job URLs to
their canonical numeric id. So all of these are **one** job:

```
https://uni.de/jobs/hiwi-ml?utm_source=newsletter
http://www.uni.de/jobs/hiwi-ml/
https://uni.de/jobs/hiwi-ml#apply
```

A `UNIQUE` index on the normalized URL catches the same posting arriving from
two different sources with two different ids.

### Delivery safety

Jobs are **stored before** Discord is called, and marked notified **only after**
Discord returns 2xx — per batch. So a Discord outage loses nothing: the jobs sit
in the database as `new` and go out on the next run. Re-running a search never
resurrects an already-sent job.

### The PhD nuance rule

*"PhD students are also welcome to apply"* must **not** reject a HiWi role.
PhD-flavoured negative keywords only reject when the text shows a real
requirement (*"a completed PhD is required"*, *"abgeschlossene Promotion"*).
Configurable via `filtering.phd_requires_explicit_signal`.

---

## License

MIT. Use at your own risk, and be a good citizen of the sites you query.
