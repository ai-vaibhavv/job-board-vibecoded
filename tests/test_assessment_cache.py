"""The assessment cache: a run must cost its LLM only what is new.

Measured before this existed, on a real run: 108 candidates survived the keyword
filter, and re-judging every one of them each run wasted the model's time (and,
on a metered endpoint, its budget) re-buying verdicts already paid for the day
before. These tests are what stop that coming back.

Every HTTP call is mocked. No test touches a live endpoint.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from job_alerts.database import Database
from job_alerts.llm.prompt import PROMPT_VERSION
from job_alerts.pipeline import Pipeline

COLAB_BASE = "https://colab.example"
COLAB_URL = f"{COLAB_BASE}/v1/chat/completions"


def colab_reply(*job_ids: str, score: int = 85, **overrides) -> httpx.Response:
    entries = []
    for job_id in job_ids:
        entry = {
            "job_id": job_id,
            "is_job_posting": True,
            "role_type": "hiwi",
            "requires_completed_phd": False,
            "german_required": False,
            "suitable_for_masters": True,
            "core_ai_focus": True,
            "seniority": "student",
            "topics": ["machine learning"],
            "language": "en",
            "country": "Germany",
            "score": score,
            "reasoning": "Student research role in ML.",
        }
        entry.update(overrides)
        entries.append(entry)
    text = json.dumps({"assessments": entries})
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "jobs.db") as database:
        yield database


@pytest.fixture
def pipe(settings, sources_config, secrets, db):
    settings.search.enrich = False  # not what these tests are about
    settings.llm.colab_base_url = COLAB_BASE
    return Pipeline(settings, sources_config, secrets, db)


class TestCacheHits:
    @respx.mock
    async def test_a_second_run_makes_no_llm_calls(self, pipe, db, job_factory, summary_factory):
        """The headline guarantee, and the whole reason the table exists."""
        route = respx.post(COLAB_URL).mock(return_value=colab_reply("j1"))
        job = job_factory(id="j1", content_hash="h1")

        first = summary_factory()
        await pipe._assess_with_llm([job], first)
        db.upsert(job)
        pipe._flush_assessments(dry_run=False)
        assert route.call_count == 1
        assert first.llm_cached == 0

        second = summary_factory()
        got = await pipe._assess_with_llm([job], second)
        assert route.call_count == 1, "the second run bought the same verdict again"
        assert second.llm_cached == 1
        assert got["j1"].score == 85

    @respx.mock
    async def test_only_the_new_jobs_are_judged(self, pipe, db, job_factory, summary_factory):
        respx.post(COLAB_URL).mock(return_value=colab_reply("j1"))
        known = job_factory(id="j1", content_hash="h1")
        await pipe._assess_with_llm([known], summary_factory())
        db.upsert(known)
        pipe._flush_assessments(dry_run=False)

        # A new job turns up alongside the known one. respx reuses one route per
        # pattern, so measure the delta rather than the total.
        fresh = job_factory(id="j2", content_hash="h2", url="https://example.de/jobs/2")
        route = respx.post(COLAB_URL).mock(return_value=colab_reply("j2"))
        before = route.call_count
        summary = summary_factory()
        got = await pipe._assess_with_llm([known, fresh], summary)

        assert route.call_count - before == 1
        assert summary.llm_cached == 1
        assert set(got) == {"j1", "j2"}

    @respx.mock
    async def test_an_edited_posting_is_judged_again(self, pipe, db, job_factory, summary_factory):
        """`content_hash` is in the key so a changed posting gets a fresh look —
        otherwise a job rewritten from "PhD required" to "students welcome" would
        keep its old verdict forever."""
        respx.post(COLAB_URL).mock(return_value=colab_reply("j1"))
        job = job_factory(id="j1", content_hash="h1")
        await pipe._assess_with_llm([job], summary_factory())
        db.upsert(job)
        pipe._flush_assessments(dry_run=False)

        job.content_hash = "h2"
        route = respx.post(COLAB_URL).mock(return_value=colab_reply("j1", score=20))
        before = route.call_count
        summary = summary_factory()
        got = await pipe._assess_with_llm([job], summary)

        assert route.call_count - before == 1
        assert summary.llm_cached == 0
        assert got["j1"].score == 20

    @respx.mock
    async def test_bumping_the_prompt_invalidates_the_cache(
        self, pipe, db, job_factory, summary_factory, monkeypatch
    ):
        """Otherwise two rubrics live in one table and a score cannot be traced
        back to the prompt that produced it."""
        respx.post(COLAB_URL).mock(return_value=colab_reply("j1"))
        job = job_factory(id="j1", content_hash="h1")
        await pipe._assess_with_llm([job], summary_factory())
        db.upsert(job)
        pipe._flush_assessments(dry_run=False)

        monkeypatch.setattr("job_alerts.pipeline.PROMPT_VERSION", PROMPT_VERSION + 1)
        route = respx.post(COLAB_URL).mock(return_value=colab_reply("j1"))
        before = route.call_count
        summary = summary_factory()
        await pipe._assess_with_llm([job], summary)

        assert route.call_count - before == 1
        assert summary.llm_cached == 0


class TestCacheWrites:
    @respx.mock
    async def test_a_dry_run_writes_nothing(self, pipe, db, job_factory, summary_factory):
        """A dry run promises no side effects. Reading the cache is still free."""
        respx.post(COLAB_URL).mock(return_value=colab_reply("j1"))
        job = job_factory(id="j1", content_hash="h1")

        await pipe._assess_with_llm([job], summary_factory(), dry_run=True)
        db.upsert(job)
        pipe._flush_assessments(dry_run=True)

        assert db.get_assessment("j1", "h1", PROMPT_VERSION) is None

    @respx.mock
    async def test_a_verdict_for_an_unstored_job_is_dropped_not_raised(
        self, pipe, db, job_factory, summary_factory
    ):
        """Assessment happens before storage, and a job can be rejected before it
        is ever stored. Its verdict has nothing to attach to — the foreign key
        says so, and the run must not die over a cache write."""
        respx.post(COLAB_URL).mock(return_value=colab_reply("ghost"))
        job = job_factory(id="ghost", content_hash="h1")

        await pipe._assess_with_llm([job], summary_factory())
        pipe._flush_assessments(dry_run=False)  # job never stored

        assert db.get_assessment("ghost", "h1", PROMPT_VERSION) is None

    @respx.mock
    async def test_a_cached_verdict_survives_a_round_trip_intact(
        self, pipe, db, job_factory, summary_factory
    ):
        """Including the fields added in v2 — a cache that quietly drops
        `german_required` would un-filter every German-only job on the next run."""
        respx.post(COLAB_URL).mock(
            return_value=colab_reply("j1", german_required=True, country="Austria")
        )
        job = job_factory(id="j1", content_hash="h1")
        await pipe._assess_with_llm([job], summary_factory())
        db.upsert(job)
        pipe._flush_assessments(dry_run=False)

        got = await pipe._assess_with_llm([job], summary_factory())
        assert got["j1"].german_required is True
        assert got["j1"].country == "Austria"
