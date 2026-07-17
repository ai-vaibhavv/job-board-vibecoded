"""The academic taxonomy coerces free-form and German labels safely."""

from __future__ import annotations

import pytest

from job_alerts.taxonomy import (
    AcademicField,
    ApplicantLevel,
    InstitutionType,
    OpportunityType,
)


class TestOpportunityType:
    def test_canonical_value_round_trips(self) -> None:
        assert OpportunityType.coerce("research_assistant") is OpportunityType.RESEARCH_ASSISTANT

    def test_display_casing_and_spaces(self) -> None:
        assert OpportunityType.coerce("PhD Position") is OpportunityType.PHD_POSITION

    def test_already_a_member_passes_through(self) -> None:
        assert OpportunityType.coerce(OpportunityType.HIWI) is OpportunityType.HIWI

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Studentische Hilfskraft", OpportunityType.HIWI),
            ("wissenschaftliche hilfskraft", OpportunityType.HIWI),
            ("Werkstudent", OpportunityType.WORKING_STUDENT),
            ("Masterarbeit", OpportunityType.MASTER_THESIS),
            ("Bachelorarbeit", OpportunityType.BACHELOR_THESIS),
            ("Forschungspraktikum", OpportunityType.RESEARCH_INTERNSHIP),
            ("Promotionsstelle", OpportunityType.PHD_POSITION),
            ("Doktorand", OpportunityType.DOCTORAL_RESEARCHER),
        ],
    )
    def test_german_aliases(self, raw: str, expected: OpportunityType) -> None:
        assert OpportunityType.coerce(raw) is expected

    def test_unknown_falls_back_to_other(self) -> None:
        assert OpportunityType.coerce("sales manager") is OpportunityType.OTHER

    def test_non_string_falls_back(self) -> None:
        assert OpportunityType.coerce(None) is OpportunityType.OTHER
        assert OpportunityType.coerce(42) is OpportunityType.OTHER

    def test_legacy_role_types_all_map(self) -> None:
        # Every historical JobAssessment.role_type value must survive the pivot.
        legacy = [
            "research_assistant",
            "hiwi",
            "werkstudent",
            "research_intern",
            "master_thesis",
            "phd_position",
            "postdoc",
            "senior",
            "other",
        ]
        for value in legacy:
            assert OpportunityType.coerce(value) is not OpportunityType.OTHER or value == "other"


class TestApplicantLevel:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("bachelor", ApplicantLevel.BACHELOR),
            ("Undergraduate", ApplicantLevel.BACHELOR),
            ("MSc", ApplicantLevel.MASTER),
            ("masters", ApplicantLevel.MASTER),
            ("recent grad", ApplicantLevel.RECENT_GRADUATE),
            ("all", ApplicantLevel.MULTIPLE),
        ],
    )
    def test_aliases(self, raw: str, expected: ApplicantLevel) -> None:
        assert ApplicantLevel.coerce(raw) is expected

    def test_blank_and_unknown_are_unspecified(self) -> None:
        assert ApplicantLevel.coerce("") is ApplicantLevel.UNSPECIFIED
        assert ApplicantLevel.coerce("   ") is ApplicantLevel.UNSPECIFIED
        assert ApplicantLevel.coerce("whatever") is ApplicantLevel.UNSPECIFIED


class TestAcademicField:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Machine Learning", AcademicField.ML),
            ("maschinelles Lernen", AcademicField.ML),
            ("Künstliche Intelligenz", AcademicField.AI),
            ("Informatik", AcademicField.COMPUTER_SCIENCE),
            ("Robotik", AcademicField.ROBOTICS),
            ("Physik", AcademicField.PHYSICS),
        ],
    )
    def test_umlaut_and_german_aliases(self, raw: str, expected: AcademicField) -> None:
        assert AcademicField.coerce(raw) is expected

    def test_unknown_is_other(self) -> None:
        assert AcademicField.coerce("basket weaving") is AcademicField.OTHER


class TestInstitutionType:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Universität", InstitutionType.UNIVERSITY),
            ("Lehrstuhl", InstitutionType.CHAIR),
            ("Institut", InstitutionType.RESEARCH_INSTITUTE),
            ("Arbeitsgruppe", InstitutionType.RESEARCH_GROUP),
            ("Klinikum", InstitutionType.UNIVERSITY_HOSPITAL),
        ],
    )
    def test_german_aliases(self, raw: str, expected: InstitutionType) -> None:
        assert InstitutionType.coerce(raw) is expected
