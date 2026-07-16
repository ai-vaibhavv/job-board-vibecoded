"""The host denylist: refusing to fetch LinkedIn, whatever robots.txt says.

Every HTTP call is mocked. No test touches a live website.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from job_alerts.config import HttpSettings
from job_alerts.http import HostDenied, PoliteClient, host_is_denied

# linkedin.com/robots.txt, abridged but structurally faithful as of 2026-07-16:
# a header notice prohibiting automated access, then rules for ~36 *named* bots
# and no `User-agent: *` group at all.
LINKEDIN_ROBOTS = """\
# Notice: The use of robots or other automated means to access LinkedIn without
# the express permission of LinkedIn is strictly prohibited.
# See https://www.linkedin.com/legal/user-agreement.

User-agent: LinkedInBot
Allow: /

User-agent: Googlebot
Disallow: /feed/update/

User-agent: Bingbot
Disallow: /feed/update/
"""


@pytest.fixture
def http_settings() -> HttpSettings:
    return HttpSettings(
        per_domain_delay=0.0, cache_ttl_seconds=0, max_retries=1, respect_robots=True
    )


class TestHostIsDenied:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.linkedin.com/jobs/view/123", True),
            ("https://linkedin.com/posts/someone-activity-7475170864473407490-QRn6", True),
            # One entry has to cover every country subdomain.
            ("https://de.linkedin.com/jobs/view/1", True),
            ("https://ng.linkedin.com/jobs/view/1", True),
            # Not a substring match — these are different companies.
            ("https://notlinkedin.com/x", False),
            ("https://linkedin.com.evil.example/x", False),
            ("https://jobs.fraunhofer.de/x", False),
            ("", False),
        ],
    )
    def test_host_is_denied(self, url, expected):
        assert host_is_denied(url) is expected


class TestDenylistBeatsRobots:
    """The point of the denylist, stated as a test.

    LinkedIn's robots.txt has no `User-agent: *` group, so `can_fetch()` returns
    True for our UA — the standard's default when no group matches. robots.txt
    therefore *permits* the fetch that LinkedIn's own header text prohibits. If
    this project ever relied on the robots check to stay off LinkedIn, it would
    walk straight in. So: assert the fetch is refused, and assert it is refused
    even though robots said yes.
    """

    @respx.mock
    async def test_robots_really_does_permit_it(self, http_settings):
        """Guard the premise. If LinkedIn ever adds a `User-agent: *` group this
        fails, and the denylist stops being load-bearing — worth knowing."""
        respx.get("https://www.linkedin.com/robots.txt").mock(
            return_value=httpx.Response(200, text=LINKEDIN_ROBOTS)
        )
        async with PoliteClient(http_settings) as client:
            assert await client.is_allowed("https://www.linkedin.com/jobs/view/123") is True

    @respx.mock
    async def test_get_text_refuses_linkedin_anyway(self, http_settings):
        respx.get("https://www.linkedin.com/robots.txt").mock(
            return_value=httpx.Response(200, text=LINKEDIN_ROBOTS)
        )
        page = respx.get("https://www.linkedin.com/jobs/view/123").mock(
            return_value=httpx.Response(200, text="<html>a job</html>")
        )
        async with PoliteClient(http_settings) as client:
            with pytest.raises(HostDenied):
                await client.get_text("https://www.linkedin.com/jobs/view/123")
        assert not page.called

    @respx.mock
    async def test_a_post_url_is_refused(self, http_settings):
        """The link-following path is what makes this reachable at all."""
        respx.get("https://www.linkedin.com/robots.txt").mock(
            return_value=httpx.Response(200, text=LINKEDIN_ROBOTS)
        )
        url = "https://www.linkedin.com/posts/someone_hiring-activity-7475170864473407490-QRn6"
        page = respx.get(url).mock(return_value=httpx.Response(200, text="we're hiring"))
        async with PoliteClient(http_settings) as client:
            with pytest.raises(HostDenied):
                await client.get_text(url)
        assert not page.called

    @respx.mock
    async def test_check_robots_false_does_not_bypass_the_denylist(self, http_settings):
        """`check_robots=False` exists for our own API keys on documented
        endpoints. It is not a back door to LinkedIn."""
        page = respx.get("https://de.linkedin.com/jobs/view/1").mock(
            return_value=httpx.Response(200, text="job")
        )
        async with PoliteClient(http_settings) as client:
            with pytest.raises(HostDenied):
                await client.get_text("https://de.linkedin.com/jobs/view/1", check_robots=False)
        assert not page.called

    @respx.mock
    async def test_post_json_is_refused_too(self, http_settings):
        page = respx.post("https://www.linkedin.com/voyager/api/x").mock(
            return_value=httpx.Response(200, json={})
        )
        async with PoliteClient(http_settings) as client:
            with pytest.raises(HostDenied):
                await client.post_json(
                    "https://www.linkedin.com/voyager/api/x", json_body={"q": "1"}
                )
        assert not page.called

    @respx.mock
    async def test_denial_is_not_retried(self, http_settings):
        """A denied host will be denied identically forever."""
        http_settings.max_retries = 3
        page = respx.get("https://www.linkedin.com/jobs/view/9").mock(
            return_value=httpx.Response(200, text="job")
        )
        async with PoliteClient(http_settings) as client:
            with pytest.raises(HostDenied):
                await client.get_text("https://www.linkedin.com/jobs/view/9", check_robots=False)
        assert page.call_count == 0

    @respx.mock
    async def test_other_hosts_are_unaffected(self, http_settings):
        respx.get("https://jobs.fraunhofer.de/robots.txt").mock(return_value=httpx.Response(404))
        respx.get("https://jobs.fraunhofer.de/search").mock(
            return_value=httpx.Response(200, text="<html>jobs</html>")
        )
        async with PoliteClient(http_settings) as client:
            assert "jobs" in await client.get_text("https://jobs.fraunhofer.de/search")
