"""Configurable HTML career-page source, driven by CSS selectors from YAML.

Adding a university career page should mean editing `sources.yaml`, not writing
Python. A config block looks like:

    selectors:
      item: "div.job-listing"      # repeated element, one per job
      title: "h3.job-title"
      url: "a.job-link@href"       # `@attr` reads an attribute
      location: "span.location"
      organization: "span.dept"
      description: "div.summary"
      published_at: "time@datetime"

Everything except `item`, `title` and `url` is optional. Selectors are resolved
relative to each `item` element.

The adapter is fully functional and tested; whether any *particular* site's
selectors are right is a separate question, which is why real-world entries in
`sources.example.yaml` ship disabled and marked UNVERIFIED.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import JobCandidate, SearchQuery
from .base import BaseSource

logger = logging.getLogger(__name__)

_REQUIRED_SELECTORS = ("item", "title", "url")


class SelectorError(ValueError):
    """The configured selectors cannot work. A config bug, not a site bug."""


class GenericHtmlSource(BaseSource):
    """Scrapes a static career page using selectors from configuration."""

    async def search(self, query: SearchQuery) -> list[JobCandidate]:
        url = self.config.url
        if not url:
            raise SelectorError(f"source {self.name!r} has type 'html' but no url")
        missing = [s for s in _REQUIRED_SELECTORS if not self.config.selectors.get(s)]
        if missing:
            raise SelectorError(
                f"source {self.name!r} is missing required selector(s): {', '.join(missing)}. "
                f"Required: {', '.join(_REQUIRED_SELECTORS)}."
            )

        body = await self.client.get_text(url)
        return self.parse(body, base_url=url)

    def parse(self, body: str, *, base_url: str = "") -> list[JobCandidate]:
        soup = BeautifulSoup(body, "lxml")
        selectors = self.config.selectors
        items = soup.select(selectors["item"])
        if not items:
            # Almost always a stale selector after a site redesign — worth a
            # warning, because the source silently returning zero jobs looks
            # identical to "no jobs today".
            logger.warning(
                "source %s: selector %r matched no elements — the site layout may have changed",
                self.name,
                selectors["item"],
            )
            return []

        candidates: list[JobCandidate] = []
        for item in items:
            candidate = self._parse_item(item, selectors, base_url)
            if candidate:
                candidates.append(candidate)
        logger.debug("source %s: parsed %d/%d items", self.name, len(candidates), len(items))
        return candidates

    def _parse_item(
        self, item: Tag, selectors: dict[str, str], base_url: str
    ) -> JobCandidate | None:
        title = _extract(item, selectors.get("title"))
        raw_url = _extract(item, selectors.get("url"))
        if not title or not raw_url:
            return None

        try:
            return JobCandidate(
                source=self.name,
                source_job_id=_extract(item, selectors.get("source_job_id")) or None,
                title=title,
                organization=_extract(item, selectors.get("organization")) or None,
                location=_extract(item, selectors.get("location")) or None,
                description=_extract(item, selectors.get("description")) or None,
                url=urljoin(base_url, raw_url),
                published_at=_extract(item, selectors.get("published_at")) or None,
                application_deadline=_extract(item, selectors.get("application_deadline")) or None,
                employment_type=_extract(item, selectors.get("employment_type")) or None,
                salary=_extract(item, selectors.get("salary")) or None,
            )
        except ValueError as exc:
            logger.debug("source %s: invalid item: %s", self.name, exc)
            return None


def _extract(root: Tag, selector: str | None) -> str:
    """Resolve one selector against `root`.

    Supports a trailing `@attribute` to read an attribute instead of text —
    `a@href` for links, `time@datetime` for machine-readable dates. A bare
    selector returns the element's text.
    """
    if not selector:
        return ""

    css, _, attribute = selector.partition("@")
    css = css.strip()

    try:
        element = root.select_one(css) if css else root
    except Exception as exc:
        raise SelectorError(f"invalid CSS selector {css!r}: {exc}") from exc

    if element is None:
        return ""
    if attribute:
        value = element.get(attribute.strip())
        if isinstance(value, list):  # e.g. class="a b" comes back as a list
            return " ".join(value).strip()
        return (value or "").strip()
    return element.get_text(separator=" ", strip=True)
