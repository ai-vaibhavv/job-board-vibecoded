"""Discord: embed limits, sanitization, delivery honesty, dry-run safety."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from job_alerts.config import NotificationSettings
from job_alerts.notifications.discord import (
    CARD_SUMMARY_MAX,
    MAX_EMBED_TITLE,
    MAX_EMBED_TOTAL,
    MAX_EMBEDS_PER_MESSAGE,
    DiscordNotifier,
    _embed_length,
    _pretty_label,
    build_embed,
    build_messages,
    org_image_url,
    render_dry_run,
    sanitize,
    truncate,
)

from .conftest import TEST_WEBHOOK as WEBHOOK
from .conftest import make_job


@pytest.fixture
def notify_settings() -> NotificationSettings:
    return NotificationSettings(max_per_run=10, embeds_per_message=5, description_excerpt_chars=400)


class TestAcademicTaxonomyFields:
    def test_taxonomy_fields_are_shown_when_present(self, notify_settings):
        job = make_job(opportunity_type="master_thesis", applicant_level="master", academic_field="ml")
        embed = build_embed(job, notify_settings)
        names = [f["name"] for f in embed["fields"]]
        values = [f["value"] for f in embed["fields"]]
        assert any("Opportunity" in n for n in names)
        assert "Master Thesis" in values
        assert "ML" in values

    def test_absent_taxonomy_adds_no_fields(self, notify_settings):
        job = make_job()  # opportunity_type/applicant_level/academic_field are None
        embed = build_embed(job, notify_settings)
        names = [f["name"] for f in embed["fields"]]
        assert not any("Opportunity" in n for n in names)

    def test_pretty_label_keeps_acronyms(self):
        assert _pretty_label("ml") == "ML"
        assert _pretty_label("phd_position") == "PhD Position"
        assert _pretty_label("student_assistant") == "Student Assistant"


class TestEmbedLimits:
    """Discord enforces these as hard 400s, so they are not advisory."""

    def test_long_title_is_truncated(self, notify_settings):
        embed = build_embed(make_job(title="R" * 1000), notify_settings)
        assert len(embed["title"]) <= MAX_EMBED_TITLE

    def test_huge_job_stays_within_the_total_budget(self, notify_settings):
        job = make_job(
            title="Research Assistant " * 30,
            organization="Institute " * 100,
            location="Munich " * 100,
            description="Machine learning. " * 2000,
            salary="EUR " * 200,
            matched_keywords=[f"keyword-{i}" for i in range(100)],
        )
        embed = build_embed(job, notify_settings)
        assert _embed_length(embed) <= MAX_EMBED_TOTAL

    def test_every_field_respects_its_own_cap(self, notify_settings):
        job = make_job(
            organization="O" * 5000,
            location="L" * 5000,
            employment_type="T" * 5000,
            matched_keywords=["k" * 500 for _ in range(20)],
        )
        embed = build_embed(job, notify_settings)
        for field in embed["fields"]:
            assert len(field["name"]) <= 256
            assert len(field["value"]) <= 1024
        assert len(embed["fields"]) <= 25

    def test_description_is_excerpted_to_the_configured_length(self, notify_settings):
        embed = build_embed(make_job(description="word " * 5000), notify_settings)
        # Every card is capped to one uniform length, regardless of config.
        assert len(embed["description"]) <= CARD_SUMMARY_MAX


class TestCardRedesign:
    """Consistent, image-rich cards: an LLM blurb capped to one length for every
    job, and an organization logo."""

    def test_card_summary_is_used_as_the_description(self, notify_settings):
        job = make_job(
            card_summary="Student research assistant in computer vision at TU Munich.",
            description="A" * 4000,  # the raw posting must NOT be what shows.
        )
        embed = build_embed(job, notify_settings)
        assert "computer vision" in embed["description"]
        assert "AAAA" not in embed["description"]

    def test_a_long_summary_is_capped_uniformly(self, notify_settings):
        embed = build_embed(make_job(card_summary="ml role. " * 200), notify_settings)
        assert len(embed["description"]) <= CARD_SUMMARY_MAX

    def test_without_a_summary_it_falls_back_to_a_capped_excerpt(self, notify_settings):
        job = make_job(card_summary=None, description="Machine learning HiWi. " * 200)
        embed = build_embed(job, notify_settings)
        assert embed["description"]
        assert len(embed["description"]) <= CARD_SUMMARY_MAX

    def test_a_known_org_gets_a_curated_logo(self):
        job = make_job(organization="Fraunhofer FKIE", url="https://jobs.fraunhofer.de/x")
        assert "fraunhofer.de" in (org_image_url(job) or "")

    def test_an_unknown_org_falls_back_to_the_url_host(self):
        job = make_job(organization="Acme Robotics GmbH", url="https://careers.acme-robotics.de/1")
        assert "careers.acme-robotics.de" in (org_image_url(job) or "")

    def test_the_embed_carries_an_author_and_thumbnail(self, notify_settings):
        job = make_job(organization="Fraunhofer FKIE", url="https://jobs.fraunhofer.de/x")
        embed = build_embed(job, notify_settings)
        assert embed["author"]["name"]
        assert embed["author"]["icon_url"]
        assert embed["thumbnail"]["url"]

    def test_organization_is_not_also_a_field(self, notify_settings):
        """It lives in the author block now; duplicating it as a field was the
        redundancy the redesign removes."""
        embed = build_embed(make_job(organization="TU Munich"), notify_settings)
        assert not any("Organization" in f["name"] for f in embed["fields"])

    def test_image_rich_card_still_respects_the_total_budget(self, notify_settings):
        job = make_job(
            organization="Institute " * 100,
            card_summary="ml. " * 200,
            matched_keywords=[f"k{i}" for i in range(100)],
        )
        embed = build_embed(job, notify_settings)
        assert _embed_length(embed) <= MAX_EMBED_TOTAL

    def test_never_more_than_ten_embeds_per_message(self, notify_settings):
        notify_settings.embeds_per_message = 10
        jobs = [make_job(id=f"j{i}", url=f"https://e.de/{i}") for i in range(25)]
        for payload, _ in build_messages(jobs, notify_settings):
            assert len(payload["embeds"]) <= MAX_EMBEDS_PER_MESSAGE

    def test_content_stays_within_2000_chars(self, notify_settings):
        jobs = [make_job(id=f"j{i}", url=f"https://e.de/{i}") for i in range(3)]
        for payload, _ in build_messages(jobs, notify_settings, extra_stored=999):
            assert len(payload["content"]) <= 2000

    def test_truncate_helper_marks_elision(self):
        assert truncate("abcdef", 4) == "abc…"
        assert truncate("abc", 10) == "abc"


class TestSanitization:
    """Job text comes from third-party sites and is never trusted."""

    def test_everyone_mention_is_defanged(self):
        assert "@everyone" not in sanitize("Hello @everyone apply now")

    def test_here_mention_is_defanged(self):
        assert "@here" not in sanitize("@here")

    def test_markdown_is_escaped_not_stripped(self):
        cleaned = sanitize("**bold** and _italic_ and `code`")
        assert "\\*" in cleaned
        assert "bold" in cleaned  # readable text survives

    def test_allowed_mentions_disables_pings_at_the_api_level(self, notify_settings):
        payload, _ = build_messages([make_job()], notify_settings)[0]
        assert payload["allowed_mentions"] == {"parse": []}

    def test_malicious_title_reaches_the_embed_defanged(self, notify_settings):
        embed = build_embed(make_job(title="@everyone **URGENT**"), notify_settings)
        assert "@everyone" not in embed["title"]

    def test_sanitize_handles_none(self):
        assert sanitize(None) == ""


class TestBatching:
    def test_jobs_are_split_across_messages(self, notify_settings):
        notify_settings.embeds_per_message = 5
        jobs = [make_job(id=f"j{i}", url=f"https://e.de/{i}") for i in range(12)]
        messages = build_messages(jobs, notify_settings)
        assert len(messages) == 3
        assert [len(p["embeds"]) for p, _ in messages] == [5, 5, 2]

    def test_every_job_id_is_accounted_for_exactly_once(self, notify_settings):
        jobs = [make_job(id=f"j{i}", url=f"https://e.de/{i}") for i in range(12)]
        batched = [jid for _, ids in build_messages(jobs, notify_settings) for jid in ids]
        assert batched == [j.id for j in jobs]

    def test_extra_stored_count_is_reported_to_the_user(self, notify_settings):
        messages = build_messages([make_job()], notify_settings, extra_stored=7)
        assert "+7 more" in messages[-1][0]["content"]

    def test_no_extra_note_when_nothing_was_held_back(self, notify_settings):
        messages = build_messages([make_job()], notify_settings, extra_stored=0)
        assert "more matching job" not in messages[-1][0]["content"]


class TestDryRun:
    """The spec is explicit: dry-run makes no network call to Discord."""

    @respx.mock
    async def test_dry_run_render_makes_no_http_call(self, notify_settings):
        route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        output = render_dry_run([make_job()], notify_settings)
        assert "DRY RUN" in output
        assert not route.called
        assert len(respx.calls) == 0

    def test_dry_run_shows_the_real_content(self, notify_settings):
        output = render_dry_run(
            [make_job(title="Research Assistant", relevance_score=88)], notify_settings
        )
        assert "Research Assistant" in output
        assert "88" in output

    def test_dry_run_with_no_jobs(self, notify_settings):
        assert "No jobs would be sent." in render_dry_run([], notify_settings)


class TestDelivery:
    @respx.mock
    async def test_successful_send_reports_delivered_ids(self, notify_settings):
        respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        async with DiscordNotifier(WEBHOOK, notify_settings) as notifier:
            result = await notifier.send_jobs(
                [make_job(id="a"), make_job(id="b", url="https://e.de/b")]
            )
        assert result.ok
        assert set(result.delivered_ids) == {"a", "b"}
        assert not result.failed_ids

    @respx.mock
    async def test_failed_send_reports_no_deliveries(self, notify_settings):
        """The spec's key safety property: a job must never be marked notified
        when Discord did not accept it."""
        respx.post(WEBHOOK).mock(return_value=httpx.Response(500))
        async with DiscordNotifier(WEBHOOK, notify_settings, max_retries=2) as notifier:
            result = await notifier.send_jobs([make_job(id="a")])
        assert result.delivered_ids == []
        assert result.failed_ids == ["a"]
        assert not result.ok

    @respx.mock
    async def test_permanent_4xx_is_not_retried(self, notify_settings):
        """A revoked webhook fails identically forever; retrying just stalls."""
        route = respx.post(WEBHOOK).mock(return_value=httpx.Response(404, text="Unknown Webhook"))
        async with DiscordNotifier(WEBHOOK, notify_settings, max_retries=3) as notifier:
            result = await notifier.send_jobs([make_job(id="a")])
        assert route.call_count == 1
        assert result.failed_ids == ["a"]

    @respx.mock
    async def test_server_error_is_retried_then_succeeds(self, notify_settings):
        route = respx.post(WEBHOOK).mock(side_effect=[httpx.Response(503), httpx.Response(204)])
        async with DiscordNotifier(WEBHOOK, notify_settings, max_retries=3) as notifier:
            result = await notifier.send_jobs([make_job(id="a")])
        assert route.call_count == 2
        assert result.delivered_ids == ["a"]

    @respx.mock
    async def test_rate_limit_is_honoured_then_retried(self, notify_settings):
        route = respx.post(WEBHOOK).mock(
            side_effect=[
                httpx.Response(429, json={"retry_after": 0.01}),
                httpx.Response(204),
            ]
        )
        async with DiscordNotifier(WEBHOOK, notify_settings, max_retries=3) as notifier:
            result = await notifier.send_jobs([make_job(id="a")])
        assert route.call_count == 2
        assert result.delivered_ids == ["a"]

    @respx.mock
    async def test_partial_batch_failure_marks_only_what_landed(self, notify_settings):
        """One message failing must not discard the jobs that did get through,
        nor claim the ones that did not."""
        notify_settings.embeds_per_message = 1
        route = respx.post(WEBHOOK).mock(
            side_effect=[httpx.Response(204), httpx.Response(400, text="bad")]
        )
        jobs = [make_job(id="a"), make_job(id="b", url="https://e.de/b")]
        async with DiscordNotifier(WEBHOOK, notify_settings, max_retries=1) as notifier:
            result = await notifier.send_jobs(jobs)
        assert route.call_count == 2
        assert result.delivered_ids == ["a"]
        assert result.failed_ids == ["b"]

    @respx.mock
    async def test_network_error_is_reported_not_raised(self, notify_settings):
        respx.post(WEBHOOK).mock(side_effect=httpx.ConnectError("no route to host"))
        async with DiscordNotifier(WEBHOOK, notify_settings, max_retries=2) as notifier:
            result = await notifier.send_jobs([make_job(id="a")])
        assert result.failed_ids == ["a"]
        assert result.errors

    @respx.mock
    async def test_empty_job_list_sends_nothing(self, notify_settings):
        route = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        async with DiscordNotifier(WEBHOOK, notify_settings) as notifier:
            result = await notifier.send_jobs([])
        assert not route.called
        assert result.messages_sent == 0

    @respx.mock
    async def test_send_test_success_and_failure(self, notify_settings):
        respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
        async with DiscordNotifier(WEBHOOK, notify_settings) as notifier:
            assert await notifier.send_test() is True

        respx.post(WEBHOOK).mock(return_value=httpx.Response(401))
        async with DiscordNotifier(WEBHOOK, notify_settings) as notifier:
            assert await notifier.send_test() is False


class TestEmbedContent:
    def test_embed_carries_every_required_field(self, notify_settings):
        """The spec lists exactly what a notification must contain."""
        job = make_job(
            title="Research Assistant",
            organization="TU Munich",
            location="Munich",
            source="euraxess",
            relevance_score=82,
            matched_keywords=["research assistant"],
            application_deadline=datetime.now(UTC) + timedelta(days=14),
            description="A machine learning role.",
        )
        embed = build_embed(job, notify_settings)
        rendered = str(embed)

        assert embed["title"] == "Research Assistant"
        assert embed["url"] == job.url  # the clickable "open job" link
        assert "TU Munich" in rendered
        assert "Munich" in rendered
        assert "euraxess" in rendered
        assert "82" in rendered
        assert "research assistant" in rendered
        assert "Deadline" in rendered
        assert "machine learning role" in embed["description"]

    def test_publication_date_is_shown_when_known(self, notify_settings):
        embed = build_embed(make_job(published_at=datetime.now(UTC)), notify_settings)
        assert any("Published" in f["name"] for f in embed["fields"])

    def test_discovery_date_is_shown_when_publication_is_unknown(self, notify_settings):
        embed = build_embed(make_job(published_at=None), notify_settings)
        assert any("Discovered" in f["name"] for f in embed["fields"])

    def test_colour_reflects_score(self, notify_settings):
        high = build_embed(make_job(relevance_score=90), notify_settings)["color"]
        low = build_embed(make_job(relevance_score=56), notify_settings)["color"]
        assert high != low
