"""The source interface every adapter implements."""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

from ..config import SourceConfig
from ..http import PoliteClient
from ..models import JobCandidate, SearchQuery, SourceResult

logger = logging.getLogger(__name__)


@runtime_checkable
class JobSource(Protocol):
    """A place jobs can come from.

    Adapters return raw `JobCandidate`s and do no filtering, scoring or
    deduplication — that is the pipeline's job. An adapter's only contract is
    "give me what you found, or raise".
    """

    name: str

    async def search(self, query: SearchQuery) -> list[JobCandidate]: ...


class BaseSource:
    """Shared plumbing: config, HTTP client, defaults, safe execution."""

    def __init__(self, config: SourceConfig, client: PoliteClient) -> None:
        self.config = config
        self.client = client
        self.name = config.name

    async def search(self, query: SearchQuery) -> list[JobCandidate]:  # pragma: no cover
        raise NotImplementedError

    def apply_defaults(self, candidate: JobCandidate) -> JobCandidate:
        """Fill blanks from the source's `defaults:` block.

        Lets a feed that never names its employer still produce jobs labelled
        "Max Planck Society" without the adapter hard-coding it.
        """
        for field, value in self.config.defaults.items():
            if hasattr(candidate, field) and getattr(candidate, field) in (None, ""):
                setattr(candidate, field, value)
        return candidate

    async def run(self, query: SearchQuery) -> SourceResult:
        """Execute the source, converting any failure into a `SourceResult`.

        This is the isolation boundary the spec demands: one broken source
        records an error and the run continues with the others. Nothing escapes
        except `asyncio.CancelledError`, which must propagate for shutdown and
        the run timeout to work.
        """
        started = time.monotonic()

        if self.config.forbidden:
            return SourceResult(
                source=self.name,
                skipped_reason="source terms disallow automated access",
                duration_seconds=0.0,
            )

        try:
            candidates = await self.search(query)
        except Exception as exc:
            logger.warning(
                "source %s failed: %s", self.name, exc, exc_info=logger.isEnabledFor(logging.DEBUG)
            )
            return SourceResult(
                source=self.name,
                error=f"{type(exc).__name__}: {exc}",
                duration_seconds=time.monotonic() - started,
            )

        limited = candidates[: query.max_results]
        if len(candidates) > query.max_results:
            logger.debug(
                "source %s returned %d candidates, capped to %d",
                self.name,
                len(candidates),
                query.max_results,
            )
        return SourceResult(
            source=self.name,
            candidates=[self.apply_defaults(c) for c in limited],
            duration_seconds=time.monotonic() - started,
        )
