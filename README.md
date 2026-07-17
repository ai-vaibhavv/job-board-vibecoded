# LabScout

**Discover. Match. Research.** LabScout finds **university, lab and
research-institute opportunities** — HiWi, student / research / teaching assistant,
research internships, Bachelor/Master thesis, and PhD positions — **Germany-first,
in German and English** — scores each one, and either sends the good ones to Discord
or lets you browse and publish them from a web dashboard. A single instance can run
broad across all academic fields, or load the `core_ai` profile preset to stay narrow
(e.g. an AI/ML/CV/NLP/robotics-only view). Runs entirely on your own machine.

> The Python package keeps its historical name `job_alerts` for stability; the
> product is **LabScout**.

```
🎓 3 new research positions in Germany

┌─ STUDENTISCHE HILFSKRAFT im Bereich Softwareentwicklung für Simulationen
│  🏛️ Fraunhofer FKIE   📍 Wachtberg   ⭐ 55/100   🔎 fraunhofer
└─ Open job →
```

## What it does

On each run it searches every enabled source concurrently, normalizes the
results into one `Job` model, filters and scores them 0–100 (keywords **or** a
self-hosted LLM judge), deduplicates against history, stores everything in
SQLite, and sends **only new, relevant** jobs to Discord — marking them notified
only after Discord confirms delivery. A run summary is printed at the end.

- **Two ways to use it**: a headless CLI/scheduler, or a **React dashboard**
  (FastAPI + Vite) for browsing, translating, searching and hand-picking what to
  publish.
- **German → English**: the dashboard translates German postings on demand (via
  the LLM) and Discord always receives the English version.
- **Explainable scoring**: every score carries a stored, human-readable reason.
- **Compliant by design** (see below).

## Compliance

- **Never logs in to LinkedIn.** LinkedIn is covered only as a *discovery-link*
  source: a legitimate search API is asked for public `…/jobs/view/…` URLs; the
  app never contacts `linkedin.com` itself. Thinner metadata is the honest trade.
- `robots.txt` is respected, requests are rate-limited per domain, and every
  request carries a real User-Agent (**set your contact info in `settings.yaml`**).
- No browser automation. `academics.de` ships `forbidden: true` and is hard-blocked.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[api]"                # omit [api] for CLI-only

cp .env.example .env                    # add DISCORD_WEBHOOK_URL
cp config/settings.example.yaml config/settings.yaml
cp config/sources.example.yaml config/sources.yaml

python -m job_alerts search --dry-run   # safe first run, sends nothing
python -m job_alerts send-test          # check the Discord webhook
python -m job_alerts search             # real run
```

Secrets (webhook, API keys) live only in `.env`; everything else is in
`config/*.yaml`. A search API key is optional — without it the app still runs on
RSS/HTML sources. An optional self-hosted OpenAI-compatible LLM
(`llm.colab_base_url`) sharpens filtering and powers translation; without it the
keyword scorer is used.

## Commands

| Command | Purpose |
|---|---|
| `search [--dry-run]` | Run one full search now |
| `serve` | Launch the JSON API for the React dashboard (see below) |
| `send-test` | Test the Discord webhook |
| `list [--new] [--min-score N] [--explain]` | List stored jobs |
| `stats` | Database statistics |
| `run-scheduler` | Stay running; search on a schedule (08:00 / 18:00) |
| `export --format csv\|json` | Export stored jobs |
| `check-source <name>` | Fetch one source and show what it parsed |
| `show-config` | Effective config, secrets masked |

## Dashboard

A **React** SPA (Vite + TypeScript + Tailwind) over a thin **FastAPI** JSON layer.
A two-pane job board: filters, a scrollable list of job cards, and a detail panel.

```bash
python -m job_alerts serve              # JSON API on http://127.0.0.1:7860
cd frontend && npm install && npm run dev   # UI on http://localhost:5173 (proxies /api)
```

- **Browse & filter** all stored jobs; click a card for the full posting.
- **German jobs** show an English translation first (cached after the first
  open), original German collapsible below.
- **Publish per job** to Discord, in English — publishing a filtered-out or
  already-sent job asks for confirmation.
- **Search** turns keywords / topics / an uploaded resume into live queries and
  runs the pipeline as a **background task** (poll for progress), **storing only**
  (nothing auto-sent). "Hide" declutters your view without deleting.
- **No LLM startup gate**: browsing works immediately; only translation and new
  searches need the tunnel, and they degrade gracefully when it is down.
- Set `JOB_ALERTS_API_AUTH=user:pass` to require HTTP Basic on the write
  endpoints (publish / run-search / resume) before exposing the API beyond
  localhost.

## Docker

**One container, one command.** The image builds the React SPA and FastAPI serves
it alongside `/api` from a single process — no separate frontend server. The
dashboard is at http://localhost:7860. Config is mounted live (editing
`llm.colab_base_url` is picked up without a restart); the database persists in a
named volume.

```bash
docker compose up                          # dashboard on http://localhost:7860
docker compose --profile scheduler up -d   # also run scheduled searches
docker compose run --rm app send-test
```

> Local development still runs the API and Vite dev server separately (above) for
> hot-reload; Docker bundles them into the one image.

## How it works

`pipeline.py` runs the sequence search → normalize → filter → score → dedupe →
store → notify. **Identity** is `source:source_job_id`, or a stable hash of the
normalized URL/title/org/location; a `UNIQUE` index on the normalized URL
collapses the same posting arriving from two sources. **Delivery is safe**: jobs
are stored *before* Discord and marked notified only *after* a 2xx, so an outage
loses nothing. The **PhD nuance rule** means "PhD students welcome" never rejects
a HiWi role — only a stated requirement ("abgeschlossene Promotion") does.

Layout: `config.py` (pydantic settings), `models.py`, `database.py` (SQLite +
additive migrations), `filtering.py` / `scoring.py`, `llm/` (prompt + providers +
translation), `sources/` (rss, html, search_api, linkedin_posts), `dashboard/`
(Gradio-free service layer reused by the API), `api/` (FastAPI JSON layer),
`frontend/` (React SPA), `notifications/discord.py`.

## Development

```bash
pip install -e ".[dev,api]"
pytest          # offline Python test suite
ruff check src tests

cd frontend && npm install
npm run build   # typecheck (tsc) + production build
```

## Privacy

All data stays on your machine (`data/jobs.db`) — no telemetry. Secrets live only
in gitignored `.env`; logs are scrubbed of webhooks, keys and cookies. Outbound
traffic goes only to the sources you enable, your search/LLM endpoints, and
`discord.com`.

## License

MIT. Use at your own risk, and be a good citizen of the sites you query.
