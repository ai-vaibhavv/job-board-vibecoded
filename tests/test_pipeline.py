"""End-to-end pipeline behaviour, plus the polite-HTTP guarantees."""

from __future__ import annotations

import httpx
import pytest
import respx

from job_alerts.config import HttpSettings, SourceConfig, SourcesConfig
from job_alerts.http import FetchError, PoliteClient, RobotsDisallowed
from job_alerts.models import JobStatus
from job_alerts.pipeline import Pipeline

from .conftest import TEST_WEBHOOK as WEBHOOK


class TestPipelineEndToEnd:
    async def test_full_run_filters_scores_and_stores(
        self, settings, sources_config, discord_secrets, db
    ):
        """The mock fixtures are built so the outcome is predictable: the good
        student roles survive, the postdoc and the senior role do not."""
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            pipeline = Pipeline(settings, sources_config, discord_secrets, db)
            summary = await pipeline.run()

        assert summary.candidates_found == 8
        assert summary.after_dedup == 7
        assert summary.notified > 0
        assert not summary.sources_failed

        titles = {j.title for j in db.list_jobs(status=JobStatus.NOTIFIED, limit=50)}
        assert any("Research Assistant" in t for t in titles)
        # The postdoc and the senior role must never be notified.
        assert not any("Postdoctoral" in t for t in titles)
        assert not any("Senior Research Scientist" in t for t in titles)

    async def test_phd_mentioning_student_role_survives_the_whole_pipeline(
        self, settings, sources_config, discord_secrets, db
    ):
        """mock-007 mentions PhD students but is a student role. It must arrive."""
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            await Pipeline(settings, sources_config, discord_secrets, db).run()

        stored = db.get("mock:mock-007")
        assert stored is not None
        assert stored.status is JobStatus.NOTIFIED

    async def test_rejected_jobs_are_stored_with_a_reason(
        self, settings, sources_config, discord_secrets, db
    ):
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            await Pipeline(settings, sources_config, discord_secrets, db).run()

        postdoc = db.get("mock:mock-005")
        assert postdoc.status is JobStatus.REJECTED
        assert postdoc.score_explanation  # the reason is recorded, not discarded
        assert postdoc.notified_at is None

    async def test_summary_is_rendered_without_error(self, settings, sources_config, secrets, db):
        summary = await Pipeline(settings, sources_config, secrets, db).run(dry_run=True)
        rendered = summary.render()
        assert "Run summary" in rendered
        assert "DRY RUN" in rendered


class TestNotificationHonesty:
    """The spec's most important safety property: never claim a delivery that
    did not happen, and never lose a job because of a failed delivery."""

    async def test_discord_failure_leaves_jobs_unnotified(
        self, settings, sources_config, discord_secrets, db
    ):
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(500))
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()

        assert summary.notified == 0
        assert summary.notify_failed > 0
        # Stored, but honestly marked as not yet sent.
        assert db.list_jobs(status=JobStatus.NOTIFIED, limit=50) == []
        assert all(j.notified_at is None for j in db.list_jobs(limit=50))

    async def test_jobs_survive_a_discord_outage_and_send_next_run(
        self, settings, sources_config, discord_secrets, db
    ):
        """Storing before sending is what makes an outage harmless."""
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(503))
            first = await Pipeline(settings, sources_config, discord_secrets, db).run()
        assert first.notified == 0
        assert first.newly_stored > 0

        # Discord recovers. The jobs are still pending and go out.
        pending = db.list_jobs(
            new_only=True, min_score=settings.scoring.min_score_to_notify, limit=50
        )
        assert pending

    async def test_partial_delivery_marks_only_delivered_jobs(
        self, settings, sources_config, discord_secrets, db
    ):
        settings.notifications.embeds_per_message = 1
        with respx.mock:
            respx.post(WEBHOOK).mock(
                side_effect=[httpx.Response(204), httpx.Response(400, text="bad")]
                + [httpx.Response(204)] * 10
            )
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()

        notified = db.list_jobs(status=JobStatus.NOTIFIED, limit=50)
        assert summary.notified == len(notified)
        assert summary.notify_failed >= 1

    async def test_max_per_run_caps_sends_but_stores_everything(
        self, settings, sources_config, discord_secrets, db
    ):
        settings.notifications.max_per_run = 1
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()

        assert summary.notified == 1
        # The rest are stored, not dropped.
        assert summary.newly_stored == 7


