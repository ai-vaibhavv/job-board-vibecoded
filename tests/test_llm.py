"""LLM assessment: providers, parsing, and the fallback guarantees.

The fallback tests matter most. An LLM outage, a rate limit, a mangled reply or
a missing key must all degrade to keyword scoring without losing a single job.
Every HTTP call is mocked; no test spends an API credit.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from job_alerts.config import LlmSettings, Secrets
from job_alerts.llm import (
    GeminiProvider,
    GroqProvider,
    JobAssessment,
    LlmAssessor,
    LlmError,
    build_providers,
    build_user_prompt,
    parse_assessments,
)
from job_alerts.llm.providers import _extract_json

from .conftest import make_job

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def gemini_reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def groq_reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def assessment_json(job_id: str, score: int = 85, **overrides) -> str:
    import json

    entry = {
        "job_id": job_id,
        "is_job_posting": True,
        "role_type": "hiwi",
        "requires_completed_phd": False,
        "suitable_for_masters": True,
        "seniority": "student",
        "topics": ["machine learning"],
        "language": "en",
        "score": score,
        "reasoning": "Student research role in ML.",
    }
    entry.update(overrides)
    return json.dumps({"assessments": [entry]})


PROMPT_KWARGS = {"topics": ["machine learning"], "locations": ["Berlin"], "all_germany": True}


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


class TestGeminiProvider:
    @respx.mock
    async def test_successful_assessment(self):
        route = respx.post(GEMINI_URL).mock(return_value=gemini_reply(assessment_json("a")))
        provider = GeminiProvider("secret-key", model="gemini-2.5-flash")
        try:
            result = await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()
        assert result[0].score == 85
        assert route.calls[0].request.headers["x-goog-api-key"] == "secret-key"

    @respx.mock
    async def test_key_is_not_in_the_url(self):
        """A key in a query string leaks into logs and proxies."""
        route = respx.post(GEMINI_URL).mock(return_value=gemini_reply(assessment_json("a")))
        provider = GeminiProvider("secret-key")
        try:
            await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()
        assert "secret-key" not in str(route.calls[0].request.url)

    @respx.mock
    async def test_rate_limit_becomes_llm_error(self):
        respx.post(GEMINI_URL).mock(return_value=httpx.Response(429, text="quota exceeded"))
        provider = GeminiProvider("k")
        try:
            with pytest.raises(LlmError, match="429"):
                await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()

    @respx.mock
    async def test_safety_block_becomes_llm_error(self):
        respx.post(GEMINI_URL).mock(
            return_value=httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
        )
        provider = GeminiProvider("k")
        try:
            with pytest.raises(LlmError, match="no candidates"):
                await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()

    @respx.mock
    async def test_network_error_becomes_llm_error(self):
        respx.post(GEMINI_URL).mock(side_effect=httpx.ConnectError("down"))
        provider = GeminiProvider("k")
        try:
            with pytest.raises(LlmError, match="request failed"):
                await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()


class TestGroqProvider:
    @respx.mock
    async def test_successful_assessment(self):
        route = respx.post(GROQ_URL).mock(return_value=groq_reply(assessment_json("a", score=70)))
        provider = GroqProvider("groq-key")
        try:
            result = await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()
        assert result[0].score == 70
        assert route.calls[0].request.headers["Authorization"] == "Bearer groq-key"

    @respx.mock
    async def test_http_error_becomes_llm_error(self):
        respx.post(GROQ_URL).mock(return_value=httpx.Response(503, text="unavailable"))
        provider = GroqProvider("k")
        try:
            with pytest.raises(LlmError, match="503"):
                await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()


class TestProviderSelection:
    def test_order_follows_settings(self):
        secrets = Secrets(_env_file=None, gemini_api_key="g", groq_api_key="q")
        providers = build_providers(secrets, LlmSettings(providers=["groq", "gemini"]))
        assert [p.name for p in providers] == ["groq", "gemini"]

    def test_providers_without_a_key_are_skipped(self):
        secrets = Secrets(_env_file=None, gemini_api_key="", groq_api_key="q")
        providers = build_providers(secrets, LlmSettings())
        assert [p.name for p in providers] == ["groq"]

    def test_no_keys_means_no_providers(self):
        providers = build_providers(Secrets(_env_file=None), LlmSettings())
        assert providers == []

    def test_has_llm_reflects_either_key(self):
        assert Secrets(_env_file=None).has_llm is False
        assert Secrets(_env_file=None, gemini_api_key="g").has_llm is True
        assert Secrets(_env_file=None, groq_api_key="q").has_llm is True


class TestFallbackChain:
    """The headline guarantee: Gemini -> Groq -> keyword scoring, seamlessly."""

    def _assessor(self, providers, **kw):
        # Pacing and backoff are disabled here: they are real sleeps, and a
        # suite that waits 20s per rate-limit test is a suite nobody runs.
        # `TestPacingAndRetry` covers the timing behaviour explicitly instead.
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
    async def test_gemini_is_preferred_when_it_works(self):
        gemini = respx.post(GEMINI_URL).mock(return_value=gemini_reply(assessment_json("a", 90)))
        groq = respx.post(GROQ_URL).mock(return_value=groq_reply(assessment_json("a", 10)))

        assessor = self._assessor([GeminiProvider("g"), GroqProvider("q")])
        try:
            result = await assessor.assess_all([make_job(id="a")])
        finally:
            await assessor.aclose()

        assert result["a"].score == 90
        assert gemini.called and not groq.called
        assert assessor.provider_used["a"] == "gemini"

    @respx.mock
    async def test_groq_takes_over_when_gemini_fails(self):
        """The core requirement: a Gemini error must transparently become Groq."""
        gemini = respx.post(GEMINI_URL).mock(return_value=httpx.Response(429, text="quota"))
        groq = respx.post(GROQ_URL).mock(return_value=groq_reply(assessment_json("a", 75)))

        assessor = self._assessor([GeminiProvider("g"), GroqProvider("q")])
        try:
            result = await assessor.assess_all([make_job(id="a")])
        finally:
            await assessor.aclose()

        assert gemini.called and groq.called
        assert result["a"].score == 75
        assert assessor.provider_used["a"] == "groq"
        assert any("gemini" in f for f in assessor.failures)

    @respx.mock
    async def test_both_failing_yields_nothing_not_an_exception(self):
        """Callers rely on {} meaning "score these with keywords instead"."""
        respx.post(GEMINI_URL).mock(return_value=httpx.Response(500))
        respx.post(GROQ_URL).mock(return_value=httpx.Response(500))

        assessor = self._assessor([GeminiProvider("g"), GroqProvider("q")])
        try:
            result = await assessor.assess_all([make_job(id="a")])
        finally:
            await assessor.aclose()
        assert result == {}

    @respx.mock
    async def test_a_garbage_reply_falls_through_to_the_next_provider(self):
        respx.post(GEMINI_URL).mock(return_value=gemini_reply("I'm afraid I can't do that."))
        groq = respx.post(GROQ_URL).mock(return_value=groq_reply(assessment_json("a", 60)))

        assessor = self._assessor([GeminiProvider("g"), GroqProvider("q")])
        try:
            result = await assessor.assess_all([make_job(id="a")])
        finally:
            await assessor.aclose()
        assert groq.called
        assert result["a"].score == 60

    @respx.mock
    async def test_batches_fall_back_independently(self):
        """Gemini dying on batch 2 must not discard batch 1's good answers."""
        import json

        jobs = [make_job(id=f"j{i}", url=f"https://e.de/{i}") for i in range(4)]

        def gemini_side_effect(request):
            body = json.loads(request.content)
            text = body["contents"][0]["parts"][0]["text"]
            if "j0" in text:  # first batch succeeds
                return gemini_reply(assessment_json("j0", 90))
            return httpx.Response(429, text="quota")

        respx.post(GEMINI_URL).mock(side_effect=gemini_side_effect)

        def groq_side_effect(request):
            body = json.loads(request.content)
            text = body["messages"][1]["content"]
            for i in (1, 2, 3):
                if f"j{i}" in text:
                    return groq_reply(assessment_json(f"j{i}", 50))
            return httpx.Response(500)

        respx.post(GROQ_URL).mock(side_effect=groq_side_effect)

        assessor = LlmAssessor(
            [GeminiProvider("g"), GroqProvider("q")],
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

        assert result["j0"].score == 90  # from gemini
        assert assessor.provider_used["j0"] == "gemini"
        assert result["j1"].score == 50  # from groq
        assert assessor.provider_used["j1"] == "groq"

    async def test_no_providers_yields_nothing(self):
        assessor = self._assessor([])
        assert await assessor.assess_all([make_job(id="a")]) == {}
        assert assessor.available is False

    async def test_empty_job_list_makes_no_calls(self):
        assessor = self._assessor([GeminiProvider("g")])
        try:
            assert await assessor.assess_all([]) == {}
        finally:
            await assessor.aclose()


class TestPacingAndRetry:
    """Rate limiting is not hypothetical: measured live, an 8-job prompt is
    ~4.7k tokens against Groq's 12k tokens/minute, and two unpaced concurrent
    requests got an immediate 429 from both providers."""

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

        respx.post(GEMINI_URL).mock(side_effect=lambda request: gemini_reply(assessment_json("j0")))
        jobs = [make_job(id="j0")]
        assessor = self._assessor([GeminiProvider("g")], min_request_interval=0.3)
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

        respx.post(GEMINI_URL).mock(return_value=gemini_reply(assessment_json("j0")))
        assessor = self._assessor([GeminiProvider("g")], min_request_interval=0.0)
        try:
            started = time.monotonic()
            await assessor.assess_all([make_job(id="j0")])
            assert time.monotonic() - started < 0.3
        finally:
            await assessor.aclose()

    @respx.mock
    async def test_transient_failure_on_all_providers_is_retried(self):
        """A 429 from everyone means "wait", not "give up"."""
        gemini = respx.post(GEMINI_URL).mock(
            side_effect=[
                httpx.Response(429, text="quota"),
                gemini_reply(assessment_json("j0", 77)),
            ]
        )
        assessor = self._assessor(
            [GeminiProvider("g")], min_request_interval=0.0, retry_base_delay=0.01, max_retries=2
        )
        try:
            result = await assessor.assess_all([make_job(id="j0")])
        finally:
            await assessor.aclose()
        assert gemini.call_count == 2
        assert result["j0"].score == 77

    @respx.mock
    async def test_permanent_failure_is_not_retried(self):
        """A bad API key fails identically forever — retrying just delays the
        fallback and wastes the user's time."""
        gemini = respx.post(GEMINI_URL).mock(return_value=httpx.Response(401, text="bad key"))
        assessor = self._assessor(
            [GeminiProvider("g")], min_request_interval=0.0, retry_base_delay=0.01, max_retries=3
        )
        try:
            result = await assessor.assess_all([make_job(id="j0")])
        finally:
            await assessor.aclose()
        assert gemini.call_count == 1  # tried once, not four times
        assert result == {}

    @pytest.mark.parametrize(
        ("status", "transient"),
        [(429, True), (503, True), (500, True), (401, False), (400, False), (403, False)],
    )
    @respx.mock
    async def test_status_codes_are_classified_correctly(self, status, transient):
        respx.post(GEMINI_URL).mock(return_value=httpx.Response(status, text="x"))
        provider = GeminiProvider("k")
        try:
            with pytest.raises(LlmError) as excinfo:
                await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()
        assert excinfo.value.transient is transient

    @respx.mock
    async def test_malformed_json_is_transient(self):
        """Seen live from Gemini. The same prompt often parses on a retry."""
        respx.post(GEMINI_URL).mock(return_value=gemini_reply('{"assessments": [{"a":,}]}'))
        provider = GeminiProvider("k")
        try:
            with pytest.raises(LlmError) as excinfo:
                await provider.assess([make_job(id="a")], **PROMPT_KWARGS)
        finally:
            await provider.aclose()
        assert excinfo.value.transient is True

    @respx.mock
    async def test_repeated_failures_are_deduplicated_in_the_summary(self):
        """Ten batches hitting one 429 is one problem, not ten lines."""
        respx.post(GEMINI_URL).mock(return_value=httpx.Response(429, text="quota exceeded"))
        jobs = [make_job(id=f"j{i}", url=f"https://e.de/{i}") for i in range(5)]
        assessor = self._assessor(
            [GeminiProvider("g")], min_request_interval=0.0, retry_base_delay=0.0, max_retries=0
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
        lines = a.explanation("gemini")
        assert "gemini" in lines[0]
        assert "88" in lines[0]
        assert "HiWi in ML." in lines[0]

    def test_explanation_surfaces_hard_rejects(self):
        a = JobAssessment(job_id="a", score=5, requires_completed_phd=True, is_job_posting=False)
        text = " ".join(a.explanation("groq"))
        assert "completed PhD" in text
        assert "not an individual job posting" in text
