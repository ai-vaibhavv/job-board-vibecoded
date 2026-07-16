"""Generic RSS 2.0 / Atom source.

Parsed with BeautifulSoup's XML mode rather than `feedparser` so RSS and Atom
share one code path and the dependency list stays as the spec specifies.

This adapter is fully functional: point it at any valid feed and it works. The
feeds listed in `sources.example.yaml` are a separate question — see the
status markers there.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import JobCandidate, SearchQuery
from .base import BaseSource

logger = logging.getLogger(__name__)

# Tag names by role, in preference order. RSS and Atom disagree on all of them.
# Matching is case-insensitive (see `_find_tag`), so the RSS 2.0 spelling
# `<pubDate>` and the Atom `<published>` are both reachable from one list.
_TITLE_TAGS = ("title",)
_LINK_TAGS = ("link", "guid", "id")
_DESCRIPTION_TAGS = ("description", "summary", "content", "content:encoded", "encoded")
_DATE_TAGS = ("pubdate", "published", "updated", "dc:date", "date")
_ID_TAGS = ("guid", "id")

# Who posted this. `dc:publisher` is the institution ("Technische Universität
# München"); `dc:creator` is usually a person; and RSS 2.0 defines `<author>` as
# *an email address*, which is why it is last and why bare emails are rejected —
# "pia.lorenz@tum.de" is not an organization.
_ORG_TAGS = ("dc:publisher", "publisher", "dc:creator", "creator", "author")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RssSource(BaseSource):
    """Reads jobs from an RSS/Atom feed."""

    async def search(self, query: SearchQuery) -> list[JobCandidate]:
        url = self.config.url
        if not url:
            raise ValueError(f"source {self.name!r} has type 'rss' but no url")

        body = await self.client.get_text(url)
        return self.parse(body, base_url=url)

    def parse(self, body: str, *, base_url: str = "") -> list[JobCandidate]:
        """Feed XML -> candidates. Exposed separately so tests can feed it
        fixtures without any network involved."""
        soup = BeautifulSoup(body, "xml")
        entries = soup.find_all(["item", "entry"])
        if not entries:
            logger.debug("source %s: feed contained no <item>/<entry>", self.name)
            return []

        candidates: list[JobCandidate] = []
        for entry in entries:
            candidate = self._parse_entry(entry, base_url)
            if candidate:
                candidates.append(candidate)
        return candidates

    def _parse_entry(self, entry: Tag, base_url: str) -> JobCandidate | None:
        title = _first_text(entry, _TITLE_TAGS)
        link = self._extract_link(entry, base_url)
        if not title or not link:
            # Without a title and a link there is nothing to notify about.
            logger.debug("source %s: skipping entry with no title/link", self.name)
            return None

        try:
            return JobCandidate(
                source=self.name,
                source_job_id=_first_text(entry, _ID_TAGS) or None,
                title=title,
                organization=_organization(entry),
                location=_first_text(entry, ("location", "job:location")) or None,
                description=_first_text(entry, _DESCRIPTION_TAGS) or None,
                url=link,
                published_at=_first_text(entry, _DATE_TAGS) or None,
            )
        except ValueError as exc:
            logger.debug("source %s: invalid entry: %s", self.name, exc)
            return None

    def _extract_link(self, entry: Tag, base_url: str) -> str | None:
        """RSS puts the URL in <link>text</link>; Atom in <link href="..."/>.

        Atom feeds often carry several links; `rel="alternate"` (or no rel at
        all) is the human-facing page, which is what belongs in the alert.
        """
        for tag in entry.find_all("link"):
            href = tag.get("href")
            rel = tag.get("rel")
            if href and (not rel or "alternate" in rel):
                return urljoin(base_url, href.strip())
            text = tag.get_text(strip=True)
            if text:
                return urljoin(base_url, text)

        for name in _LINK_TAGS:
            value = _first_text(entry, (name,))
            if value and value.startswith(("http://", "https://")):
                return value
        return None


def _find_tag(entry: Tag, name: str) -> Tag | None:
    """Find a child tag by name, case-insensitively and ignoring any namespace.

    XML is case-sensitive and BeautifulSoup's xml mode honours that, so
    `find("pubdate")` does not match `<pubDate>` — the spelling RSS 2.0 actually
    mandates. Every RSS feed's dates were therefore dropped on the floor, which
    stayed invisible because a job with no date is treated as recent enough to
    keep. Match on the lowercased local name instead.
    """
    wanted = name.split(":", 1)[-1].lower()
    return entry.find(lambda tag: tag.name.lower() == wanted)


def _first_text(entry: Tag, names: tuple[str, ...]) -> str:
    """First non-empty text among `names`, tolerant of case and namespaces."""
    for name in names:
        tag = _find_tag(entry, name)
        if tag is not None:
            text = tag.get_text(strip=True)
            if text:
                return text
    return ""


def _organization(entry: Tag) -> str | None:
    """The posting institution, if the feed names one.

    Skips bare email addresses: RSS 2.0 defines `<author>` as an email, and an
    inbox is not an employer. A source's `defaults: {organization: ...}` fills
    the gap where a feed says nothing useful.
    """
    for name in _ORG_TAGS:
        tag = _find_tag(entry, name)
        if tag is None:
            continue
        text = tag.get_text(strip=True)
        if text and not _EMAIL_RE.match(text):
            return text
    return None