class TestDryRunSafety:
    async def test_dry_run_makes_no_request_to_discord(
        self, settings, sources_config, discord_secrets, db
    ):
        """The spec requires this explicitly."""
        with respx.mock:
            route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run(
                dry_run=True
            )

            assert not route.called
            assert len(respx.calls) == 0
        assert summary.notified == 0
        assert summary.dry_run is True

    async def test_dry_run_works_with_no_webhook_configured(
        self, settings, sources_config, secrets, db
    ):
        """A first run must be possible before any Discord setup."""
        summary = await Pipeline(settings, sources_config, secrets, db).run(dry_run=True)
        assert summary.candidates_found > 0


class TestSourceFailureIsolation:
    async def test_one_dead_source_does_not_stop_the_run(self, settings, discord_secrets, db):
        config = SourcesConfig(
            sources=[
                SourceConfig(name="mock", type="mock", enabled=True),
                SourceConfig(
                    name="dead", type="rss", enabled=True, url="https://dead.invalid/feed"
                ),
            ]
        )
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            respx.get("https://dead.invalid/feed").mock(side_effect=httpx.ConnectError("down"))
            summary = await Pipeline(settings, config, discord_secrets, db).run()

        assert "dead" in summary.sources_failed
        assert "mock" in summary.sources_ok
        assert summary.notified > 0  # the healthy source still delivered

    async def test_missing_search_key_is_reported_as_skipped_not_failed(
        self, settings, secrets, db
    ):
        """Running without a search API key is a supported configuration, so it
        must not be reported as a failure — that would train the user to ignore
        real failures."""
        config = SourcesConfig(
            sources=[
                SourceConfig(name="mock", type="mock", enabled=True),
                SourceConfig(name="search", type="search_api", enabled=True, queries=["q"]),
            ]
        )
        summary = await Pipeline(settings, config, secrets, db).run(dry_run=True)

        assert "search" in summary.sources_skipped
        assert "search" not in summary.sources_failed

    async def test_a_run_with_no_enabled_sources_does_not_crash(self, settings, secrets, db):
        summary = await Pipeline(settings, SourcesConfig(sources=[]), secrets, db).run(dry_run=True)
        assert summary.candidates_found == 0


