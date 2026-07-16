"""The dashboard service: listing, publish gating, and query injection.

Every test runs against a `:memory`-style tmp database with the Discord send and
the LLM mocked. No test touches a live endpoint or the user's real config.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from job_alerts.config import SourceConfig, SourcesConfig
from job_alerts.dashboard import service as svc
from job_alerts.database import Database
from job_alerts.models import JobStatus, Language, RunSummary
from job_alerts.notifications.base import DeliveryResult

SEARCH_DISCOVERY = SourceConfig(
    name="search_discovery",
    type="search_api",
    enabled=True,
    allowed_domains=["de.linkedin.com", "fraunhofer.de", "mpg.de"],
    queries=["original query"],
)


@pytest.fixture
def app(tmp_path, settings, discord_secrets, job_factory):
    """A configured dashboard over a seeded tmp database."""
    settings.database.path = tmp_path / "jobs.db"
    with Database(settings.database.path) as db:
        db.upsert(job_factory(id="new1", url="https://x.de/1", status=JobStatus.NEW))
        db.upsert(
            job_factory(
                id="rej1",
                url="https://x.de/2",
                status=JobStatus.REJECTED,
                score_explanation=["rejected: requires a completed PhD"],
            )
        )
        notified = job_factory(id="not1", url="https://x.de/3", status=JobStatus.NOTIFIED)
        db.upsert(notified)
        db.mark_notified(["not1"], when=datetime(2026, 7, 1, tzinfo=UTC))
        db.upsert(
            job_factory(
                id="de1",
                url="https://x.de/4",
                language=Language.DE,
                description="Wir suchen eine studentische Hilfskraft.",
            )
        )

    cfg = svc.AppConfig(
        settings=settings,
        sources=SourcesConfig(sources=[SEARCH_DISCOVERY.model_copy(deep=True)]),
        secrets=discord_secrets,
    )
    svc._config = cfg
    yield cfg
    svc._config = None


class TestListing:
    def test_lists_all_by_default(self, app):
        _, ids = svc.list_rows()
        assert set(ids) == {"new1", "rej1", "not1", "de1"}

    def test_status_filter(self, app):
        _, ids = svc.list_rows(status="rejected")
        assert ids == ["rej1"]

    def test_hidden_jobs_are_excluded(self, app):
        with Database(app.db_path) as db:
            db.hide_job("rej1")
        _, ids = svc.list_rows()
        assert "rej1" not in ids
        _, ids_shown = svc.list_rows(show_hidden=True)
        assert "rej1" in ids_shown

    def test_text_filter(self, app):
        _, ids = svc.list_rows(text="TU Munich")
        assert ids  # all seeded jobs are at TU Munich
        _, none = svc.list_rows(text="zzz-nonexistent")
        assert none == []


class TestPublishGating:
    def test_rejected_blocks_without_confirm(self, app):
        msg = svc.publish_job("rej1", confirm=False)
        assert "Blocked" in msg and "completed PhD" in msg

    def test_already_sent_blocks_without_confirm(self, app):
        msg = svc.publish_job("not1", confirm=False)
        assert "Blocked" in msg and "already sent" in msg.lower()

    def test_publish_sends_and_marks_notified(self, app, monkeypatch):
        sent = {}

        async def fake_send(webhook, settings, job):
            sent["job"] = job
            return DeliveryResult(delivered_ids=[job.id])

        monkeypatch.setattr(svc, "_send", fake_send)
        msg = svc.publish_job("new1", confirm=False)
        assert "✅" in msg
        assert sent["job"].id == "new1"
        with Database(app.db_path) as db:
            assert db.get("new1").notified_at is not None

    def test_confirm_lets_a_rejected_job_through(self, app, monkeypatch):
        async def fake_send(webhook, settings, job):
            return DeliveryResult(delivered_ids=[job.id])

        monkeypatch.setattr(svc, "_send", fake_send)
        assert "✅" in svc.publish_job("rej1", confirm=True)

    def test_german_job_is_sent_in_english(self, app, monkeypatch):
        async def fake_translate(text, llm, secrets):
            return {
                "description_en": "We are looking for a student assistant.",
                "card_summary_en": "Student assistant role. Suits Master's students.",
                "truncated": False,
            }

        captured = {}

        async def fake_send(webhook, settings, job):
            captured["job"] = job
            return DeliveryResult(delivered_ids=[job.id])

        monkeypatch.setattr(svc, "translate_job_text", fake_translate)
        monkeypatch.setattr(svc, "_send", fake_send)

        svc.publish_job("de1", confirm=False)
        assert captured["job"].description == "We are looking for a student assistant."
        assert captured["job"].card_summary == "Student assistant role. Suits Master's students."


class TestTranslate:
    def test_cache_first(self, app, monkeypatch):
        calls = {"n": 0}

        async def fake_translate(text, llm, secrets):
            calls["n"] += 1
            return {"description_en": "EN", "card_summary_en": "blurb", "truncated": False}

        monkeypatch.setattr(svc, "translate_job_text", fake_translate)
        first = svc.translate_job("de1")
        second = svc.translate_job("de1")
        assert first["description_en"] == "EN"
        assert second["description_en"] == "EN"
        assert calls["n"] == 1, "second call should be served from the cache"


class TestSearchInjection:
    def test_search_discovery_queries_are_injected(self, app, monkeypatch):
        captured = {}

        async def fake_run_once(settings, sources, secrets, **kwargs):
            captured["sources"] = sources
            captured["max_per_run"] = settings.notifications.max_per_run
            now = datetime.now(UTC)
            return RunSummary(started_at=now, finished_at=now)

        monkeypatch.setattr(svc, "run_once", fake_run_once)
        svc.run_search("reinforcement learning, computer vision", ["NLP"], None)

        sd = next(s for s in captured["sources"].sources if s.name == "search_discovery")
        assert sd.queries != ["original query"]
        assert all(q.startswith("site:") for q in sd.queries)
        assert '"reinforcement learning"' in sd.queries[0]
        # Stores only — nothing is auto-sent.
        assert captured["max_per_run"] == 0

    def test_no_terms_keeps_original_queries(self, app, monkeypatch):
        captured = {}

        async def fake_run_once(settings, sources, secrets, **kwargs):
            captured["sources"] = sources
            now = datetime.now(UTC)
            return RunSummary(started_at=now, finished_at=now)

        monkeypatch.setattr(svc, "run_once", fake_run_once)
        svc.run_search("", None, None)
        sd = next(s for s in captured["sources"].sources if s.name == "search_discovery")
        assert sd.queries == ["original query"]

    def test_lock_collision_is_friendly(self, app, monkeypatch):
        from job_alerts.scheduler import RunLockedError

        async def fake_run_once(*a, **k):
            raise RunLockedError("busy")

        monkeypatch.setattr(svc, "run_once", fake_run_once)
        msg = svc.run_search("nlp", None, None)
        assert "in progress" in msg.lower()
