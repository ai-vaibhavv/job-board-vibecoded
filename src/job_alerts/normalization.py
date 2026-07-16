"""Turning messy source output into clean, comparable `Job` records.

The single most important job of this module is making identity *stable*: the
same posting discovered via an RSS feed, a Google result and a LinkedIn link
must collapse to one row, even though the three URLs carry different tracking
junk. Everything else here (dates, language, remote status) is best-effort and
degrades to a sensible default rather than raising.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .models import Job, JobCandidate, Language, RemoteStatus

BERLIN = ZoneInfo("Europe/Berlin")

# Query parameters that identify a *referral*, not a *resource*. Dropping these
# is what makes the same posting from two discovery routes deduplicate.
_TRACKING_PARAM_PREFIXES = ("utm_", "pk_", "mtm_", "ga_", "hsa_", "vero_", "_hs")
_TRACKING_PARAMS_EXACT = frozenset(
    {
        "trk",
        "trkinfo",
        "refid",
        "ref",
        "referer",
        "referrer",
        "source",
        "src",
        "gclid",
        "fbclid",
        "msclkid",
        "dclid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "spm",
        "scid",
        "campaign",
        "campaignid",
        "adgroupid",
        "position",
        "trackingid",
        "trackingId",
        "originalsubdomain",
        "original_referer",
        "savedsearchid",
        "eblanded",
        "lipi",
        "licu",
        "sessionid",
        "jsessionid",
        "phpsessid",
    }
)

# LinkedIn job URLs carry the canonical id in the path; everything else in the
# query string is tracking. `/jobs/view/some-title-at-org-4012345678` and
# `/jobs/view/4012345678` are the same posting.
_LINKEDIN_JOB_RE = re.compile(r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})")

# LinkedIn feed posts: `/posts/<author>_<slugified-first-words>-activity-<id>-<hash>`.
# The author and the activity id are stable; the slug is derived from the post
# text and the trailing hash VARIES BETWEEN SHARES OF THE SAME POST. Keep the
# hash and one post arrives as a new job every single run, forever — the exact
# failure the `/jobs/view/` collapse above already prevents for job pages.
#
# The author stays in the canonical form because the id alone is enough to
# identify the post but not enough to read it back; two different authors cannot
# share an activity id, so this costs nothing and keeps the URL meaningful.
_LINKEDIN_POST_RE = re.compile(r"/posts/([\w\-%.]+?)_[^/?#]*?-activity-(\d{10,})")


def is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in {p.lower() for p in _TRACKING_PARAMS_EXACT} or lowered.startswith(
        _TRACKING_PARAM_PREFIXES
    )


def normalize_url(url: str) -> str:
    """Strip tracking, unify casing/slashes, keep the parameters that matter.

    Deliberately conservative: query parameters that are not recognisably
    tracking are kept and sorted, because on many job boards the posting id
    lives in the query string (`?jobId=123`). Dropping unknown parameters would
    merge genuinely different jobs.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url

    parts = urlsplit(url)
    scheme = parts.scheme.lower() or "https"
    if scheme == "http":
        # Practically every job board redirects to https; unifying prevents the
        # same posting appearing twice under two schemes.
        scheme = "https"

    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Drop default ports.
    netloc = re.sub(r":(80|443)$", "", netloc)

    path = parts.path or "/"

    # Collapse LinkedIn URLs to their canonical numeric form. Country subdomains
    # (de./ng./lk.) all collapse to the bare host, so the same posting found via
    # two national mirrors deduplicates to one row.
    if netloc == "linkedin.com" or netloc.endswith(".linkedin.com"):
        match = _LINKEDIN_JOB_RE.search(path)
        if match:
            return f"https://linkedin.com/jobs/view/{match.group(1)}"
        match = _LINKEDIN_POST_RE.search(path)
        if match:
            return f"https://linkedin.com/posts/{match.group(1)}-activity-{match.group(2)}"

    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not is_tracking_param(k)
    ]
    query = urlencode(sorted(kept))

    # Fragments never identify a posting.
    return urlunsplit((scheme, netloc, path, query, ""))