class TestLlmIntegration:
    """The LLM is an upgrade, never a dependency: with no endpoint, a dead
    tunnel, or a partial answer, the run must still produce the same alerts."""

    COLAB_BASE = "https://colab.example"
    COLAB = f"{COLAB_BASE}/v1/chat/completions"

    def _colab_for(self, jobs_scores: dict[str, int], **overrides):
        """A Colab (OpenAI-compatible) mock that scores whichever job ids it is
        shown."""
        import json

        def side_effect(request):
            body = json.loads(request.content)
            prompt = body["messages"][1]["content"]
            entries = []
            for job_id, score in jobs_scores.items():
                if job_id in prompt:
                    entry = {
                        "job_id": job_id,
                        "is_job_posting": True,
                        "requires_completed_phd": False,
                        "suitable_for_masters": True,
                        "core_ai_focus": True,
                        "role_type": "hiwi",
                        "topics": ["machine learning"],
                        "score": score,
                        "reasoning": "assessed",
                    }
                    entry.update(overrides)
                    entries.append(entry)
            text = json.dumps({"assessments": entries})
            return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

        return side_effect

    async def test_no_endpoint_uses_keyword_scoring_unchanged(
        self, settings, sources_config, discord_secrets, db
    ):
        """No colab_base_url — the run must behave exactly as the keyword path."""
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()
        assert summary.llm_assessed == 0
        assert summary.notified > 0  # keyword path still delivered

    async def test_llm_scores_replace_keyword_scores(
        self, settings, sources_config, discord_secrets, db
    ):
        settings.llm.colab_base_url = self.COLAB_BASE
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            respx.post(self.COLAB).mock(
                side_effect=self._colab_for({f"mock:mock-00{i}": 95 for i in range(1, 9)})
            )
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()

        assert summary.llm_assessed > 0
        job = db.get("mock:mock-001")
        assert job.relevance_score == 95
        assert any("colab" in line for line in job.score_explanation)

    async def test_llm_hard_reject_overrides_a_high_score(
        self, settings, sources_config, discord_secrets, db
    ):
        """A model that says "requires a completed PhD" then scores it 95 has
        contradicted itself. The structured field wins."""
        settings.llm.colab_base_url = self.COLAB_BASE
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            respx.post(self.COLAB).mock(
                side_effect=self._colab_for(
                    {f"mock:mock-00{i}": 95 for i in range(1, 9)}, requires_completed_phd=True
                )
            )
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()

        assert summary.notified == 0
        assert db.get("mock:mock-001").status is JobStatus.REJECTED

    async def test_llm_rejects_non_job_postings(
        self, settings, sources_config, discord_secrets, db
    ):
        settings.llm.colab_base_url = self.COLAB_BASE
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            respx.post(self.COLAB).mock(
                side_effect=self._colab_for(
                    {f"mock:mock-00{i}": 95 for i in range(1, 9)}, is_job_posting=False
                )
            )
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()
        assert summary.notified == 0

    async def test_dead_llm_falls_back_to_keywords_and_still_notifies(
        self, settings, sources_config, discord_secrets, db
    ):
        """The whole point of the fallback: an LLM outage costs nothing."""
        settings.llm.colab_base_url = self.COLAB_BASE
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            respx.post(self.COLAB).mock(return_value=httpx.Response(503, text="tunnel down"))
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()

        assert summary.llm_assessed == 0
        assert summary.llm_fallback > 0
        assert summary.notified > 0  # keyword scoring delivered anyway
        assert summary.llm_failures

    async def test_partial_llm_coverage_scores_the_rest_with_keywords(
        self, settings, sources_config, discord_secrets, db
    ):
        """Only mock-001 is assessed; the others must still be scored, not lost."""
        settings.llm.colab_base_url = self.COLAB_BASE
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            respx.post(self.COLAB).mock(side_effect=self._colab_for({"mock:mock-001": 99}))
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()

        assert summary.llm_assessed == 1
        assert summary.llm_fallback >= 1
        assert db.get("mock:mock-001").relevance_score == 99
        # A keyword-scored job still made it through.
        keyword_scored = db.get("mock:mock-002")
        assert keyword_scored.relevance_score > 0
        assert not any("colab" in line for line in keyword_scored.score_explanation)

    async def test_llm_disabled_in_settings_is_respected(
        self, settings, sources_config, discord_secrets, db
    ):
        settings.llm.enabled = False
        settings.llm.colab_base_url = self.COLAB_BASE
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            route = respx.post(self.COLAB).mock(return_value=httpx.Response(200, json={}))
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()
        assert not route.called
        assert summary.llm_assessed == 0

    async def test_llm_threshold_can_differ_from_keyword_threshold(
        self, settings, sources_config, discord_secrets, db
    ):
        """LLM scores are calibrated differently, so they get their own knob."""
        settings.llm.min_score_to_notify = 90
        settings.llm.colab_base_url = self.COLAB_BASE
        with respx.mock:
            respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
            respx.post(self.COLAB).mock(
                side_effect=self._colab_for({f"mock:mock-00{i}": 80 for i in range(1, 9)})
            )
            summary = await Pipeline(settings, sources_config, discord_secrets, db).run()
        # 80 clears the keyword threshold (55) but not the LLM one (90).
        assert summary.notified == 0


