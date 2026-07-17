"""The self-hosted (Colab) LLM provider.

Called over plain HTTP rather than through a vendor SDK: the request shape is a
dozen lines, and an SDK would add a dependency tree, an auth model and a release
cadence to a project whose whole point is that it keeps working unattended.

The provider owns its httpx client with its own (longer) timeout — an LLM call
routinely takes 10-30s, where a job page takes under one. It exposes two calls:
`assess` (Pass 1: relevance + score) and `classify_details` (Pass 2: taxonomy).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from ..models import Job
from .base import JobAssessment, LlmError, OpportunityDetail
from .prompt import (
    DETAIL_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_detail_prompt,
    build_user_prompt,
)

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


def parse_details(
    payload: dict[str, Any], jobs: list[Job], provider: str
) -> list[OpportunityDetail]:
    """Validate a Pass-2 reply and align it to the jobs by `job_id`.

    Same match-by-id discipline as `parse_assessments`. Unlike Pass 1 this is pure
    best-effort, so an empty result is returned quietly rather than raised — the
    caller keeps the jobs at their default taxonomy values.
    """
    raw = payload.get("details")
    if raw is None and isinstance(payload.get("assessments"), list):
        raw = payload["assessments"]  # occasional key drift
    if not isinstance(raw, list):
        return []

    wanted = {job.id for job in jobs}
    by_id: dict[str, OpportunityDetail] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            detail = OpportunityDetail.model_validate(item)
        except Exception as exc:
            logger.debug("%s: skipping unparseable detail: %s", provider, exc)
            continue
        if detail.job_id in wanted:
            by_id[detail.job_id] = detail
    return list(by_id.values())


class ColabProvider:
    """A self-hosted, OpenAI-compatible model — e.g. Qwen2.5-7B served by Ollama
    on Google Colab and reached through an ngrok tunnel. See docs/colab-model.md.

    It issues an ordinary chat-completions request and reads the reply from
    `choices[0].message.content`, with two deliberate choices:

    * `endpoint` is built from a per-session base URL (the tunnel address changes
      every time the notebook restarts), not a fixed class constant.
    * `response_format` is omitted. Some local servers do not support JSON mode,
      and `_extract_json` already recovers JSON from a plain-text or fenced
      reply — so depending on the field would only add a way to fail.

    `api_key` is optional: a private tunnel usually needs no auth (Ollama checks
    none). When empty, no Authorization header is sent.
    """

    name = "colab"

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        model: str = "Qwen/Qwen2.5-7B-Instruct-AWQ",
        timeout: float = 60.0,
        max_output_tokens: int = 0,
        disable_thinking: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/v1/chat/completions"
        self.native_endpoint = f"{self.base_url}/api/chat"
        self.api_key = api_key
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.disable_thinking = disable_thinking
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _chat(self, system: str, user: str) -> str:
        """One chat round-trip; returns the reply text.

        Shared by Pass 1 (`assess`) and Pass 2 (`classify_details`) — the only
        difference between the two calls is which prompt goes in.

        A reasoning model (e.g. a Qwen3 served by Ollama) spends its whole token
        budget in a separate `reasoning` field and leaves `content` EMPTY, so
        every batch silently falls back to keyword scoring. Ollama's OpenAI-compat
        `/v1` layer ignores any request to disable that, but its NATIVE `/api/chat`
        honours `think: false` — which turns a 200s empty reply into a 2s JSON one.
        So when `disable_thinking` is set we speak the native protocol instead.
        """
        if self.disable_thinking:
            return await self._chat_native(system, user)
        return await self._chat_openai(system, user)

    async def _chat_openai(self, system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
        }
        if self.max_output_tokens > 0:
            body["max_tokens"] = self.max_output_tokens
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = await self._client.post(self.endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LlmError(f"colab request failed: {exc}", transient=True) from exc

        if response.status_code != 200:
            raise _http_error("colab", response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmError(f"colab returned non-JSON: {exc}") from exc

        choices = payload.get("choices") or []
        if not choices:
            raise LlmError("colab returned no choices")
        return (choices[0].get("message") or {}).get("content") or ""

    async def _chat_native(self, system: str, user: str) -> str:
        """Ollama's native `/api/chat`, with reasoning turned off."""
        options: dict[str, Any] = {"temperature": 0.0}
        if self.max_output_tokens > 0:
            options["num_predict"] = self.max_output_tokens
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": False,
            "options": options,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = await self._client.post(self.native_endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LlmError(f"colab request failed: {exc}", transient=True) from exc

        if response.status_code != 200:
            raise _http_error("colab", response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmError(f"colab returned non-JSON: {exc}") from exc

        return (payload.get("message") or {}).get("content") or ""

    async def assess(self, jobs: list[Job], **prompt_kwargs: Any) -> list[JobAssessment]:
        prompt = build_user_prompt(jobs, **prompt_kwargs)
        text = await self._chat(SYSTEM_PROMPT, prompt)
        return parse_assessments(_extract_json(text), jobs, self.name)

    async def classify_details(
        self, jobs: list[Job], **prompt_kwargs: Any
    ) -> list[OpportunityDetail]:
        # Pass 2 only takes max_description_chars; drop any Pass-1-only kwargs.
        max_chars = prompt_kwargs.get("max_description_chars", 1500)
        prompt = build_detail_prompt(jobs, max_description_chars=max_chars)
        text = await self._chat(DETAIL_SYSTEM_PROMPT, prompt)
        return parse_details(_extract_json(text), jobs, self.name)


def _error_excerpt(response: httpx.Response) -> str:
    """A short, key-free excerpt of an error body.

    The API key travels in a header, never the body, so nothing secret can be in
    here — but it is capped anyway to keep logs readable.
    """
    try:
        return response.text.replace("\n", " ")[:300]
    except Exception:  # pragma: no cover
        return "<unreadable body>"
