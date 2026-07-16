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

from .config import (
    ProfileSettings,
    Secrets,
    Settings,
    SourcesConfig,
    build_search_query,
)
from .database import Database
from .enrich import Enricher
from .filtering import filter_job, is_recent_enough, matches_location
from .http import PoliteClient
from .llm import JobAssessment, LlmAssessor, build_providers
from .llm.prompt import PROMPT_VERSION
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
        profile: ProfileSettings | None = None,
    ) -> None:
        self.settings = settings
        self.sources_config = sources_config
        self.secrets = secrets
        self.db = database
        # Defaults rather than a required argument: a profile.yaml is optional,
        # and "Germany, no German-only jobs" is the right answer without one.
        self.profile = profile or ProfileSettings()
        # job_id -> provider that judged it; used only for the explanation.
        self._llm_provider_used: dict[str, str] = {}
        # Verdicts waiting for their jobs to exist. See `_flush_assessments`.
        self._pending_assessments: list[tuple[str, str, dict, str | None]] = []
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

            relevant = await self._filter_and_score(jobs, summary, client, dry_run=dry_run)
            summary.above_threshold = len(relevant)

        to_notify = self._store(jobs, relevant, summary, dry_run=dry_run)
        self._flush_assessments(dry_run=dry_run)
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

    async def _filter_and_score(
        self, jobs: list[Job], summary: RunSummary, client: PoliteClient, *, dry_run: bool = False
    ) -> list[Job]:
        """Decide which jobs are worth sending.

        Ordered by cost, cheapest first, because each stage exists to spare the
        next one work:

          1. Cheap deterministic gates — obviously-stale, wrong place, wrong
             keywords. Free.
          2. Enrichment. One HTTP fetch per survivor, so it runs *after* the
             free gates and not before: there is no sense paying for the page of
             a job the keywords already rejected. This is the same bargain
             `prefilter_with_keywords` already strikes on the LLM's behalf.
          3. The recency rule proper, now that enrichment has either found a date
             or established that there isn't one.
          4. The LLM, cache-first, on what is left.

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
            # Only jobs that state a date can be judged stale yet — an undated
            # one has not been looked at, and the real check runs after
            # enrichment. This pass exists purely to avoid fetching a page we
            # already know is too old.
            if job.published_at and not is_recent_enough(job, self.settings.search.max_age_days):
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

        survivors = await self._enrich(survivors, client, summary)

        recent: list[Job] = []
        for job in survivors:
            if is_recent_enough(
                job,
                self.settings.search.max_age_days,
                drop_undated_when_enriched=self.settings.search.drop_undated_when_enriched,
            ):
                recent.append(job)
            else:
                job.status = JobStatus.REJECTED
                job.score_explanation = [
                    "rejected: no publication date on the posting itself"
                    if job.published_at is None
                    else "rejected: older than max_age_days"
                ]
        summary.dropped_as_stale = len(survivors) - len(recent)

        assessments = await self._assess_with_llm(recent, summary, dry_run=dry_run)
        relevant = self._apply_scores(recent, assessments, summary)
        relevant.sort(key=lambda j: j.relevance_score, reverse=True)
        return relevant

    async def _enrich(
        self, jobs: list[Job], client: PoliteClient, summary: RunSummary
    ) -> list[Job]:
        """Fetch the real posting for anything still thin.

        Concurrency is the shared `PoliteClient`'s problem: it caps parallelism
        and paces each domain, so 80 postings on one university's server queue
        politely behind each other rather than arriving as a burst.

        Never fatal, and never removes a job — an unenriched job simply carries
        on with whatever it had.
        """
        if not self.settings.search.enrich:
            return jobs

        enricher = Enricher(client, min_description_chars=self.settings.search.enrich_below_chars)
        wanted = [j for j in jobs if enricher.needs_enriching(j)]
        if not wanted:
            return jobs

        logger.info("enriching %d of %d job(s) from their own pages", len(wanted), len(jobs))
        try:
            await asyncio.wait_for(
                asyncio.gather(*(enricher.enrich(j) for j in wanted)),
                timeout=self.settings.search.enrich_timeout,
            )
        except TimeoutError:
            # Partial enrichment is fine: whatever finished kept its data, and
            # everything else is simply un-enriched, which is a state the rest of
            # the pipeline already handles.
            logger.warning(
                "enrichment exceeded %ss; continuing with what completed",
                self.settings.search.enrich_timeout,
            )

        summary.enriched = sum(1 for j in wanted if j.enriched_at is not None)
        summary.enrich_failed = len(wanted) - summary.enriched
        return jobs

    @property
    def _llm_prefilter_enabled(self) -> bool:
        llm = self.settings.llm
        return not (llm.enabled and self.secrets.has_llm) or llm.prefilter_with_keywords

    def _cached_assessments(self, jobs: list[Job]) -> dict[str, JobAssessment]:
        """Verdicts already paid for, for jobs whose text has not changed.

        This is what keeps a run's LLM cost proportional to what is *new* rather
        than to the size of the database. Measured before it existed: a run with
        108 surviving candidates exhausted the free tier of Gemini AND Groq and
        fell back to keyword scoring for 48 of them — every run, re-judging jobs
        it had already judged yesterday.
        """
        found: dict[str, JobAssessment] = {}
        for job in jobs:
            raw = self.db.get_assessment(job.id, job.content_hash, PROMPT_VERSION)
            if raw is None:
                continue
            try:
                found[job.id] = JobAssessment.model_validate(raw)
            except Exception:  # pragma: no cover — a corrupt row must not stop a run
                logger.debug("ignoring unreadable cached assessment for %s", job.id)
        return found

    async def _assess_with_llm(
        self, jobs: list[Job], summary: RunSummary, *, dry_run: bool = False
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

        cached = self._cached_assessments(jobs)
        summary.llm_cached = len(cached)
        pending = [j for j in jobs if j.id not in cached]
        if cached:
            logger.info(
                "%d assessment(s) served from cache, %d to judge", len(cached), len(pending)
            )
        if not pending:
            return cached

        providers = build_providers(self.secrets, llm)
        if not providers:
            return cached

        assessor = LlmAssessor(
            providers,
            llm,
            topics=self.settings.keywords.topics,
            locations=self.settings.locations.include,
            all_germany=self.settings.locations.all_germany,
        )
        try:
            fresh = await assessor.assess_all(pending)
        except Exception:
            logger.exception("llm assessment failed entirely; falling back to keyword scoring")
            return cached
        finally:
            await assessor.aclose()

        # Queued rather than written now: an assessment references a job row, and
        # the job is not stored until later in the run. The foreign key says so,
        # and it is right to — a verdict about a job we never kept is garbage.
        # Flushed by `_flush_assessments` once `_store` has run.
        if not dry_run:
            by_id = {j.id: j for j in pending}
            for job_id, assessment in fresh.items():
                job = by_id.get(job_id)
                if job is None:  # pragma: no cover — assess_all matches by id
                    continue
                self._pending_assessments.append(
                    (
                        job.id,
                        job.content_hash,
                        assessment.model_dump(mode="json"),
                        assessor.provider_used.get(job.id),
                    )
                )

        assessments = {**cached, **fresh}

        self._llm_provider_used = assessor.provider_used
        summary.llm_assessed = len(assessments)
        summary.llm_fallback = len(jobs) - len(assessments)
        if assessor.failures:
            summary.llm_failures = assessor.failures[:5]
        return assessments

    def _flush_assessments(self, *, dry_run: bool) -> None:
        """Write queued verdicts, now that their jobs exist.

        Runs after `_store` because `job_assessments.job_id` references a real
        job row. A verdict whose job was never stored is worthless anyway, so
        skipping it is the correct outcome rather than a loss — and the run must
        not die over a cache write, which is an optimisation, not the product.
        """
        if dry_run or not self._pending_assessments:
            self._pending_assessments.clear()
            return

        saved = 0
        for job_id, content_hash, payload, provider in self._pending_assessments:
            try:
                self.db.save_assessment(job_id, content_hash, PROMPT_VERSION, payload, provider)
                saved += 1
            except Exception as exc:
                logger.debug("could not cache the assessment for %s: %s", job_id, exc)
        logger.info("cached %d/%d assessment(s)", saved, len(self._pending_assessments))
        self._pending_assessments.clear()

    def _wrong_country(self, assessment: JobAssessment) -> str | None:
        """Is this job somewhere I cannot work? None means keep it.

        An unknown country is never grounds to reject. That is the whole lesson
        of the `country = "Germany"` default: treating "we do not know" as an
        answer is what let Nigerian postings into a German job board, and the
        mirror-image mistake — treating "we do not know" as disqualifying —
        would silently throw away every posting that simply never states where
        it is. Only a country the model actually read, and that is not on the
        list, is a rejection.
        """
        if not assessment.country:
            return None
        allowed = {c.strip().casefold() for c in self.profile.countries}
        if assessment.country.strip().casefold() in allowed:
            return None
        return f"in {assessment.country}, not in {', '.join(self.profile.countries)}"

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
                elif self.profile.exclude_german_required and assessment.german_required:
                    # The requirement, not the language. A posting merely written
                    # in German is fine and most of the TUM board is exactly that.
                    hard_reject = "requires fluent German"
                elif (reject := self._wrong_country(assessment)) is not None:
                    hard_reject = reject
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
