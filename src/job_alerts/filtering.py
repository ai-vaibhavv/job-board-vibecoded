"""Keyword filtering across German and English postings.

Two rules drive the design:

* Matching is on word boundaries. Without it "hiwi" matches inside unrelated
  words and "senior" rejects a job mentioning "seniority policy".
* A PhD mention is not a PhD requirement. "PhD students are also welcome to
  apply" must not reject a HiWi role, so PhD-flavoured negatives only fire when
  the text shows a genuine requirement. The spec calls this out explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from .config import FilteringSettings, KeywordSettings
from .models import Job
from .normalization import normalize_text_key

# Negatives that describe a doctorate. Handled by the PhD nuance rule rather
# than rejecting on sight.
_PHD_NEGATIVES = frozenset(
    {"postdoc", "postdoctoral", "mandatory completed phd", "doctorate required", "phd", "promotion"}
)

# Negatives that only make sense as a *title* signal. "senior" in a description
# ("you report to a senior researcher") says nothing about the role's own level,
# so these are checked against the title only.
_TITLE_ONLY_NEGATIVES = frozenset({"senior", "principal", "director", "head of", "lead"})


# A light, multilingual academic-vocabulary signal. The authoritative in-scope
# decision is the LLM's `is_academic_opportunity` (LabScout is LLM-first), but a
# deterministic signal is useful for ranking, logging, and any future
# LLM-unavailable path. Folded through `normalize_text_key`, so umlauts and
# punctuation do not matter; compared as substrings on the folded haystack.
ACADEMIC_TERMS = frozenset(
    normalize_text_key(t)
    for t in (
        # English
        "university", "faculty", "department", "institute", "laboratory", "research group",
        "research assistant", "student assistant", "teaching assistant", "research fellow",
        "research internship", "phd position", "doctoral", "postdoc", "thesis", "professor",
        "chair of", "graduate school", "research project", "principal investigator",
        # German
        "universitat", "hochschule", "fakultat", "fachbereich", "institut", "labor",
        "lehrstuhl", "arbeitsgruppe", "hilfskraft", "hiwi", "werkstudent", "forschung",
        "forschungspraktikum", "wissenschaftliche", "studentische", "doktorand",
        "promotion", "abschlussarbeit", "bachelorarbeit", "masterarbeit", "tutor",
    )
)


def looks_academic(*texts: str | None) -> bool:
    """True when any academic-vocabulary term appears in the given texts.

    A best-effort heuristic, never a hard gate on its own — a company can say
    "university" and a real lab posting can omit every listed word. Used to
    enrich, not to decide.
    """
    haystack = normalize_text_key(" ".join(t for t in texts if t))
    return any(term in haystack for term in ACADEMIC_TERMS)


@dataclass(slots=True)
class FilterDecision:
    """Why a job was kept or dropped. Surfaced by `list`/`export` and logged."""

    passed: bool
    matched_keywords: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed


@lru_cache(maxsize=2048)
def _compile(keyword: str, word_boundary: bool) -> re.Pattern[str]:
    """Cached pattern for one keyword.

    The cache matters: filtering runs every keyword against every candidate on
    every run, and these patterns never change within a process.

    A trailing `s`/`es` is optional on the final word, so a singular keyword
    still matches a plural posting. Real listings say "Master's students are
    encouraged to apply" and "we are hiring research assistants"; without this,
    the singular keywords in settings.yaml quietly miss them. German plurals
    are not covered — "Hilfskräfte" is not "Hilfskraft" plus a suffix — so
    those stay explicit entries in the keyword list.
    """
    folded = normalize_text_key(keyword)
    escaped = re.escape(folded).replace(r"\ ", r"\s+")
    pattern = rf"\b{escaped}(?:es|s)?\b" if word_boundary else escaped
    return re.compile(pattern, re.IGNORECASE)


def matches(text: str, keyword: str, *, word_boundary: bool = True) -> bool:
    """Does `keyword` occur in `text`? Both are folded first, so umlaut and
    punctuation differences ("Wissenschaftliche Hilfskraft (m/w/d)") still hit."""
    if not text or not keyword:
        return False
    return _compile(keyword, word_boundary).search(normalize_text_key(text)) is not None


def find_matches(text: str, keywords: list[str], *, word_boundary: bool = True) -> list[str]:
    folded = normalize_text_key(text)
    if not folded:
        return []
    return [kw for kw in keywords if _compile(kw, word_boundary).search(folded)]


def requires_completed_phd(text: str, signals: list[str], *, word_boundary: bool = True) -> bool:
    """True only when the text demands a *finished* doctorate."""
    return any(matches(text, signal, word_boundary=word_boundary) for signal in signals)


def is_negative_hit(
    job: Job,
    keyword: str,
    settings: FilteringSettings,
    haystack: str,
) -> tuple[bool, str]:
    """Should `keyword` reject this job? Returns (reject, reason)."""
    wb = settings.word_boundary_matching
    folded_keyword = normalize_text_key(keyword)

    if folded_keyword in _TITLE_ONLY_NEGATIVES:
        if matches(job.title, keyword, word_boundary=wb):
            return True, f"negative keyword in title: {keyword!r}"
        return False, ""

    is_phd_negative = (
        folded_keyword in _PHD_NEGATIVES or "phd" in folded_keyword or "promotion" in folded_keyword
    )
    if is_phd_negative and settings.phd_requires_explicit_signal:
        # A postdoc/professor *title* is a hard no regardless of the nuance rule:
        # the role itself is out of reach, not merely PhD-adjacent.
        if matches(job.title, keyword, word_boundary=wb) and folded_keyword in {
            "postdoc",
            "postdoctoral",
        }:
            return True, f"negative keyword in title: {keyword!r}"
        if requires_completed_phd(haystack, settings.phd_requirement_signals, word_boundary=wb):
            return True, f"job requires a completed PhD (matched {keyword!r})"
        return False, ""

    if matches(haystack, keyword, word_boundary=wb):
        return True, f"negative keyword: {keyword!r}"
    return False, ""


def filter_job(job: Job, keywords: KeywordSettings, settings: FilteringSettings) -> FilterDecision:
    """Keep or drop one job.

    A job must match at least one positive keyword, and must not trip a
    negative. Positives are checked against title + description so that a
    generically titled posting ("Open position at the AI lab") whose body says
    "studentische Hilfskraft" still gets through.
    """
    wb = settings.word_boundary_matching
    haystack = " ".join(filter(None, [job.title, job.description, job.organization]))

    positive_hits = find_matches(haystack, keywords.positive, word_boundary=wb)
    if not positive_hits:
        return FilterDecision(False, [], ["no positive keyword matched"])

    for keyword in keywords.negative:
        rejected, reason = is_negative_hit(job, keyword, settings, haystack)
        if rejected:
            return FilterDecision(False, positive_hits, [reason])

    return FilterDecision(
        True,
        positive_hits,
        [f"matched {len(positive_hits)} positive keyword(s): {', '.join(positive_hits[:5])}"],
    )


def is_recent_enough(
    job: Job, max_age_days: int, now=None, *, drop_undated_when_enriched: bool = True
) -> bool:
    """Within the age window?

    The hard part is a job with no date, and the answer turns on whether we ever
    looked. Two very different situations produce the same empty field:

      * we only ever saw a search snippet, or the page refused to be fetched —
        we have no idea how old it is, and inventing an opinion would silently
        blind whole sources (every job in the first live database was undated);
      * we fetched the real posting and it genuinely states no date — that is a
        finding, and a hiring post nobody dated is not one to chase.

    `enriched_at` is what separates them. Un-enriched, we fall back to
    `discovered_at` and keep the job. Enriched and still undated, we drop it.
    The rule therefore tightens on its own as enrichment coverage grows, instead
    of needing a flag day.
    """
    from datetime import UTC, datetime, timedelta

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=max_age_days)

    if job.published_at:
        return job.published_at >= cutoff
    if drop_undated_when_enriched and job.enriched_at is not None:
        return False
    if job.discovered_at is None:
        return True
    return job.discovered_at >= cutoff


def matches_location(job: Job, locations: list[str], all_germany: bool) -> bool:
    """Is this job in an acceptable place?

    With `all_germany` on, everything passes — the location list is then only a
    scoring hint. With it off, the job must match a configured location, but a
    job with *no* location string still passes: German sources routinely omit
    it, and rejecting on a missing field loses real jobs.
    """
    if all_germany:
        return True
    haystack = " ".join(filter(None, [job.location, job.title, job.description]))
    if not normalize_text_key(job.location or ""):
        return True
    return any(matches(haystack, loc, word_boundary=False) for loc in locations)
