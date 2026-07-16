# Germany Research Job Alerts

Finds **Research Assistant, HiWi, Werkstudent, Research Intern and Master-thesis
positions across Germany** — in German and English — scores each one, and either
sends the good ones to Discord or lets you browse and publish them from a web
dashboard. Built for a Master's student hunting AI/ML/CV/NLP/robotics research
roles; runs entirely on your own machine.

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

- **Two ways to use it**: a headless CLI/scheduler, or a **Gradio dashboard** for
  browsing, translating, searching and hand-picking what to publish.
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
pip install -e ".[dashboard]"          # omit [dashboard] for CLI-only

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
| `dashboard` | Launch the web UI (see below) |
| `send-test` | Test the Discord webhook |
| `list [--new] [--min-score N] [--explain]` | List stored jobs |
| `stats` | Database statistics |
| `run-scheduler` | Stay running; search on a schedule (08:00 / 18:00) |
| `export --format csv\|json` | Export stored jobs |
| `check-source <name>` | Fetch one source and show what it parsed |
| `show-config` | Effective config, secrets masked |

## Dashboard

```bash
python -m job_alerts dashboard          # http://127.0.0.1:7860
```

- **Browse & filter** all stored jobs; click a row for the full posting.
- **German jobs** show an English translation first (cached after the first
  open), original German collapsible below.
- **Publish per job** to Discord, in English — publishing a filtered-out or
  already-sent job asks for confirmation.
- **Search** turns keywords / topics / an uploaded resume into live queries, runs
  the pipeline, and **stores only** (nothing auto-sent). "Hide" declutters your
  view without deleting.
- `--share` exposes a public link and therefore **requires** `--auth user:pass`.

## Docker

`docker compose up` waits for the LLM endpoint to come online, then serves the
dashboard on http://localhost:7860. Config is mounted live (editing
`llm.colab_base_url` is picked up without a restart); the database persists in a
named volume.

```bash
docker compose up                          # dashboard, gated on the LLM being online
docker compose --profile scheduler up -d   # also run scheduled searches
docker compose run --rm dashboard send-test
```

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
(Gradio UI), `notifications/discord.py`.

## Development

```bash
pip install -e ".[dev,dashboard]"
pytest          # offline test suite
ruff check src tests
```

## Privacy

All data stays on your machine (`data/jobs.db`) — no telemetry. Secrets live only
in gitignored `.env`; logs are scrubbed of webhooks, keys and cookies. Outbound
traffic goes only to the sources you enable, your search/LLM endpoints, and
`discord.com`.

## License

MIT. Use at your own risk, and be a good citizen of the sites you query.
