"""Core data models shared by every source, filter and notifier."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RemoteStatus(StrEnum):
    ON_SITE = "on_site"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class JobStatus(StrEnum):
    NEW = "new"
    """Stored, passed filtering, not yet sent."""
    NOTIFIED = "notified"
    """Discord confirmed delivery."""
    REJECTED = "rejected"
    """Filtered out, or scored below the notify threshold."""


class Language(StrEnum):
    EN = "en"
    DE = "de"
    UNKNOWN = "unknown"


class JobCandidate(BaseModel):
    """A job as a source found it, before normalization.

    Sources are deliberately allowed to be sloppy here: dates arrive as raw
    strings in whatever format the site uses, text still has HTML in it, and
    most fields are optional. `normalization.normalize_candidate` turns this
    into a `Job`. Keeping the messy shape separate from the clean one means a
    badly behaved source cannot put junk into the database.
    """

    model_config = ConfigDict(extra="ignore")

    source: str
    source_job_id: str | None = None
    title: str
    organization: str | None = None
    location: str | None = None
    country: str | None = None
    description: str | None = None
    url: str
    published_at: str | datetime | None = None
    application_deadline: str | datetime | None = None
    employment_type: str | None = None
    salary: str | None = None

    contact_email: str | None = None
    contact_url: str | None = None
    """How to apply, when the source knows. A LinkedIn post often names an
    address and nothing else — that address is the advert. Most sources leave
    these empty and the enricher fills them from the posting page instead."""

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("title must not be blank")
        return cleaned


class Job(BaseModel):
    """A normalized, deduplicated, scored job. This is what gets persisted."""

    model_config = ConfigDict(extra="forbid")

    id: str
    """Stable identity. `source:source_job_id` when the source gives an id,
    otherwise a hash of the normalized url/title/organization/location."""

    source: str
    source_job_id: str | None = None
    title: str
    organization: str | None = None
    location: str | None = None

    country: str | None = None
    """Where the job actually is, when that is known — otherwise None.

    This defaulted to "Germany" and nothing ever verified it, so every one of
    the 125 rows in the first live database claimed Germany, including the
    Nigerian and Sri Lankan LinkedIn postings that a broken `site:` filter let
    in. A default is not a fact. A source's `defaults: {country: Germany}` block
    still sets this, because a source that only ever lists German jobs genuinely
    knows; a guess made here would not.
    """

    city: str | None = None
    """The town, when the posting names one. For reading, not for maths — there
    is no radius search, only a country allowlist."""

    remote_status: RemoteStatus = RemoteStatus.UNKNOWN
    description: str | None = None
    url: str

    contact_email: str | None = None
    contact_url: str | None = None
    """How to apply, when the posting is someone saying "email me" rather than a
    job page with an apply button. Surfaced in the alert; never mailed
    automatically — an untailored auto-application burns a real contact."""

    published_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    enriched_at: datetime | None = None
    """When the real posting page was last fetched, or None if it never was.

    Load-bearing for the recency rule. A job with no `published_at` might be
    undated because nobody dated it, or because we only ever saw a search
    snippet. Only the first is grounds for dropping it, and this is what tells
    them apart."""
    application_deadline: datetime | None = None
    employment_type: str | None = None
    language: Language = Language.UNKNOWN
    salary: str | None = None
    relevance_score: int = 0
    matched_keywords: list[str] = Field(default_factory=list)
    score_explanation: list[str] = Field(default_factory=list)
    """Human-readable reasons behind `relevance_score`, e.g.
    "+30 exact title match: 'research assistant'". Stored so a score can
    always be justified after the fact."""

    card_summary: str | None = None
    """A short, uniform blurb the LLM wrote for the Discord card (see
    `JobAssessment.card_summary`). None when there was no LLM verdict; the
    notifier then falls back to a trimmed posting excerpt."""

    opportunity_type: str | None = None
    """LabScout academic taxonomy — a `taxonomy.OpportunityType` value (hiwi,
    master_thesis, phd_position, …). Filled by the Pass-2 LLM detail call; None
    until a job has been fine-classified. Stored as a plain string, coerced onto
    the enum where it is read/displayed."""

    applicant_level: str | None = None
    """A `taxonomy.ApplicantLevel` value (bachelor / master / phd_applicant / …).
    Pass-2 output; None until classified."""

    academic_field: str | None = None
    """A `taxonomy.AcademicField` value (ml / robotics / physics / …). Pass-2
    output; None until classified."""

    content_hash: str
    """Hash of the meaningful content. Changes when a posting is edited,
    which lets us tell a genuinely updated job from a duplicate."""

    notified_at: datetime | None = None
    status: JobStatus = JobStatus.NEW

    def short_description(self, limit: int) -> str:
        """Description trimmed to `limit` chars on a word boundary."""
        if not self.description:
            return ""
        text = " ".join(self.description.split())
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


class SearchQuery(BaseModel):
    """What the pipeline asks each source for."""

    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    all_germany: bool = True
    max_age_days: int = 30
    max_results: int = 100


class SourceResult(BaseModel):
    """Outcome of one source, successful or not.

    Sources fail independently: the pipeline records the failure here and keeps
    going rather than letting one dead site kill the run.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    candidates: list[JobCandidate] = Field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped_reason is None


