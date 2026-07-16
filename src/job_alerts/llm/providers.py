"""Gemini and Groq providers.

Both are called over plain HTTP rather than through a vendor SDK: the request
shape is a dozen lines each, and two SDKs would add two dependency trees, two
auth models and two release cadences to a project whose whole point is that it
keeps working unattended.

Each provider owns its httpx client with its own (longer) timeout — an LLM call
routinely takes 10-30s, where a job page takes under one.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..models import Job
from .base import JobAssessment, LlmError
from .prompt import SYSTEM_PROMPT, build_user_prompt

logger = logging.getLogger(__name__)

# Retrying these gets a different answer; retrying anything else does not.
# 429 = rate limited, 503 = "model is experiencing high demand" (seen live from
# Gemini), 500/502/504 = server-side blips.
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _http_error(provider: str, response: httpx.Response) -> LlmError:
    status = response.status_code
    return LlmError(
        f"{provider} HTTP {status}: {_error_excerpt(response)}",
        transient=status in _TRANSIENT_STATUS,
    )


# Some models wrap JSON in a markdown fence despite being told not to.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """Parse a model's reply into JSON, tolerating the usual sloppiness."""
    if not text or not text.strip():
        raise LlmError("model returned an empty response")

    cleaned = text.strip()
    if match := _FENCE_RE.match(cleaned):
        cleaned = match.group(1)

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: pull the outermost JSON object out of surrounding prose.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LlmError(f"model did not return JSON: {cleaned[:200]!r}") from None
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            # Non-deterministic: the same prompt often parses on a retry.
            raise LlmError(f"model returned malformed JSON: {exc}", transient=True) from exc

    if not isinstance(parsed, dict):
        raise LlmError(f"expected a JSON object, got {type(parsed).__name__}", transient=True)
    return parsed


def parse_assessments(
    payload: dict[str, Any], jobs: list[Job], provider: str
) -> list[JobAssessment]:
    """Validate the reply and align it to the jobs we asked about.

    Matching is by `job_id`, never by position: models drop, merge and reorder
    array entries, and positional matching would silently attach one job's score
    to another — a far worse failure than a missing assessment.
    """
    raw = payload.get("assessments")
    if raw is None and isinstance(payload.get("jobs"), list):
        raw = payload["jobs"]  # occasional key drift
    if not isinstance(raw, list):
        raise LlmError(f"reply had no 'assessments' array (keys: {list(payload)[:5]})")

    wanted = {job.id for job in jobs}
    by_id: dict[str, JobAssessment] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            assessment = JobAssessment.model_validate(item)
        except Exception as exc:
            logger.debug("%s: skipping unparseable assessment: %s", provider, exc)
            continue
        if assessment.job_id in wanted:
            by_id[assessment.job_id] = assessment
        else:
            logger.debug("%s: assessment for unknown job_id %r", provider, assessment.job_id)

    if not by_id:
        raise LlmError(f"{provider} returned no usable assessments for {len(jobs)} job(s)")

    missing = wanted - set(by_id)
    if missing:
        # Partial results are still worth having; the caller scores the rest
        # with the keyword fallback.
        logger.warning(
            "%s did not assess %d/%d job(s); they fall back to keyword scoring",
            provider,
            len(missing),
            len(jobs),
        )
    return list(by_id.values())


class GeminiProvider:
    """Google Gemini via the Generative Language API.

    Uses the `x-goog-api-key` header rather than the `?key=` query parameter the
    quickstart shows: a key in a URL leaks into logs, proxies and error
    messages. Same reason as Tavily's Bearer auth.
    """

    name = "gemini"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-2.5-flash",
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    @property
    def endpoint(self) -> str:
        return (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def assess(self, jobs: list[Job], **prompt_kwargs: Any) -> list[JobAssessment]:
        prompt = build_user_prompt(jobs, **prompt_kwargs)
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                # Deterministic: the same posting must not score 80 today and 55
                # tomorrow, or dedup/threshold behaviour becomes unexplainable.
                "temperature": 0.0,
                "responseMimeType": "application/json",
                "maxOutputTokens": 8192,
            },
        }
        try:
            response = await self._client.post(
                self.endpoint,
                json=body,
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            # Network trouble is by definition worth another go.
            raise LlmError(f"gemini request failed: {exc}", transient=True) from exc

        if response.status_code != 200:
            raise _http_error("gemini", response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmError(f"gemini returned non-JSON: {exc}") from exc

        candidates = payload.get("candidates") or []
        if not candidates:
            # Usually a safety block or an empty generation.
            feedback = payload.get("promptFeedback", {})
            raise LlmError(f"gemini returned no candidates (feedback: {feedback})", transient=True)

        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        return parse_assessments(_extract_json(text), jobs, self.name)


class GroqProvider:
    """Groq, via its OpenAI-compatible chat-completions endpoint."""

    name = "groq"
    endpoint = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "llama-3.3-70b-versatile",
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def assess(self, jobs: list[Job], **prompt_kwargs: Any) -> list[JobAssessment]:
        prompt = build_user_prompt(jobs, **prompt_kwargs)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        try:
            response = await self._client.post(
                self.endpoint,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise LlmError(f"groq request failed: {exc}", transient=True) from exc

        if response.status_code != 200:
            raise _http_error("groq", response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmError(f"groq returned non-JSON: {exc}") from exc

        choices = payload.get("choices") or []
        if not choices:
            raise LlmError("groq returned no choices")
        text = (choices[0].get("message") or {}).get("content") or ""
        return parse_assessments(_extract_json(text), jobs, self.name)


def _error_excerpt(response: httpx.Response) -> str:
    """A short, key-free excerpt of an error body.

    The API key travels in a header, never the body, so nothing secret can be in
    here — but it is capped anyway to keep logs readable.
    """
    try:
        return response.text.replace("\n", " ")[:300]
    except Exception:  # pragma: no cover
        return "<unreadable body>"
