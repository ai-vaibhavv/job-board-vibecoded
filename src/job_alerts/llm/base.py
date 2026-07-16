"""The LLM assessment contract.

An LLM reads a job posting the way a person would — it understands that
"pursuing a PhD" is not "holds a PhD", that a page titled "Studentische
Hilfskraft Machine Learning Jobs" is a search page rather than a job, and that
"Sensordatenfusion" is signal processing even though that word is in nobody's
topic list. Keyword matching cannot do any of that.

The provider's only job is: given jobs, return one `JobAssessment` per job.
Everything else — fallback, batching, mapping onto `Job` — happens above.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import Job


class LlmError(RuntimeError):
    """A provider could not produce a usable assessment.

    Always recoverable by design: the chain catches this and tries the next
    provider, and if every provider raises, the pipeline falls back to keyword
    scoring. An LLM outage must never cost the user their job alerts.

    `transient` separates "try again in a moment" (429 rate limit, 503 overload,
    a timeout) from "this will fail identically forever" (401 bad key, 400 bad
    request). Retrying a bad key wastes a minute and still fails; not retrying a
    429 throws away a working provider. Both mistakes were observed live before
    this flag existed.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class JobAssessment(BaseModel):
    """One LLM verdict on one job.

    Fields mirror the judgments the keyword pipeline makes, so an assessment can
    be swapped in for a `Scorer` result without the pipeline caring which
    produced it.
    """

    model_config = ConfigDict(extra="ignore")

    job_id: str

    is_job_posting: bool = True
    """False for search-result pages, careers homepages, PDF indexes — anything
    that is not one applicable position. Catches the class of junk that the
    regex listing-filter can only approximate."""

    is_hiring_post: bool = True
    """False when the text is *about* a job without offering one.

    Only social posts can get this wrong, and they get it wrong constantly. Real
    examples from one live search: "New chapter: I recently joined mylantech
    GmbH as a Working Student in AI Automation" — every keyword matches, and it
    is somebody celebrating, not hiring. "Hot Startup Positions in Munich… 63
    open positions" — a newsletter. "Was sind uns stabile Releases wert?" — an
    opinion.

    Defaults True so an ordinary job page, which is self-evidently an offer, is
    unaffected."""

    role_type: str = "other"
    """research_assistant | hiwi | werkstudent | research_intern | master_thesis
    | phd_position | postdoc | senior | other"""

    requires_completed_phd: bool = False
    """True ONLY for a finished doctorate. "PhD students welcome" is not."""

    german_required: bool = False
    """True ONLY when fluent German is a stated requirement.

    Not the same thing as the posting being written in German, and the
    difference decides whether the best source survives: most of TU München's
    HiWi board is German-language advertising roles that work in English. Filter
    on language and the good jobs go with the bad. Same shape as
    `requires_completed_phd` — a mention is not a requirement."""

    suitable_for_masters: bool = False
    seniority: str = "unknown"
    topics: list[str] = Field(default_factory=list)
    language: str = "unknown"

    country: str | None = None
    """Where the job is, in English ("Germany", "Austria"), or None when the
    posting does not say.

    None means unknown and must never be read as Germany — assuming that is the
    original sin this field exists to undo."""

    score: int = 0
    reasoning: str = ""

    @field_validator("score")
    @classmethod
    def _clamp_score(cls, value: int) -> int:
        # Models occasionally emit 150 or -10 despite the instructions. Clamp
        # rather than reject: a slightly wrong score beats losing the whole
        # batch to a validation error.
        return max(0, min(100, int(value)))

    @field_validator("topics", mode="before")
    @classmethod
    def _coerce_topics(cls, value: object) -> object:
        # Models sometimes return a comma-joined string instead of an array.
        if isinstance(value, str):
            return [t.strip() for t in value.split(",") if t.strip()]
        return value

    def explanation(self, provider: str) -> list[str]:
        """Render as `Job.score_explanation` lines, matching the keyword
        scorer's format so `list --explain` looks the same either way."""
        lines = [f"LLM ({provider}) scored {self.score}/100: {self.reasoning}".strip()]
        if self.role_type and self.role_type != "other":
            lines.append(f"role type: {self.role_type}")
        if self.topics:
            lines.append(f"topics: {', '.join(self.topics[:5])}")
        if self.requires_completed_phd:
            lines.append("requires a completed PhD")
        if not self.suitable_for_masters:
            lines.append("not suitable for a Master's student")
        if not self.is_job_posting:
            lines.append("not an individual job posting (listing/index page)")
        return lines


@runtime_checkable
class LlmProvider(Protocol):
    """One LLM backend.

    `@runtime_checkable` only checks that a method *exists*, never its
    signature, so this declaration had drifted from both implementations
    without anything noticing — write a new provider against it and the call in
    `chain.py` would miss `**prompt_kwargs` and fail at runtime.
    """

    name: str

    async def assess(self, jobs: list[Job], **prompt_kwargs: object) -> list[JobAssessment]:
        """Assess a batch. Raises `LlmError` if it cannot.

        `prompt_kwargs` are forwarded to the prompt builder (topics, locations,
        …); `chain.py` passes them from settings.
        """
        ...

    async def aclose(self) -> None: ...
