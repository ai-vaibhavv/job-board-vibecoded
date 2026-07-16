"""LinkedIn posts via a vendor API.

Every HTTP call is mocked. No test touches a live API, and nothing here ever
touches linkedin.com — that is the point of the source, not an accident of the
tests.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from job_alerts.config import HttpSettings, Secrets, SourceConfig
from job_alerts.http import PoliteClient
from job_alerts.models import SearchQuery
from job_alerts.sources import build_source
from job_alerts.sources.linkedin_posts import (
    LinkedInPostsSource,
    PostsUnavailable,
    synthesize_title,
)

QUERY = SearchQuery(max_results=100)
APIFY_URL = (
    "https://api.apify.com/v2/acts/"
    "apimaestro~linkedin-posts-search-scraper-no-cookies/run-sync-get-dataset-items"
)


def apify_item(**overrides) -> dict:
    """The real shape, taken from a live run of the actor."""
    item = {
        "activity_id": "7483141707195273217",
        "post_url": (
            "https://www.linkedin.com/posts/dachpulse_hot-startup-positions"
            "-activity-7483141707195273217-lErH?utm_source=social_share_send"
        ),
        "text": "Wir suchen eine studentische Hilfskraft im Bereich Machine Learning.",
        "author": {"name": "DACHpulse", "headline": "358 followers"},
        "posted_at": {
            "display_text": "12h",
            "date": "2026-07-15 14:53:33",
            "timestamp": 1784120013045,
        },
        "stats": {"total_reactions": 22, "comments": 3},
    }
    item.update(overrides)
    return item


@pytest.fixture
def http_settings() -> HttpSettings:
    return HttpSettings(
        per_domain_delay=0.0, cache_ttl_seconds=0, max_retries=1, respect_robots=False
    )


@pytest.fixture
async def client(http_settings):
    async with PoliteClient(http_settings) as c:
        yield c


@pytest.fixture
def config() -> SourceConfig:
    return SourceConfig(
        name="linkedin_posts",
        type="linkedin_posts",
        enabled=True,
        backend="apify",
        actor="apimaestro/linkedin-posts-search-scraper-no-cookies",
        queries=['"studentische hilfskraft" machine learning'],
        max_results_per_query=25,
    )


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(_env_file=None, apify_token="apify_api_supersecret")


class TestApifyBackend:
    @respx.mock
    async def test_posts_become_candidates(self, client, config, secrets):
        respx.post(APIFY_URL).mock(return_value=httpx.Response(200, json=[apify_item()]))
        candidates = await LinkedInPostsSource(config, client, secrets).search(QUERY)

        assert len(candidates) == 1
        assert "studentische Hilfskraft" in candidates[0].title
        assert candidates[0].published_at is not None

    @respx.mock
    async def test_the_token_never_reaches_the_url(self, client, config, secrets):
        """A key in a query string lands in every proxy log. The actor's own docs
        suggest `?token=`; this project does not."""
        route = respx.post(APIFY_URL).mock(return_value=httpx.Response(200, json=[]))
        await LinkedInPostsSource(config, client, secrets).search(QUERY)

        assert "supersecret" not in str(route.calls[0].request.url)
        assert route.calls[0].request.headers["Authorization"] == "Bearer apify_api_supersecret"

    @respx.mock
    async def test_the_age_window_is_enforced_at_the_vendor(self, client, config, secrets):
        """Cheaper than paying for posts we would only drop as stale: $5/1000."""
        import json

        route = respx.post(APIFY_URL).mock(return_value=httpx.Response(200, json=[]))
        await LinkedInPostsSource(config, client, secrets).search(QUERY)

        body = json.loads(route.calls[0].request.content)
        assert body["date_filter"] == "past-week"
        assert body["sort_type"] == "date_posted"
        assert body["limit"] == 25

    @respx.mock
    async def test_the_post_url_collapses_to_author_and_activity(self, client, config, secrets):
        """Tracking params and the per-share hash are not part of which post this
        is. Keep them and the same post arrives new every run, forever."""
        from job_alerts.normalization import normalize_candidate

        respx.post(APIFY_URL).mock(return_value=httpx.Response(200, json=[apify_item()]))
        candidates = await LinkedInPostsSource(config, client, secrets).search(QUERY)
        job = normalize_candidate(candidates[0])

        assert job.url == "https://linkedin.com/posts/dachpulse-activity-7483141707195273217"

    @respx.mock
    async def test_without_a_token_the_source_skips_rather_than_fails(self, client, config):
        no_token = Secrets(_env_file=None, apify_token="")
        source = LinkedInPostsSource(config, client, no_token)

        assert source.available is False
        result = await source.run(QUERY)
        assert not result.ok
        assert PostsUnavailable.__name__ in (result.error or "")

    @respx.mock
    async def test_a_broken_actor_does_not_take_the_run_down(self, client, config, secrets):
        """A community-run actor scraping a company that fights scrapers WILL
        break. The other sources must not care."""
        respx.post(APIFY_URL).mock(return_value=httpx.Response(500, text="actor failed"))
        result = await LinkedInPostsSource(config, client, secrets).run(QUERY)
        assert not result.ok  # reported, not raised

    @respx.mock
    async def test_an_empty_dataset_is_not_an_error(self, client, config, secrets):
        """Two results for a real German query was a normal day."""
        respx.post(APIFY_URL).mock(return_value=httpx.Response(200, json=[]))
        assert await LinkedInPostsSource(config, client, secrets).search(QUERY) == []

    @respx.mock
    async def test_a_post_with_no_text_is_skipped(self, client, config, secrets):
        respx.post(APIFY_URL).mock(
            return_value=httpx.Response(200, json=[apify_item(text=""), apify_item()])
        )
        assert len(await LinkedInPostsSource(config, client, secrets).search(QUERY)) == 1

    def test_the_source_type_is_registered(self, client, secrets, config):
        assert isinstance(build_source(config, client, secrets), LinkedInPostsSource)


class TestContactExtraction:
    """A post that names a way to apply is the point of this source — and the
    route is itself the evidence that the post is real."""

    @respx.mock
    async def test_an_email_in_the_post_is_extracted(self, client, config, secrets):
        respx.post(APIFY_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    apify_item(
                        text=(
                            "Wir suchen eine studentische Hilfskraft ML. "
                            "Meldet euch per Mail an hiwi@cs.uni-example.de"
                        )
                    )
                ],
            )
        )
        candidates = await LinkedInPostsSource(config, client, secrets).search(QUERY)
        assert candidates[0].contact_email == "hiwi@cs.uni-example.de"

    @respx.mock
    async def test_a_google_form_is_extracted(self, client, config, secrets):
        respx.post(APIFY_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    apify_item(
                        text=(
                            "Werkstudent gesucht! Apply here: "
                            "https://docs.google.com/forms/d/e/1FAIpQ/viewform"
                        )
                    )
                ],
            )
        )
        candidates = await LinkedInPostsSource(config, client, secrets).search(QUERY)
        assert "docs.google.com/forms" in candidates[0].contact_url

    @respx.mock
    async def test_an_outbound_link_becomes_the_apply_route(self, client, config, secrets):
        respx.post(APIFY_URL).mock(
            return_value=httpx.Response(
                200,
                json=[apify_item(text="HiWi gesucht, Details hier: https://lnkd.in/du8-2hqx")],
            )
        )
        candidates = await LinkedInPostsSource(config, client, secrets).search(QUERY)
        # lnkd.in is a shortener: a request to it answers with a redirect, and
        # the page we end up reading is a university's, never LinkedIn's.
        assert candidates[0].contact_url == "https://lnkd.in/du8-2hqx"

    @respx.mock
    async def test_a_link_back_to_linkedin_is_not_an_apply_route(self, client, config, secrets):
        """Following it would mean reading LinkedIn, which we do not do — and
        the denylist would refuse it anyway."""
        respx.post(APIFY_URL).mock(
            return_value=httpx.Response(
                200,
                json=[
                    apify_item(
                        text="HiWi gesucht. Mein Profil: https://www.linkedin.com/in/someone"
                    )
                ],
            )
        )
        candidates = await LinkedInPostsSource(config, client, secrets).search(QUERY)
        assert candidates[0].contact_url is None

    @respx.mock
    async def test_the_poster_is_not_assumed_to_be_the_employer(self, client, config, secrets):
        """ "DACHpulse" is a newsletter account. Putting it in Organization would
        attribute somebody else's job to it."""
        respx.post(APIFY_URL).mock(return_value=httpx.Response(200, json=[apify_item()]))
        candidates = await LinkedInPostsSource(config, client, secrets).search(QUERY)

        assert candidates[0].organization is None
        # Still visible to the LLM as context rather than as a claim.
        assert "DACHpulse" in candidates[0].description


class TestTitleSynthesis:
    """A post has no title; `JobCandidate` requires one."""

    def test_the_first_real_line_becomes_the_title(self):
        assert synthesize_title("Wir suchen eine HiWi\n\nDetails folgen.") == "Wir suchen eine HiWi"

    def test_an_emoji_opener_is_skipped(self):
        """LinkedIn posts open with "🚀🚀🚀" constantly."""
        assert synthesize_title("🚀🚀🚀\n\nWir suchen eine studentische Hilfskraft") == (
            "Wir suchen eine studentische Hilfskraft"
        )

    def test_a_long_first_line_is_trimmed_on_a_word(self):
        title = synthesize_title("Wir suchen ab sofort " + "sehr " * 40 + "dringend jemanden")
        assert len(title) <= 111
        assert title.endswith("…")

    def test_a_post_with_no_usable_line_yields_nothing(self):
        assert synthesize_title("🚀") == ""
        assert synthesize_title("") == ""
