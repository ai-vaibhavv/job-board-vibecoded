"""The generic json_api connector, driven by a captured EURAXESS-shaped fixture.

This is the repo's first captured-payload fixture (tests/fixtures/), the pattern
LabScout will grow for every new connector.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_alerts.config import SourceConfig
from job_alerts.sources import JsonApiSource, build_source
from job_alerts.sources.research_sources import resolve_path

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "euraxess_sample.json").read_text())

_CONFIG = SourceConfig(
    name="euraxess_de",
    type="json_api",
    url="https://euraxess.example/api/jobs?country=Germany",
    items_path="data.results",
    field_map={
        "source_job_id": "id",
        "title": "title",
        "organization": "organisation.name",
        "location": "location.city",
        "country": "location.country",
        "url": "links.self",
        "published_at": "posted_at",
        "application_deadline": "deadline",
        "employment_type": "employment_type",
        "description": "summary",
        "contact_email": "contact.email",
    },
)


def _source() -> JsonApiSource:
    return JsonApiSource(_CONFIG, client=None)  # type: ignore[arg-type]  # parse() needs no client


class TestResolvePath:
    def test_dotted_nested(self):
        assert resolve_path({"a": {"b": {"c": 1}}}, "a.b.c") == 1

    def test_missing_is_none(self):
        assert resolve_path({"a": {}}, "a.b.c") is None

    def test_list_walk_collects(self):
        data = {"categories": [{"name": "Robotics"}, {"name": "Engineering"}]}
        assert resolve_path(data, "categories[].name") == ["Robotics", "Engineering"]

    def test_empty_path_is_identity(self):
        assert resolve_path({"x": 1}, "") == {"x": 1}


class TestParse:
    def test_maps_fields_and_flattens_nested(self):
        candidates = _source().parse(_FIXTURE)
        # The title-less third row is dropped.
        assert len(candidates) == 2
        first = candidates[0]
        assert first.title.startswith("Student Assistant (HiWi)")
        assert first.organization == "University of Tübingen"
        assert first.location == "Tübingen"
        assert first.country == "Germany"
        assert first.url == "https://euraxess.example/jobs/100234"
        assert first.contact_email == "ml-lab@uni-tuebingen.example"
        assert first.source == "euraxess_de"

    def test_null_fields_become_none(self):
        phd = _source().parse(_FIXTURE)[1]
        assert phd.application_deadline is None  # "deadline": null

    def test_missing_title_row_is_skipped(self):
        ids = {c.source_job_id for c in _source().parse(_FIXTURE)}
        assert "job-100236" not in ids

    def test_items_path_miss_returns_empty(self):
        assert _source().parse({"data": {}}) == []


class TestConfigValidation:
    def test_registry_builds_json_api(self):
        # A plain SourceConfig with no client is enough to prove the registry
        # knows the type; construction must not raise.
        src = build_source(_CONFIG, client=None, secrets=None)  # type: ignore[arg-type]
        assert isinstance(src, JsonApiSource)

    @pytest.mark.asyncio
    async def test_missing_field_map_raises(self):
        cfg = SourceConfig(name="x", type="json_api", url="https://e/api")
        with pytest.raises(ValueError, match="field_map"):
            await JsonApiSource(cfg, client=None).search(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_missing_title_mapping_raises(self):
        cfg = SourceConfig(
            name="x", type="json_api", url="https://e/api", field_map={"url": "links.self"}
        )
        with pytest.raises(ValueError, match="title"):
            await JsonApiSource(cfg, client=None).search(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_missing_url_raises(self):
        cfg = SourceConfig(name="x", type="json_api", field_map={"title": "t", "url": "u"})
        with pytest.raises(ValueError, match="no url"):
            await JsonApiSource(cfg, client=None).search(None)  # type: ignore[arg-type]
