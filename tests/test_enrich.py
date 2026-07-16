"""Enrichment: fetching the real posting instead of judging a snippet.

Every HTTP call is mocked. No test touches a live website.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from job_alerts.config import HttpSettings
from job_alerts.enrich import Enricher, extract
from job_alerts.http import PoliteClient

JOB_PAGE = """
<html><head>
  <meta property="article:published_time" content="2026-07-14T09:00:00+02:00">
</head><body>
  <nav>Home Jobs Contact</nav>
  <main>
    <h1>Studentische Hilfskraft (m/w/d) - Machine Learning</h1>
    <span itemprop="addressLocality">Garching bei München</span>
    <p>Die Professur für Informatik sucht ab Oktober 2026 eine studentische
       Hilfskraft im Bereich maschinelles Lernen. 10 Stunden pro Woche.
       Voraussetzung ist ein laufendes Masterstudium der Informatik oder
       verwandter Fächer. Erfahrung mit Python und PyTorch ist erwünscht.
       Die Stelle ist zunächst auf sechs Monate befristet und kann verlängert
       werden. Bewerbungen bitte per Mail.</p>
    <p>Kontakt: <a href="mailto:hiwi@in.tum.de">hiwi@in.tum.de</a></p>
  </main>
  <footer>Impressum Datenschutz</footer>
</body></html>
"""


@pytest.fixture
def http_settings() -> HttpSettings:
    return HttpSettings(
        per_domain_delay=0.0, cache_ttl_seconds=0, max_retries=1, respect_robots=False
    )


class TestExtract:
    """Pure parsing — no network involved at all."""

    def test_pulls_the_description_out_of_main(self):
        got = extract(JOB_PAGE)
        assert "studentische" in got.description.lower()
        assert "PyTorch" in got.description

    def test_chrome_is_not_part_of_the_job(self):
        got = extract(JOB_PAGE)
        assert "Impressum" not in got.description
        assert "Home Jobs Contact" not in got.description

    def test_reads_a_published_date(self):
        assert extract(JOB_PAGE).published_at == datetime(
            2026, 7, 14, 7, 0, tzinfo=UTC
        )  # 09:00 +02:00

    def test_reads_a_time_element(self):
        html = '<html><body><main><time datetime="2026-07-10T12:00:00Z">10 July</time>' + (
            "<p>" + "x" * 300 + "</p></main></body></html>"
        )
        assert extract(html).published_at == datetime(2026, 7, 10, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        "prose",
        [
            "Veröffentlicht am: 14.07.2026",
            "Online seit 14.07.2026",
            "Eingestellt am 14.07.2026",
            "Posted on 2026-07-14",
        ],
    )
    def test_reads_a_date_written_in_prose(self, prose):
        """German job pages state the date in words far more often than in
        markup. A bare date is read as Europe/Berlin — so midnight on the 14th
        is 22:00 UTC on the 13th, which is right, and irrelevant to a 7-day
        window either way."""
        html = f"<html><body><main><p>{prose}</p><p>{'x' * 300}</p></main></body></html>"
        assert extract(html).published_at == datetime(2026, 7, 13, 22, 0, tzinfo=UTC)

    def test_reads_the_location(self):
        assert extract(JOB_PAGE).location == "Garching bei München"

    def test_reads_the_contact_email(self):
        assert extract(JOB_PAGE).contact_email == "hiwi@in.tum.de"

    def test_a_noreply_address_is_not_a_way_to_apply(self):
        """TU München's own feed sends `noreply@tum.de`. It is plumbing."""
        html = """<html><body><main><p>Bitte bewerben Sie sich.</p>
            <a href="mailto:noreply@tum.de">noreply@tum.de</a>
            <a href="mailto:prof.schmidt@tum.de">Prof. Schmidt</a>
        </main></body></html>"""
        assert extract(html).contact_email == "prof.schmidt@tum.de"

    def test_finds_a_google_form(self):
        html = """<html><body><main><p>Apply here:</p>
            <a href="https://docs.google.com/forms/d/e/1FAIpQ/viewform">Application form</a>
        </main></body></html>"""
        assert "docs.google.com/forms" in extract(html).contact_url

    def test_breadcrumbs_are_not_part_of_the_job(self):
        """Structure copied from a real TUM posting: Plone, no <main>, and a
        breadcrumb trail inside #content. Extraction used to open with
        "Sitemap > Schwarzes Brett > Studentische Hilfskräfte, ..." — tokens
        paid for on every LLM call, and noise in the alert."""
        html = """<html><body>
          <td id="portal-column-content"><div id="content" class="contentBox">
            <div id="portal-breadcrumbs">Sitemap &gt; Schwarzes Brett &gt;
              Studentische Hilfskräfte, Praktikantenstellen, Studienarbeiten</div>
            <div id="maincontentwrapper"><div id="news-content">
              <h1>Student Assistant (m/w/d) to support us with data science tasks</h1>
              <p>For the TUM School of Management at the Heilbronn Data Science Center we
                 are looking for a student assistant for 9-20 hours per week. You will
                 support ongoing research projects with data collection and analysis.</p>
            </div></div>
          </div></td>
        </body></html>"""
        got = extract(html)
        assert got.description.startswith("Student Assistant")
        assert "Sitemap" not in got.description
        assert "Heilbronn" in got.description

    def test_a_page_with_nothing_useful_yields_nothing(self):
        got = extract("<html><body></body></html>")
        assert got.published_at is None
        assert got.contact_email is None

    def test_malformed_html_does_not_raise(self):
        extract("<html><body><main><p>unclosed")
        extract("")


