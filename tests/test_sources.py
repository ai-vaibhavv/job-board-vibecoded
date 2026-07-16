"""Source adapters: parsing, and failure isolation.

Every HTTP call is mocked. No test touches a live website.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from job_alerts.config import HttpSettings, Secrets, SourceConfig
from job_alerts.http import PoliteClient
from job_alerts.models import SearchQuery
from job_alerts.sources import build_source, build_sources
from job_alerts.sources.generic_html import GenericHtmlSource, SelectorError
from job_alerts.sources.mock import MockSource
from job_alerts.sources.rss import RssSource
from job_alerts.sources.search_api import (
    SearchApiSource,
    _clean_title,
    _guess_location,
    _guess_organization,
    host_allowed,
    looks_like_listing_page,
)

QUERY = SearchQuery(max_results=100)


@pytest.fixture
def http_settings() -> HttpSettings:
    return HttpSettings(
        per_domain_delay=0.0, cache_ttl_seconds=0, max_retries=2, respect_robots=False
    )


@pytest.fixture
async def client(http_settings):
    async with PoliteClient(http_settings) as c:
        yield c


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>University Jobs</title>
    <item>
      <title>Studentische Hilfskraft (m/w/d) - Machine Learning</title>
      <link>https://uni-beispiel.de/jobs/123?utm_source=rss</link>
      <description>&lt;p&gt;Wir suchen eine &lt;b&gt;studentische Hilfskraft&lt;/b&gt;.&lt;/p&gt;</description>
      <pubDate>Sun, 15 Mar 2026 10:30:00 +0100</pubDate>
      <guid>job-123</guid>
      <dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">TU Beispiel</dc:creator>
    </item>
    <item>
      <title>Research Assistant - Computer Vision</title>
      <link>https://uni-beispiel.de/jobs/124</link>
      <description>A research assistant position.</description>
      <pubDate>Mon, 16 Mar 2026 09:00:00 +0100</pubDate>
      <guid>job-124</guid>
    </item>
  </channel>
</rss>
"""

ATOM_FIXTURE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Institute Jobs</title>
  <entry>
    <title>HiWi Position - Robotics</title>
    <link rel="alternate" href="https://institut.de/jobs/1"/>
    <link rel="edit" href="https://institut.de/api/jobs/1"/>
    <id>urn:job:1</id>
    <summary>A HiWi position in robotics.</summary>
    <published>2026-03-15T10:30:00Z</published>
  </entry>