class RunSummary(BaseModel):
    """The concise per-run report the spec asks for."""

    model_config = ConfigDict(extra="forbid")

    started_at: datetime
    finished_at: datetime
    sources_ok: list[str] = Field(default_factory=list)
    sources_failed: dict[str, str] = Field(default_factory=dict)
    sources_skipped: dict[str, str] = Field(default_factory=dict)
    unhealthy_sources: dict[str, str] = Field(default_factory=dict)
    """Sources whose rolling health crossed a warning threshold (repeatedly empty
    or erroring), source -> reason. A source failing once is normal; failing every
    run means a rotted selector or a moved feed that needs a look."""
    candidates_found: int = 0
    after_dedup: int = 0
    passed_filter: int = 0
    enriched: int = 0
    """Jobs whose own page was successfully fetched and read."""
    enrich_failed: int = 0
    """Wanted enriching but could not be — robots, 404, timeout, denied host.
    Not an error: they carry on with whatever the source gave."""
    dropped_as_stale: int = 0
    """Older than max_age_days, or fetched and found to carry no date at all."""
    above_threshold: int = 0
    newly_stored: int = 0
    notified: int = 0
    notify_failed: int = 0
    llm_assessed: int = 0
    llm_cached: int = 0
    """Verdicts reused from a previous run, costing no API quota at all. In a
    steady state this should be most of them — if it is not, the cache key is
    wrong and every run is re-buying yesterday's answers."""
    llm_fallback: int = 0
    """Jobs the LLM could not judge, scored by keywords instead."""
    llm_failures: list[str] = Field(default_factory=list)
    dry_run: bool = False

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def render(self) -> str:
        lines = [
            "",
            "══ Run summary ══════════════════════════════════════════",
            f"  Duration        : {self.duration_seconds:.1f}s"
            + ("   (DRY RUN — nothing sent)" if self.dry_run else ""),
            f"  Sources OK      : {len(self.sources_ok)}"
            + (f" ({', '.join(self.sources_ok)})" if self.sources_ok else ""),
        ]
        if self.sources_skipped:
            lines.append(f"  Sources skipped : {len(self.sources_skipped)}")
            for name, why in self.sources_skipped.items():
                lines.append(f"      - {name}: {why}")
        if self.sources_failed:
            lines.append(f"  Sources FAILED  : {len(self.sources_failed)}")
            for name, why in self.sources_failed.items():
                lines.append(f"      - {name}: {why}")
        if self.unhealthy_sources:
            lines.append(f"  Sources AILING  : {len(self.unhealthy_sources)}")
            for name, why in self.unhealthy_sources.items():
                lines.append(f"      ⚠ {name}: {why}")
        if self.llm_assessed or self.llm_fallback:
            lines.append(
                f"  LLM assessed    : {self.llm_assessed}"
                + (f"   ({self.llm_cached} from cache)" if self.llm_cached else "")
                + (f"   ({self.llm_fallback} fell back to keywords)" if self.llm_fallback else "")
            )
            for failure in self.llm_failures:
                lines.append(f"      ! {failure[:90]}")
        lines += [
            f"  Candidates      : {self.candidates_found}",
            f"  After dedup     : {self.after_dedup}",
            f"  Passed filter   : {self.passed_filter}",
        ]
        if self.enriched or self.enrich_failed:
            lines.append(
                f"  Enriched        : {self.enriched}"
                + (f"   ({self.enrich_failed} could not be fetched)" if self.enrich_failed else "")
            )
        if self.dropped_as_stale:
            lines.append(f"  Dropped (stale) : {self.dropped_as_stale}")
        lines += [
            f"  Above threshold : {self.above_threshold}",
            f"  Newly stored    : {self.newly_stored}",
            f"  Notified        : {self.notified}"
            + (f"   ({self.notify_failed} failed)" if self.notify_failed else ""),
            "═════════════════════════════════════════════════════════",
        ]
        return "\n".join(lines)
