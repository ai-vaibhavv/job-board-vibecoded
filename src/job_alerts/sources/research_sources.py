"""Generic JSON-API source for research job boards.

Many research boards expose JSON rather than RSS or HTML. Rather than shipping
a hand-written adapter per site — which rots the moment a site changes and which
the spec explicitly warns against inventing — this maps an arbitrary JSON
endpoint onto `JobCandidate` via a field map in YAML:

    - name: some_board
      type: json_api
      url: "https://board.example/api/jobs?country=DE"
      items_path: "data.results"      # where the list lives; "" means top level
      field_map:
        source_job_id: "id"
        title: "title"
        organization: "employer.name"
        url: "links.self"
        published_at: "posted_at"

Paths are dotted. `[]` walks into a list, so `categories[].name` collects every
name and joins them. A path that does not resolve yields None rather than
raising — job boards omit fields constantly.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import JobCandidate, SearchQuery
from .base import BaseSource

logger = logging.getLogger(__name__)

_CANDIDATE_FIELDS = frozenset(
    {
        "source_job_id",
        "title",
        "organization",
        "location",
        "country",
        "description",
        "url",
        "published_at",
        "application_deadline",
        "employment_type",
        "salary",
    }
)


def resolve_path(data: Any, path: str) -> Any:
    """Walk a dotted path through nested dicts/lists.

    Returns None on any miss. `field[]` and `field[].sub` iterate a list.
    """
    if not path:
        return data
    current: Any = data
    for segment in path.split("."):
        if current is None:
            return None
        if segment.endswith("[]"):
            key = segment[:-2]
            if key:
                current = current.get(key) if isinstance(current, dict) else None
            if not isinstance(current, list):
                return None
            continue
        if isinstance(current, list):
            # A dotted segment after `[]`: pull the key from each element.
            collected = [
                item.get(segment)
                for item in current
                if isinstance(item, dict) and item.get(segment) is not None
            ]
            current = collected or None
            continue
        if isinstance(current, dict):
            current = current.get(segment)
        else:
            return None
    return current


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [p for p in (_stringify(v) for v in value) if p]
        return ", ".join(parts) or None
    if isinstance(value, dict):
        # A nested object where a scalar was expected usually means the field
        # map points one level too high; the common useful keys are tried.
        for key in ("name", "title", "label", "value", "city"):
            if key in value:
                return _stringify(value[key])
        return None
    return str(value)


class JsonApiSource(BaseSource):
    """Maps a JSON job endpoint onto the shared `Job` model."""

    async def search(self, query: SearchQuery) -> list[JobCandidate]:
        url = self.config.url
        if not url:
            raise ValueError(f"source {self.name!r} has type 'json_api' but no url")
        if not self.config.field_map:
            raise ValueError(
                f"source {self.name!r} has no field_map; at minimum map 'title' and 'url'"
            )
        for required in ("title", "url"):
            if required not in self.config.field_map:
                raise ValueError(f"source {self.name!r} field_map is missing {required!r}")

        payload = await self.client.get_json(url)
        return self.parse(payload)

    def parse(self, payload: Any) -> list[JobCandidate]:
        items = resolve_path(payload, self.config.items_path or "")
        if items is None:
            logger.warning(
                "source %s: items_path %r resolved to nothing", self.name, self.config.items_path
            )
            return []
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            raise ValueError(
                f"source {self.name!r}: items_path {self.config.items_path!r} is a "
                f"{type(items).__name__}, expected a list"
            )

        unknown = set(self.config.field_map) - _CANDIDATE_FIELDS
        if unknown:
            logger.warning(
                "source %s: field_map has unknown field(s) %s — ignored",
                self.name,
                ", ".join(sorted(unknown)),
            )

        candidates: list[JobCandidate] = []
        for item in items:
            candidate = self._parse_item(item)
            if candidate:
                candidates.append(candidate)
        return candidates

    def _parse_item(self, item: Any) -> JobCandidate | None:
        values: dict[str, Any] = {}
        for field, path in self.config.field_map.items():
            if field in _CANDIDATE_FIELDS:
                values[field] = _stringify(resolve_path(item, path))

        if not values.get("title") or not values.get("url"):
            return None
        try:
            return JobCandidate(source=self.name, **values)
        except ValueError as exc:
            logger.debug("source %s: invalid item: %s", self.name, exc)
            return None