</feed>
"""


class TestRssSource:
    async def test_parses_rss_items(self, client):
        source = RssSource(SourceConfig(name="test_rss", type="rss", url="https://x.de/f"), client)
        candidates = source.parse(RSS_FIXTURE, base_url="https://x.de/f")

        assert len(candidates) == 2
        first = candidates[0]
        assert first.title == "Studentische Hilfskraft (m/w/d) - Machine Learning"
        assert first.url == "https://uni-beispiel.de/jobs/123?utm_source=rss"
        assert first.source_job_id == "job-123"
        assert first.organization == "TU Beispiel"
        assert "studentische Hilfskraft" in first.description

    async def test_rss_pubdate_is_parsed(self, client):
        """RSS 2.0 spells it `<pubDate>`, and XML is case-sensitive — so the
        lowercase tag list here silently matched nothing and every RSS feed
        arrived undated. It stayed hidden because an undated job is treated as
        recent enough to keep, and because this fixture carried a `<pubDate>`
        that no test ever looked at."""
        source = RssSource(SourceConfig(name="test_rss", type="rss", url="https://x.de/f"), client)
        candidates = source.parse(RSS_FIXTURE, base_url="https://x.de/f")

        assert candidates[0].published_at == "Sun, 15 Mar 2026 10:30:00 +0100"
        assert candidates[1].published_at == "Mon, 16 Mar 2026 09:00:00 +0100"

    async def test_atom_published_is_parsed(self, client):
        source = RssSource(SourceConfig(name="atom", type="rss", url="https://x.de/f"), client)
        assert source.parse(ATOM_FIXTURE)[0].published_at == "2026-03-15T10:30:00Z"

    @pytest.mark.parametrize("tag", ["pubDate", "pubdate", "PUBDATE"])
    async def test_date_tags_match_whatever_case_the_feed_uses(self, client, tag):
        feed = f"""<?xml version="1.0"?><rss><channel><item>
            <title>T</title><link>https://x.de/1</link>
            <{tag}>Sun, 15 Mar 2026 10:30:00 +0100</{tag}>
        </item></channel></rss>"""
        source = RssSource(SourceConfig(name="r", type="rss", url="https://x.de/f"), client)
        assert source.parse(feed)[0].published_at == "Sun, 15 Mar 2026 10:30:00 +0100"

    async def test_an_author_email_is_not_an_organization(self, client):
        """RSS 2.0 defines `<author>` as an email address. TU München's feed
        sends `<author>pia.lorenz@tum.de</author>` alongside
        `<dc:publisher>Technische Universität München</dc:publisher>` — the
        publisher is the employer; the inbox is not."""
        feed = """<?xml version="1.0"?>
        <rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item>
            <title>Studentische Hilfskraft</title>
            <link>https://portal.mytum.de/1</link>
            <dc:publisher>Technische Universität München</dc:publisher>
            <author>pia.lorenz@tum.de</author>
            <dc:creator>Pia Lorenz</dc:creator>
        </item></channel></rss>"""
        source = RssSource(SourceConfig(name="r", type="rss", url="https://x.de/f"), client)
        assert source.parse(feed)[0].organization == "Technische Universität München"

    async def test_a_bare_email_never_becomes_the_organization(self, client):
        """With no publisher and no creator, an email is still not an employer —
        leave it blank so the source's `defaults:` can fill it."""
        feed = """<?xml version="1.0"?><rss><channel><item>
            <title>T</title><link>https://x.de/1</link>
            <author>someone@example.de</author>
        </item></channel></rss>"""
        source = RssSource(SourceConfig(name="r", type="rss", url="https://x.de/f"), client)
        assert source.parse(feed)[0].organization is None

    async def test_a_named_author_is_still_used(self, client):
        """Plenty of feeds misuse `<author>` for a name. Take it if it is one."""
        feed = """<?xml version="1.0"?><rss><channel><item>
            <title>T</title><link>https://x.de/1</link>
            <author>Institute of Robotics</author>
        </item></channel></rss>"""
        source = RssSource(SourceConfig(name="r", type="rss", url="https://x.de/f"), client)
        assert source.parse(feed)[0].organization == "Institute of Robotics"

    async def test_parses_atom_entries_and_picks_the_alternate_link(self, client):
        source = RssSource(SourceConfig(name="atom", type="rss", url="https://x.de/f"), client)
        candidates = source.parse(ATOM_FIXTURE, base_url="https://x.de/f")

        assert len(candidates) == 1
        # Not the rel="edit" API link.
        assert candidates[0].url == "https://institut.de/jobs/1"
        assert candidates[0].source_job_id == "urn:job:1"

    async def test_entry_without_title_or_link_is_skipped(self, client):
        feed = """<?xml version="1.0"?><rss><channel>
            <item><description>No title, no link</description></item>
            <item><title>Good</title><link>https://x.de/1</link></item>
        </channel></rss>"""
        source = RssSource(SourceConfig(name="r", type="rss", url="https://x.de/f"), client)
        assert len(source.parse(feed)) == 1

    async def test_empty_feed_returns_nothing(self, client):
        source = RssSource(SourceConfig(name="r", type="rss", url="https://x.de/f"), client)
        assert source.parse('<?xml version="1.0"?><rss><channel></channel></rss>') == []

    @respx.mock
    async def test_fetches_over_http(self, client):
        respx.get("https://x.de/feed").mock(return_value=httpx.Response(200, text=RSS_FIXTURE))
        source = RssSource(SourceConfig(name="r", type="rss", url="https://x.de/feed"), client)
        assert len(await source.search(QUERY)) == 2

    async def test_missing_url_is_an_error(self, client):
        source = RssSource(SourceConfig(name="r", type="rss"), client)
        with pytest.raises(ValueError, match="no url"):
            await source.search(QUERY)


HTML_FIXTURE = """
<html><body>
  <div class="job-listing">
    <h3 class="job-title">Werkstudent Forschung (m/w/d)</h3>
    <a class="job-link" href="/jobs/werkstudent-1">Details</a>
    <span class="location">München</span>
    <span class="dept">Institut für Informatik</span>
    <time datetime="2026-03-15">15.03.2026</time>
  </div>
  <div class="job-listing">
    <h3 class="job-title">Research Intern - NLP</h3>
    <a class="job-link" href="https://other.de/jobs/2">Details</a>
    <span class="location">Berlin</span>
  </div>
  <div class="job-listing">
    <h3 class="job-title">Broken entry with no link</h3>
  </div>
</body></html>
"""


