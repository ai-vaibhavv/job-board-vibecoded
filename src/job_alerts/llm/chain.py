"""Provider fallback: Colab → keyword scoring.

The contract is that a job alert never depends on an LLM being up. The
self-hosted Colab model is ephemeral (the notebook idles out and the tunnel URL
changes every session), so failure is handled at two levels:

  1. Per job — the provider returned 7 of 8 assessments? The 8th is scored by
     the keyword scorer. No job is dropped for lacking an LLM verdict.
  2. Whole run — no `colab_base_url`, the tunnel is down, or LLM disabled? Every
     job is scored by the keyword path and the run is otherwise identical.

That is what "seamless" has to mean here: degrade quietly and keep sending jobs.
`build_providers` returns a list, and `LlmAssessor` loops it, so restoring a
hosted fallback later is just another branch here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from ..config import LlmSettings, Secrets
from ..models import Job
from .base import JobAssessment, LlmError, LlmProvider, OpportunityDetail
from .providers import ColabProvider

logger = logging.getLogger(__name__)


def build_providers(secrets: Secrets, settings: LlmSettings) -> list[LlmProvider]:
    """Configured providers, in `llm.providers` order.

    Only the self-hosted `colab` provider exists; it is built when
    `colab_base_url` is set and skipped otherwise (the run then falls back to
    keyword scoring).
    """
    available: list[LlmProvider] = []
    for name in settings.providers:
        if name == "colab" and settings.colab_base_url.strip():
            available.append(
                ColabProvider(
                    settings.colab_base_url.strip(),
                    api_key=secrets.colab_api_key.strip(),
                    model=settings.colab_model,
                    timeout=settings.timeout,
                    max_output_tokens=settings.max_output_tokens,
                )
            )
        elif name == "colab":
            logger.debug("llm provider colab listed but has no colab_base_url; skipping")
        else:
            logger.warning("unknown llm provider %r in settings; ignoring", name)
    return available


class LlmAssessor:
    """Assesses jobs with the first provider that works."""

    def __init__(
        self,
        providers: list[LlmProvider],
        settings: LlmSettings,
        *,
        topics: list[str],
        locations: list[str],
        all_germany: bool,
        core_ai_mode: bool = False,
    ) -> None:
        self.providers = providers
        self.settings = settings
        self.prompt_kwargs = {
            "topics": topics,
            "locations": locations,
            "all_germany": all_germany,
            "core_ai_mode": core_ai_mode,
            "max_description_chars": settings.max_description_chars,
        }
        self._last_call: dict[str, float] = {}
        self._pace_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.provider_used: dict[str, str] = {}
        """job_id -> provider name, so the explanation can say who judged it."""
        self.failures: list[str] = []

    @property
    def available(self) -> bool:
        return bool(self.providers)

    async def aclose(self) -> None:
        for provider in self.providers:
            try:
                await provider.aclose()
            except Exception:
                logger.debug("error closing provider %s", provider.name, exc_info=True)

    async def assess_all(self, jobs: list[Job]) -> dict[str, JobAssessment]:
        """Assess every job. Returns what succeeded, keyed by job id.

        Jobs missing from the result are the caller's problem to score — that is
        the documented contract, not an error.
        """
        if not self.providers or not jobs:
            return {}

        batches = [
            jobs[i : i + self.settings.batch_size]
            for i in range(0, len(jobs), self.settings.batch_size)
        ]
        logger.info(
            "assessing %d job(s) in %d batch(es) via %s",
            len(jobs),
            len(batches),
            " -> ".join(p.name for p in self.providers),
        )

        # Bounded concurrency: free tiers are rate-limited per minute, and
        # firing 15 batches at once is the fastest way to get 429'd off both
        # providers at the same moment.
        semaphore = asyncio.Semaphore(self.settings.max_concurrency)

        async def run(batch: list[Job]) -> list[JobAssessment]:
            async with semaphore:
                return await self._assess_batch(batch)

        results = await asyncio.gather(*(run(b) for b in batches), return_exceptions=True)

        assessments: dict[str, JobAssessment] = {}
        for result in results:
            if isinstance(result, BaseException):
                # _assess_batch already swallows LlmError; this is a genuine bug
                # or a cancellation, and must not take the run down.
                logger.error("llm batch raised unexpectedly: %r", result)
                continue
            for assessment in result:
                assessments[assessment.job_id] = assessment
        return assessments

    async def detail_all(self, jobs: list[Job]) -> dict[str, OpportunityDetail]:
        """Pass 2: fine-classify jobs that already passed Pass 1.

        Best-effort by contract — a job missing from the result simply keeps its
        default taxonomy values, exactly like a job missing from `assess_all`
        keeps the keyword score. No retry storm: Pass 2 is enrichment, not the
        gate, so a batch that fails once is dropped rather than re-attempted.
        """
        if not self.providers or not jobs:
            return {}

        batches = [
            jobs[i : i + self.settings.batch_size]
            for i in range(0, len(jobs), self.settings.batch_size)
        ]
        semaphore = asyncio.Semaphore(self.settings.max_concurrency)

        async def run(batch: list[Job]) -> list[OpportunityDetail]:
            async with semaphore:
                return await self._detail_batch(batch)

        results = await asyncio.gather(*(run(b) for b in batches), return_exceptions=True)
        details: dict[str, OpportunityDetail] = {}
        for result in results:
            if isinstance(result, BaseException):
                logger.error("llm detail batch raised unexpectedly: %r", result)
                continue
            for detail in result:
                details[detail.job_id] = detail
        return details

    async def _detail_batch(self, batch: list[Job]) -> list[OpportunityDetail]:
        for provider in self.providers:
            try:
                await self._pace(provider.name)
                return await provider.classify_details(batch, **self.prompt_kwargs)
            except LlmError as exc:
                logger.warning(
                    "llm detail pass failed on a batch of %d (%s); leaving defaults",
                    len(batch),
                    exc,
                )
                self._record_failure(f"{provider.name} (detail): {exc}")
                continue
            except Exception as exc:
                logger.exception("llm provider %s raised during detail pass", provider.name)
                self._record_failure(f"{provider.name} (detail): {type(exc).__name__}: {exc}")
                continue
        return []

    async def _pace(self, provider_name: str) -> None:
        """Space out requests to one provider.

        Free tiers are rate-limited per minute, and firing as fast as asyncio
        allows earns an immediate 429 — measured live: an 8-job prompt is ~4.7k
        tokens against Groq's 12k tokens/minute, i.e. two requests per minute.
        Waiting a few seconds is far cheaper than burning the provider and
        falling back.
        """
        interval = self.settings.min_request_interval
        if interval <= 0:
            return
        async with self._pace_locks[provider_name]:
            last = self._last_call.get(provider_name)
            if last is not None:
                wait = interval - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_call[provider_name] = time.monotonic()

    async def _assess_batch(self, batch: list[Job]) -> list[JobAssessment]:
        """Try every provider; on transient failures, back off and try again.

        Order matters: a fallback provider is tried *immediately* (it is free and
        probably fine), and only when every provider has failed transiently do we
        wait. That keeps the common case fast and the rate-limited case correct.
        """
        for attempt in range(self.settings.max_retries + 1):
            transient_failures = 0

            for provider in self.providers:
                try:
                    await self._pace(provider.name)
                    assessments = await provider.assess(batch, **self.prompt_kwargs)
                except LlmError as exc:
                    # The expected path when a free tier rate-limits us. Log at
                    # warning (not error): the next provider will probably work,
                    # and crying wolf trains the user to ignore real problems.
                    if exc.transient:
                        transient_failures += 1
                    logger.warning(
                        "llm provider %s failed on a batch of %d (%s%s); trying next",
                        provider.name,
                        len(batch),
                        "transient: " if exc.transient else "permanent: ",
                        exc,
                    )
                    self._record_failure(f"{provider.name}: {exc}")
                    continue
                except Exception as exc:
                    logger.exception("llm provider %s raised unexpectedly", provider.name)
                    self._record_failure(f"{provider.name}: {type(exc).__name__}: {exc}")
                    continue

                for assessment in assessments:
                    self.provider_used[assessment.job_id] = provider.name
                logger.debug(
                    "%s assessed %d/%d job(s)", provider.name, len(assessments), len(batch)
                )
                return assessments

            # Every provider failed. Retrying only makes sense if at least one
            # of them might answer differently next time — a bad API key will
            # not fix itself, and sleeping on it just delays the fallback.
            if transient_failures == 0:
                logger.debug("all llm providers failed permanently; not retrying")
                break
            if attempt < self.settings.max_retries:
                delay = self.settings.retry_base_delay * (2**attempt)
                logger.info(
                    "all llm providers rate-limited/unavailable; waiting %.0fs before retry %d/%d",
                    delay,
                    attempt + 1,
                    self.settings.max_retries,
                )
                await asyncio.sleep(delay)

        logger.warning(
            "every llm provider failed for a batch of %d job(s); "
            "these fall back to keyword scoring",
            len(batch),
        )
        return []

    def _record_failure(self, message: str) -> None:
        # Deduplicated: ten batches hitting the same 429 should read as one
        # problem in the summary, not ten.
        short = message[:120]
        if short not in self.failures:
            self.failures.append(short)
