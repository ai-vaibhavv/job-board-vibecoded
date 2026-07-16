"""Fetching the real posting, because a search snippet is not a job.

WHY THIS EXISTS
    Everything downstream was judging shadows. A search result gives a title, a
    URL and ~160 characters; a list page gives a title and nothing else. The
    README already recorded the consequence — a title-only job ceilings out at
    exactly 55 (30 exact title + 15 topic in title + 10 location) — and a
    measurement of the first live database found the rest of it: 76/100
    search-discovered jobs had no location at all, and 125/125 had no date.

    No amount of cleverness extracts a city from a sentence that does not
    contain one. The page does contain one. So fetch the page.

WHAT IT DOES NOT DO
    It does not fetch LinkedIn: `http.py` refuses that host outright, and the
    refusal is deliberate rather than incidental (LinkedIn's robots.txt permits
    us, its terms do not). It does not retry forever, it does not bypass
    robots.txt, and it never raises into the pipeline: a job that could not be
    enriched keeps whatever thin metadata it arrived with and carries on, the
    same way `BaseSource.run` isolates a failing source.

THE POINT OF `enriched_at`
    A job with no date might have no date because nobody published one, or
    because we only ever saw a search snippet. Those are different facts and the
    recency rule treats them differently — only the first is grounds for
    dropping the job. `enriched_at` is what tells them apart, so it is set
    whenever a fetch *succeeded*, including when the page turned out to say
    nothing useful.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .http import FetchError, PoliteClient
from .models import Job
from .normalization import parse_datetime, strip_html

logger = logging.getLogger(__name__)

# Chrome that is on every page and is never the job.
_STRIP_TAGS = ("script", "style", "nav", "header", "footer", "aside", "form", "noscript")

# More chrome, this time only identifiable by id/class. Breadcrumbs are the
# reason: a real TUM posting extracted as "Sitemap > Schwarzes Brett >
# Studentische Hilfskräfte, Praktikantenstellen, Studienarbeiten > ..." before
# the job text, which costs tokens on every LLM call and reads as noise in the
# alert.
_STRIP_SELECTORS = (
    "#portal-breadcrumbs",
    ".breadcrumb",
    ".breadcrumbs",
    "#breadcrumbs",
    "#portal-globalnav",
    "#portal-personaltools",
    ".skiplinks",
    ".cookie-banner",
    "#cookie-consent",
)

# Where a job page usually keeps its body, tightest first — the first match that
# clears `_MIN_USEFUL_DESCRIPTION` wins, so a container that wraps the whole page
# must come after one that wraps only the posting. Falls back to <body>.
#
# The Plone ids are here because Plone runs a lot of German university sites
# (TUM's board among them) and none of them emit <main>.
_MAIN_SELECTORS = (
    "main",
    "article",
    '[role="main"]',
    ".job-description",
    ".jobad",
    ".stellenanzeige",
    "#job",
    "#news-content",
    "#maincontentwrapper",
    "#content",
    ".content",
    "#portal-column-content",
)

# A description shorter than this is boilerplate ("Loading…", a cookie banner),
# not a posting. Keep what we already had rather than overwrite it with junk.
_MIN_USEFUL_DESCRIPTION = 200

_MAX_DESCRIPTION_CHARS = 20_000

# German job pages state the date in prose far more often than in markup.
_DATE_LABEL_RE = re.compile(
    r"(?:ver(?:ö|oe)ffentlicht(?:\s+am)?|online\s+seit|eingestellt\s+am|posted(?:\s+on)?"
    r"|published(?:\s+on)?|date\s+posted)\s*[:\-]?\s*"
    r"(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Emails that are plumbing, not a person to apply to.
_EMAIL_NOISE = re.compile(
    r"^(?:no-?reply|do-?not-?reply|noreply|webmaster|postmaster|privacy|datenschutz"
    r"|impressum|info|support|abuse)@",
    re.IGNORECASE,
)

_APPLY_URL_RE = re.compile(
    r"(docs\.google\.com/forms|forms\.gle|forms\.office\.com)", re.IGNORECASE
)


class Enricher:
    """Fills a `Job` in from its own page."""

    def __init__(self, client: PoliteClient, *, min_description_chars: int = 400) -> None:
        self.client = client
        # Below this, the job is worth a fetch: the source gave us a stub.
        self.min_description_chars = min_description_chars

    def needs_enriching(self, job: Job) -> bool:
        if job.enriched_at is not None:
            return False
        if not job.url:
            return False
        thin = len(job.description or "") < self.min_description_chars
        return thin or job.published_at is None or job.location is None

    async def enrich(self, job: Job) -> Job:
        """Fetch and fill. Returns the job either way — never raises."""
        try:
            html = await self.client.get_text(job.url)
        except FetchError as exc:
            # Expected and survivable: robots said no, the host is denied, the
            # page 404'd, the site was slow. `enriched_at` stays None, which is
            # the honest record that we never got to look.
            logger.debug("not enriched: %s (%s)", job.url, exc)
            return job
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("unexpected error enriching %s: %s", job.url, exc)
            return job

        extracted = extract(html)

        # Only ever fill gaps or improve on a stub. A source that stated a fact
        # outranks our scrape of its page: `defaults:` and feed metadata are
        # assertions, this is inference.
        if extracted.description and len(extracted.description) > len(job.description or ""):
            job.description = extracted.description[:_MAX_DESCRIPTION_CHARS]
        if job.published_at is None and extracted.published_at:
            job.published_at = extracted.published_at
        if job.location is None and extracted.location:
            job.location = extracted.location
        if job.contact_email is None and extracted.contact_email:
            job.contact_email = extracted.contact_email
        if job.contact_url is None and extracted.contact_url:
            job.contact_url = extracted.contact_url

        # Set even when the page said nothing useful. "We looked and found no
        # date" is exactly the fact the recency rule needs.
        job.enriched_at = datetime.now(UTC)
        return job


class Extracted:
    """What a page yielded. Plain attributes; nothing is required."""

    __slots__ = ("contact_email", "contact_url", "description", "location", "published_at")

    def __init__(
        self,
        description: str | None = None,
        published_at: datetime | None = None,
        location: str | None = None,
        contact_email: str | None = None,
        contact_url: str | None = None,
    ) -> None:
        self.description = description
        self.published_at = published_at
        self.location = location
        self.contact_email = contact_email
        self.contact_url = contact_url


def extract(html: str) -> Extracted:
    """Pull what we can out of a job page. Pure, so it tests without a network."""
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    for selector in _STRIP_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()

    return Extracted(
        description=_main_text(soup),
        published_at=_published_at(soup),
        location=_location(soup),
        contact_email=_contact_email(soup),
        contact_url=_contact_url(soup),
    )


def _main_text(soup: BeautifulSoup) -> str | None:
    for selector in _MAIN_SELECTORS:
        node = soup.select_one(selector)
        if node:
            text = " ".join(node.get_text(" ", strip=True).split())
            if len(text) >= _MIN_USEFUL_DESCRIPTION:
                return text
    body = soup.body or soup
    text = " ".join(body.get_text(" ", strip=True).split())
    return text or None


def _published_at(soup: BeautifulSoup) -> datetime | None:
    # Machine-readable first: <time datetime>, then the meta tags that news and
    # ATS pages emit, then the date written in prose.
    for node in soup.select("time[datetime]"):
        parsed = parse_datetime(node.get("datetime"))
        if parsed:
            return parsed

    for selector in (
        'meta[property="article:published_time"]',
        'meta[name="date"]',
        'meta[itemprop="datePosted"]',
        'meta[name="dc.date"]',
    ):
        node = soup.select_one(selector)
        if node and (parsed := parse_datetime(node.get("content"))):
            return parsed

    text = soup.get_text(" ", strip=True)
    if match := _DATE_LABEL_RE.search(text):
        return parse_datetime(match.group(1))
    return None


# Labels that mean the element is a taxonomy, not a place. Fraunhofer's job
# pages put their tag list inside `<p class="job-location">` — the class name
# says location and the content says "Job Segment: Research Assistant,
# Industrial Engineer, Mechanical Engineer, Intern, ...". Trusting the class
# name shipped that string into the Location field of a Discord alert.
_NOT_A_PLACE_RE = re.compile(
    r"^\s*(job\s*segment|stellensegment|segment|kategorie|category|keywords?|tags?)\s*:",
    re.IGNORECASE,
)

_MAX_PLACE_CHARS = 60


def looks_like_a_place(value: str) -> bool:
    """Is this plausibly somewhere, rather than a taxonomy that landed in a
    field named after one?

    A place is short and has few commas: "Freiburg im Breisgau", "Garching bei
    München", "Berlin, Germany". A list of eight job categories is neither, and
    no selector can tell the difference — only the value can.
    """
    text = " ".join((value or "").split())
    if not text or len(text) > _MAX_PLACE_CHARS:
        return False
    if _NOT_A_PLACE_RE.match(text):
        return False
    # "Berlin, Germany" is fine. "Research Assistant, Industrial Engineer,
    # Mechanical Engineer, Intern, Research" is a list wearing a place's clothes.
    if text.count(",") > 2:
        return False
    return any(ch.isalpha() for ch in text)


def _location(soup: BeautifulSoup) -> str | None:
    for selector in (
        'meta[itemprop="jobLocation"]',
        '[itemprop="addressLocality"]',
        ".job-location",
        ".location",
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        value = strip_html(node.get("content") or node.get_text(" ", strip=True))
        value = " ".join(value.split())
        if value and looks_like_a_place(value):
            return value[:_MAX_PLACE_CHARS]
    return None


def _contact_email(soup: BeautifulSoup) -> str | None:
    """The address to apply to, if the page names one.

    Prefers a `mailto:` link — a page that links an address means it. Skips
    no-reply and webmaster addresses: they are plumbing, and a HiWi posting that
    only offers `noreply@` is not offering a way to apply.
    """
    for link in soup.select('a[href^="mailto:"]'):
        address = (link.get("href") or "")[7:].split("?")[0].strip()
        if address and not _EMAIL_NOISE.match(address):
            return address
    for match in _EMAIL_RE.finditer(soup.get_text(" ", strip=True)):
        if not _EMAIL_NOISE.match(match.group(0)):
            return match.group(0)
    return None


def _contact_url(soup: BeautifulSoup) -> str | None:
    """A Google Form or similar — the other way a lab says "apply here"."""
    for link in soup.select("a[href]"):
        href = link.get("href") or ""
        if _APPLY_URL_RE.search(href):
            return href
    return None


# --- plain text -------------------------------------------------------------
#
# A LinkedIn post has no markup — it is a paragraph someone typed. These work on
# that, and live here so the definition of "an address worth applying to" exists
# once rather than once per source.

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)


def contact_email_from_text(text: str) -> str | None:
    """The address a post says to apply to, if it names one."""
    for match in _EMAIL_RE.finditer(text or ""):
        if not _EMAIL_NOISE.match(match.group(0)):
            return match.group(0).rstrip(".,;:")
    return None


def apply_url_from_text(text: str) -> str | None:
    """A Google Form or similar named in the text."""
    for match in _URL_IN_TEXT_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:")
        if _APPLY_URL_RE.search(url):
            return url
    return None


def outbound_links_from_text(text: str) -> list[str]:
    """Every link in a post, in order, minus the ones that lead back to LinkedIn
    itself.

    `lnkd.in` is kept: it is LinkedIn's URL shortener, and a request to it
    returns a redirect to somewhere else entirely — the content we end up
    reading belongs to a university or an ATS, never to LinkedIn. Links to
    linkedin.com proper are dropped, because following them would mean reading
    LinkedIn, which this project does not do.
    """
    links: list[str] = []
    for match in _URL_IN_TEXT_RE.finditer(text or ""):
        url = match.group(0).rstrip(".,;:")
        host = (urlsplit(url).hostname or "").lower()
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            continue
        if url not in links:
            links.append(url)
    return links
