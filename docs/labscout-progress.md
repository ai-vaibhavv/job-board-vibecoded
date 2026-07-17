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

## 🔧 In progress — LLM performance / reliability (BLOCKER for live runs)
- [ ] **Fix empty LLM responses** — model (`qwen3.5-8k`) returns 200 but empty content on the
      assessment prompt (works on trivial prompts). Suspects: prompt too long, needs `max_tokens`
      / `num_predict`, thinking-model output routing, or batch too large. Diagnose + fix.
- [ ] **Speed up** — batches take 3–5 min each; `min_request_interval: 6` is pointless for a
      self-hosted model (tuned for free cloud tiers) → set ~0–1. Consider batch size, shorter
      `max_description_chars`, smaller/faster model.
- [ ] **Incremental board population** — store + display jobs as each batch is classified instead
      of all-at-once (backend: per-batch store; frontend: poll `/api/jobs` during a run). UX ask.

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