def strip_html(value: str | None) -> str:
    """HTML -> readable plain text. Cheap and forgiving."""
    if not value:
        return ""
    if "<" not in value:
        return " ".join(value.split())
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d.%m.%Y",  # German
    "%d.%m.%y",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
)

_GERMAN_MONTHS = {
    "januar": "January",
    "februar": "February",
    "märz": "March",
    "maerz": "March",
    "april": "April",
    "mai": "May",
    "juni": "June",
    "juli": "July",
    "august": "August",
    "september": "September",
    "oktober": "October",
    "november": "November",
    "dezember": "December",
}


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parse whatever a site gave us into an aware UTC datetime.

    Returns None instead of raising: a job with an unparseable date is still a
    useful job, and the spec requires malformed dates be handled gracefully.
    Naive datetimes are read as Europe/Berlin, since every source here is German.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)

    text = str(value).strip()
    if not text:
        return None

    # RFC 2822, the RSS/Atom default.
    try:
        return _as_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError):
        pass

    try:
        return _as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass

    lowered = text.lower()
    for german, english in _GERMAN_MONTHS.items():
        if german in lowered:
            text = re.sub(german, english, lowered, flags=re.IGNORECASE)
            break

    for fmt in _DATE_FORMATS:
        try:
            return _as_utc(datetime.strptime(text, fmt))
        except ValueError:
            continue

    # Last resort: a bare date embedded in a longer string.
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            return _as_utc(datetime(int(match[1]), int(match[2]), int(match[3])))
        except ValueError:
            return None
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if match:
        try:
            return _as_utc(datetime(int(match[3]), int(match[2]), int(match[1])))
        except ValueError:
            return None
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=BERLIN)
    return value.astimezone(UTC)


_GERMAN_MARKERS = (
    " und ",
    " für ",
    " mit ",
    " der ",
    " die ",
    " das ",
    " bei ",
    " wir ",
    "hilfskraft",
    "werkstudent",
    "studentische",
    "wissenschaftliche",
    "masterarbeit",
    "abschlussarbeit",
    "bewerbung",
    "kenntnisse",
    "aufgaben",
    "stelle",
    "mitarbeiter",
    "forschung",
    "studium",
    "sucht",
)
_ENGLISH_MARKERS = (
    " and ",
    " for ",
    " with ",
    " the ",
    " we ",
    " you ",
    " your ",
    "research",
    "student",
    "thesis",
    "candidate",
    "apply",
    "skills",
    "responsibilities",
    "position",
    "experience",
    "team",
)


def detect_language(*texts: str | None) -> Language:
    """Which language is the posting written in? Crude marker counting.

    Good enough for a +5 scoring nudge, and never worth a dependency.
    """
    blob = " ".join(t for t in texts if t).lower()
    if not blob.strip():
        return Language.UNKNOWN
    blob = f" {blob} "
    german = sum(blob.count(m) for m in _GERMAN_MARKERS)
    english = sum(blob.count(m) for m in _ENGLISH_MARKERS)
    # Umlauts/ß are a strong German signal that short texts otherwise miss.
    german += 2 * len(re.findall(r"[äöüßÄÖÜ]", blob))
    if german == 0 and english == 0:
        return Language.UNKNOWN
    if german > english:
        return Language.DE
    if english > german:
        return Language.EN
    return Language.UNKNOWN


_REMOTE_PATTERNS = (
    (
        RemoteStatus.HYBRID,
        ("hybrid", "teilweise remote", "partly remote", "remote possible", "remote optional"),
    ),
    (
        RemoteStatus.REMOTE,
        (
            "fully remote",
            "100% remote",
            "remote work",
            "homeoffice",
            "home office",
            "work from home",
            "remote position",
            "vollständig remote",
        ),
    ),
    (RemoteStatus.ON_SITE, ("on-site", "on site", "vor ort", "präsenz", "in person")),
)


