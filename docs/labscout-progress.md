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
- [ ] Wire a real academic source (EURAXESS has no clean JSON API; use an institute HTML source
      or another JSON board) — connector is ready, just needs a live endpoint.
- [ ] University-domain discovery expansion; department/lab/institute connectors
- [ ] Source-health monitoring; de-Germanization (config-driven country/timezone/language)

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
