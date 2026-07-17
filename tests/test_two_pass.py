"""The two-pass LLM path: Pass 1 relevance + the Pass-2 taxonomy detail call."""

from __future__ import annotations

import pytest

from job_alerts.database import Database
from job_alerts.llm.base import JobAssessment, OpportunityDetail
from job_alerts.llm.prompt import (
    DETAIL_PROMPT_VERSION,
    PROMPT_VERSION,
    build_detail_prompt,
    build_user_prompt,
)
from job_alerts.llm.providers import parse_details
from job_alerts.pipeline import Pipeline


def _pipeline(settings, sources_config, secrets, db):
    return Pipeline(settings, sources_config, secrets, db)


class TestPassOneContract:
    def test_is_academic_defaults_true(self):
        # A cached verdict from before the field existed omits it.
        a = JobAssessment(job_id="j1", is_job_posting=True, score=80)
        assert a.is_academic_opportunity is True

    def test_prompt_versions_bumped(self):
        assert PROMPT_VERSION == 5
        assert DETAIL_PROMPT_VERSION == 1

    def test_broad_prompt_does_not_demand_core_ai(self):
        job = _job()
        broad = build_user_prompt([job], topics=["ml"], locations=[], all_germany=True)
        assert "all academic fields are in scope" in broad
        assert "penalise a role for its field" in broad

    def test_core_ai_prompt_keeps_the_narrow_persona(self):
        job = _job()
        narrow = build_user_prompt(
            [job], topics=["ml"], locations=[], all_germany=True, core_ai_mode=True
        )
        assert "CORE of the work is AI/ML" in narrow
        assert "score MUST be 45 or below" in narrow

    def test_output_shape_includes_is_academic(self):
        assert "is_academic_opportunity" in build_user_prompt(
            [_job()], topics=[], locations=[], all_germany=True
        )


class TestPassTwoParsing:
    def test_detail_prompt_lists_the_taxonomy(self):
        prompt = build_detail_prompt([_job()])
        assert "opportunity_type" in prompt
        assert "applicant_level" in prompt
        assert "academic_field" in prompt
        assert "hiwi" in prompt and "master_thesis" in prompt

    def test_parse_matches_by_id_and_drops_unknown(self):
        jobs = [_job("j1"), _job("j2")]
        payload = {
            "details": [
                {"job_id": "j2", "opportunity_type": "phd_position"},
                {"job_id": "j1", "opportunity_type": "hiwi", "academic_field": "ml"},
                {"job_id": "ghost", "opportunity_type": "other"},
            ]
        }
        out = {d.job_id: d for d in parse_details(payload, jobs, "colab")}
        assert set(out) == {"j1", "j2"}
        assert out["j1"].opportunity_type == "hiwi"
        assert out["j2"].opportunity_type == "phd_position"

    def test_parse_tolerates_junk(self):
        assert parse_details({"nope": 1}, [_job()], "colab") == []
        assert parse_details({"details": "not a list"}, [_job()], "colab") == []

    def test_comma_string_skills_are_coerced(self):
        d = OpportunityDetail(job_id="j1", technical_skills="Python, PyTorch")
        assert d.technical_skills == ["Python", "PyTorch"]


class TestApplyDetail:
    def test_coerces_onto_taxonomy_and_merges_sparse_keywords(
        self, settings, sources_config, secrets, tmp_path, job_factory
    ):
        with Database(tmp_path / "t.db") as db:
            pipe = _pipeline(settings, sources_config, secrets, db)
            job = job_factory(id="j1", matched_keywords=[])
            detail = OpportunityDetail(
                job_id="j1",
                opportunity_type="Studentische Hilfskraft",  # German -> coerced
                applicant_level="MSc",
                academic_field="Maschinelles Lernen",
                research_topics=["computer vision"],
                technical_skills=["PyTorch"],
            )
            pipe._apply_detail(job, detail)
            assert job.opportunity_type == "hiwi"
            assert job.applicant_level == "master"
            assert job.academic_field == "ml"
            # Sparse keywords got the Pass-2 terms.
            assert "computer vision" in job.matched_keywords
            assert "PyTorch" in job.matched_keywords

    def test_good_keywords_are_not_overwritten(
        self, settings, sources_config, secrets, tmp_path, job_factory
    ):
        with Database(tmp_path / "t.db") as db:
            pipe = _pipeline(settings, sources_config, secrets, db)
            job = job_factory(id="j1", matched_keywords=["a", "b", "c"])
            pipe._apply_detail(
                job, OpportunityDetail(job_id="j1", research_topics=["x"], technical_skills=["y"])
            )
            assert job.matched_keywords == ["a", "b", "c"]

    def test_unknown_values_fall_back_safely(
        self, settings, sources_config, secrets, tmp_path, job_factory
    ):
        with Database(tmp_path / "t.db") as db:
            pipe = _pipeline(settings, sources_config, secrets, db)
            job = job_factory(id="j1")
            pipe._apply_detail(job, OpportunityDetail(job_id="j1", opportunity_type="astronaut"))
            assert job.opportunity_type == "other"
            assert job.applicant_level == "unspecified"


class TestClassifyDetailsDegrades:
    @pytest.mark.asyncio
    async def test_no_llm_configured_leaves_taxonomy_none(
        self, settings, sources_config, secrets, tmp_path, job_factory, summary_factory
    ):
        # No colab_base_url -> Pass 2 must be a no-op, not a crash.
        settings.llm.colab_base_url = ""
        with Database(tmp_path / "t.db") as db:
            pipe = _pipeline(settings, sources_config, secrets, db)
            job = job_factory(id="j1")
            await pipe._classify_details([job], summary_factory())
            assert job.opportunity_type is None


def _job(job_id: str = "j1"):
    from job_alerts.models import Job

    return Job(id=job_id, source="s", title="HiWi ML", url=f"http://x/{job_id}", content_hash="h")
