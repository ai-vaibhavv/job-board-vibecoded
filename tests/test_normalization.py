"""Normalization: URLs, dates, timezones, language, identity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from job_alerts.models import JobCandidate, Language, RemoteStatus
from job_alerts.normalization import (
    BERLIN,
    content_hash,
    derive_job_id,
    detect_language,
    detect_remote_status,
    normalize_candidate,
    normalize_url,
    parse_datetime,
    strip_html,
)


class TestNormalizeUrl:
    def test_strips_utm_parameters(self):
        assert (
            normalize_url("https://example.de/job/1?utm_source=x&utm_medium=y&utm_campaign=z")
            == "https://example.de/job/1"
        )

    def test_strips_trk_and_friends(self):
        assert (
            normalize_url("https://example.de/job/1?trk=public_jobs&refId=abc&trackingId=xyz")
            == "https://example.de/job/1"
        )

    def test_keeps_meaningful_parameters(self):
        # The posting id lives in the query string here — dropping unknown
        # params would merge different jobs into one.
        assert (
            normalize_url("https://example.de/jobs?jobId=42") == "https://example.de/jobs?jobId=42"
        )

    def test_keeps_meaningful_and_drops_tracking_together(self):
        assert (
            normalize_url("https://example.de/jobs?jobId=42&utm_source=news&trk=abc")
            == "https://example.de/jobs?jobId=42"
        )

    def test_unifies_scheme_www_case_and_trailing_slash(self):
        variants = [
            "https://www.Example.de/job/1/",
            "http://example.de/job/1",
            "https://EXAMPLE.de/job/1#apply",
            "https://www.example.de:443/job/1",
        ]
        assert {normalize_url(v) for v in variants} == {"https://example.de/job/1"}

    def test_sorts_query_parameters(self):
        # Same job, parameters in a different order.
        assert normalize_url("https://e.de/j?b=2&a=1") == normalize_url("https://e.de/j?a=1&b=2")

    def test_linkedin_urls_collapse_to_canonical_id(self):
        variants = [
            "https://www.linkedin.com/jobs/view/research-assistant-at-tum-4012345678?trk=public_jobs",
            "https://linkedin.com/jobs/view/4012345678",
            "https://de.linkedin.com/jobs/view/4012345678?refId=x&trackingId=y",
        ]
        assert {normalize_url(v) for v in variants} == {"https://linkedin.com/jobs/view/4012345678"}

    def test_linkedin_post_urls_collapse_to_author_and_activity_id(self):
        """A feed post's URL is `/posts/<author>_<slug>-activity-<id>-<hash>`.

        The trailing hash differs between shares of the same post, and the
        country mirror differs by who found it. Neither changes which post it is
        — but keep either and the same "we're hiring" post arrives as a brand
        new job on every run, forever. These are real URLs from a live search.
        """
        variants = [
            "https://www.linkedin.com/posts/ssblanco_i-announced-our-first-company-off-site-at-activity-7475170864473407490-QRn6",
            # Same post, different share hash, different national mirror, tracking on.
            "https://de.linkedin.com/posts/ssblanco_i-announced-our-first-company-off-site-at-activity-7475170864473407490-ZZZZ?utm_source=share",
        ]
        assert {normalize_url(v) for v in variants} == {
            "https://linkedin.com/posts/ssblanco-activity-7475170864473407490"
        }

    def test_different_posts_stay_different(self):
        a = normalize_url(
            "https://www.linkedin.com/posts/ssblanco_i-announced-activity-7475170864473407490-QRn6"
        )
        b = normalize_url(
            "https://www.linkedin.com/posts/rachael-omilegan_i-have-been-activity-7474749821279657984-HvRp"
        )
        assert a != b

    def test_a_lookalike_host_is_not_treated_as_linkedin(self):
        url = "https://notlinkedin.com/posts/x_y-activity-1234567890-ab"
        assert normalize_url(url) == url

    def test_handles_empty_and_protocol_relative(self):
        assert normalize_url("") == ""
        assert normalize_url("//example.de/j") == "https://example.de/j"


class TestParseDatetime:
    @pytest.mark.parametrize(
        "value",
        [
            "2026-03-15T10:30:00+00:00",
            "2026-03-15T10:30:00Z",
            "Sun, 15 Mar 2026 10:30:00 +0000",  # RFC 2822 (RSS)
        ],
    )
    def test_parses_common_formats_to_utc(self, value):
        parsed = parse_datetime(value)
        assert parsed == datetime(2026, 3, 15, 10, 30, tzinfo=UTC)
        assert parsed.tzinfo is not None

    def test_parses_german_date_format_day_first(self):
        # 15.03.2026 is 15 March, not 3 March. Asserted in Berlin time: a
        # date-only value means midnight *local*, which is 23:00 UTC the day
        # before, so checking .day in UTC would be checking the wrong thing.
        local = parse_datetime("15.03.2026").astimezone(BERLIN)
        assert (local.year, local.month, local.day) == (2026, 3, 15)

    def test_ambiguous_day_month_is_read_the_german_way(self):
        # 03.04.2026 must be 3 April, not 4 March — day-first, as every German
        # job board writes it.
        local = parse_datetime("03.04.2026").astimezone(BERLIN)
        assert (local.month, local.day) == (4, 3)

    def test_parses_german_month_name(self):
        parsed = parse_datetime("15 März 2026")
        assert (parsed.year, parsed.month) == (2026, 3)

    def test_naive_datetimes_are_read_as_berlin(self):
        # Every source here is German; a naive "10:00" means 10:00 in Berlin.
        parsed = parse_datetime("2026-01-15T10:00:00")
        expected = datetime(2026, 1, 15, 10, 0, tzinfo=BERLIN).astimezone(UTC)
        assert parsed == expected
        assert parsed.hour == 9  # CET is UTC+1 in January

    def test_berlin_summer_time_is_handled(self):
        # CEST is UTC+2 in July — a fixed offset would get this wrong.
        parsed = parse_datetime("2026-07-15T10:00:00")
        assert parsed.hour == 8

    def test_aware_datetime_is_converted_not_reinterpreted(self):
        source = datetime(2026, 3, 15, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        assert parse_datetime(source) == source.astimezone(UTC)

    @pytest.mark.parametrize("value", [None, "", "   ", "not a date", "yesterday", "TBD"])
    def test_malformed_dates_return_none_rather_than_raising(self, value):
        assert parse_datetime(value) is None


class TestStripHtml:
    def test_removes_tags_and_collapses_whitespace(self):
        assert strip_html("<p>Hello   <b>world</b></p>\n<p>Again</p>") == "Hello world Again"

    def test_drops_script_and_style_content(self):
        assert "alert" not in strip_html("<div>Text<script>alert('x')</script></div>")

    def test_passes_plain_text_through(self):
        assert strip_html("Just text") == "Just text"

    def test_handles_none_and_empty(self):
        assert strip_html(None) == ""
        assert strip_html("") == ""


class TestDetectLanguage:
    def test_detects_german(self):
        assert (
            detect_language(
                "Studentische Hilfskraft",
                "Wir suchen eine studentische Hilfskraft für die Forschung.",
            )
            is Language.DE
        )

    def test_detects_english(self):
        assert (
            detect_language(
                "Research Assistant", "We are looking for a research assistant to join the team."
            )
            is Language.EN
        )

    def test_unknown_for_empty(self):
        assert detect_language(None, "") is Language.UNKNOWN


class TestDetectRemoteStatus:
    def test_detects_hybrid_before_remote(self):
        # "hybrid (remote possible)" is hybrid, not remote.
        assert detect_remote_status("Hybrid role, remote work possible") is RemoteStatus.HYBRID

    def test_detects_remote(self):
        assert detect_remote_status("Fully remote position") is RemoteStatus.REMOTE

    def test_detects_german_homeoffice(self):
        assert detect_remote_status("Homeoffice möglich") is RemoteStatus.REMOTE

    def test_unknown_when_unstated(self):
        assert detect_remote_status("Research Assistant", "A job.") is RemoteStatus.UNKNOWN


class TestIdentity:
    def test_prefers_source_and_source_job_id(self):
        candidate = JobCandidate(
            source="rss", source_job_id="abc-1", title="T", url="https://e.de/1"
        )
        assert derive_job_id(candidate, "https://e.de/1") == "rss:abc-1"

    def test_falls_back_to_stable_hash(self):
        candidate = JobCandidate(source="rss", title="T", organization="O", url="https://e.de/1")
        first = derive_job_id(candidate, "https://e.de/1")
        second = derive_job_id(candidate, "https://e.de/1")
        assert first == second
        assert first.startswith("rss:h:")

    def test_hash_id_differs_for_different_jobs(self):
        a = JobCandidate(source="rss", title="A", url="https://e.de/1")
        b = JobCandidate(source="rss", title="B", url="https://e.de/2")
        assert derive_job_id(a, "https://e.de/1") != derive_job_id(b, "https://e.de/2")

    def test_content_hash_ignores_case_and_punctuation(self):
        assert content_hash("Research Assistant!", "TUM", "Munich", "d") == content_hash(
            "research assistant", "tum", "munich", "d"
        )

    def test_content_hash_changes_when_content_changes(self):
        assert content_hash("A", "O", "L", "one") != content_hash("A", "O", "L", "two")


class TestNormalizeCandidate:
    def test_produces_a_clean_job(self):
        job = normalize_candidate(
            JobCandidate(
                source="test",
                source_job_id="1",
                title="  Research   Assistant  ",
                description="<p>Machine <b>learning</b> role</p>",
                url="https://www.Example.de/job/1?utm_source=x",
                published_at="2026-03-15T10:00:00Z",
                organization="TU Munich",
                location="Munich",
            )
        )
        assert job.title == "Research Assistant"
        assert job.description == "Machine learning role"
        assert job.url == "https://example.de/job/1"
        assert job.id == "test:1"
        # The candidate never said which country, so neither do we.
        assert job.country is None
        assert job.published_at == datetime(2026, 3, 15, 10, 0, tzinfo=UTC)

    def test_future_published_date_is_discarded(self):
        # A date parsed into the future is a parsing artefact; keeping it would
        # let the job claim the recency bonus forever.
        job = normalize_candidate(
            JobCandidate(
                source="t",
                title="X",
                url="https://e.de/1",
                published_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
            )
        )
        assert job.published_at is None

    def test_missing_optional_fields_are_tolerated(self):
        job = normalize_candidate(JobCandidate(source="t", title="X", url="https://e.de/1"))
        assert job.organization is None
        assert job.location is None
        assert job.published_at is None
        assert job.country is None

    def test_an_unstated_country_is_never_invented(self):
        """The bug this encodes: `country` defaulted to "Germany", so all 125
        rows of the first live database claimed Germany — including the Nigerian
        and Sri Lankan postings a broken `site:` filter let through. Nothing
        downstream could tell a known country from an assumed one."""
        job = normalize_candidate(
            JobCandidate(
                source="search",
                title="Research Intern at Simple List NG",
                url="https://ng.linkedin.com/jobs/view/4415343807",
            )
        )
        assert job.country is None

    def test_a_source_that_knows_its_country_still_says_so(self):
        """`defaults: {country: Germany}` on a German-only source is a real
        assertion, not a guess, and must still flow through."""
        job = normalize_candidate(
            JobCandidate(
                source="fraunhofer",
                title="Studentische Hilfskraft",
                url="https://jobs.fraunhofer.de/job/1",
                country="Germany",
            )
        )
        assert job.country == "Germany"

    def test_description_is_length_limited(self):
        job = normalize_candidate(
            JobCandidate(source="t", title="X", url="https://e.de/1", description="x" * 10_000),
            max_description_chars=500,
        )
        assert len(job.description) == 500

    def test_unparseable_date_does_not_raise(self):
        job = normalize_candidate(
            JobCandidate(source="t", title="X", url="https://e.de/1", published_at="soon-ish")
        )
        assert job.published_at is None
