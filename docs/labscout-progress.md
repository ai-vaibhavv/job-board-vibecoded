# LabScout — progress tracker

Living checklist of the pivot (full plan: `~/.claude/plans/there-is-a-plan-md-adaptive-barto.md`).
Tick items as they land. Last updated: 2026-07-17.

## ✅ Milestone 1 — academic-opportunity foundation (DONE, PR #1)
- [x] Rebrand Job Board → **LabScout** (UI, title, favicon/logo, palette, API title, Discord)
- [x] `taxonomy.py` — OpportunityType / ApplicantLevel / AcademicField / InstitutionType
- [x] Two-pass LLM contract + prompts (`is_academic_opportunity`, Pass-2 detail), `PROMPT_VERSION 5`
- [x] DB migration v8 (taxonomy columns + `job_opportunity_details` cache)
- [x] Academic relevance gate + `core_ai` preset flags (broad default / narrow live)
- [x] `JsonApiSource` (`json_api`) + captured fixture test
- [x] Richer Discord embeds + card chip + detail-panel taxonomy rows
- [x] 608 tests green, ruff clean, frontend builds, committed + PR opened

## ✅ LLM performance / reliability (DONE)
- [x] **Fixed empty LLM responses** — root cause: `qwen3.5` is a REASONING model; on Ollama's
      OpenAI `/v1` it spends the whole budget in a hidden `reasoning` field and returns empty
      `content`. Fix: `disable_thinking` → Ollama native `/api/chat` with `think:false` (~2s JSON).
- [x] **Sped up** — `min_request_interval: 6 → 0` (self-hosted has no rate limit); `max_output_tokens`.
- [x] **Incremental board population** — backend stores each job as its verdict lands
      (`Pipeline.run(incremental=True)` via a per-batch `on_batch` callback + `known_before`
      snapshot so early stores don't break the notify set); frontend refreshes `/api/jobs` on every
      poll while a run is in flight. Dashboard search uses it.

## ⏳ Still worth doing on LLM infra
- [ ] Raw model speed is still ~8s/job on the free Colab GPU (a full run ≈ 13 min). Options: a
      smaller/faster model, or fewer jobs per run. Not a code issue.

## ⏭️ Next up (from the plan roadmap)
### Phase 2 — broader coverage
- [x] **Real academic source wired** — Bundesagentur für Arbeit public Jobsuche JSON API
      (`arbeitsagentur`, enabled), queried for academic terms; covers universities, Fraunhofer,
      MPG, Helmholtz, uni hospitals. Enhanced the `json_api` connector: `item_url_template`
      (build URLs from `refnr`), `headers` (public API key), and `{query}` multi-query fetch.
      Verified live via `check-source`. (EURAXESS stays a disabled reference — no clean JSON API.)
- [x] **University-domain discovery expansion** — added ~18 AI-strong German university/institute
      domains (CISPA, HPI, Freiburg, Saarland, Stuttgart, Bonn, Dresden, DLR, Jülich, Berlin unis…)
      + targeted `site:` queries to `search_discovery`.
- [x] **Source-health monitoring** — migration v9 `source_health`, per-run streak tracking, "Sources
      AILING" in the run summary, `source-health` CLI.
- [x] **Multilingual terminology** — accent-folded `ACADEMIC_TERMS` with German + French/Dutch/
      Italian/Spanish academic vocabulary (`looks_academic`).
- [x] **De-Germanization (config-driven)** — `http.accept_language` is now a setting (Germany-first
      default); LabScout user-agent. Deeper de-Germanization (country/timezone/`all_germany`,
      `_GERMAN_CITIES`) remains for when the product goes pan-European.
- [ ] Department/lab/institute HTML connectors (more sources) — ongoing; the json_api/html/rss
      framework is ready, each new source is a config + fixture + `check-source`.

**Phase 2 core complete.** Remaining is incremental source-adding, done as needed.

### Phase 3 — central academic profile (single-user, local-first)
- [ ] Persistent résumé upload (PDF/DOCX/MD/LaTeX/ZIP), immutable originals
- [ ] Structured profile extraction with provenance; profile editor; delete/export

### Phase 4 — matching & semantic search
- [ ] Profile↔opportunity match analysis (evidence-cited categories, **no ATS score**)
- [ ] Embeddings + hybrid search + filters + similar opportunities

### Phase 5 — résumé tailoring
- [ ] Suggested edits + diff + accept/reject; sandboxed LaTeX compile; PDF preview

### Phase 6 — research intelligence & launch
- [ ] Lab/PI context (OpenAlex/ORCID/arXiv/DBLP); application-material generation; launch hardening
- [ ] Optional internal rename (`job_alerts` → labscout, `jobs` table → opportunities)

## Notes
- Live `config/settings.yaml` runs the narrow **core_ai** preset (Vaibhav's own view); the shipped
  `settings.example.yaml` is broad. Both gitignored except the `.example`.
- Discord stays co-equal with the web UI.