class TestEnricher:
    @respx.mock
    async def test_a_thin_job_is_filled_in_from_its_page(self, http_settings, job_factory):
        respx.get("https://x.de/job/1").mock(return_value=httpx.Response(200, text=JOB_PAGE))
        job = job_factory(
            url="https://x.de/job/1", description="Studentische Hilfskraft", location=None
        )
        job.published_at = None

        async with PoliteClient(http_settings) as client:
            out = await Enricher(client).enrich(job)

        assert "PyTorch" in out.description
        assert out.location == "Garching bei München"
        assert out.published_at is not None
        assert out.contact_email == "hiwi@in.tum.de"
        assert out.enriched_at is not None

    @respx.mock
    async def test_a_failed_fetch_is_never_fatal(self, http_settings, job_factory):
        """One dead page must not cost the run. The job keeps what it had."""
        respx.get("https://x.de/job/1").mock(return_value=httpx.Response(404))
        job = job_factory(url="https://x.de/job/1", description="original text")

        async with PoliteClient(http_settings) as client:
            out = await Enricher(client).enrich(job)

        assert out.description == "original text"
        # The distinction the recency rule depends on: we never looked, so we
        # are not entitled to conclude anything from the absence of a date.
        assert out.enriched_at is None

    @respx.mock
    async def test_a_timeout_is_never_fatal(self, http_settings, job_factory):
        respx.get("https://x.de/job/1").mock(side_effect=httpx.ConnectTimeout("slow"))
        job = job_factory(url="https://x.de/job/1")

        async with PoliteClient(http_settings) as client:
            out = await Enricher(client).enrich(job)
        assert out.enriched_at is None

    @respx.mock
    async def test_linkedin_is_refused_and_the_job_survives(self, http_settings, job_factory):
        """The denylist raises; enrichment must absorb it like any other refusal
        rather than taking the run down."""
        job = job_factory(url="https://www.linkedin.com/jobs/view/123")
        async with PoliteClient(http_settings) as client:
            out = await Enricher(client).enrich(job)
        assert out.enriched_at is None

    @respx.mock
    async def test_a_page_that_says_nothing_still_counts_as_looked_at(
        self, http_settings, job_factory
    ):
        """ "We fetched it and it has no date" is a finding, not a failure — and
        it is the one that lets the recency rule drop the job."""
        respx.get("https://x.de/job/1").mock(
            return_value=httpx.Response(200, text="<html><body><p>nothing here</p></body></html>")
        )
        job = job_factory(url="https://x.de/job/1")
        job.published_at = None

        async with PoliteClient(http_settings) as client:
            out = await Enricher(client).enrich(job)

        assert out.published_at is None
        assert out.enriched_at is not None

    @respx.mock
    async def test_a_source_stated_fact_is_not_overwritten(self, http_settings, job_factory):
        """A feed's own pubDate is an assertion; our scrape is inference. The
        assertion wins."""
        stated = datetime(2026, 1, 1, tzinfo=UTC)
        respx.get("https://x.de/job/1").mock(return_value=httpx.Response(200, text=JOB_PAGE))
        job = job_factory(url="https://x.de/job/1", location="Munich")
        job.published_at = stated

        async with PoliteClient(http_settings) as client:
            out = await Enricher(client).enrich(job)

        assert out.published_at == stated
        assert out.location == "Munich"

    @respx.mock
    async def test_a_longer_description_does_replace_a_stub(self, http_settings, job_factory):
        respx.get("https://x.de/job/1").mock(return_value=httpx.Response(200, text=JOB_PAGE))
        job = job_factory(url="https://x.de/job/1", description="HiWi")

        async with PoliteClient(http_settings) as client:
            out = await Enricher(client).enrich(job)
        assert len(out.description) > 100

    async def test_an_already_enriched_job_is_not_fetched_again(self, http_settings, job_factory):
        job = job_factory(url="https://x.de/job/1")
        job.enriched_at = datetime.now(UTC)
        async with PoliteClient(http_settings) as client:
            assert Enricher(client).needs_enriching(job) is False

    async def test_a_fat_job_with_everything_is_left_alone(self, http_settings, job_factory):
        job = job_factory(url="https://x.de/1", description="x" * 800, location="Munich")
        job.published_at = datetime.now(UTC)
        async with PoliteClient(http_settings) as client:
            assert Enricher(client).needs_enriching(job) is False

    async def test_a_job_missing_only_its_date_is_still_worth_fetching(
        self, http_settings, job_factory
    ):
        job = job_factory(url="https://x.de/1", description="x" * 800, location="Munich")
        job.published_at = None
        async with PoliteClient(http_settings) as client:
            assert Enricher(client).needs_enriching(job) is True
