"""Résumé-tailoring suggestions: cache, service, API — no live LLM."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from job_alerts.api.app import create_app
from job_alerts.config import SourcesConfig
from job_alerts.dashboard import service as svc
from job_alerts.database import Database
from job_alerts.profile import TailoringPlan

_PLAN = {
    "tailored_summary": "MSc AI & Robotics student with SLAM and RL project work.",
    "emphasize": ["SLAM system (ROS) — the role wants robotics software"],
    "suggestions": [
        {"kind": "reword", "section": "Summary", "suggested": "Lead with robotics",
         "rationale": "the posting stresses robotics"},
    ],
    "do_not_fabricate": ["No production C++ — do not claim it"],
    "confidence": "high",
}
_PROFILE = json.dumps({"name": "V", "skills": {"programming": ["Python", "C++"]}})


class TestTailoringModel:
    def test_never_fabricate_field_present(self):
        assert "do_not_fabricate" in TailoringPlan.model_fields

    def test_junk_suggestions_dropped(self):
        p = TailoringPlan.model_validate({"suggestions": [{"kind": "reword"}, "junk", 5]})
        assert len(p.suggestions) == 1


class TestTailoringCache:
    def test_round_trip_and_invalidation(self, tmp_path, job_factory):
        with Database(tmp_path / "t.db") as db:
            db.upsert(job_factory(id="j1"))
            assert db.get_tailoring("j1", "c1", "p1", 1) is None
            db.save_tailoring("j1", "c1", "p1", 1, _PLAN)
            assert db.get_tailoring("j1", "c1", "p1", 1)["confidence"] == "high"
            assert db.get_tailoring("j1", "c2", "p1", 1) is None  # edited posting
            assert db.get_tailoring("j1", "c1", "p2", 1) is None  # edited profile

    def test_deleting_profile_clears_tailoring(self, tmp_path, job_factory):
        with Database(tmp_path / "t.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_profile(_PROFILE, _PROFILE, source_upload_id=None, model_version="m",
                            prompt_version=1)
            db.save_tailoring("j1", "c1", "p1", 1, _PLAN)
            db.delete_profile()
            assert db.get_tailoring("j1", "c1", "p1", 1) is None


@pytest.fixture
def client(tmp_path, settings, discord_secrets, job_factory, monkeypatch):
    settings.database.path = tmp_path / "jobs.db"
    with Database(settings.database.path) as db:
        db.upsert(job_factory(id="j1", academic_field="robotics"))
    svc._config = svc.AppConfig(
        settings=settings, sources=SourcesConfig(sources=[]), secrets=discord_secrets
    )
    monkeypatch.setattr(svc, "llm_online", lambda: True)
    with TestClient(create_app()) as c:
        yield c
    svc._config = None


class TestTailoringApi:
    def test_no_profile(self, client):
        body = client.get("/api/jobs/j1/tailoring").json()
        assert body["available"] is False and body["reason"] == "no_profile"

    def test_computed_then_cached(self, client, settings, monkeypatch):
        with Database(settings.database.path) as db:
            db.save_profile(_PROFILE, _PROFILE, source_upload_id=None, model_version="m",
                            prompt_version=1)
        calls = {"n": 0}

        async def fake(profile_json, job_block, llm, secrets):
            calls["n"] += 1
            return _PLAN

        monkeypatch.setattr("job_alerts.llm.assist.suggest_tailoring", fake)
        first = client.get("/api/jobs/j1/tailoring").json()
        assert first["available"] is True and first["cached"] is False
        assert first["plan"]["tailored_summary"].startswith("MSc")
        assert first["plan"]["do_not_fabricate"]
        second = client.get("/api/jobs/j1/tailoring").json()
        assert second["cached"] is True and calls["n"] == 1

    def test_llm_down(self, client, settings, monkeypatch):
        with Database(settings.database.path) as db:
            db.save_profile(_PROFILE, _PROFILE, source_upload_id=None, model_version="m",
                            prompt_version=1)

        async def fake(*a, **k):
            return None

        monkeypatch.setattr("job_alerts.llm.assist.suggest_tailoring", fake)
        assert client.get("/api/jobs/j1/tailoring").json()["reason"] == "llm_unavailable"
