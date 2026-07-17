"""The central academic profile.

One canonical, structured picture of the user, extracted from their résumé and
then editable by hand. It is the source of truth that later phases tailor résumés
and explain matches against.

Two design rules:

* Every model is `extra="ignore"` and every field defaults to empty. The résumé
  is untrusted, LLM-parsed input; a missing or malformed field must degrade to
  "unknown", never raise. A slightly-thin profile beats a lost one.
* The extraction NEVER invents. The prompt is explicit: a fact absent from the
  résumé stays empty here. The user fills gaps themselves; the machine does not
  guess a GPA, a publication or a skill the person never claimed.

Provenance is kept at the store level, not per field: the immutable LLM output is
saved alongside the editable copy (see `database` v10), so "what the machine read"
and "what the user corrected" are always both recoverable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _item_to_str(item: object) -> str:
    """Flatten one list item to a display string. Models frequently return a
    list of OBJECTS where a list of strings was asked for — a publication as
    `{"title": ..., "authors": [...]}`, a skill as `{"name": ...}`. Prefer the
    obvious title-ish key, else join the object's scalar values."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("title", "name", "label", "value", "text"):
            if isinstance(item.get(key), str) and item[key].strip():
                return item[key].strip()
        parts = [str(v).strip() for v in item.values() if isinstance(v, str | int | float)]
        return ", ".join(p for p in parts if p)
    if item is None:
        return ""
    return str(item).strip()


def _coerce_str_list(value: object) -> object:
    """Coerce a list-of-strings field from the shapes models actually emit: a
    comma/semicolon/newline-joined string, or a list containing dicts."""
    if isinstance(value, str):
        parts = value.replace(";", ",").replace("\n", ",").split(",")
        return [p.strip() for p in parts if p.strip()]
    if isinstance(value, list):
        return [s for s in (_item_to_str(v) for v in value) if s]
    return value


class Education(BaseModel):
    model_config = ConfigDict(extra="ignore")
    degree: str = ""
    """e.g. "MSc Artificial Intelligence & Robotics"."""
    level: str = ""
    """bachelor | master | phd | other — coarse level, if stated."""
    institution: str = ""
    field: str = ""
    start: str = ""
    end: str = ""
    """Free text so "expected 2027" survives."""
    grade: str = ""


class Experience(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = ""
    organization: str = ""
    kind: str = ""
    """work | research | teaching | other, if discernible."""
    description: str = ""
    start: str = ""
    end: str = ""


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    description: str = ""
    technologies: list[str] = Field(default_factory=list)

    _coerce = field_validator("technologies", mode="before")(staticmethod(_coerce_str_list))


class Skills(BaseModel):
    model_config = ConfigDict(extra="ignore")
    programming: list[str] = Field(default_factory=list)
    technical: list[str] = Field(default_factory=list)
    research_methods: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    """Spoken/written human languages (e.g. "German (C1)", "English")."""

    _coerce = field_validator(
        "programming", "technical", "research_methods", "languages", mode="before"
    )(staticmethod(_coerce_str_list))


class Links(BaseModel):
    model_config = ConfigDict(extra="ignore")
    github: str = ""
    scholar: str = ""
    orcid: str = ""
    portfolio: str = ""
    linkedin: str = ""
    email: str = ""


class AcademicProfile(BaseModel):
    """The canonical, editable profile."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    headline: str = ""
    """A one-line self-description, e.g. "MSc AI & Robotics student"."""
    summary: str = ""
    research_interests: list[str] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    publications: list[str] = Field(default_factory=list)
    awards: list[str] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)
    links: Links = Field(default_factory=Links)

    _coerce = field_validator(
        "research_interests", "publications", "awards", mode="before"
    )(staticmethod(_coerce_str_list))

    @field_validator("education", "experience", "projects", mode="before")
    @classmethod
    def _drop_scalars(cls, value: object) -> object:
        # A model that emits a bare string where a list of objects is expected
        # shouldn't take down the whole parse.
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        return []

    def is_empty(self) -> bool:
        """True when nothing meaningful was extracted — the caller shows a
        'could not read this résumé' message rather than an empty shell."""
        return not any(
            [
                self.name,
                self.summary,
                self.research_interests,
                self.education,
                self.experience,
                self.projects,
                self.skills.programming,
                self.skills.technical,
            ]
        )