class TestGenericHtmlSource:
    @pytest.fixture
    def html_config(self) -> SourceConfig:
        return SourceConfig(
            name="uni",
            type="html",
            url="https://uni.de/jobs",
            selectors={
                "item": "div.job-listing",
                "title": "h3.job-title",
                "url": "a.job-link@href",
                "location": "span.location",
                "organization": "span.dept",
                "published_at": "time@datetime",
            },
        )

    async def test_parses_items_with_configured_selectors(self, client, html_config):
        candidates = GenericHtmlSource(html_config, client).parse(
            HTML_FIXTURE, base_url="https://uni.de/jobs"
        )
        assert len(candidates) == 2  # the broken third entry is skipped

        first = candidates[0]
        assert first.title == "Werkstudent Forschung (m/w/d)"
        assert first.location == "München"
        assert first.organization == "Institut für Informatik"
        assert first.published_at == "2026-03-15"

    async def test_relative_urls_are_resolved_against_the_page(self, client, html_config):
        candidates = GenericHtmlSource(html_config, client).parse(
            HTML_FIXTURE, base_url="https://uni.de/jobs"
        )
        assert candidates[0].url == "https://uni.de/jobs/werkstudent-1"

    async def test_absolute_urls_are_left_alone(self, client, html_config):
        candidates = GenericHtmlSource(html_config, client).parse(
            HTML_FIXTURE, base_url="https://uni.de/jobs"
        )
        assert candidates[1].url == "https://other.de/jobs/2"

    async def test_stale_selector_returns_nothing_rather_than_raising(self, client, html_config):
        """A site redesign must not crash the run — but it must be loud."""
        html_config.selectors["item"] = "div.no-such-class"
        assert GenericHtmlSource(html_config, client).parse(HTML_FIXTURE) == []

    async def test_missing_required_selector_is_rejected(self, client):
        config = SourceConfig(
            name="bad", type="html", url="https://x.de", selectors={"item": "div"}
        )
        with pytest.raises(SelectorError, match="missing required selector"):
            await GenericHtmlSource(config, client).search(QUERY)

    async def test_missing_url_is_rejected(self, client, html_config):
        html_config.url = None
        with pytest.raises(SelectorError, match="no url"):
            await GenericHtmlSource(html_config, client).search(QUERY)


