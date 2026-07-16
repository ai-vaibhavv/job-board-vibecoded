"""LLM assessment: the provider, parsing, and the fallback guarantees.

The fallback tests matter most. A dead tunnel, a stale URL, a mangled reply or
no configured model must all degrade to keyword scoring without losing a single
job. Every HTTP call is mocked; no test touches a live endpoint.

Only the self-hosted `colab` provider exists now. Where a test needs to exercise
the assessor's provider-fallback loop, it wires two Colab providers at different
URLs — the loop is general even though production lists a single provider.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from job_alerts.config import LlmSettings, Secrets
from job_alerts.llm import (
    ColabProvider,
    JobAssessment,
    LlmAssessor,
    LlmError,
    build_providers,
    build_user_prompt,
    parse_assessments,
)
from job_alerts.llm.providers import _extract_json

from .conftest import make_job

COLAB_BASE = "https://colab.example"
COLAB_URL = f"{COLAB_BASE}/v1/chat/completions"
COLAB2_BASE = "https://colab-2.example"
COLAB2_URL = f"{COLAB2_BASE}/v1/chat/completions"


def openai_reply(text: str) -> httpx.Response:
    """An OpenAI-compatible chat-completions reply — the shape Ollama/vLLM
    return and the shape ColabProvider parses."""
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def assessment_json(job_id: str, score: int = 85, **overrides) -> str:
    import json

    entry = {
        "job_id": job_id,
        "is_job_posting": True,
        "role_type": "hiwi",
        "requires_completed_phd": False,
        "suitable_for_masters": True,
        "core_ai_focus": True,
        "seniority": "student",
        "topics": ["machine learning"],
        "language": "en",
        "score": score,
        "reasoning": "Student research role in ML.",
    }
    entry.update(overrides)
    return json.dumps({"assessments": [entry]})


PROMPT_KWARGS = {"topics": ["machine learning"], "locations": ["Berlin"], "all_germany": True}


def _colab(base: str = COLAB_BASE, *, name: str | None = None, **kw) -> ColabProvider:
    provider = ColabProvider(base, **kw)
    if name is not None:
        provider.name = name  # distinguish instances in fallback assertions
    return provider


class TestPrompt:
    def test_prompt_includes_the_jobs_and_their_ids(self):
        jobs = [
            make_job(id="a", title="HiWi ML"),
            make_job(id="b", title="RA CV", url="https://e.de/b"),
        ]
        prompt = build_user_prompt(jobs, **PROMPT_KWARGS)
        assert "HiWi ML" in prompt
        assert "RA CV" in prompt
        assert '"a"' in prompt and '"b"' in prompt
        assert "exactly 2 assessment(s)" in prompt

    def test_prompt_reflects_configured_topics(self):
        """The profile comes from settings, so changing topics changes the
        prompt — otherwise the LLM would be a second, invisible config."""
        prompt = build_user_prompt(
            [make_job()], topics=["quantum computing"], locations=[], all_germany=True
        )
        assert "quantum computing" in prompt

    def test_prompt_states_the_phd_rule_both_ways(self):
        prompt = build_user_prompt([make_job()], **PROMPT_KWARGS)
        assert "PhD students are also welcome" in prompt  # the FALSE example
        assert "abgeschlossene Promotion" in prompt  # the TRUE example

    def test_prompt_includes_the_url(self):
        # The URL is often the clearest "this is a search page" signal.
        prompt = build_user_prompt(
            [make_job(url="https://de.indeed.com/q-hiwi-jobs.html")], **PROMPT_KWARGS
        )
        assert "q-hiwi-jobs.html" in prompt

    def test_prompt_truncates_long_descriptions(self):
        prompt = build_user_prompt(
            [make_job(description="x" * 10_000)], **PROMPT_KWARGS, max_description_chars=100
        )
        assert "x" * 101 not in prompt

    def test_locations_are_listed_when_not_all_germany(self):
        prompt = build_user_prompt(
            [make_job()], topics=[], locations=["Munich", "Berlin"], all_germany=False
        )
        assert "Only these locations" in prompt
        assert "Munich" in prompt


class TestJsonExtraction:
    def test_plain_json(self):
        assert _extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fenced_json(self):
        # Models add fences despite being told not to.
        assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
        assert _extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_json_embedded_in_prose(self):
        assert _extract_json('Sure! Here you go:\n{"a": 1}\nHope that helps.') == {"a": 1}

    @pytest.mark.parametrize("text", ["", "   ", "no json here at all"])
    def test_unusable_replies_raise_llm_error(self, text):
        with pytest.raises(LlmError):
            _extract_json(text)

    def test_malformed_json_raises_llm_error(self):
        with pytest.raises(LlmError, match=r"malformed|did not return"):
            _extract_json('{"a": 1,,,}')

    def test_json_array_at_top_level_is_rejected(self):
        with pytest.raises(LlmError, match="expected a JSON object"):
            _extract_json("[1, 2]")


class TestParseAssessments:
    def test_matches_by_job_id_not_position(self):
        """Models reorder arrays. Positional matching would attach one job's
        score to a different job — silently wrong, and worse than no answer."""
        jobs = [make_job(id="a"), make_job(id="b", url="https://e.de/b")]
        payload = {
            "assessments": [
                {"job_id": "b", "score": 90, "reasoning": "b"},
                {"job_id": "a", "score": 10, "reasoning": "a"},
            ]
        }
        by_id = {x.job_id: x for x in parse_assessments(payload, jobs, "test")}
        assert by_id["a"].score == 10
        assert by_id["b"].score == 90

    def test_unknown_job_ids_are_ignored(self):
        jobs = [make_job(id="a")]
        payload = {"assessments": [{"job_id": "hallucinated", "score": 99}]}
        with pytest.raises(LlmError, match="no usable assessments"):
            parse_assessments(payload, jobs, "test")

    def test_partial_results_are_kept(self):
        """7 of 8 assessments is worth having; the 8th falls back."""
        jobs = [make_job(id="a"), make_job(id="b", url="https://e.de/b")]
        payload = {"assessments": [{"job_id": "a", "score": 80}]}
        result = parse_assessments(payload, jobs, "test")
        assert [x.job_id for x in result] == ["a"]

    def test_one_bad_entry_does_not_lose_the_batch(self):
        jobs = [make_job(id="a"), make_job(id="b", url="https://e.de/b")]
        payload = {"assessments": ["not a dict", {"job_id": "b", "score": 70}]}
        assert [x.job_id for x in parse_assessments(payload, jobs, "test")] == ["b"]

    def test_missing_assessments_key_raises(self):
        with pytest.raises(LlmError, match="no 'assessments' array"):
            parse_assessments({"nonsense": []}, [make_job(id="a")], "test")

    def test_score_is_clamped(self):
        jobs = [make_job(id="a"), make_job(id="b", url="https://e.de/b")]
        payload = {"assessments": [{"job_id": "a", "score": 150}, {"job_id": "b", "score": -20}]}
        by_id = {x.job_id: x for x in parse_assessments(payload, jobs, "test")}
        assert by_id["a"].score == 100
        assert by_id["b"].score == 0

    def test_topics_as_a_string_are_coerced(self):
        payload = {"assessments": [{"job_id": "a", "score": 50, "topics": "ml, nlp"}]}
        result = parse_assessments(payload, [make_job(id="a")], "test")
        assert result[0].topics == ["ml", "nlp"]


class TestColabProvider:
    """The self-hosted OpenAI-compatible endpoint (Qwen on Colab via Ollama)."""

    @respx.mock
    async def test_successful_assessment(self):
        route = respx.post(COLAB_URL).mock(return_value=openai_reply(assessment_json("a", 77)))
        provider = ColabProvider(COLAB_BASE, api_key="secret")
        try:
            result = await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()
        assert result[0].score == 77
        assert route.calls[0].request.headers["Authorization"] == "Bearer secret"

    def test_base_url_is_normalized_into_the_endpoint(self):
        provider = ColabProvider(COLAB_BASE + "/")
        assert provider.endpoint == COLAB_URL

    @respx.mock
    async def test_no_api_key_sends_no_authorization_header(self):
        route = respx.post(COLAB_URL).mock(return_value=openai_reply(assessment_json("a")))
        provider = ColabProvider(COLAB_BASE, api_key="")
        try:
            await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()
        assert "Authorization" not in route.calls[0].request.headers

    @respx.mock
    async def test_http_error_becomes_llm_error(self):
        respx.post(COLAB_URL).mock(return_value=httpx.Response(503, text="loading"))
        provider = ColabProvider(COLAB_BASE)
        try:
            with pytest.raises(LlmError, match="503"):
                await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()

    @respx.mock
    async def test_network_error_becomes_transient_llm_error(self):
        respx.post(COLAB_URL).mock(side_effect=httpx.ConnectError("tunnel down"))
        provider = ColabProvider(COLAB_BASE)
        try:
            with pytest.raises(LlmError) as excinfo:
                await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()
        assert excinfo.value.transient is True


class TestProviderSelection:
    def test_colab_is_built_when_a_base_url_is_set(self):
        providers = build_providers(
            Secrets(_env_file=None), LlmSettings(providers=["colab"], colab_base_url=COLAB_BASE)
        )
        assert [p.name for p in providers] == ["colab"]

    def test_colab_is_skipped_without_a_base_url(self):
        assert build_providers(Secrets(_env_file=None), LlmSettings(providers=["colab"])) == []

    def test_default_settings_list_only_colab(self):
        assert LlmSettings().providers == ["colab"]

    def test_optional_token_is_passed_through(self):
        secrets = Secrets(_env_file=None, colab_api_key="tok")
        providers = build_providers(
            secrets, LlmSettings(providers=["colab"], colab_base_url=COLAB_BASE)
        )
        assert providers[0].api_key == "tok"


class TestAssessorFallback:
    """The headline guarantee: a working provider is used, and any failure
    degrades to {} so the caller scores those jobs with keywords instead."""

    def _assessor(self, providers, **kw):
        # Pacing and backoff are disabled here: they are real sleeps, and a
        # suite that waits per test is a suite nobody runs. `TestPacingAndRetry`
        # covers the timing behaviour explicitly instead.
        kw.setdefault("min_request_interval", 0.0)
        kw.setdefault("retry_base_delay", 0.0)
        kw.setdefault("max_retries", 0)
        return LlmAssessor(
            providers,
            LlmSettings(batch_size=10, **kw),
            topics=["machine learning"],
            locations=[],
            all_germany=True,
        )

    @respx.mock
    async def test_the_provider_result_is_used(self):
        route = respx.post(COLAB_URL).mock(return_value=openai_reply(assessment_json("a", 90)))
        assessor = self._assessor([_colab(name="colab")])
        try:
            result = await assessor.assess_all([make_job(id="a")])
        finally:
            await assessor.aclose()
        assert result["a"].score == 90
        assert route.called
        assert assessor.provider_used["a"] == "colab"

    @respx.mock
    async def test_a_second_provider_takes_over_when_the_first_fails(self):
        """The assessor tries providers in order; a failure must transparently
        fall through to the next one."""
        first = respx.post(COLAB_URL).mock(return_value=httpx.Response(503, text="down"))
        second = respx.post(COLAB2_URL).mock(return_value=openai_reply(assessment_json("a", 75)))

        assessor = self._assessor(
            [_colab(COLAB_BASE, name="primary"), _colab(COLAB2_BASE, name="secondary")]
        )
        try:
            result = await assessor.assess_all([make_job(id="a")])
        finally:
            await assessor.aclose()

        assert first.called and second.called
        assert result["a"].score == 75
        assert assessor.provider_used["a"] == "secondary"
        assert any("primary" in f for f in assessor.failures)

    @respx.mock
    async def test_all_providers_failing_yields_nothing_not_an_exception(self):
        """Callers rely on {} meaning "score these with keywords instead"."""
        respx.post(COLAB_URL).mock(return_value=httpx.Response(500))
        respx.post(COLAB2_URL).mock(return_value=httpx.Response(500))

        assessor = self._assessor([_colab(COLAB_BASE, name="a"), _colab(COLAB2_BASE, name="b")])
        try:
            result = await assessor.assess_all([make_job(id="a")])
        finally:
            await assessor.aclose()
        assert result == {}

    @respx.mock
    async def test_a_garbage_reply_falls_through_to_the_next_provider(self):
        respx.post(COLAB_URL).mock(return_value=openai_reply("I'm afraid I can't do that."))
        second = respx.post(COLAB2_URL).mock(return_value=openai_reply(assessment_json("a", 60)))

        assessor = self._assessor([_colab(COLAB_BASE, name="a"), _colab(COLAB2_BASE, name="b")])
        try:
            result = await assessor.assess_all([make_job(id="a")])
        finally:
            await assessor.aclose()
        assert second.called
        assert result["a"].score == 60

    @respx.mock
    async def test_batches_fall_back_independently(self):
        """The first provider dying on batch 2 must not discard batch 1's good
        answers; the second provider fills only what is missing."""
        import json

        jobs = [make_job(id=f"j{i}", url=f"https://e.de/{i}") for i in range(4)]

        def primary_side_effect(request):
            text = json.loads(request.content)["messages"][1]["content"]
            if "j0" in text:  # first batch succeeds
                return openai_reply(assessment_json("j0", 90))
            return httpx.Response(503, text="down")

        respx.post(COLAB_URL).mock(side_effect=primary_side_effect)

        def secondary_side_effect(request):
            text = json.loads(request.content)["messages"][1]["content"]
            for i in (1, 2, 3):
                if f"j{i}" in text:
                    return openai_reply(assessment_json(f"j{i}", 50))
            return httpx.Response(500)

        respx.post(COLAB2_URL).mock(side_effect=secondary_side_effect)

        assessor = LlmAssessor(
            [_colab(COLAB_BASE, name="primary"), _colab(COLAB2_BASE, name="secondary")],
            LlmSettings(
                batch_size=1,
                max_concurrency=1,
                min_request_interval=0.0,
                retry_base_delay=0.0,
                max_retries=0,
            ),
            topics=[],
            locations=[],
            all_germany=True,
        )
        try:
            result = await assessor.assess_all(jobs)
        finally:
            await assessor.aclose()

        assert result["j0"].score == 90  # from primary
        assert assessor.provider_used["j0"] == "primary"
        assert result["j1"].score == 50  # from secondary
        assert assessor.provider_used["j1"] == "secondary"

    async def test_no_providers_yields_nothing(self):
        assessor = self._assessor([])
        assert await assessor.assess_all([make_job(id="a")]) == {}
        assert assessor.available is False

    async def test_empty_job_list_makes_no_calls(self):
        assessor = self._assessor([_colab(name="colab")])
        try:
            assert await assessor.assess_all([]) == {}
        finally:
            await assessor.aclose()


class TestPacingAndRetry:
    """Pacing keeps a single local GPU from queueing overlapping requests, and
    transient failures are retried while permanent ones are not."""

    def _assessor(self, providers, **kw):
        return LlmAssessor(
            providers,
            LlmSettings(batch_size=1, **kw),
            topics=[],
            locations=[],
            all_germany=True,
        )

    @respx.mock
    async def test_requests_to_one_provider_are_spaced_out(self):
        import time

        respx.post(COLAB_URL).mock(side_effect=lambda request: openai_reply(assessment_json("j0")))
        jobs = [make_job(id="j0")]
        assessor = self._assessor([_colab(name="colab")], min_request_interval=0.3)
        try:
            started = time.monotonic()
            await assessor.assess_all(jobs)
            await assessor.assess_all(jobs)  # second call must wait
            elapsed = time.monotonic() - started
        finally:
            await assessor.aclose()
        assert elapsed >= 0.3

    @respx.mock
    async def test_pacing_can_be_disabled(self):
        import time

        respx.post(COLAB_URL).mock(return_value=openai_reply(assessment_json("j0")))
        assessor = self._assessor([_colab(name="colab")], min_request_interval=0.0)
        try:
            started = time.monotonic()
            await assessor.assess_all([make_job(id="j0")])
            assert time.monotonic() - started < 0.3
        finally:
            await assessor.aclose()

    @respx.mock
    async def test_transient_failure_is_retried(self):
        """A 503 (model still loading) means "wait", not "give up"."""
        route = respx.post(COLAB_URL).mock(
            side_effect=[
                httpx.Response(503, text="loading"),
                openai_reply(assessment_json("j0", 77)),
            ]
        )
        assessor = self._assessor(
            [_colab(name="colab")], min_request_interval=0.0, retry_base_delay=0.01, max_retries=2
        )
        try:
            result = await assessor.assess_all([make_job(id="j0")])
        finally:
            await assessor.aclose()
        assert route.call_count == 2
        assert result["j0"].score == 77

    @respx.mock
    async def test_permanent_failure_is_not_retried(self):
        """A 400 fails identically forever — retrying just delays the keyword
        fallback and wastes time."""
        route = respx.post(COLAB_URL).mock(return_value=httpx.Response(400, text="bad request"))
        assessor = self._assessor(
            [_colab(name="colab")], min_request_interval=0.0, retry_base_delay=0.01, max_retries=3
        )
        try:
            result = await assessor.assess_all([make_job(id="j0")])
        finally:
            await assessor.aclose()
        assert route.call_count == 1  # tried once, not four times
        assert result == {}

    @pytest.mark.parametrize(
        ("status", "transient"),
        [(429, True), (503, True), (500, True), (401, False), (400, False), (403, False)],
    )
    @respx.mock
    async def test_status_codes_are_classified_correctly(self, status, transient):
        respx.post(COLAB_URL).mock(return_value=httpx.Response(status, text="x"))
        provider = ColabProvider(COLAB_BASE)
        try:
            with pytest.raises(LlmError) as excinfo:
                await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()
        assert excinfo.value.transient is transient

    @respx.mock
    async def test_malformed_json_is_transient(self):
        """A truncated reply often parses on a retry (e.g. after num_ctx warms)."""
        respx.post(COLAB_URL).mock(return_value=openai_reply('{"assessments": [{"a":,}]}'))
        provider = ColabProvider(COLAB_BASE)
        try:
            with pytest.raises(LlmError) as excinfo:
                await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()
        assert excinfo.value.transient is True

    @respx.mock
    async def test_repeated_failures_are_deduplicated_in_the_summary(self):
        """Ten batches hitting one 503 is one problem, not ten lines."""
        respx.post(COLAB_URL).mock(return_value=httpx.Response(503, text="loading"))
        jobs = [make_job(id=f"j{i}", url=f"https://e.de/{i}") for i in range(5)]
        assessor = self._assessor(
            [_colab(name="colab")], min_request_interval=0.0, retry_base_delay=0.0, max_retries=0
        )
        try:
            await assessor.assess_all(jobs)
        finally:
            await assessor.aclose()
        assert len(assessor.failures) == 1


class TestAssessmentRendering:
    def test_explanation_names_the_provider_and_reason(self):
        a = JobAssessment(
            job_id="a", score=88, reasoning="HiWi in ML.", topics=["machine learning"]
        )
        lines = a.explanation("colab")
        assert "colab" in lines[0]
        assert "88" in lines[0]
        assert "HiWi in ML." in lines[0]

    def test_explanation_surfaces_hard_rejects(self):
        a = JobAssessment(job_id="a", score=5, requires_completed_phd=True, is_job_posting=False)
        text = " ".join(a.explanation("colab"))
        assert "completed PhD" in text
        assert "not an individual job posting" in text
