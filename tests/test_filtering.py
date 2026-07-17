"""Filtering: German/English matching, negatives, and the PhD nuance rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from job_alerts.filtering import (
    filter_job,
    find_matches,
    is_recent_enough,
    looks_academic,
    matches,
    matches_location,
    requires_completed_phd,
)

from .conftest import make_job


class TestLooksAcademic:
    def test_english_academic_terms(self):
        assert looks_academic("Research Assistant at the Institute of Physics")
        assert looks_academic("PhD position in a university laboratory")

    def test_german_academic_terms(self):
        assert looks_academic("Studentische Hilfskraft am Lehrstuhl für Informatik")
        assert looks_academic(None, "Doktorand gesucht, Universität Tübingen")
        assert looks_academic("Promotionsstelle am Forschungszentrum Jülich")

    def test_accented_european_terms(self):
        # Accent-folded, so French/Italian/Spanish postings match too.
        assert looks_academic("Doctorant en apprentissage automatique, Université de Paris")
        assert looks_academic("Dottorando presso l'università di Bologna")
        assert looks_academic("Becario de investigación en la universidad")
        assert looks_academic("Promovendus aan de Universiteit van Amsterdam")

    def test_non_academic_text_is_false(self):
        assert not looks_academic("Senior Sales Manager at a fintech startup")

    def test_empty_is_false(self):
        assert not looks_academic(None, "", None)


class TestKeywordMatching:
    def test_matches_are_case_insensitive(self):
        assert matches("Research Assistant Position", "research assistant")

    def test_matches_ignore_punctuation_and_gender_suffixes(self):
        # German postings almost always carry "(m/w/d)".
        assert matches("Wissenschaftliche Hilfskraft (m/w/d)", "wissenschaftliche hilfskraft")

    def test_umlauts_are_folded(self):
        assert matches("Werkstudent Forschung", "werkstudent forschung")
        assert matches("Studentische Hilfskräfte gesucht", "studentische hilfskraft") is False
        # ...but the singular form does match the singular keyword:
        assert matches("Studentische Hilfskraft gesucht", "studentische hilfskraft")

    def test_singular_keyword_matches_plural_text(self):
        """Postings say "Master's students are encouraged to apply" and "hiring
        research assistants". A singular keyword must still match, or the
        configured keywords silently miss most real listings."""
        assert matches("Master's students are encouraged to apply", "master's student")
        assert matches("We are hiring research assistants", "research assistant")

    def test_plural_tolerance_does_not_match_unrelated_words(self):
        # "student" +s must not reach "studentische", nor "hiwi" reach "hiwis…pedia".
        assert matches("Hiwipedia nonsense", "hiwi") is False
        assert matches("Studentenwerk cafeteria", "student") is False

    def test_word_boundaries_prevent_false_positives(self):
        # Without boundaries "hiwi" would match inside longer words.
        assert matches("Hiwi position", "hiwi") is True
        assert matches("Hiwipedia nonsense", "hiwi", word_boundary=True) is False

    def test_word_boundary_can_be_disabled(self):
        assert matches("Hiwipedia", "hiwi", word_boundary=False) is True

    def test_multi_word_keywords_tolerate_extra_whitespace(self):
        assert matches("research    assistant", "research assistant")

    def test_find_matches_returns_every_hit(self):
        hits = find_matches(
            "Research assistant and student assistant roles",
            ["research assistant", "student assistant", "postdoc"],
        )
        assert set(hits) == {"research assistant", "student assistant"}


class TestGermanAndEnglishFiltering:
    def test_english_job_passes(self, settings):
        job = make_job(title="Research Assistant", description="A machine learning role.")
        assert filter_job(job, settings.keywords, settings.filtering).passed

    def test_german_job_passes(self, settings):
        job = make_job(
            title="Studentische Hilfskraft (m/w/d)",
            description="Wir suchen eine studentische Hilfskraft für unsere Forschung.",
        )
        decision = filter_job(job, settings.keywords, settings.filtering)
        assert decision.passed
        assert "studentische hilfskraft" in decision.matched_keywords

    def test_hiwi_shorthand_passes(self, settings):
        job = make_job(title="HiWi gesucht", description="HiWi Stelle im Bereich KI.")
        assert filter_job(job, settings.keywords, settings.filtering).passed

    def test_keyword_found_only_in_description_still_passes(self, settings):
        # A generic title must not hide a real HiWi role.
        job = make_job(
            title="Open position at the AI lab",
            description="We are hiring a studentische Hilfskraft for our group.",
        )
        assert filter_job(job, settings.keywords, settings.filtering).passed

    def test_unrelated_job_is_rejected(self, settings):
        job = make_job(title="Marketing Manager", description="Lead our campaigns.")
        decision = filter_job(job, settings.keywords, settings.filtering)
        assert not decision.passed
        assert "no positive keyword matched" in decision.reasons[0]


class TestNegativeKeywords:
    def test_professor_role_is_rejected(self, settings):
        job = make_job(
            title="Research Assistant", description="You will support the professor of our chair."
        )
        assert not filter_job(job, settings.keywords, settings.filtering).passed

    def test_senior_in_title_is_rejected(self, settings):
        job = make_job(title="Senior Research Assistant", description="Machine learning.")
        decision = filter_job(job, settings.keywords, settings.filtering)
        assert not decision.passed
        assert "title" in decision.reasons[0]

    def test_senior_only_in_description_does_not_reject(self, settings):
        """ "You report to a senior researcher" says nothing about *this* role's
        level. Rejecting on it would drop good student jobs."""
        job = make_job(
            title="Research Assistant",
            description="You will report to a senior researcher in our team.",
        )
        assert filter_job(job, settings.keywords, settings.filtering).passed

    def test_ausbildung_is_rejected(self, settings):
        job = make_job(
            title="Research Assistant", description="Diese Ausbildung dauert drei Jahre."
        )
        assert not filter_job(job, settings.keywords, settings.filtering).passed


class TestPhdNuance:
    """The spec is explicit: do not reject every role that mentions a PhD.
    Reject only when a completed PhD is genuinely required."""

    def test_passing_phd_mention_does_not_reject(self, settings):
        job = make_job(
            title="Student Research Assistant",
            description=(
                "Master's students are encouraged to apply; PhD students are also welcome. "
                "You will work alongside our PhD candidates."
            ),
        )
        assert filter_job(job, settings.keywords, settings.filtering).passed

    def test_required_phd_rejects(self, settings):
        job = make_job(
            title="Research Assistant",
            description="A completed PhD is required for this position. PhD required.",
        )
        decision = filter_job(job, settings.keywords, settings.filtering)
        assert not decision.passed
        assert "completed PhD" in decision.reasons[0]

    def test_german_promotion_requirement_rejects(self, settings):
        job = make_job(
            title="Wissenschaftliche Hilfskraft",
            description="Eine abgeschlossene Promotion wird vorausgesetzt.",
        )
        assert not filter_job(job, settings.keywords, settings.filtering).passed

    def test_postdoc_title_always_rejects(self, settings):
        """A postdoc title is out of reach regardless of the nuance rule."""
        job = make_job(
            title="Postdoc Research Assistant",
            description="Join our team. Master students welcome to ask questions.",
        )
        assert not filter_job(job, settings.keywords, settings.filtering).passed

    def test_nuance_can_be_disabled(self, settings):
        """With the nuance off, a bare "phd" negative rejects on mention alone.
        This is the blunt behaviour the flag exists to switch off by default."""
        settings.keywords.negative = [*settings.keywords.negative, "phd"]
        job = make_job(
            title="Research Assistant", description="PhD students are also welcome to apply."
        )
        # Nuance on (the default): a passing mention is tolerated.
        assert filter_job(job, settings.keywords, settings.filtering).passed

        settings.filtering.phd_requires_explicit_signal = False
        assert not filter_job(job, settings.keywords, settings.filtering).passed

    def test_requires_completed_phd_helper(self, settings):
        signals = settings.filtering.phd_requirement_signals
        assert requires_completed_phd("A completed PhD required.", signals)
        assert not requires_completed_phd("PhD students welcome.", signals)


class TestRecency:
    def test_recent_job_passes(self):
        assert is_recent_enough(make_job(published_at=datetime.now(UTC) - timedelta(days=5)), 30)

    def test_old_job_is_rejected(self):
        assert not is_recent_enough(
            make_job(published_at=datetime.now(UTC) - timedelta(days=45)), 30
        )

    def test_job_without_date_is_kept(self):
        """Most HTML sources expose no date; dropping them would blind whole
        sources. `discovered_at` is the fallback clock."""
        job = make_job(published_at=None, discovered_at=datetime.now(UTC))
        assert is_recent_enough(job, 30)

    def test_undated_job_discovered_long_ago_is_rejected(self):
        job = make_job(published_at=None, discovered_at=datetime.now(UTC) - timedelta(days=60))
        assert not is_recent_enough(job, 30)


class TestLocationFiltering:
    def test_all_germany_accepts_everything(self):
        assert matches_location(make_job(location="Kleinkleckersdorf"), ["Berlin"], True)

    def test_configured_location_matches(self):
        assert matches_location(make_job(location="Munich"), ["Munich", "Berlin"], False)

    def test_non_matching_location_is_rejected(self):
        assert not matches_location(make_job(location="Vienna"), ["Munich", "Berlin"], False)

    def test_missing_location_is_kept(self):
        """German sources routinely omit the location; rejecting on a missing
        field loses real jobs."""
        assert matches_location(make_job(location=None), ["Munich"], False)