def detect_remote_status(*texts: str | None) -> RemoteStatus:
    blob = " ".join(t for t in texts if t).lower()
    if not blob.strip():
        return RemoteStatus.UNKNOWN
    # Hybrid first: "hybrid (remote possible)" is hybrid, not remote.
    for status, patterns in _REMOTE_PATTERNS:
        if any(p in blob for p in patterns):
            return status
    return RemoteStatus.UNKNOWN


def normalize_text_key(value: str | None) -> str:
    """Fold text for comparison: lowercase, punctuation-free, single-spaced."""
    if not value:
        return ""
    lowered = value.lower().strip()
    lowered = lowered.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    return " ".join(lowered.split())


def content_hash(
    title: str, organization: str | None, location: str | None, description: str | None
) -> str:
    """Hash of the parts that make a posting *this* posting.

    Description is included but truncated: long descriptions often carry
    volatile boilerplate (view counters, "posted 3 days ago") that would make
    the hash unstable across runs.
    """
    parts = [
        normalize_text_key(title),
        normalize_text_key(organization),
        normalize_text_key(location),
        normalize_text_key(description)[:500],
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def derive_job_id(candidate: JobCandidate, normalized_url: str) -> str:
    """Stable identity for a posting.

    Prefers `source:source_job_id` per the spec. Falls back to a hash over the
    normalized URL plus title/org/location — the URL alone is not enough,
    because some boards serve several postings from one URL with a fragment we
    have already stripped.
    """
    if candidate.source_job_id:
        return f"{candidate.source}:{candidate.source_job_id}"
    digest = hashlib.sha256(
        "|".join(
            [
                normalized_url,
                normalize_text_key(candidate.title),
                normalize_text_key(candidate.organization),
                normalize_text_key(candidate.location),
            ]
        ).encode()
    ).hexdigest()[:24]
    return f"{candidate.source}:h:{digest}"


_MAX_DESCRIPTION_CHARS = 5000


def normalize_candidate(
    candidate: JobCandidate,
    *,
    default_country: str | None = None,
    max_description_chars: int = _MAX_DESCRIPTION_CHARS,
    now: datetime | None = None,
) -> Job:
    """`JobCandidate` -> `Job`. Never raises on bad data; fills defaults.

    `default_country` is None on purpose: this used to default to "Germany" and
    stamped that on every job regardless of where it was. A source that knows its
    own country still says so via its `defaults:` block, which reaches the
    candidate before this runs.
    """
    now = now or datetime.now(UTC)
    url = normalize_url(candidate.url)
    title = " ".join(strip_html(candidate.title).split())
    description = strip_html(candidate.description)[:max_description_chars] or None
    organization = strip_html(candidate.organization) or None
    location = strip_html(candidate.location) or None

    published_at = parse_datetime(candidate.published_at)
    # A published date in the future is a parsing artefact (e.g. a US-format
    # date read as day-first). Treat it as unknown rather than "brand new",
    # otherwise it would silently collect the recency bonus forever.
    if published_at and published_at > now + timedelta(days=1):
        published_at = None

    return Job(
        id=derive_job_id(candidate, url),
        source=candidate.source,
        source_job_id=candidate.source_job_id,
        title=title,
        organization=organization,
        location=location,
        country=candidate.country or default_country,
        remote_status=detect_remote_status(title, location, description),
        description=description,
        url=url,
        contact_email=strip_html(candidate.contact_email) or None,
        contact_url=strip_html(candidate.contact_url) or None,
        published_at=published_at,
        discovered_at=now,
        application_deadline=parse_datetime(candidate.application_deadline),
        employment_type=candidate.employment_type,
        language=detect_language(title, description),
        salary=candidate.salary,
        content_hash=content_hash(title, organization, location, description),
    )