class TestSearchApiSource:
    """The compliant LinkedIn route. Note what these tests do NOT do: none of
    them contacts linkedin.com, because the adapter never does."""

    @pytest.fixture
    def search_config(self) -> SourceConfig:
        return SourceConfig(
            name="search",
            type="search_api",
            enabled=True,
            queries=['site:linkedin.com/jobs/view "research assistant" Germany'],
            max_results_per_query=10,
        )

    @respx.mock
    async def test_tavily_provider_returns_candidates(self, client, search_config):
        """Tavily is the no-credit-card default. It is a POST with a JSON body
        and calls its snippet field `content`, unlike every other provider."""
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Research Assistant at TU Berlin | LinkedIn",
                            "url": "https://www.linkedin.com/jobs/view/4012345678",
                            "content": "Research assistant role in Berlin.",
                            "score": 0.94,
                        }
                    ]
                },
            )
        )
        secrets = Secrets(_env_file=None, search_api_provider="tavily", search_api_key="tvly-k")
        candidates = await SearchApiSource(search_config, client, secrets).search(QUERY)

        assert len(candidates) == 1
        assert candidates[0].title == "Research Assistant at TU Berlin"
        assert candidates[0].description == "Research assistant role in Berlin."
        assert "linkedin.com/jobs/view/4012345678" in candidates[0].url

        request = route.calls[0].request
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer tvly-k"

    @respx.mock
    async def test_tavily_key_never_appears_in_the_url(self, client, search_config):
        """Bearer auth, not a query param — a key in the URL leaks into logs
        and proxy access logs."""
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        secrets = Secrets(
            _env_file=None, search_api_provider="tavily", search_api_key="tvly-supersecret"
        )
        await SearchApiSource(search_config, client, secrets).search(QUERY)
        assert "tvly-supersecret" not in str(route.calls[0].request.url)

    @respx.mock
    async def test_tavily_empty_results_are_not_an_error(self, client, search_config):
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        secrets = Secrets(_env_file=None, search_api_provider="tavily", search_api_key="k")
        assert await SearchApiSource(search_config, client, secrets).search(QUERY) == []

    @respx.mock
    async def test_tavily_bad_key_surfaces_clearly(self, client, search_config):
        """A 401 is permanent — it must fail fast, not retry."""
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(401, json={"detail": "Invalid API key"})
        )
        secrets = Secrets(_env_file=None, search_api_provider="tavily", search_api_key="bad")
        result = await SearchApiSource(search_config, client, secrets).run(QUERY)
        assert not result.ok
        assert route.call_count == 1

    @respx.mock
    async def test_brave_provider_returns_candidates(self, client, search_config):
        respx.get("https://api.search.brave.com/res/v1/web/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "Research Assistant at TU Berlin | LinkedIn",
                                "url": "https://www.linkedin.com/jobs/view/4012345678",
                                "description": "Research assistant role in Berlin.",
                            }
                        ]
                    }
                },
            )
        )
        secrets = Secrets(_env_file=None, search_api_provider="brave", search_api_key="k")
        candidates = await SearchApiSource(search_config, client, secrets).search(QUERY)

        assert len(candidates) == 1
        assert candidates[0].title == "Research Assistant at TU Berlin"
        assert "linkedin.com/jobs/view/4012345678" in candidates[0].url

    @respx.mock
    async def test_google_cse_provider(self, client, search_config):
        respx.get("https://www.googleapis.com/customsearch/v1").mock(
            side_effect=[
                httpx.Response(
                    200, json={"items": [{"title": "RA", "link": "https://x.de/1", "snippet": "s"}]}
                ),
                httpx.Response(200, json={}),
            ]
        )
        secrets = Secrets(
            _env_file=None, search_api_provider="google_cse", search_api_key="k", google_cse_id="cx"
        )
        assert len(await SearchApiSource(search_config, client, secrets).search(QUERY)) == 1

    @respx.mock
    async def test_serpapi_provider(self, client, search_config):
        respx.get("https://serpapi.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "organic_results": [{"title": "RA", "link": "https://x.de/1", "snippet": "s"}]
                },
            )
        )
        secrets = Secrets(_env_file=None, search_api_provider="serpapi", search_api_key="k")
        assert len(await SearchApiSource(search_config, client, secrets).search(QUERY)) == 1

    @respx.mock
    async def test_bing_provider(self, client, search_config):
        respx.get("https://api.bing.microsoft.com/v7.0/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "webPages": {"value": [{"name": "RA", "url": "https://x.de/1", "snippet": "s"}]}
                },
            )
        )
        secrets = Secrets(_env_file=None, search_api_provider="bing", search_api_key="k")
        assert len(await SearchApiSource(search_config, client, secrets).search(QUERY)) == 1

    async def test_without_a_key_the_source_reports_unavailable_not_broken(
        self, client, search_config, secrets
    ):
        """Running with no search key is a supported mode, so this surfaces as
        *skipped* rather than as a failure."""
        result = await SearchApiSource(search_config, client, secrets).run(QUERY)
        assert result.error is not None
        assert "SearchUnavailable" in result.error
        assert not SearchApiSource(search_config, client, secrets).available

    @respx.mock
    async def test_duplicate_urls_across_queries_are_collapsed(self, client, search_config):
        search_config.queries = ["query one", "query two"]
        respx.get("https://api.search.brave.com/res/v1/web/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "web": {
                        "results": [{"title": "RA", "url": "https://x.de/1", "description": "d"}]
                    }
                },
            )
        )
        secrets = Secrets(_env_file=None, search_api_provider="brave", search_api_key="k")
        assert len(await SearchApiSource(search_config, client, secrets).search(QUERY)) == 1

    @respx.mock
    async def test_one_failing_query_does_not_lose_the_others(self, client, search_config):
        search_config.queries = ["bad", "good"]
        respx.get("https://api.search.brave.com/res/v1/web/search").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(500),  # retried
                httpx.Response(
                    200, json={"web": {"results": [{"title": "RA", "url": "https://x.de/1"}]}}
                ),
            ]
        )
        secrets = Secrets(_env_file=None, search_api_provider="brave", search_api_key="k")
        assert len(await SearchApiSource(search_config, client, secrets).search(QUERY)) == 1

    @respx.mock
    async def test_search_apis_are_not_gated_by_robots_txt(self, http_settings, search_config):
        """robots.txt governs crawlers, not an authorized API client using its
        own key. If the provider's robots.txt could veto the call, a hostile or
        merely careless crawler policy would silently disable a paid, fully
        authorized integration. No robots.txt route is mocked here, so the test
        fails if the client tries to fetch one."""
        http_settings.respect_robots = True
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(
                200, json={"results": [{"title": "RA", "url": "https://x.de/1", "content": "s"}]}
            )
        )
        secrets = Secrets(_env_file=None, search_api_provider="tavily", search_api_key="k")
        async with PoliteClient(http_settings) as robots_client:
            source = SearchApiSource(search_config, robots_client, secrets)
            assert len(await source.search(QUERY)) == 1

    def test_unknown_provider_is_rejected(self, client):
        from job_alerts.sources.search_api import SearchProviderError, build_provider

        secrets = Secrets(_env_file=None, search_api_key="k")
        # Bypass the Literal validator to simulate a bad env value reaching us.
        object.__setattr__(secrets, "search_api_provider", "altavista")
        with pytest.raises(SearchProviderError, match="unknown SEARCH_API_PROVIDER"):
            build_provider(secrets, client)

    def test_google_cse_without_an_engine_id_is_not_ready(self):
        secrets = Secrets(_env_file=None, search_api_provider="google_cse", search_api_key="k")
        assert secrets.has_search_api is False


