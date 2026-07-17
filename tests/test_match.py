"""Profile↔opportunity match analysis: cache, service, API — no live LLM."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from job_alerts.api.app import create_app
from job_alerts.config import SourcesConfig
from job_alerts.dashboard import service as svc
from job_alerts.database import Database
from job_alerts.profile import MatchAnalysis, MatchCategory

_MATCH = {
    "category": "good",
    "summary": "Solid robotics fit with a couple of gaps.",
    "strong_matches": ["You built a SLAM system in ROS — the role wants robotics software"],
    "missing_requirements": ["No stated C++ in a production setting"],
    "confidence": "high",
}
_PROFILE = json.dumps({"name": "V", "skills": {"programming": ["Python", "C++"]}})


class TestMatchModel:
    def test_category_coerces(self):
        assert MatchCategory.coerce("Strong") is MatchCategory.STRONG
        assert MatchCategory.coerce("weird") is MatchCategory.UNLIKELY

    def test_no_numeric_score_field(self):
        # An ATS number is deliberately absent.
        assert "score" not in MatchAnalysis.model_fields


class TestMatchCache:
    def test_round_trip_and_invalidation(self, tmp_path, job_factory):
        with Database(tmp_path / "m.db") as db:
            db.upsert(job_factory(id="j1"))
            assert db.get_match("j1", "c1", "p1", 1) is None
            db.save_match("j1", "c1", "p1", 1, _MATCH)
            assert db.get_match("j1", "c1", "p1", 1)["category"] == "good"
            # Edited posting, edited profile, or new prompt version -> miss.
            assert db.get_match("j1", "c2", "p1", 1) is None
            assert db.get_match("j1", "c1", "p2", 1) is None
            assert db.get_match("j1", "c1", "p1", 2) is None

    def test_deleting_profile_clears_matches(self, tmp_path, job_factory):
        with Database(tmp_path / "m.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_profile(_PROFILE, _PROFILE, source_upload_id=None, model_version="m",
                            prompt_version=1)
            db.save_match("j1", "c1", "p1", 1, _MATCH)
            db.delete_profile()
            assert db.get_match("j1", "c1", "p1", 1) is None


@pytest.fixture
def client(tmp_path, settings, discord_secrets, job_factory, monkeypatch):
    settings.database.path = tmp_path / "jobs.db"
    with Database(settings.database.path) as db:
        db.upsert(job_factory(id="j1", opportunity_type="research_assistant", academic_field="robotics"))
    svc._config = svc.AppConfig(
        settings=settings, sources=SourcesConfig(sources=[]), secrets=discord_secrets
    )
    monkeypatch.setattr(svc, "llm_online", lambda: True)
    with TestClient(create_app()) as c:
        yield c
    svc._config = None


class TestMatchApi:
    def test_no_profile_yet(self, client):
        body = client.get("/api/jobs/j1/match").json()
        assert body["available"] is False
        assert body["reason"] == "no_profile"

    def test_unknown_job(self, client, settings):
        with Database(settings.database.path) as db:
            db.save_profile(_PROFILE, _PROFILE, source_upload_id=None, model_version="m",
                            prompt_version=1)
        body = client.get("/api/jobs/ghost/match").json()
        assert body["available"] is False and body["reason"] == "unknown_job"

    def test_match_computed_and_then_cached(self, client, settings, monkeypatch):
        with Database(settings.database.path) as db:
            db.save_profile(_PROFILE, _PROFILE, source_upload_id=None, model_version="m",
                            prompt_version=1)

        calls = {"n": 0}

        async def fake_analyze(profile_json, job_block, llm, secrets):
            calls["n"] += 1
            assert "robotics" in job_block  # taxonomy is in the prompt
            return _MATCH

        monkeypatch.setattr("job_alerts.llm.assist.analyze_match", fake_analyze)

        first = client.get("/api/jobs/j1/match").json()
        assert first["available"] is True
        assert first["cached"] is False
        assert first["match"]["category"] == "good"
        assert first["match"]["strong_matches"]

        second = client.get("/api/jobs/j1/match").json()
        assert second["available"] is True and second["cached"] is True
        assert calls["n"] == 1  # served from cache, no second LLM call

    def test_llm_down_is_reported(self, client, settings, monkeypatch):
        with Database(settings.database.path) as db:
            db.save_profile(_PROFILE, _PROFILE, source_upload_id=None, model_version="m",
                            prompt_version=1)

        async def fake_analyze(*a, **k):
            return None

        monkeypatch.setattr("job_alerts.llm.assist.analyze_match", fake_analyze)
        body = client.get("/api/jobs/j1/match").json()
        assert body["available"] is False and body["reason"] == "llm_unavailable"
