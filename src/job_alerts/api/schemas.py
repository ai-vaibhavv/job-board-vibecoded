"""Request bodies for the JSON API.

Responses are the plain dicts the service layer already returns (job summaries,
the `stats()` dict, `job_detail_json`), so they are not re-modelled here — the
`Job` shape is owned by `models.Job` and reached via `model_dump(mode="json")`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PublishRequest(BaseModel):
    confirm: bool = False


class PreviewRequest(BaseModel):
    keywords: str = ""
    topics: list[str] = Field(default_factory=list)


class SearchRunRequest(BaseModel):
    keywords: str = ""
    topics: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