class TestDomainEnforcement:
    """The provider's domain filter is advisory. Ours is not.

    Measured against Tavily during design: `site:linkedin.com/jobs/view ...
    Germany` returned four `ng.linkedin.com` (Nigeria) and one `lk.linkedin.com`
    (Sri Lanka) posting, and `include_domains: ["fraunhofer.de"]` returned zero
    fraunhofer results and a page of dictionary entries. Those results are how a
    German job board came to hold "Research Intern at Simple List NG". A provider
    that cannot satisfy a domain constraint answers semantically instead of
    returning nothing, and nothing downstream can tell the difference — so the
    domain is enforced against the URLs that actually came back.
    """

    @pytest.mark.parametrize(
        "url,allowed,expected",
        [
            # The bug: `linkedin.com` legitimately admits every country subdomain.
            ("https://ng.linkedin.com/jobs/view/1", ["linkedin.com"], True),
            # The fix: name the host you actually want.
            ("https://ng.linkedin.com/jobs/view/1", ["de.linkedin.com"], False),
            ("https://lk.linkedin.com/jobs/view/1", ["de.linkedin.com"], False),
            ("https://de.linkedin.com/jobs/view/1", ["de.linkedin.com"], True),
            ("https://www.de.linkedin.com/jobs/view/1", ["de.linkedin.com"], True),
            # A parent domain still admits its own subdomains, which is wanted.
            ("https://jobs.fraunhofer.de/x", ["fraunhofer.de"], True),
            ("https://www.fraunhofer.de/x", ["fraunhofer.de"], True),
            # Off-domain junk the provider substituted when it gave up.
            ("https://m.dict.cc/englisch-deutsch/student.html", ["fraunhofer.de"], False),
            ("https://www.azjobconnection.gov/search/jobs", ["fraunhofer.de"], False),
            # A suffix match must not be a substring match.
            ("https://notfraunhofer.de/x", ["fraunhofer.de"], False),
            ("https://fraunhofer.de.evil.com/x", ["fraunhofer.de"], False),
            # No constraint declared means no filtering.
            ("https://anything.example/x", [], True),
            ("", ["fraunhofer.de"], False),
        ],
    )
    def test_host_allowed(self, url, allowed, expected):
        assert host_allowed(url, allowed) is expected

    @respx.mock
    async def test_off_domain_results_never_become_candidates(self, client):
        """The Nigeria regression test, built from real returned URLs."""
        config = SourceConfig(
            name="search",
            type="search_api",
            enabled=True,
            queries=['"research assistant"'],
            allowed_domains=["de.linkedin.com"],
        )
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Research Intern at Simple List NG",
                            "url": "https://ng.linkedin.com/jobs/view/4415343807",
                            "content": "Research intern role.",
                        },
                        {
                            "title": "Research Assistant Intern at Lake House",
                            "url": "https://lk.linkedin.com/jobs/view/3869494124",
                            "content": "Research assistant.",
                        },
                        {
                            "title": "student - dict.cc",
                            "url": "https://m.dict.cc/englisch-deutsch/student.html",
                            "content": "Dictionary entry.",
                        },
                        {
                            "title": "Research Scientist Intern at Prior Labs",
                            "url": "https://de.linkedin.com/jobs/view/4407422864",
                            "content": "Research role in Germany.",
                        },
                    ]
                },
            )
        )
        secrets = Secrets(_env_file=None, search_api_provider="tavily", search_api_key="k")
        candidates = await SearchApiSource(config, client, secrets).search(QUERY)

        assert [c.url for c in candidates] == ["https://de.linkedin.com/jobs/view/4407422864"]

    @respx.mock
    async def test_denied_url_patterns_drop_profile_pages(self, client):
        """`de.linkedin.com/in/<person>` is on the right host and is not a job."""
        config = SourceConfig(
            name="search",
            type="search_api",
            enabled=True,
            queries=['"research assistant"'],
            allowed_domains=["de.linkedin.com"],
            denied_url_patterns=[r"linkedin\.com/(in|company)/"],
        )
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Likhith Anand",
                            "url": "https://de.linkedin.com/in/likhith-anand-ba8007253",
                            "content": "Profile.",
                        },
                        {
                            "title": "Research Scientist Intern at Prior Labs",
                            "url": "https://de.linkedin.com/jobs/view/4407422864",
                            "content": "Research role.",
                        },
                    ]
                },
            )
        )
        secrets = Secrets(_env_file=None, search_api_provider="tavily", search_api_key="k")
        candidates = await SearchApiSource(config, client, secrets).search(QUERY)

        assert [c.url for c in candidates] == ["https://de.linkedin.com/jobs/view/4407422864"]

    @respx.mock
    async def test_allowed_domains_and_country_are_sent_to_tavily(self, client):
        """Advisory, but free — send them, then verify the results anyway."""
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        config = SourceConfig(
            name="search",
            type="search_api",
            enabled=True,
            queries=["research assistant"],
            allowed_domains=["de.linkedin.com"],
            search_country="germany",
        )
        secrets = Secrets(_env_file=None, search_api_provider="tavily", search_api_key="k")
        await SearchApiSource(config, client, secrets).search(QUERY)

        body = json.loads(route.calls[0].request.content)
        assert body["include_domains"] == ["de.linkedin.com"]
        assert body["country"] == "germany"
        # `country` is only honoured for topic=general.
        assert body["topic"] == "general"


class TestSearchResultParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Research Assistant at TU Berlin | LinkedIn", "Research Assistant at TU Berlin"),
            ("HiWi Position - Indeed", "HiWi Position"),
            ("TU Munich hiring Research Assistant in Munich", "Research Assistant"),
        ],
    )
    def test_clean_title_strips_site_noise(self, raw, expected):
        assert _clean_title(raw) == expected

    def test_guess_organization_from_at_pattern(self):
        assert _guess_organization("Research Assistant at TU Berlin | LinkedIn") == "TU Berlin"

    def test_guess_organization_from_hiring_pattern(self):
        assert _guess_organization("DFKI hiring Research Intern") == "DFKI"

    def test_a_city_is_not_mistaken_for_an_employer(self):
        assert _guess_organization("Research Assistant at Berlin") is None

    def test_guess_organization_returns_none_when_unclear(self):
        # A guessed employer is worse than a missing one.
        assert _guess_organization("Some random title") is None

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Research Assistant in Munich", "Munich"),
            ("HiWi Stelle in München", "München"),
            ("Remote role in Germany", "Germany"),
            ("No location here", None),
        ],
    )
    def test_guess_location(self, text, expected):
        assert _guess_location(text) == expected


class TestListingPageFiltering:
    """Search engines return "pages that list jobs" alongside actual postings.
    You cannot apply to a search box, and an index page's URL is stable, so it
    would reappear in every run forever."""

    @pytest.mark.parametrize(
        ("title", "url"),
        [
            # The exact result seen live from Tavily.
            (
                "Studentische Hilfskraft Machine Learning Jobs",
                "https://de.indeed.com/q-studentische-hilfskraft-machine-learning-jobs.html",
            ),
            ("Research Assistant Jobs in Berlin", "https://example.de/x"),
            ("HiWi Stellenangebote", "https://example.de/x"),
            # Seen live: Groq scored this 90/100 as though it were a real
            # posting. A page that counts its jobs is not one job.
            ("Mehr als 100 Machine Learning-Jobs (Studierende)", "https://example.de/x"),
            ("Über 50 HiWi Stellen", "https://example.de/x"),
            ("Machine Learning-Jobs (Studierende)", "https://example.de/x"),
            ("120 Werkstudent Jobs in München", "https://example.de/x"),
            ("Anything", "https://example.de/jobs/search?q=hiwi"),
            ("Anything", "https://example.de/jobsuche/"),
            ("Anything", "https://example.de/stellenangebote.html"),
            # Seen live: scored 95 and reached a real Discord alert. A page that
            # invites you to come and discuss a thesis is not a position.
            (
                "AIML Lab - Thesis Opportunities",
                "https://ml.informatik.tu-darmstadt.de/thesis/proposal/index.html",
            ),
            ("Research Opportunities", "https://example.de/x"),
            ("Current Openings", "https://example.de/x"),
            ("PhD Openings in Machine Learning", "https://example.de/x"),
        ],
    )
    def test_listing_pages_are_dropped(self, title, url):
        assert looks_like_listing_page(title, url) is True

    @pytest.mark.parametrize(
        ("title", "url"),
        [
            # Real postings seen live — these must survive.
            (
                "STUDENTISCHE HILFSKRAFT im Bereich Softwareentwicklung",
                "https://jobs.fraunhofer.de/job/Wachtberg-STUDENTISCHE-HILFSKRAFT-53343/1414773833",
            ),
            (
                "Research Internship (Fall, 2026) - Cohere",
                "https://linkedin.com/jobs/view/4407952980",
            ),
            (
                "Studentische/Wissenschaftliche Hilfskraft gesucht!",
                "https://informatik.uni-wuerzburg.de/sia/aktuelles/single/news/studentische-wiss",
            ),
            ("Research Assistant - Machine Learning", "https://uni.de/jobs/1234"),
            # Real postings seen live that the count/parenthetical patterns must
            # NOT catch — gender suffixes are on nearly every German posting.
            ("Studentische Hilfskraft (m/w/d): Energietechnik", "https://uni.de/jobs/9"),
            ("HiWi / student assistants - Max Planck Institute", "https://mpi.de/jobs/3"),
            ("Student Assistant for the VisPer Project", "https://uni.de/jobs/4"),
            # Real, and the reason "positions" is not a listing plural: one
            # applyable Cyber Valley page offering several seats. Add "positions"
            # to the plurals and the `<plural> in <place>` branch eats this.
            (
                "Student Assistant (HiWi)/Internship Positions in Vision-Based Autonomous Systems",
                "https://cyber-valley.de/de/jobs/student-assistant-hiwi-internship-positions",
            ),
            # Singular: one thesis on offer, not a catalogue of them.
            ("Master Thesis Opportunity in NLP", "https://uni.de/jobs/7"),
        ],
    )
    def test_real_postings_are_kept(self, title, url):
        # A false positive silently hides a real job — worse than one index page.
        assert looks_like_listing_page(title, url) is False

    @respx.mock
    async def test_listing_pages_never_become_candidates(self, client):
        config = SourceConfig(name="search", type="search_api", enabled=True, queries=["q"])
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Studentische Hilfskraft Machine Learning Jobs",
                            "url": "https://de.indeed.com/q-studentische-hilfskraft-machine-learning-jobs.html",
                            "content": "listing page",
                        },
                        {
                            "title": "Research Assistant - Machine Learning",
                            "url": "https://uni.de/jobs/1234",
                            "content": "a real posting",
                        },
                    ]
                },
            )
        )
        secrets = Secrets(_env_file=None, search_api_provider="tavily", search_api_key="k")
        candidates = await SearchApiSource(config, client, secrets).search(QUERY)

        assert [c.url for c in candidates] == ["https://uni.de/jobs/1234"]


