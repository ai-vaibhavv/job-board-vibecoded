"""Relevance scoring: the arithmetic, the cap, and the explanation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from job_alerts.models import Language
from job_alerts.scoring import Scorer

from .conftest import make_job


@pytest.fixture
def scorer(settings, scoring_settings):
    return Scorer(
        scoring_settings,
        topics=settings.keywords.topics,
        locations=["Munich", "Berlin", "Germany"],
        phd_signals=settings.filtering.phd_requirement_signals,
    )


class TestPositiveSignals:
    def test_exact_title_match_awards_its_weight(self, scorer):
        score, why = scorer.score(
            make_job(
                title="Research Assistant",
                description="",
                location=None,
                published_at=None,
                organization=None,
                country="",
            )
        )
        assert score == scorer.weights.exact_title_match
        assert any("exact target title match" in line for line in why)

    def test_german_exact_title_with_gender_suffix_matches(self, scorer):
        _, why = scorer.score(make_job(title="Studentische Hilfskraft (m/w/d)"))
        assert any("exact target title match" in line for line in why)

    def test_long_german_title_starting_with_the_role_is_an_exact_match(self, scorer):
        """Real German postings are long and descriptive. "STUDENTISCHE
        HILFSKRAFT im Bereich Softwareentwicklung für Simulationen" *is* a
        studentische Hilfskraft role and must score as one — this was found by
        running against live Fraunhofer listings, where every job was
        under-scored by 10 points until the rule anchored at the start."""
        _, why = scorer.score(
            make_job(
                title="STUDENTISCHE HILFSKRAFT im Bereich Softwareentwicklung für Simulationen"
            )
        )
        assert any("exact target title match" in line for line in why)

    def test_role_named_mid_title_is_not_an_exact_match(self, scorer):
        """The target must lead the title. Merely mentioning it does not make
        the posting that role."""
        _, why = scorer.score(make_job(title="Support the research assistant team with catering"))
        assert not any("exact target title match" in line for line in why)

    def test_role_after_a_separator_is_an_exact_match(self, scorer):
        """Boards staple role names together: "Initiativbewerbung Praktikum /
        Bachelor- und Masterarbeiten / Studentische Hilfskraft"."""
        _, why = scorer.score(
            make_job(title="Initiativbewerbung Praktikum / Studentische Hilfskraft (all genders)")
        )
        assert any("exact target title match" in line for line in why)

    def test_student_keyword_in_title_awards_weaker_weight(self, scorer):
        _, why = scorer.score(make_job(title="Working Student Position in our Lab"))
        assert any("research/student keyword in title" in line for line in why)

    def test_exact_and_keyword_title_bonuses_do_not_stack(self, scorer):
        """Otherwise "Research Assistant" quietly earns 50 instead of 30 and the
        configured weights stop meaning what they say."""
        _, why = scorer.score(make_job(title="Research Assistant"))
        assert sum("title" in line and "topic" not in line for line in why) == 1

    def test_topic_in_title_scores_more_than_in_description(self, scorer):
        in_title, _ = scorer.score(
            make_job(title="Research Assistant Machine Learning", description="A role.")
        )
        in_desc, _ = scorer.score(
            make_job(title="Research Assistant", description="A machine learning role.")
        )
        assert in_title > in_desc

    def test_topic_counted_once_not_in_both_places(self, scorer):
        _, why = scorer.score(
            make_job(title="Research Assistant Machine Learning", description="machine learning")
        )
        assert sum("topic in description" in line for line in why) == 0

    def test_masters_eligibility_awards_its_weight(self, scorer):
        _, why = scorer.score(
            make_job(
                title="Research Assistant",
                description="Master students are welcome. We are looking for an enrolled student.",
            )
        )
        assert any("Master's students eligible" in line for line in why)

    def test_english_role_awards_its_weight(self, scorer):
        _, why = scorer.score(
            make_job(
                title="Research Assistant",
                description="We are looking for a research assistant to join the team and work with you.",
                language=Language.EN,
            )
        )
        assert any("English-language posting" in line for line in why)

    def test_recent_publication_awards_its_weight(self, scorer):
        _, why = scorer.score(make_job(published_at=datetime.now(UTC) - timedelta(days=1)))
        assert any("published within" in line for line in why)

    def test_old_publication_awards_nothing(self, scorer):
        _, why = scorer.score(make_job(published_at=datetime.now(UTC) - timedelta(days=20)))
        assert not any("published within" in line for line in why)

    def test_missing_date_is_not_treated_as_recent(self, scorer):
        _, why = scorer.score(make_job(published_at=None))
        assert not any("published within" in line for line in why)


