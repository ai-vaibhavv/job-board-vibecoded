"""Research-group intelligence via OpenAlex (Phase 6).

Given the institution an opportunity is hosted by, look up what the group actually
works on and which recent papers are worth reading — from OpenAlex, a free,
keyless, structured scholarly API. No LLM involved, so this works regardless of
the self-hosted model's state.

Deliberately conservative: OpenAlex is queried politely (a `mailto` puts us in the
fast pool), everything is best-effort, and a miss returns empty rather than
raising — a job whose employer OpenAlex does not know simply shows no research
panel.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.openalex.org"
_CONTACT = "labscout (+https://github.com/ai-vaibhavv/labscout)"

# academic_field (taxonomy) -> OpenAlex concept id, to focus recent works on the
# opportunity's field. Missing / other -> no concept filter (institution-wide).
_FIELD_CONCEPTS: dict[str, str] = {
    "ai": "C154945302",  # Artificial intelligence
    "ml": "C119857082",  # Machine learning
    "computer_science": "C41008148",
    "data_science": "C2522767166",
    "robotics": "C90509273",
    "electrical_engineering": "C47446073",
    "mechanical_engineering": "C97355855",
    "physics": "C121332964",
    "mathematics": "C33923547",
    "chemistry": "C185592680",
    "biology": "C86803240",
    "medicine": "C71924100",
    "neuroscience": "C169760540",
    "psychology": "C15744967",
    "economics": "C162324750",
    "environmental_science": "C39432304",
}

# Common employer-name noise that hurts an institution search.
_ORG_NOISE = (
    "gmbh", "e.v.", "e. v.", "ev", "ggmbh", "mbh", "ag", "zentrale", "gesellschaft",
    "the", "of", "for", "und", "and",
)


def clean_org_name(name: str) -> str:
    """Trim legal-form and filler tokens so "Fraunhofer-Gesellschaft e.V. Zentrale
    München" searches as "Fraunhofer München" — OpenAlex matches the core name far
    better without the boilerplate."""
    tokens = [t for t in name.replace("-", " ").replace(",", " ").split() if t]
    kept = [t for t in tokens if t.lower().replace(".", "") not in _ORG_NOISE]
    return " ".join(kept or tokens)[:120]


class OpenAlex:
    """A tiny async OpenAlex client. One httpx client per instance."""

    def __init__(self, *, timeout: float = 15.0, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": _CONTACT}
        )
        self._owns = client is None

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def _get(self, path: str) -> dict[str, Any] | None:
        sep = "&" if "?" in path else "?"
        url = f"{_BASE}{path}{sep}mailto={quote(_CONTACT)}"
        try:
            r = await self._client.get(url)
        except httpx.HTTPError as exc:
            logger.info("openalex request failed: %s", exc)
            return None
        if r.status_code != 200:
            logger.info("openalex HTTP %s for %s", r.status_code, path)
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def find_institution(self, name: str) -> dict[str, Any] | None:
        """Best OpenAlex institution match for an employer name, or None."""
        cleaned = clean_org_name(name)
        if not cleaned:
            return None
        data = await self._get(f"/institutions?search={quote(cleaned)}&per_page=1")
        results = (data or {}).get("results") or []
        if not results:
            return None
        inst = results[0]
        return {
            "id": (inst.get("id") or "").rsplit("/", 1)[-1],
            "display_name": inst.get("display_name"),
            "country_code": inst.get("country_code"),
            "works_count": inst.get("works_count"),
            "homepage_url": inst.get("homepage_url"),
            "openalex_url": inst.get("id"),
            "research_areas": [
                c.get("display_name")
                for c in (inst.get("x_concepts") or [])[:8]
                if c.get("display_name")
            ],
        }

    async def recent_works(
        self, institution_id: str, *, concept_id: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Recent papers from an institution, optionally narrowed to a field."""
        filters = f"institutions.id:{institution_id}"
        if concept_id:
            filters += f",concepts.id:{concept_id}"
        # Over-fetch a little so de-duplication by title still leaves `limit`.
        data = await self._get(
            f"/works?filter={filters}&sort=publication_date:desc&per_page={limit * 2}"
        )
        works: list[dict[str, Any]] = []
        seen: set[str] = set()
        for w in (data or {}).get("results", []):
            title = w.get("title") or w.get("display_name") or "(untitled)"
            key = title.strip().casefold()
            if key in seen:
                continue
            seen.add(key)
            authors = [
                a.get("author", {}).get("display_name")
                for a in (w.get("authorships") or [])[:4]
                if a.get("author")
            ]
            works.append(
                {
                    "title": title,
                    "year": w.get("publication_year"),
                    "doi": w.get("doi"),
                    "url": w.get("doi") or w.get("id"),
                    "authors": [a for a in authors if a],
                }
            )
            if len(works) >= limit:
                break
        return works


async def research_context(
    organization: str | None, academic_field: str | None, *, client: OpenAlex | None = None
) -> dict[str, Any]:
    """Assemble a research-group snapshot for an opportunity. Always returns a
    dict with `available`; never raises."""
    if not organization or not organization.strip():
        return {"available": False, "reason": "no_institution"}

    oa = client or OpenAlex()
    try:
        inst = await oa.find_institution(organization)
        if inst is None:
            return {"available": False, "reason": "institution_not_found"}
        concept = _FIELD_CONCEPTS.get((academic_field or "").lower())
        works = await oa.recent_works(inst["id"], concept_id=concept)
        # Fall back to institution-wide recent works if the field filter found none.
        if not works and concept:
            works = await oa.recent_works(inst["id"])
        return {
            "available": True,
            "institution": inst,
            "recent_works": works,
            "field_filtered": bool(concept and works),
        }
    finally:
        if client is None:
            await oa.aclose()
