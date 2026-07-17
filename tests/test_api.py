"""The FastAPI JSON layer.

Runs against a seeded tmp database with the Discord send, the LLM and the
pipeline all mocked — like `test_dashboard_service.py`, no test touches a live
endpoint or the user's real config. The API is a thin delegate, so these tests
assert the wiring (shapes, filters, status codes, auth), not the service logic
already covered next door.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from job_alerts.api.app import create_app
from job_alerts.config import HttpSettings, SourceConfig, SourcesConfig
from job_alerts.dashboard import service as svc
from job_alerts.database import Database
from job_alerts.models import JobStatus, Language

SEARCH_DISCOVERY = SourceConfig(
    name="search_discovery",
    type="search_api",
    enabled=True,
    allowed_domains=["de.linkedin.com", "fraunhofer.de", "mpg.de"],
    queries=["original query"],
)


@pytest.fixture
def client(tmp_path, settings, discord_secrets, job_factory, monkeypatch):
    """A TestClient over the API, backed by a seeded tmp database."""
    settings.database.path = tmp_path / "jobs.db"
    # Deterministic, network-free HTTP for the refresh/link-check tests: no
    # robots.txt fetches, no pacing, no retry backoff.
    settings.http = HttpSettings(
        per_domain_delay=0.0, cache_ttl_seconds=0, max_retries=1, respect_robots=False
    )
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
        db.upsert(job_factory(id="not1", url="https://x.de/3", status=JobStatus.NOTIFIED))
        db.mark_notified(["not1"], when=datetime(2026, 7, 1, tzinfo=UTC))
        db.upsert(
            job_factory(
                id="de1",
                url="https://x.de/4",
                language=Language.DE,
                description="Wir suchen eine studentische Hilfskraft.",
            )
        )

    svc._config = svc.AppConfig(
        settings=settings,
        sources=SourcesConfig(sources=[SEARCH_DISCOVERY.model_copy(deep=True)]),
        secrets=discord_secrets,
    )
    # No live network: the health probe must not actually dial the tunnel.
    monkeypatch.setattr(svc, "llm_online", lambda: True)
    with TestClient(create_app()) as c:
        yield c
    svc._config = None


class TestListing:
    def test_lists_all_by_default(self, client):
        body = client.get("/api/jobs").json()
        assert body["total"] == 4
        assert {j["id"] for j in body["jobs"]} == {"new1", "rej1", "not1", "de1"}
        # Summary carries what a card needs, including the logo URL.
        sample = body["jobs"][0]
        for key in ("id", "title", "relevance_score", "status", "logo", "score_color"):
            assert key in sample

    def test_status_filter(self, client):
        body = client.get("/api/jobs", params={"status": "rejected"}).json()
        assert [j["id"] for j in body["jobs"]] == ["rej1"]

    def test_all_means_no_filter(self, client):
        body = client.get("/api/jobs", params={"status": "all", "source": "all"}).json()
        assert body["total"] == 4

    def test_text_filter(self, client):
        assert client.get("/api/jobs", params={"text": "TU Munich"}).json()["total"] == 4
        assert client.get("/api/jobs", params={"text": "zzz-none"}).json()["total"] == 0

    def test_hidden_excluded_then_shown(self, client):
        client.post("/api/jobs/rej1/hide")
        ids = {j["id"] for j in client.get("/api/jobs").json()["jobs"]}
        assert "rej1" not in ids
        shown = client.get("/api/jobs", params={"show_hidden": True}).json()["jobs"]
        assert any(j["id"] == "rej1" and j["hidden"] for j in shown)


class TestDetail:
    def test_detail_shape(self, client):
        body = client.get("/api/jobs/new1").json()
        assert body["exists"] is True
        assert body["job"]["id"] == "new1"
        assert body["job"]["logo"]
        assert body["needs_confirm"] is False

    def test_rejected_needs_confirm(self, client):
        body = client.get("/api/jobs/rej1").json()
        assert body["needs_confirm"] is True
        assert "completed PhD" in body["rejection_reason"]

    def test_missing_job_404(self, client):
        assert client.get("/api/jobs/nope").status_code == 404

    def test_german_job_returns_cached_translation(self, client, settings):
        # GET detail is cache-only (refresh does the live translating), so a
        # cached translation is surfaced but none is computed here.
        with Database(settings.database.path) as db:
            job = db.get("de1")
            db.save_translation(
                "de1", job.content_hash, "We are looking for a student assistant.", "blurb", False
            )
        body = client.get("/api/jobs/de1").json()
        assert body["is_german"] is True
        assert body["translation"]["description_en"].startswith("We are looking")


class TestMetaAndHealth:
    def test_health(self, client):
        body = client.get("/api/health").json()
        assert body["status"] == "ok" and body["llm_online"] is True

    def test_meta(self, client):
        body = client.get("/api/meta").json()
        # `sources` are the distinct job sources present in the DB (seeded as "test").
        assert body["sources"] == ["test"]
        assert body["stats"]["total_jobs"] == 4


class TestSearch:
    def test_run_returns_task_and_polls_to_done(self, client, monkeypatch):
        monkeypatch.setattr(svc, "run_search", lambda kw, topics, locs: "Done in 1s.")
        resp = client.post("/api/search/run", json={"keywords": "nlp"})
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # The task runs in a daemon thread; poll until it settles.
        for _ in range(50):
            body = client.get(f"/api/search/run/{task_id}").json()
            if body["status"] != "running":
                break
        assert body["status"] == "done"
        assert body["result"] == "Done in 1s."

    def test_unknown_task_404(self, client):
        assert client.get("/api/search/run/deadbeef").status_code == 404

    def test_preview(self, client):
        body = client.post("/api/search/preview", json={"keywords": "nlp"}).json()
        assert "queries" in body and "scope" in body


class TestAuth:
    def test_write_requires_credentials_when_configured(self, client, monkeypatch):
        monkeypatch.setenv("JOB_ALERTS_API_AUTH", "admin:secret")
        assert client.post("/api/jobs/new1/hide").status_code == 401
        ok = client.post("/api/jobs/new1/hide", auth=("admin", "secret"))
        assert ok.status_code == 200

    def test_reads_never_require_auth(self, client, monkeypatch):
        monkeypatch.setenv("JOB_ALERTS_API_AUTH", "admin:secret")
        assert client.get("/api/jobs").status_code == 200


LONG_PAGE = (
    "<html><body><main><h1>Research Assistant</h1><p>"
    + ("This is a much longer posting body describing the role in detail. " * 40)
    + "Apply at https://forms.gle/abc123 by email.</p></main></body></html>"
)


def _seed(db_path, job_factory, **overrides):
    with Database(db_path) as db:
        db.upsert(job_factory(**overrides))


class TestRefresh:
    @respx.mock
    def test_alive_stores_longer_description(self, client, settings, job_factory):
        _seed(settings.database.path, job_factory, id="ref1", url="https://ref.de/1", description="short stub")
        respx.get("https://ref.de/1").mock(return_value=httpx.Response(200, text=LONG_PAGE))

        body = client.post("/api/jobs/ref1/refresh").json()
        assert body["link_status"] == "alive"
        assert len(body["job"]["description"]) > 500

    @respx.mock
    def test_dead_link_autohides(self, client, settings, job_factory):
        _seed(settings.database.path, job_factory, id="dead1", url="https://gone.de/1", description="no links here")
        respx.get("https://gone.de/1").mock(return_value=httpx.Response(404))

        body = client.post("/api/jobs/dead1/refresh").json()
        assert body["link_status"] == "dead"
        # Soft-hidden, reversible — it drops from the default list.
        ids = {j["id"] for j in client.get("/api/jobs").json()["jobs"]}
        assert "dead1" not in ids
        shown = client.get("/api/jobs", params={"show_hidden": True}).json()["jobs"]
        assert any(j["id"] == "dead1" for j in shown)

    @respx.mock
    def test_dead_with_live_alternate_is_not_hidden(self, client, settings, job_factory):
        _seed(
            settings.database.path,
            job_factory,
            id="moved1",
            url="https://gone.de/2",
            description="See https://alt.de/apply to apply.",
        )
        respx.get("https://gone.de/2").mock(return_value=httpx.Response(410))
        respx.get("https://alt.de/apply").mock(return_value=httpx.Response(200, text="<html/>"))

        body = client.post("/api/jobs/moved1/refresh").json()
        assert body["link_status"] == "moved"
        assert body["apply_url"] == "https://alt.de/apply"
        ids = {j["id"] for j in client.get("/api/jobs").json()["jobs"]}
        assert "moved1" in ids  # not hidden — a live alternate exists

    @respx.mock
    def test_timeout_does_not_hide(self, client, settings, job_factory):
        _seed(settings.database.path, job_factory, id="slow1", url="https://slow.de/1", description="x")
        respx.get("https://slow.de/1").mock(side_effect=httpx.ConnectTimeout("timed out"))

        body = client.post("/api/jobs/slow1/refresh").json()
        assert body["link_status"] == "unverifiable"
        ids = {j["id"] for j in client.get("/api/jobs").json()["jobs"]}
        assert "slow1" in ids  # a transient failure never hides

    @respx.mock
    def test_german_job_translates_on_full_text(self, client, settings, job_factory, monkeypatch):
        _seed(
            settings.database.path,
            job_factory,
            id="deref",
            url="https://de.de/1",
            language=Language.DE,
            description="kurz",
        )
        respx.get("https://de.de/1").mock(
            return_value=httpx.Response(
                200,
                text="<html><body><main><p>"
                + ("Wir suchen eine studentische Hilfskraft mit Erfahrung. " * 30)
                + "</p></main></body></html>",
            )
        )

        async def fake_translate(text, llm, secrets):
            return {
                "description_en": "We seek a working student. " * 20,
                "card_summary_en": "A student role.",
                "truncated": False,
            }

        monkeypatch.setattr(svc, "translate_job_text", fake_translate)
        body = client.post("/api/jobs/deref/refresh").json()
        assert body["is_german"] is True
        assert body["translation"]["description_en"].startswith("We seek")

    def test_refresh_missing_job_404(self, client):
        assert client.post("/api/jobs/nope/refresh").status_code == 404


class TestLinkSweep:
    @respx.mock
    def test_hides_only_definitely_dead(self, client, settings, job_factory):
        _seed(settings.database.path, job_factory, id="live", url="https://live.de/1", description="a")
        _seed(settings.database.path, job_factory, id="gone", url="https://gone.de/9", description="a")
        respx.get("https://live.de/1").mock(return_value=httpx.Response(200, text="<html/>"))
        respx.get("https://gone.de/9").mock(return_value=httpx.Response(404))
        # The fixture's seeded jobs (x.de/1..4) are also swept — treat them as alive.
        respx.route(host="x.de").mock(return_value=httpx.Response(200, text="<html/>"))

        msg = svc.check_all_links()
        assert "hid 1" in msg
        with Database(settings.database.path) as db:
            assert "gone" in db.hidden_ids()
            assert "live" not in db.hidden_ids()


class TestSettings:
    def test_get_masks_secrets(self, client):
        body = client.get("/api/settings").json()
        assert "discord_webhook_url" in body["secrets"]
        # The seeded discord_secrets webhook is set; the raw value is never sent.
        wh = body["secrets"]["discord_webhook_url"]
        assert wh["set"] is True and "http" not in wh["hint"]

    def test_post_persists_and_applies(self, client, settings):
        r = client.post("/api/settings", json={"colab_base_url": "https://tunnel.example"})
        assert r.status_code == 200
        assert r.json()["colab_base_url"] == "https://tunnel.example"
        # Applied to the live config in place …
        assert svc.get_config().settings.llm.colab_base_url == "https://tunnel.example"
        # … and persisted to the overlay table for the next process to read.
        with Database(settings.database.path) as db:
            assert db.get_config_overlay()["colab_base_url"] == "https://tunnel.example"

    def test_post_webhook_reflected_in_masked_status(self, client):
        client.post("/api/settings", json={"discord_webhook_url": "https://discord/xyz9999"})
        wh = client.get("/api/settings").json()["secrets"]["discord_webhook_url"]
        assert wh["set"] is True and wh["hint"].endswith("9999")

    def test_post_rejects_bad_provider(self, client):
        assert client.post("/api/settings", json={"search_api_provider": "bogus"}).status_code == 422

    def test_settings_requires_auth(self, client, monkeypatch):
        monkeypatch.setenv("JOB_ALERTS_API_AUTH", "admin:secret")
        assert client.get("/api/settings").status_code == 401
        assert client.get("/api/settings", auth=("admin", "secret")).status_code == 200


class TestSpaFallback:
    """A deep link like /profile is a client-side route, not a file — the static
    layer must serve index.html so React Router can take over on a hard load."""

    @pytest.fixture
    def spa_client(self, tmp_path):
        (tmp_path / "index.html").write_text("<!doctype html><title>LabScout</title>")
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "app.js").write_text("// built bundle")
        with TestClient(create_app(static_dir=str(tmp_path))) as c:
            yield c

    def test_deep_link_serves_index(self, spa_client):
        for route in ("/profile", "/settings", "/jobs/abc123"):
            r = spa_client.get(route)
            assert r.status_code == 200
            assert "LabScout" in r.text

    def test_real_asset_is_served(self, spa_client):
        assert spa_client.get("/assets/app.js").status_code == 200

    def test_missing_asset_still_404s(self, spa_client):
        # A path with a file extension is an asset request, not a client route.
        assert spa_client.get("/assets/nope.js").status_code == 404

    def test_api_still_wins_over_spa(self, spa_client):
        assert spa_client.get("/api/health").status_code == 200