class TestMockSource:
    async def test_returns_demo_candidates_without_network(self, client):
        source = MockSource(SourceConfig(name="mock", type="mock", enabled=True), client)
        candidates = await source.search(QUERY)
        assert len(candidates) == 8
        assert all(c.source == "mock" for c in candidates)


class TestFailureIsolation:
    """The spec's requirement: one failed source must not stop the others."""

    @respx.mock
    async def test_a_failing_source_returns_an_error_result_not_an_exception(self, client):
        respx.get("https://broken.de/feed").mock(return_value=httpx.Response(500))
        source = RssSource(
            SourceConfig(name="broken", type="rss", url="https://broken.de/feed"), client
        )
        result = await source.run(QUERY)

        assert not result.ok
        assert result.error is not None
        assert result.candidates == []

    @respx.mock
    async def test_other_sources_still_produce_results(self, client):
        """A dead source and a healthy source in the same run."""
        import asyncio

        respx.get("https://broken.de/feed").mock(side_effect=httpx.ConnectError("down"))
        respx.get("https://good.de/feed").mock(return_value=httpx.Response(200, text=RSS_FIXTURE))

        broken = RssSource(
            SourceConfig(name="broken", type="rss", url="https://broken.de/feed"), client
        )
        good = RssSource(SourceConfig(name="good", type="rss", url="https://good.de/feed"), client)
        healthy_mock = MockSource(SourceConfig(name="mock", type="mock"), client)

        results = await asyncio.gather(*(s.run(QUERY) for s in (broken, good, healthy_mock)))

        assert not results[0].ok
        assert results[1].ok and len(results[1].candidates) == 2
        assert results[2].ok and len(results[2].candidates) == 8

    async def test_a_source_raising_an_unexpected_error_is_contained(self, client):
        class ExplodingSource(MockSource):
            async def search(self, query):
                raise RuntimeError("kaboom")

        result = await ExplodingSource(SourceConfig(name="boom", type="mock"), client).run(QUERY)
        assert not result.ok
        assert "kaboom" in result.error

    async def test_forbidden_sources_are_skipped_never_fetched(self, client):
        """academics.de's terms disallow automation. `forbidden` must hard-block
        the fetch even if someone flips `enabled: true`."""
        config = SourceConfig(
            name="academics_de",
            type="html",
            enabled=True,
            forbidden=True,
            url="https://academics.de",
        )
        result = await GenericHtmlSource(config, client).run(QUERY)

        assert result.skipped_reason is not None
        assert "terms disallow" in result.skipped_reason
        assert result.error is None

    def test_build_sources_excludes_forbidden_entries(self, client, secrets):
        configs = [
            SourceConfig(name="ok", type="mock", enabled=True),
            SourceConfig(
                name="forbidden", type="html", enabled=True, forbidden=True, url="https://x.de"
            ),
        ]
        built = build_sources(configs, client, secrets)
        assert [s.name for s in built] == ["ok"]

    def test_a_source_that_cannot_be_built_does_not_abort_the_rest(
        self, client, secrets, monkeypatch
    ):
        def explode(config, client, secrets):
            if config.name == "bad":
                raise ValueError("bad config")
            return MockSource(config, client)

        monkeypatch.setattr("job_alerts.sources.build_source", explode)
        built = build_sources(
            [
                SourceConfig(name="bad", type="mock", enabled=True),
                SourceConfig(name="good", type="mock", enabled=True),
            ],
            client,
            secrets,
        )
        assert [s.name for s in built] == ["good"]


