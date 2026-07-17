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

### Phase 3 — central academic profile (single-user, local-first) ✅
- [x] Persistent résumé upload (PDF/Markdown/LaTeX/text; immutable original bytes stored + hashed).
      DOCX/ZIP deferred.
- [x] Structured `AcademicProfile` (education/experience/projects/skills/publications/links) via
      LLM extraction (`extract_profile`, disable_thinking); tolerant of the shapes models emit.
- [x] Provenance: immutable `extracted_json` kept beside the editable `profile_json` (migration v10).
- [x] Profile editor + upload/export/delete/download-original — API (`/api/profile*`) + React
      Profile page. Validated end-to-end on the real résumé.
- [x] Bonus: the `disable_thinking` fix now also applies to the dashboard translate/keyword LLM path.

### Phase 4 — matching & semantic search
- [x] **Profile↔opportunity match analysis** — LLM-driven, evidence-cited categories
      (strong/good/stretch/unlikely, **no ATS number**): strong/partial matches, missing
      requirements, suggested emphasis, level/language compatibility, concerns, confidence.
      `MatchAnalysis` model, migration v11 cache (keyed on job content + profile + prompt version),
      `analyze_match` (disable_thinking), `GET /api/jobs/{id}/match`, and a "Your fit" section in
      the job detail panel (on-demand). Fixed a latent bug: slash-containing job ids (all RSS jobs)
      broke every `/api/jobs/{id}` route — now `{job_id:path}`.
- [ ] Embeddings + hybrid search + filters + similar opportunities (needs an embedding model on the
      endpoint — deferred).

### Phase 5 — résumé tailoring
- [ ] Suggested edits + diff + accept/reject; sandboxed LaTeX compile; PDF preview

### Phase 6 — research intelligence & launch
- [ ] Lab/PI context (OpenAlex/ORCID/arXiv/DBLP); application-material generation; launch hardening
- [ ] Optional internal rename (`job_alerts` → labscout, `jobs` table → opportunities)

## Notes
- Live `config/settings.yaml` runs the narrow **core_ai** preset (Vaibhav's own view); the shipped
  `settings.example.yaml` is broad. Both gitignored except the `.example`.
- Discord stays co-equal with the web UI.
