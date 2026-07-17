"""Research-group intelligence (OpenAlex) — module, cache, service, API. Mocked."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from job_alerts.api.app import create_app
from job_alerts.config import SourcesConfig
from job_alerts.dashboard import service as svc
from job_alerts.database import Database
from job_alerts.research import OpenAlex, clean_org_name, research_context

_INST = {
    "results": [
        {
            "id": "https://openalex.org/I62916508",
            "display_name": "Technical University of Munich",
            "country_code": "DE",
            "works_count": 227000,
            "homepage_url": "https://www.tum.de",
            "x_concepts": [{"display_name": "Computer science"}, {"display_name": "Robotics"}],
        }
    ]
}
_WORKS = {
    "results": [
        {"title": "Deep RL for Manipulation", "publication_year": 2026,
         "doi": "https://doi.org/10.1/x", "id": "https://openalex.org/W1",
         "authorships": [{"author": {"display_name": "A. Researcher"}}]},
        {"title": "Deep RL for Manipulation", "publication_year": 2026, "id": "W1b"},  # dup title
        {"title": "Vision Transformers Revisited", "publication_year": 2025, "id": "https://openalex.org/W2",
         "authorships": []},
    ]
}


class TestCleanOrg:
    def test_strips_legal_and_filler(self):
        assert clean_org_name("Fraunhofer-Gesellschaft e.V. Zentrale München") == "Fraunhofer München"

    def test_keeps_core_when_all_noise(self):
        assert clean_org_name("GmbH") == "GmbH"


class TestModule:
    @respx.mock
    @pytest.mark.asyncio
    async def test_context_assembles_and_dedups(self):
        respx.get(url__regex=r".*/institutions.*").mock(return_value=httpx.Response(200, json=_INST))
        respx.get(url__regex=r".*/works.*").mock(return_value=httpx.Response(200, json=_WORKS))
        oa = OpenAlex()
        r = await research_context("Technical University of Munich", "robotics", client=oa)
        await oa.aclose()
        assert r["available"] is True
        assert r["institution"]["display_name"] == "Technical University of Munich"
        titles = [w["title"] for w in r["recent_works"]]
        assert titles == ["Deep RL for Manipulation", "Vision Transformers Revisited"]  # deduped

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_institution(self):
        respx.get(url__regex=r".*/institutions.*").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        oa = OpenAlex()
        r = await research_context("Nobody Corp", "ai", client=oa)
        await oa.aclose()
        assert r == {"available": False, "reason": "institution_not_found"}

    @pytest.mark.asyncio
    async def test_no_org(self):
        assert (await research_context("", "ai"))["reason"] == "no_institution"


class TestCache:
    def test_ttl(self, tmp_path, job_factory):
        from datetime import UTC, datetime, timedelta

        with Database(tmp_path / "r.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_research("j1", "k1", {"available": True, "institution": {}})
            assert db.get_research("j1", "k1", 30) is not None
            # A different query key (org/field changed) misses.
            assert db.get_research("j1", "k2", 30) is None
            # Too old misses.
            old = datetime.now(UTC) - timedelta(days=40)
            db.save_research("j1", "k1", {"x": 1}, when=old)
            assert db.get_research("j1", "k1", 30) is None


@pytest.fixture
def client(tmp_path, settings, discord_secrets, job_factory, monkeypatch):
    settings.database.path = tmp_path / "jobs.db"
    with Database(settings.database.path) as db:
        db.upsert(job_factory(id="j1", url="https://x.de/1",
                              organization="Technical University of Munich",
                              academic_field="robotics"))
        db.upsert(job_factory(id="noorg", url="https://x.de/2", organization=None))
    svc._config = svc.AppConfig(
        settings=settings, sources=SourcesConfig(sources=[]), secrets=discord_secrets
    )
    monkeypatch.setattr(svc, "llm_online", lambda: True)
    with TestClient(create_app()) as c:
        yield c
    svc._config = None


class TestApi:
    @respx.mock
    def test_research_fetched_then_cached(self, client):
        respx.get(url__regex=r".*/institutions.*").mock(return_value=httpx.Response(200, json=_INST))
        respx.get(url__regex=r".*/works.*").mock(return_value=httpx.Response(200, json=_WORKS))
        first = client.get("/api/jobs/j1/research").json()
        assert first["available"] is True and first["cached"] is False
        assert first["institution"]["country_code"] == "DE"
        second = client.get("/api/jobs/j1/research").json()
        assert second["cached"] is True  # served from DB, no second OpenAlex hit

    def test_no_institution(self, client):
        assert client.get("/api/jobs/noorg/research").json()["reason"] == "no_institution"