class TestSourceDefaults:
    async def test_defaults_fill_only_blank_fields(self, client):
        config = SourceConfig(
            name="r",
            type="rss",
            url="https://x.de/f",
            defaults={"organization": "Default Org", "country": "Germany"},
        )
        source = RssSource(config, client)
        candidates = source.parse(RSS_FIXTURE)

        # Entry 1 names its own author; entry 2 does not.
        assert source.apply_defaults(candidates[0]).organization == "TU Beispiel"
        assert source.apply_defaults(candidates[1]).organization == "Default Org"
        assert source.apply_defaults(candidates[1]).country == "Germany"


class TestSourceRegistry:
    @pytest.mark.parametrize(
        ("source_type", "expected"),
        [
            ("mock", MockSource),
            ("rss", RssSource),
            ("html", GenericHtmlSource),
            ("search_api", SearchApiSource),
        ],
    )
    def test_every_type_builds(self, client, secrets, source_type, expected):
        config = SourceConfig(name="s", type=source_type, url="https://x.de")
        assert isinstance(build_source(config, client, secrets), expected)


class TestMaxResults:
    async def test_results_are_capped_per_source(self, client):
        source = MockSource(SourceConfig(name="mock", type="mock"), client)
        result = await source.run(SearchQuery(max_results=3))
        assert len(result.candidates) == 3
