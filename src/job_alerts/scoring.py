"""Explainable 0–100 relevance scoring.

Every point a job gains or loses is recorded as a human-readable line in
`Job.score_explanation`, so "why did this score 72?" is always answerable from
the stored record. Weights come from settings — none of the numbers below are
hard-coded.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .config import ScoringSettings
from .filtering import find_matches, matches, requires_completed_phd
from .models import Job, Language
from .normalization import normalize_text_key

# Title words that mark a role as junior/student-level.
_STUDENT_TITLE_KEYWORDS = (
    "research assistant",
    "research intern",
    "research internship",
    "student",
    "studentische",
    "wissenschaftliche hilfskraft",
    "hilfskraft",
    "hiwi",
    "werkstudent",
    "working student",
    "intern",
    "praktikum",
    "praktikant",
    "thesis",
    "masterarbeit",
    "abschlussarbeit",
    "bachelorarbeit",
    "master",
    "graduate",
    "trainee",
    "assistant",
)

_SENIOR_TITLE_KEYWORDS = (
    "senior",
    "principal",
    "lead",
    "director",
    "head of",
    "chief",
    "manager",
    "professor",
    "w1",
    "w2",
    "w3",
    "postdoc",
    "postdoctoral",
    "gruppenleiter",
    "abteilungsleiter",
    "leiter",
)


class Scorer:
    """Scores a `Job` against the configured weights."""

    def __init__(
        self,
        settings: ScoringSettings,
        *,
        topics: list[str],
        locations: list[str],
        phd_signals: list[str] | None = None,
    ) -> None:
        self.settings = settings
        self.weights = settings.weights
        self.topics = topics
        self.locations = locations
        # The "does this job require a doctorate?" signals live on
        # FilteringSettings, since filtering and scoring must agree on the
        # answer. Injected rather than duplicated in config so they cannot
        # drift apart.
        self.phd_signals = phd_signals or []

    def score(self, job: Job, *, now: datetime | None = None) -> tuple[int, list[str]]:
        """Return (score capped to 0–100, explanation lines)."""
        now = now or datetime.now(UTC)
        w = self.weights
        total = 0
        why: list[str] = []

        def add(points: int, reason: str) -> None:
            nonlocal total
            if points:
                total += points
                why.append(f"{points:+d} {reason}")

        title = job.title or ""
        description = job.description or ""
        haystack = f"{title} {description}"

        # --- positive signals ------------------------------------------------
        exact = self._exact_title_match(title)
        if exact:
            add(w.exact_title_match, f"exact target title match: {exact!r}")
        else:
            # Only award the weaker title signal when the strong one missed;
            # otherwise a "Research Assistant" title collects both and the
            # weights stop meaning what the spec says they mean.
            student_hit = next((k for k in _STUDENT_TITLE_KEYWORDS if matches(title, k)), None)
            if student_hit:
                add(
                    w.research_or_student_keyword_in_title,
                    f"research/student keyword in title: {student_hit!r}",
                )

        topics_in_title = find_matches(title, self.topics)
        if topics_in_title:
            add(w.topic_in_title, f"topic in title: {', '.join(topics_in_title[:3])}")

        topics_in_description = [
            t for t in find_matches(description, self.topics) if t not in topics_in_title
        ]
        if topics_in_description:
            add(
                w.topic_in_description,
                f"topic in description: {', '.join(topics_in_description[:3])}",
            )

        location_hit = self._location_match(job)
        if location_hit:
            add(w.location_match, f"location match: {location_hit!r}")

        masters_hit = next((s for s in self.settings.masters_signals if matches(haystack, s)), None)
        if masters_hit:
            add(w.masters_explicitly_eligible, f"Master's students eligible: {masters_hit!r}")

        if job.language is Language.EN:
            add(w.english_speaking_role, "English-language posting")

        if self._is_recent(job, now):
            add(w.recently_published, f"published within {self.settings.recent_days} days")

        # --- negative signals ------------------------------------------------
        if requires_completed_phd(haystack, self.phd_signals):
            add(w.phd_required, "requires a completed PhD")

        senior_hit = next((k for k in _SENIOR_TITLE_KEYWORDS if matches(title, k)), None)
        if senior_hit:
            add(w.senior_role, f"senior/leadership title: {senior_hit!r}")

        unrelated_hit = next(
            (d for d in self.settings.unrelated_disciplines if matches(haystack, d)), None
        )
        if unrelated_hit:
            add(w.unrelated_discipline, f"unrelated discipline: {unrelated_hit!r}")

        capped = max(0, min(100, total))
        if capped != total:
            why.append(f"= {total} capped to {capped}")
        return capped, why

    def apply(self, job: Job, *, now: datetime | None = None) -> Job:
        """Score `job` in place and return it."""
        score, why = self.score(job, now=now)
        job.relevance_score = score
        job.score_explanation = why
        return job

    # -- helpers ---------------------------------------------------------

    def _exact_title_match(self, title: str) -> str | None:
        """An exact match means the title *is* the target role.

        The rule is "equals, or starts with, the target". Real German postings
        are long and descriptive — "STUDENTISCHE HILFSKRAFT im Bereich
        Softwareentwicklung für Simulationen" *is* a studentische Hilfskraft
        role and must score as one. Anchoring at the start is what separates
        that from a title where the target merely appears somewhere ("Support
        the research assistant team"), which should not count.

        Leading noise that boards prepend ("Initiativbewerbung Praktikum /
        Bachelor- und Masterarbeiten / Studentische Hilfskraft") is handled by
        also testing each `/`- or `-`-separated segment.
        """
        if not title.strip():
            return None

        # Split the RAW title: folding strips punctuation, so separators must
        # be found before normalization or the split silently never fires.
        segments = [normalize_text_key(title)]
        for part in re.split(r"[/|,;:·–—]|\s-\s", title):
            folded_part = normalize_text_key(part)
            if folded_part:
                segments.append(folded_part)

        for target in self.settings.exact_titles:
            folded_target = normalize_text_key(target)
            if not folded_target:
                continue
            for segment in segments:
                if segment == folded_target or segment.startswith(f"{folded_target} "):
                    return target
        return None

    def _location_match(self, job: Job) -> str | None:
        """The named city this job is in, if the text says one.

        There used to be a fallback here: any job whose `country` read "Germany"
        scored the location points. Since `country` defaulted to "Germany" and
        nothing verified it, every job in the database collected those points and
        the weight discriminated nothing — a Nigerian posting scored the same
        location bonus as one in Munich. The fallback is gone; a job now earns
        these points only by naming a place we are looking in.
        """
        haystack = " ".join(filter(None, [job.location, job.title, job.description]))
        if not haystack.strip():
            return None
        for location in self.locations:
            if matches(haystack, location, word_boundary=False):
                return location
        return None

    def _is_recent(self, job: Job, now: datetime) -> bool:
        if not job.published_at:
            return False
        return job.published_at >= now - timedelta(days=self.settings.recent_days)
