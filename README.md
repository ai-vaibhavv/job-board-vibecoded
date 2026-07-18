<p align="center">
  <img src="logos/wordmark.png" alt="LabScout" width="420">
</p>

<p align="center"><strong>Discover. Match. Research.</strong></p>

LabScout finds **university, lab and research-institute opportunities** — HiWi,
student / research / teaching assistant, research internships, Bachelor/Master
thesis, and PhD positions — **Germany-first, in German and English**. On each run
it searches every enabled source, scores each result 0–100, deduplicates against
history, stores everything in a local SQLite database, and either sends the good
ones to Discord or lets you browse and hand-pick them from a **React dashboard**.
It runs entirely on your own machine.

> The Python package keeps its historical name `job_alerts`; the product is **LabScout**.

---

## 🤖 This project is vibe-coded — and that's the whole point

**Every line of this project was written by prompting an AI coding agent, not by
hand. That is a hard rule, not a preference.**

If you contribute, you must do the same: describe what you want to an AI coding
assistant and let it write the code. **No hand-written patches.** The goal of
LabScout is to be an end-to-end, real-world app built purely through vibe-coding
— so keeping every contribution vibe-coded is what the project is *for*.

Bug reports, feature ideas, and issues are welcome from everyone. Pull requests
are welcome too — as long as the code in them was produced by vibe-coding.

<p align="center">
  <img src="logos/opensource.gif" alt="Open source" width="480">
</p>

### Good first contributions

- **Fix the UI** — polish the dashboard, improve responsiveness, dark mode, accessibility.
- **Fix bugs** — see the issue tracker, or anything you hit while running it.
- **Improve latency** — faster source fetching, smarter caching, leaner LLM prompts.
- **Add a source** — a new RSS/HTML/JSON provider under `src/job_alerts/sources/`.
- **Improve scoring/filtering** — better keyword rules or LLM prompts.
- **Docs & tests** — clearer setup, more coverage.

---

## Quick start

Requires **Python ≥ 3.12** and **Node.js ≥ 18** (for the dashboard).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[api]"                 # omit [api] for CLI-only

cp .env.example .env                     # then edit — see "Configuration" below
cp config/settings.example.yaml config/settings.yaml
cp config/sources.example.yaml config/sources.yaml

python -m job_alerts search --dry-run    # safe first run, sends nothing
python -m job_alerts send-test           # check the Discord webhook
python -m job_alerts search              # real run
```

**Dashboard (dev, hot-reload):**

```bash
python -m job_alerts serve                    # JSON API on http://127.0.0.1:7860
cd frontend && npm install && npm run dev     # UI on http://localhost:5173
```

**Docker (one container serves SPA + API):**

```bash
docker compose up                             # dashboard on http://localhost:7860
docker compose --profile scheduler up -d      # also run scheduled searches
```

---

## Configuration

Copy the three example files above, then set your own values. **Secrets live only
in `.env`** (gitignored); everything else is plain config in `config/*.yaml`.

### `.env` — secrets

| Variable | Required? | What to set |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | **Required** | Your Discord webhook (Server Settings → Integrations → Webhooks). Not needed for `--dry-run`. |
| `SEARCH_API_PROVIDER` + `SEARCH_API_KEY` | Optional | Search-engine discovery (`tavily`, `brave`, `bing`, `google_cse`, `serpapi`). Without it, RSS/HTML sources still work. |
| `GOOGLE_CSE_ID` | Optional | Only when `SEARCH_API_PROVIDER=google_cse`. |
| `COLAB_API_KEY` | Optional | Bearer token for a self-hosted LLM endpoint. Usually blank. |
| `APIFY_TOKEN` | Optional | Enables the LinkedIn-posts source; without it that source skips itself. |
| `JOB_ALERTS_API_AUTH` | Optional | `user:pass` — require HTTP Basic auth on the dashboard's write endpoints. **Set this before exposing the API beyond localhost.** |

### `config/settings.yaml` — non-secret config

| Setting | What to set |
|---|---|
| `http.user_agent` | **Put your own contact info here** (used on every outbound request). |
| `llm.colab_base_url` | Optional. URL of a self-hosted, OpenAI-compatible LLM. Empty = keyword scorer only (translation disabled). See `docs/colab-model.md`. |

---

## Commands

| Command | Purpose |
|---|---|
| `search [--dry-run]` | Run one full search now |
| `serve` | Launch the JSON API for the dashboard |
| `send-test` | Test the Discord webhook |
| `list [--new] [--min-score N] [--explain]` | List stored jobs |
| `stats` | Database statistics |
| `run-scheduler` | Stay running; search on a schedule |
| `export --format csv\|json` | Export stored jobs |
| `check-source <name>` | Fetch one source and show what it parsed |
| `show-config` | Effective config, secrets masked |

## Development

```bash
pip install -e ".[dev,api]"
pytest                       # offline Python test suite
ruff check src tests
cd frontend && npm install && npm run build   # typecheck + production build
```

## How it works

`pipeline.py` runs: search → normalize → filter → score → dedupe → store →
notify. Jobs are stored *before* Discord and marked notified only *after* a 2xx,
so an outage loses nothing. LinkedIn is used as a discovery-link source only —
the app never logs in or contacts `linkedin.com` itself. `robots.txt` is
respected and requests are rate-limited per domain.

Layout: `config.py`, `models.py`, `database.py` (SQLite), `filtering.py` /
`scoring.py`, `llm/`, `sources/`, `api/` (FastAPI), `frontend/` (React SPA),
`notifications/discord.py`.

## Privacy

All data stays on your machine (`data/jobs.db`) — no telemetry. Secrets live only
in gitignored `.env`; logs are scrubbed of webhooks, keys and cookies.

## License

MIT. Use at your own risk, and be a good citizen of the sites you query.