class TestNegativeSignals:
    def test_completed_phd_requirement_costs_its_weight(self, scorer):
        score, why = scorer.score(
            make_job(title="Research Assistant", description="A completed PhD is required.")
        )
        assert any("requires a completed PhD" in line for line in why)
        assert score < 55

    def test_passing_phd_mention_costs_nothing(self, scorer):
        _, why = scorer.score(
            make_job(title="Research Assistant", description="PhD students are also welcome.")
        )
        assert not any("requires a completed PhD" in line for line in why)

    def test_senior_title_costs_its_weight(self, scorer):
        _, why = scorer.score(make_job(title="Senior Research Assistant"))
        assert any("senior/leadership title" in line for line in why)

    def test_unrelated_discipline_costs_its_weight(self, scorer):
        _, why = scorer.score(
            make_job(title="Research Assistant", description="A nursing research role.")
        )
        assert any("unrelated discipline" in line for line in why)


class TestScoreBounds:
    def test_score_never_exceeds_100(self, scorer):
        job = make_job(
            title="Research Assistant Machine Learning",
            description=(
                "Machine learning, deep learning, computer vision, robotics and data science. "
                "Master students welcome, enrolled student. We are looking for you to join the team."
            ),
            location="Munich",
            language=Language.EN,
            published_at=datetime.now(UTC),
        )
        score, why = scorer.score(job)
        assert score <= 100
        if score == 100:
            assert any("capped" in line for line in why)

    def test_score_never_drops_below_0(self, scorer):
        job = make_job(
            title="Senior Principal Professor of Nursing",
            description="A completed PhD is required. Nursing and dentistry. Head of department.",
            location="Vienna",
            country="Austria",
        )
        score, _ = scorer.score(job)
        assert score == 0

    def test_cap_is_recorded_in_the_explanation(self, scorer):
        scorer.weights.exact_title_match = 500
        score, why = scorer.score(make_job(title="Research Assistant"))
        assert score == 100
        assert any("capped to 100" in line for line in why)


class TestExplainability:
    def test_every_explanation_line_carries_a_signed_delta(self, scorer):
        _, why = scorer.score(
            make_job(
                title="Research Assistant Machine Learning",
                description="Master students welcome.",
                location="Munich",
            )
        )
        deltas = [line for line in why if not line.startswith("=")]
        assert deltas
        assert all(line[0] in "+-" for line in deltas)

    def test_explanation_deltas_sum_to_the_score(self, scorer):
        """The explanation must actually justify the number, not merely
        accompany it."""
        job = make_job(
            title="Research Assistant Machine Learning",
            description="Master students welcome to apply.",
            location="Munich",
            published_at=datetime.now(UTC) - timedelta(days=1),
        )
        score, why = scorer.score(job)
        total = sum(int(line.split()[0]) for line in why if not line.startswith("="))
        assert max(0, min(100, total)) == score

    def test_apply_writes_score_and_explanation_onto_the_job(self, scorer):
        job = scorer.apply(make_job(title="Research Assistant"))
        assert job.relevance_score > 0
        assert job.score_explanation

    def test_weights_are_configurable(self, scorer):
        baseline, _ = scorer.score(make_job(title="Research Assistant"))
        scorer.weights.exact_title_match = 5
        adjusted, _ = scorer.score(make_job(title="Research Assistant"))
        assert adjusted < baseline


class TestRealisticJobs:
    def test_ideal_job_clears_the_default_threshold(self, scorer):
        job = make_job(
            title="Research Assistant",
            description=(
                "The Chair of Data Science seeks a research assistant for machine learning "
                "projects. Master students enrolled in computer science are welcome to apply. "
                "The working language is English and we look forward to your application."
            ),
            location="Munich",
            language=Language.EN,
            published_at=datetime.now(UTC) - timedelta(days=1),
        )
        score, _ = scorer.score(job)
        assert score >= 55

    def test_postdoc_falls_below_the_threshold(self, scorer):
        job = make_job(
            title="Postdoctoral Researcher Deep Learning",
            description="A completed PhD is required.",
            location="Munich",
        )
        score, _ = scorer.score(job)
        assert score < 55
