"""The run: search → normalize → filter → score → dedupe → store → notify.

Ordering here is deliberate and load-bearing:

* Sources run concurrently but fail independently.
* Deduplication happens *before* notification, against both this run's results
  and the database, so a job found by three sources is sent once.
* Jobs are stored *before* the Discord call, so a failed send loses nothing —
  the job is in the database and simply stays un-notified until next run.
* `mark_notified` runs *after* Discord confirms, and only for the jobs Discord
  actually accepted.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from .config import Secrets, Settings, SourcesConfig, build_search_query
from .database import Database
from .filtering import filter_job, is_recent_enough, matches_location
from .http import PoliteClient
from .llm import JobAssessment, LlmAssessor, build_providers
from .models import Job, JobStatus, RunSummary, SourceResult
from .normalization import normalize_candidate
from .notifications.discord import DiscordNotifier, render_dry_run
from .scoring import Scorer
from .sources import build_sources
from .sources.search_api import SearchUnavailable

logger = logging.getLogger(__name__)

_NO_TOPIC_SCORE_CAP = 45
"""Ceiling for a job the LLM says matches none of the user's topics. Sits below
every sensible notify threshold, so such jobs are stored but never sent."""


class Pipeline:
    """One complete search run."""

    def __init__(
        self,
        settings: Settings,
        sources_config: SourcesConfig,
        secrets: Secrets,
        database: Database,
    ) -> None:
        self.settings = settings
        self.sources_config = sources_config
        self.secrets = secrets
        self.db = database
        # job_id -> provider that judged it; used only for the explanation.
        self._llm_provider_used: dict[str, str] = {}
        self.scorer = Scorer(
            settings.scoring,
            topics=settings.keywords.topics,
            locations=settings.locations.include,
            phd_signals=settings.filtering.phd_requirement_signals,
        )

    async def run(self, *, dry_run: bool = False) -> RunSummary:
        started_at = datetime.now(UTC)
        summary = RunSummary(started_at=started_at, finished_at=started_at, dry_run=dry_run)

        active = self.sources_config.active
        if not active:
            logger.warning("no sources are enabled — check config/sources.yaml")

        query = build_search_query(self.settings)

        async with PoliteClient(self.settings.http) as client:
            sources = build_sources(active, client, self.secrets)
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*(s.run(query) for s in sources)),
                    timeout=self.settings.http.total_run_timeout,
                )
            except TimeoutError:
                logger.error(
                    "run exceeded total_run_timeout=%ss; no sources completed in time",
                    self.settings.http.total_run_timeout,
                )
                results = []
                summary.sources_failed["<run>"] = (
                    f"timed out after {self.settings.http.total_run_timeout}s"
                )

        candidates = self._collect(results, summary)
        summary.candidates_found = len(candidates)

        jobs = self._normalize_and_dedupe(candidates)
        summary.after_dedup = len(jobs)

        relevant = await self._filter_and_score(jobs, summary)
        summary.above_threshold = len(relevant)

        to_notify = self._store(jobs, relevant, summary, dry_run=dry_run)
        await self._notify(to_notify, summary, dry_run=dry_run)

        if not dry_run and self.settings.database.rejected_retention_days > 0:
            purged = self.db.purge_old_rejected(self.settings.database.rejected_retention_days)
            if purged:
                logger.info("purged %d old rejected job(s)", purged)

        summary.finished_at = datetime.now(UTC)
        if not dry_run:
            self.db.record_run(
                summary.started_at, summary.finished_at, json.dumps(summary.model_dump(mode="json"))
            )
        return summary

    # -- stages -----------------------------------------------------------

    def _collect(self, results: list[SourceResult], summary: RunSummary) -> list:
        candidates = []
        for result in results:
            if result.skipped_reason:
                summary.sources_skipped[result.source] = result.skipped_reason
                logger.info("source %s skipped: %s", result.source, result.skipped_reason)
                continue
            if result.error:
                # A missing search key is a configuration choice, not a fault —
                # reporting it as a failure would train the user to ignore
                # genuine failures.
                if SearchUnavailable.__name__ in result.error:
                    summary.sources_skipped[result.source] = "no search API key configured"
                    logger.info("source %s skipped: no search API key", result.source)
                else:
                    summary.sources_failed[result.source] = result.error
                continue
            summary.sources_ok.append(result.source)
            candidates.extend(result.candidates)
            logger.info(
                "source %s: %d candidate(s) in %.1fs",
                result.source,
                len(result.candidates),
                result.duration_seconds,
            )
        return candidates

    def _normalize_and_dedupe(self, candidates: list) -> list[Job]:
        """Normalize, then collapse duplicates *within this run*.

        Two keys are used: the derived id and the normalized URL. The URL key is
        what catches the same posting arriving from an RSS feed and a search
        engine with different tracking parameters.
        """
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for candidate in candidates:
            try:
                job = normalize_candidate(candidate, max_description_chars=5000)
            except Exception as exc:
                logger.warning("could not normalize a candidate from %s: %s", candidate.source, exc)
                continue

            if job.id in seen_ids or (job.url and job.url in seen_urls):
                logger.debug("in-run duplicate dropped: %s", job.url)
                continue
            seen_ids.add(job.id)
            if job.url:
                seen_urls.add(job.url)
            jobs.append(job)
        return jobs

    async def _filter_and_score(self, jobs: list[Job], summary: RunSummary) -> list[Job]:
        """Decide which jobs are worth sending.

        Two stages. The cheap, deterministic gates (age, location, keywords) run
        first and cost nothing. Then, if an LLM is configured, it judges the
        survivors — it understands "pursuing a PhD" ≠ "holds a PhD", spots index
        pages, and maps German terms onto the user's topics, none of which
        keyword matching can do.

        Every job the LLM does not return a verdict for is scored by the keyword
        scorer instead, so the LLM can fail wholesale, partially, or not at all
        and the run still produces alerts.
        """
        # Counted here rather than derived from `status` afterwards: a job that
        # passes the keyword filter but scores below the threshold is also
        # marked REJECTED, so deriving it would make this always equal
        # `above_threshold` and hide how much the threshold is filtering.
        passed_filter = 0
        survivors: list[Job] = []

        for job in jobs:
            if not is_recent_enough(job, self.settings.search.max_age_days):
                job.status = JobStatus.REJECTED
                job.score_explanation = ["rejected: older than max_age_days"]
                continue

            if not matches_location(
                job, self.settings.locations.include, self.settings.locations.all_germany
            ):
                job.status = JobStatus.REJECTED
                job.score_explanation = ["rejected: location not in configured list"]
                continue

            decision = filter_job(job, self.settings.keywords, self.settings.filtering)
            job.matched_keywords = decision.matched_keywords

            if not decision.passed:
                if self._llm_prefilter_enabled:
                    job.status = JobStatus.REJECTED
                    job.score_explanation = [f"rejected: {r}" for r in decision.reasons]
                    continue
                # Prefilter off: the LLM gets a look even at jobs the keywords
                # rejected, which is the whole point of turning it off.
            else:
                passed_filter += 1
            survivors.append(job)

        summary.passed_filter = passed_filter

        assessments = await self._assess_with_llm(survivors, summary)
        relevant = self._apply_scores(survivors, assessments, summary)
        relevant.sort(key=lambda j: j.relevance_score, reverse=True)
        return relevant

    @property
    def _llm_prefilter_enabled(self) -> bool:
        llm = self.settings.llm
        return not (llm.enabled and self.secrets.has_llm) or llm.prefilter_with_keywords

    async def _assess_with_llm(
        self, jobs: list[Job], summary: RunSummary
    ) -> dict[str, JobAssessment]:
        """LLM verdicts, or {} if the LLM is off, unconfigured or broken."""
        llm = self.settings.llm
        if not llm.enabled:
            logger.debug("llm assessment disabled in settings")
            return {}
        if not self.secrets.has_llm:
            logger.info(
                "no GEMINI_API_KEY or GROQ_API_KEY set — using keyword scoring "
                "(set one in .env for better filtering)"
            )
            return {}
        if not jobs:
            return {}

        providers = build_providers(self.secrets, llm)
        if not providers:
            return {}

        assessor = LlmAssessor(
            providers,
            llm,
            topics=self.settings.keywords.topics,
            locations=self.settings.locations.include,
            all_germany=self.settings.locations.all_germany,
        )
        try:
            assessments = await assessor.assess_all(jobs)
        except Exception:
            logger.exception("llm assessment failed entirely; falling back to keyword scoring")
            return {}
        finally:
            await assessor.aclose()

        self._llm_provider_used = assessor.provider_used
        summary.llm_assessed = len(assessments)
        summary.llm_fallback = len(jobs) - len(assessments)
        if assessor.failures:
            summary.llm_failures = assessor.failures[:5]
        return assessments

    def _apply_scores(
        self, jobs: list[Job], assessments: dict[str, JobAssessment], summary: RunSummary
    ) -> list[Job]:
        """Write a score onto every job, from the LLM where available."""
        llm = self.settings.llm
        keyword_threshold = self.settings.scoring.min_score_to_notify
        llm_threshold = (
            llm.min_score_to_notify if llm.min_score_to_notify is not None else keyword_threshold
        )
        relevant: list[Job] = []

        for job in jobs:
            assessment = assessments.get(job.id)

            if assessment is None:
                # No LLM verdict — keyword scoring. Identical to the old path.
                self.scorer.apply(job)
                threshold = keyword_threshold
                hard_reject: str | None = None
            else:
                provider = getattr(self, "_llm_provider_used", {}).get(job.id, "llm")
                job.relevance_score = assessment.score
                job.score_explanation = assessment.explanation(provider)
                if assessment.topics:
                    # The LLM's topic reading is better than the regex's: it
                    # maps "Softwareentwicklung" onto "software engineering".
                    job.matched_keywords = assessment.topics[:8]
                threshold = llm_threshold

                # Hard rejects override the score: a model that says "requires a
                # completed PhD" and then scores it 70 has contradicted itself,
                # and the structured field is the more reliable signal.
                hard_reject = None
                if not assessment.is_job_posting:
                    hard_reject = "not an individual job posting"
                elif assessment.requires_completed_phd:
                    hard_reject = "requires a completed PhD"
                elif not assessment.suitable_for_masters:
                    hard_reject = "not suitable for a Master's student"
                elif not assessment.topics and job.relevance_score > _NO_TOPIC_SCORE_CAP:
                    # Anti-inflation, using the model's own structured claim
                    # against its own number. Observed live: "HiWi role in
                    # unknown field" scored 55 — exactly the notify threshold —
                    # and "Studentische Hilfskraft ... Energietechnik, which is
                    # not a primary field of interest" also scored 55. A job the
                    # model itself says matches none of your topics cannot be a
                    # strong match, whatever score it attached.
                    job.relevance_score = _NO_TOPIC_SCORE_CAP
                    job.score_explanation.append(
                        f"score capped to {_NO_TOPIC_SCORE_CAP}: matches none of your topics"
                    )

            if hard_reject:
                job.status = JobStatus.REJECTED
                job.score_explanation.append(f"rejected: {hard_reject}")
                continue

            if job.relevance_score >= threshold:
                relevant.append(job)
            else:
                job.status = JobStatus.REJECTED
                job.score_explanation.append(
                    f"rejected: score {job.relevance_score} < min_score_to_notify {threshold}"
                )
        return relevant

    def _store(
        self, jobs: list[Job], relevant: list[Job], summary: RunSummary, *, dry_run: bool
    ) -> list[Job]:
        """Persist everything; return the jobs that still need notifying.

        Storing before sending is what makes a Discord outage harmless.
        """
        if dry_run:
            # A dry run must not touch the database either — otherwise the
            # first real run would consider these jobs already seen and stay
            # silent.
            fresh = [j for j in relevant if not self.db.is_duplicate(j)]
            summary.newly_stored = 0
            return fresh[: self.settings.notifications.max_per_run]

        relevant_ids = {j.id for j in relevant}
        to_notify: list[Job] = []

        for job in jobs:
            already_known = self.db.is_duplicate(job)
            is_new = self.db.upsert(job)
            if is_new:
                summary.newly_stored += 1
            if job.id in relevant_ids and not already_known:
                to_notify.append(job)

        to_notify.sort(key=lambda j: j.relevance_score, reverse=True)
        return to_notify

    async def _notify(self, jobs: list[Job], summary: RunSummary, *, dry_run: bool) -> None:
        max_per_run = self.settings.notifications.max_per_run
        selected = jobs[:max_per_run]
        extra = max(0, len(jobs) - max_per_run)

        if dry_run:
            print(render_dry_run(selected, self.settings.notifications, extra_stored=extra))
            summary.notified = 0
            return

        if not selected:
            logger.info("no new jobs to notify")
            return

        webhook = self.secrets.require_discord()
        async with DiscordNotifier(
            webhook, self.settings.notifications, max_retries=self.settings.http.max_retries
        ) as notifier:
            result = await notifier.send_jobs(selected, extra_stored=extra)

        # Only jobs Discord confirmed are marked. The rest keep status `new` and
        # are picked up by the next run.
        if result.delivered_ids:
            self.db.mark_notified(result.delivered_ids)
        summary.notified = len(result.delivered_ids)
        summary.notify_failed = len(result.failed_ids)

        if result.failed_ids:
            logger.error(
                "%d job(s) could not be delivered and remain unnotified; they will be retried "
                "next run. First error: %s",
                len(result.failed_ids),
                result.errors[0] if result.errors else "unknown",
            )
        if extra:
            logger.info(
                "%d additional job(s) stored but not sent (max_per_run=%d)", extra, max_per_run
            )