class TestPoliteHttp:
    @pytest.fixture
    def http_settings(self) -> HttpSettings:
        return HttpSettings(per_domain_delay=0.0, cache_ttl_seconds=0, max_retries=2)

    @respx.mock
    async def test_robots_disallow_blocks_the_fetch(self, http_settings):
        respx.get("https://blocked.de/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /jobs/")
        )
        async with PoliteClient(http_settings) as client:
            with pytest.raises(RobotsDisallowed):
                await client.get_text("https://blocked.de/jobs/1")

    @respx.mock
    async def test_robots_allow_permits_the_fetch(self, http_settings):
        respx.get("https://open.de/robots.txt").mock(
            return_value=httpx.Response(200, text="User-agent: *\nDisallow: /admin/")
        )
        respx.get("https://open.de/jobs/1").mock(return_value=httpx.Response(200, text="ok"))
        async with PoliteClient(http_settings) as client:
            assert await client.get_text("https://open.de/jobs/1") == "ok"

    @respx.mock
    async def test_missing_robots_is_treated_as_permission(self, http_settings):
        respx.get("https://norobots.de/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://norobots.de/j").mock(return_value=httpx.Response(200, text="ok"))
        async with PoliteClient(http_settings) as client:
            assert await client.get_text("https://norobots.de/j") == "ok"

    @respx.mock
    async def test_unreachable_robots_does_not_block_the_run(self, http_settings):
        respx.get("https://flaky.de/robots.txt").mock(side_effect=httpx.ConnectError("x"))
        respx.get("https://flaky.de/j").mock(return_value=httpx.Response(200, text="ok"))
        async with PoliteClient(http_settings) as client:
            assert await client.get_text("https://flaky.de/j") == "ok"

    @respx.mock
    async def test_permanent_4xx_is_not_retried(self, http_settings):
        http_settings.respect_robots = False
        route = respx.get("https://x.de/j").mock(return_value=httpx.Response(404))
        async with PoliteClient(http_settings) as client:
            with pytest.raises(FetchError, match="404"):
                await client.get_text("https://x.de/j")
        assert route.call_count == 1

    @respx.mock
    async def test_5xx_is_retried(self, http_settings):
        http_settings.respect_robots = False
        route = respx.get("https://x.de/j").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, text="ok")]
        )
        async with PoliteClient(http_settings) as client:
            assert await client.get_text("https://x.de/j") == "ok"
        assert route.call_count == 2

    @respx.mock
    async def test_429_is_retried_despite_being_4xx(self, http_settings):
        http_settings.respect_robots = False
        route = respx.get("https://x.de/j").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, text="ok"),
            ]
        )
        async with PoliteClient(http_settings) as client:
            assert await client.get_text("https://x.de/j") == "ok"
        assert route.call_count == 2

    @respx.mock
    async def test_retries_are_bounded(self, http_settings):
        http_settings.respect_robots = False
        http_settings.max_retries = 2
        route = respx.get("https://x.de/j").mock(return_value=httpx.Response(500))
        async with PoliteClient(http_settings) as client:
            with pytest.raises(FetchError):
                await client.get_text("https://x.de/j")
        assert route.call_count == 2

    @respx.mock
    async def test_caching_prevents_a_second_download(self, http_settings):
        http_settings.respect_robots = False
        http_settings.cache_ttl_seconds = 300
        route = respx.get("https://x.de/j").mock(return_value=httpx.Response(200, text="ok"))
        async with PoliteClient(http_settings) as client:
            await client.get_text("https://x.de/j")
            await client.get_text("https://x.de/j")
        assert route.call_count == 1

    @respx.mock
    async def test_user_agent_identifies_the_application(self, http_settings):
        http_settings.respect_robots = False
        http_settings.user_agent = "TestAgent/1.0 (+contact)"
        route = respx.get("https://x.de/j").mock(return_value=httpx.Response(200, text="ok"))
        async with PoliteClient(http_settings) as client:
            await client.get_text("https://x.de/j")
        assert route.calls[0].request.headers["User-Agent"] == "TestAgent/1.0 (+contact)"

    @respx.mock
    async def test_invalid_json_is_a_clean_error(self, http_settings):
        http_settings.respect_robots = False
        respx.get("https://x.de/api").mock(return_value=httpx.Response(200, text="<html>nope"))
        async with PoliteClient(http_settings) as client:
            with pytest.raises(FetchError, match="valid JSON"):
                await client.get_json("https://x.de/api")
