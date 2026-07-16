"""The three filters added in Phase 2: recency, German, country.

Each one exists because of something the first live database got wrong, and each
one has a failure mode that looks like success — a filter that silently drops
everything is indistinguishable from a quiet job market.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from job_alerts.config import ProfileSettings
from job_alerts.database import Database
from job_alerts.filtering import is_recent_enough
from job_alerts.llm.base import JobAssessment
from job_alerts.pipeline import Pipeline

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class TestRecency:
    """`max_age_days` was inert: 125/125 stored jobs had no date, and an undated
    job fell back to `discovered_at`, which for a freshly-found job is always
    now. The rule turns on whether we ever actually looked."""

    def test_a_recent_dated_job_is_kept(self, job_factory):
        job = job_factory()
        job.published_at = NOW - timedelta(days=3)
        assert is_recent_enough(job, 7, NOW) is True

    def test_an_old_dated_job_is_dropped(self, job_factory):
        job = job_factory()
        job.published_at = NOW - timedelta(days=10)
        assert is_recent_enough(job, 7, NOW) is False

    def test_undated_and_never_fetched_is_kept(self, job_factory):
        """We have no idea how old it is, and inventing an opinion would blind
        whole sources — fraunhofer has never published a date in its life."""
        job = job_factory()
        job.published_at = None
        job.enriched_at = None
        job.discovered_at = NOW
        assert is_recent_enough(job, 7, NOW) is True

    def test_undated_after_a_successful_fetch_is_dropped(self, job_factory):
        """We fetched the real posting and it states no date anywhere. That is a
        finding, not a gap."""
        job = job_factory()
        job.published_at = None
        job.enriched_at = NOW
        job.discovered_at = NOW
        assert is_recent_enough(job, 7, NOW) is False

    def test_the_enriched_rule_can_be_turned_off(self, job_factory):
        job = job_factory()
        job.published_at = None
        job.enriched_at = NOW
        job.discovered_at = NOW
        assert is_recent_enough(job, 7, NOW, drop_undated_when_enriched=False) is True

    def test_an_enriched_job_that_found_a_date_is_judged_on_that_date(self, job_factory):
        job = job_factory()
        job.enriched_at = NOW
        job.published_at = NOW - timedelta(days=2)
        assert is_recent_enough(job, 7, NOW) is True

    def test_a_long_undiscovered_job_ages_out(self, job_factory):
        """Undated and unfetched still cannot live forever; `discovered_at` is
        the fallback clock."""
        job = job_factory()
        job.published_at = None
        job.enriched_at = None
        job.discovered_at = NOW - timedelta(days=30)
        assert is_recent_enough(job, 7, NOW) is False


def _pipeline(settings, sources_config, secrets, db, profile=None) -> Pipeline:
    return Pipeline(settings, sources_config, secrets, db, profile)


class TestGermanRequired:
    """The distinction that protects the best source in the config."""

    @pytest.fixture
    def pipe(self, settings, sources_config, secrets, tmp_path):
        with Database(tmp_path / "t.db") as db:
            yield _pipeline(settings, sources_config, secrets, db)

    def _assess(self, **kw):
        base = dict(
            job_id="j1",
            is_job_posting=True,
            suitable_for_masters=True,
            topics=["machine learning"],
            score=85,
        )
        return JobAssessment(**{**base, **kw})

    def test_a_job_requiring_german_is_dropped(self, pipe, job_factory, summary_factory):
        job = job_factory(id="j1")
        out = pipe._apply_scores(
            [job], {"j1": self._assess(german_required=True)}, summary_factory()
        )
        assert out == []
        assert any("requires fluent German" in r for r in job.score_explanation)

    def test_a_job_where_german_is_merely_a_plus_is_kept(self, pipe, job_factory, summary_factory):
        job = job_factory(id="j1")
        out = pipe._apply_scores(
            [job], {"j1": self._assess(german_required=False)}, summary_factory()
        )
        assert out == [job]

    def test_a_posting_written_in_german_is_kept(self, pipe, job_factory, summary_factory):
        """THE test. Most of TU München's HiWi board is German-language adverts
        for groups that work in English. Filtering on `language` rather than
        `german_required` would throw the best source away."""
        job = job_factory(id="j1")
        out = pipe._apply_scores(
            [job],
            {"j1": self._assess(language="de", german_required=False)},
            summary_factory(),
        )
        assert out == [job]

    def test_the_german_filter_can_be_turned_off(
        self, settings, sources_config, secrets, tmp_path, job_factory, summary_factory
    ):
        with Database(tmp_path / "t.db") as db:
            pipe = _pipeline(
                settings,
                sources_config,
                secrets,
                db,
                ProfileSettings(exclude_german_required=False),
            )
            job = job_factory(id="j1")
            out = pipe._apply_scores(
                [job], {"j1": self._assess(german_required=True)}, summary_factory()
            )
        assert out == [job]


class TestCountry:
    @pytest.fixture
    def pipe(self, settings, sources_config, secrets, tmp_path):
        with Database(tmp_path / "t.db") as db:
            yield _pipeline(settings, sources_config, secrets, db)

    def _assess(self, country):
        return JobAssessment(
            job_id="j1",
            is_job_posting=True,
            suitable_for_masters=True,
            topics=["machine learning"],
            score=85,
            country=country,
        )

    def test_a_nigerian_job_is_dropped(self, pipe, job_factory, summary_factory):
        """ "Research Intern at Simple List NG" was a real row in a job board
        whose name begins with the word Germany."""
        job = job_factory(id="j1")
        assert pipe._apply_scores([job], {"j1": self._assess("Nigeria")}, summary_factory()) == []
        assert any("not in Germany" in r for r in job.score_explanation)

    def test_a_german_job_is_kept(self, pipe, job_factory, summary_factory):
        job = job_factory(id="j1")
        assert pipe._apply_scores([job], {"j1": self._assess("Germany")}, summary_factory()) == [
            job
        ]

    def test_country_matching_ignores_case(self, pipe, job_factory, summary_factory):
        job = job_factory(id="j1")
        assert pipe._apply_scores([job], {"j1": self._assess("germany")}, summary_factory()) == [
            job
        ]

    def test_an_unknown_country_is_kept(self, pipe, job_factory, summary_factory):
        """The mirror image of the original bug. Treating "we do not know" as an
        answer put Nigerian jobs in; treating it as disqualifying would throw out
        every posting that never says where it is. Unknown is not a verdict."""
        job = job_factory(id="j1")
        assert pipe._apply_scores([job], {"j1": self._assess(None)}, summary_factory()) == [job]

    def test_austria_is_dropped_by_default(self, pipe, job_factory, summary_factory):
        """A German student permit does not authorise work in Vienna."""
        job = job_factory(id="j1")
        assert pipe._apply_scores([job], {"j1": self._assess("Austria")}, summary_factory()) == []

    def test_austria_is_kept_once_it_is_on_the_list(
        self, settings, sources_config, secrets, tmp_path, job_factory, summary_factory
    ):
        with Database(tmp_path / "t.db") as db:
            pipe = _pipeline(
                settings,
                sources_config,
                secrets,
                db,
                ProfileSettings(countries=["Germany", "Austria"]),
            )
            job = job_factory(id="j1")
            out = pipe._apply_scores([job], {"j1": self._assess("Austria")}, summary_factory())
        assert out == [job]
