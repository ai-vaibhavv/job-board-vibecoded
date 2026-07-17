"""The central academic profile: persistence, service, and API — no live LLM."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from job_alerts.api.app import create_app
from job_alerts.config import SourcesConfig
from job_alerts.dashboard import service as svc
from job_alerts.database import Database

_SAMPLE = {
    "name": "Vaibhav",
    "headline": "MSc AI & Robotics student",
    "research_interests": ["computer vision", "robotics"],
    "education": [{"degree": "MSc AI", "level": "master", "institution": "Uni X", "end": "2027"}],
    "skills": {"programming": ["Python", "C++"]},
}


@pytest.fixture
def client(tmp_path, settings, discord_secrets, monkeypatch):
    settings.database.path = tmp_path / "jobs.db"
    with Database(settings.database.path):
        pass  # force migration
    svc._config = svc.AppConfig(
        settings=settings, sources=SourcesConfig(sources=[]), secrets=discord_secrets
    )
    monkeypatch.setattr(svc, "llm_online", lambda: True)
    with TestClient(create_app()) as c:
        yield c
    svc._config = None


def _seed_profile(settings):
    import json

    with Database(settings.database.path) as db:
        pj = json.dumps(_SAMPLE)
        db.save_profile(pj, pj, source_upload_id=None, model_version="qwen", prompt_version=1)


class TestModelCoercion:
    """The résumé is LLM-parsed, so the model must survive the shapes models
    actually emit — a list of objects where strings were asked for, etc."""

    def test_publications_as_objects_are_flattened(self):
        from job_alerts.profile import AcademicProfile

        p = AcademicProfile.model_validate(
            {
                "publications": [
                    {"title": "Relating Muscle Activity", "authors": ["A", "B"]},
                    {"title": "SigNet-TAM", "authors": ["C"]},
                ],
                "research_interests": ["ML", {"name": "robotics"}],
                "skills": {"programming": ["Python", {"name": "C++"}]},
            }
        )
        assert p.publications == ["Relating Muscle Activity", "SigNet-TAM"]
        assert p.research_interests == ["ML", "robotics"]
        assert p.skills.programming == ["Python", "C++"]

    def test_string_where_list_expected(self):
        from job_alerts.profile import AcademicProfile

        p = AcademicProfile.model_validate({"research_interests": "computer vision; robotics, NLP"})
        assert p.research_interests == ["computer vision", "robotics", "NLP"]


class TestPersistence:
    def test_extracted_copy_survives_an_edit(self, tmp_path):
        import json

        with Database(tmp_path / "p.db") as db:
            orig = json.dumps({"name": "A"})
            db.save_profile(orig, orig, source_upload_id=None, model_version="m", prompt_version=1)
            db.update_profile_json(json.dumps({"name": "B"}))
            row = db.get_profile()
            assert json.loads(row["profile_json"])["name"] == "B"  # edited working copy
            assert json.loads(row["extracted_json"])["name"] == "A"  # immutable original
            assert row["user_edited"] == 1

    def test_upload_bytes_are_immutable_and_hashed(self, tmp_path):
        with Database(tmp_path / "p.db") as db:
            uid = db.save_profile_upload("cv.pdf", "application/pdf", b"hello")
            up = db.get_profile_upload(uid)
            assert up["raw"] == b"hello"
            assert len(up["content_hash"]) == 64

    def test_delete_removes_profile_and_uploads(self, tmp_path):
        with Database(tmp_path / "p.db") as db:
            db.save_profile_upload("cv.pdf", None, b"x")
            db.save_profile("{}", "{}", source_upload_id=None, model_version=None, prompt_version=1)
            db.delete_profile()
            assert db.get_profile() is None
            assert db.latest_profile_upload() is None


class TestApi:
    def test_get_empty_profile(self, client):
        body = client.get("/api/profile").json()
        assert body["exists"] is False
        assert "profile" in body  # an empty shell to render

    def test_get_after_seed(self, client, settings):
        _seed_profile(settings)
        body = client.get("/api/profile").json()
        assert body["exists"] is True
        assert body["profile"]["name"] == "Vaibhav"
        assert body["user_edited"] is False

    def test_edit_preserves_extracted(self, client, settings):
        _seed_profile(settings)
        edited = {**_SAMPLE, "name": "Vaibhav Prajapati"}
        r = client.put("/api/profile", json=edited)
        assert r.status_code == 200
        body = r.json()
        assert body["profile"]["name"] == "Vaibhav Prajapati"
        assert body["user_edited"] is True
        assert body["extracted"]["name"] == "Vaibhav"  # provenance kept

    def test_edit_without_profile_is_422(self, client):
        assert client.put("/api/profile", json=_SAMPLE).status_code == 422

    def test_edit_rejects_garbage(self, client, settings):
        _seed_profile(settings)
        # education must be a list of objects; a scalar is coerced away, not fatal,
        # but a wholly wrong type for a list field is rejected by the model.
        r = client.put("/api/profile", json={"research_interests": {"not": "a list"}})
        assert r.status_code == 422

    def test_export(self, client, settings):
        _seed_profile(settings)
        body = client.get("/api/profile/export").json()
        assert body["profile"]["name"] == "Vaibhav"
        assert "message" not in body

    def test_delete(self, client, settings):
        _seed_profile(settings)
        assert client.delete("/api/profile").json()["exists"] is False
        assert client.get("/api/profile").json()["exists"] is False

    def test_upload_calls_extractor_and_persists(self, client, settings, monkeypatch):
        # Stub the LLM extraction so no live tunnel is needed.
        async def fake_extract(text, llm, secrets):
            return _SAMPLE

        monkeypatch.setattr("job_alerts.llm.assist.extract_profile", fake_extract)
        files = {"file": ("cv.txt", b"MSc AI student, Python, robotics", "text/plain")}
        r = client.post("/api/profile", files=files)
        assert r.status_code == 200
        assert r.json()["exists"] is True
        # The original bytes were stored immutably and are downloadable.
        orig = client.get("/api/profile/original")
        assert orig.status_code == 200
        assert orig.content == b"MSc AI student, Python, robotics"

    def test_upload_rejects_empty(self, client):
        r = client.post("/api/profile", files={"file": ("cv.txt", b"", "text/plain")})
        assert r.status_code == 400
