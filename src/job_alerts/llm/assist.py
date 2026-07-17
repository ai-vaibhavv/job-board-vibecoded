"""Dashboard LLM helpers: translation and resume keyword extraction.

Same self-hosted, OpenAI-compatible endpoint as `ColabProvider` — one `httpx`
POST to `{colab_base_url}/v1/chat/completions`, read `choices[0].message.content`
— but for two dashboard-only jobs the assessment pipeline never needed:

  * `translate_job_text` — turn a German posting into English AND write a fresh,
    uniform English card blurb from that translation, so a published card reads
    consistently with what the dashboard shows.
  * `extract_search_terms` — read an uploaded resume into a handful of search
    keywords/topics.

Both are best-effort. The endpoint is an ephemeral tunnel that is often down,
so every function here returns None / empty rather than raising: the dashboard
then falls back to the original German, or to whatever the user typed.
"""

from __future__ import annotations

import logging

import httpx

from ..config import LlmSettings, Secrets
from .providers import _extract_json

logger = logging.getLogger(__name__)

# Translation input/output caps now live in LlmSettings (translate_max_input_chars
# / translate_max_output_tokens) so they can be tuned to the model's context
# without a code change. The resume path keeps its own module-level cap.
_MAX_RESUME_INPUT_CHARS = 6000
_MAX_KEYWORDS = 5


async def endpoint_online(llm: LlmSettings, secrets: Secrets, *, timeout: float = 5.0) -> bool:
    """Is the self-hosted LLM actually serving the OpenAI API right now?

    A GET to `/v1/models`, which both vLLM and Ollama answer with 200 when up.
    An ephemeral tunnel that is down raises a connection error (→ False); a live
    server returns its model list (→ True). Used by the container's startup gate
    to wait for the Colab notebook before bringing the dashboard up.
    """
    base = llm.colab_base_url.strip()
    if not base:
        return False
    url = f"{base.rstrip('/')}/v1/models"
    headers = {}
    key = secrets.colab_api_key.strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


async def _chat(
    llm: LlmSettings,
    secrets: Secrets,
    system: str,
    user: str,
    *,
    max_tokens: int,
) -> str | None:
    """One chat-completions call, or None on any failure.

    Deliberately swallows everything: the caller's contract is "best-effort",
    and a dead tunnel must degrade the dashboard, not crash a handler.
    """
    base = llm.colab_base_url.strip()
    if not base:
        logger.debug("no colab_base_url configured; assist call skipped")
        return None

    root = base.rstrip("/")
    headers = {"Content-Type": "application/json"}
    key = secrets.colab_api_key.strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    # A reasoning model (Qwen3 via Ollama) returns EMPTY content on the /v1 layer —
    # it spends its budget in a hidden `reasoning` field. Ollama's native /api/chat
    # with think:false fixes it, so honour `disable_thinking` here exactly as
    # ColabProvider does; otherwise translation and résumé extraction quietly fail.
    if llm.disable_thinking:
        endpoint = f"{root}/api/chat"
        body: dict = {
            "model": llm.colab_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "think": False,
            "options": {"temperature": 0.0, "num_predict": max_tokens},
        }
    else:
        endpoint = f"{root}/v1/chat/completions"
        body = {
            "model": llm.colab_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }

    try:
        async with httpx.AsyncClient(timeout=llm.timeout) as client:
            response = await client.post(endpoint, json=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.info("assist request failed: %s", exc)
        return None

    if response.status_code != 200:
        logger.info("assist endpoint returned HTTP %s", response.status_code)
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.info("assist endpoint returned non-JSON")
        return None

    if llm.disable_thinking:
        return (payload.get("message") or {}).get("content") or ""
    choices = payload.get("choices") or []
    if not choices:
        return None
    return (choices[0].get("message") or {}).get("content") or ""


_TRANSLATE_SYSTEM = (
    "You are a precise German-to-English translator for job postings. You never "
    "invent facts and you reply with valid JSON only — no prose, no markdown fences."
)


async def translate_job_text(
    text: str, llm: LlmSettings, secrets: Secrets
) -> dict | None:
    """Translate a German posting to English and write a matching card blurb.

    Returns `{"description_en", "card_summary_en", "truncated"}`, or None when
    the endpoint is unavailable or the reply is unusable. `truncated` reports
    that the input was clipped to `llm.translate_max_input_chars` before sending,
    so the UI can label the result an excerpt.
    """
    if not text or not text.strip():
        return None

    max_input = llm.translate_max_input_chars
    truncated = len(text) > max_input
    clipped = text[:max_input]

    user = (
        "Translate this German job posting into natural English. Then write a "
        "card_summary: EXACTLY two plain sentences (no markdown, no line breaks, "
        "max 240 characters) describing what the role is and who it suits, based "
        "only on the translation.\n\n"
        "Return JSON with exactly this shape and nothing else:\n"
        '{"translation": "<the English translation>", '
        '"card_summary": "<two-sentence English blurb>"}\n\n'
        f"POSTING:\n{clipped}"
    )

    reply = await _chat(
        llm, secrets, _TRANSLATE_SYSTEM, user, max_tokens=llm.translate_max_output_tokens
    )
    if reply is None:
        return None

    try:
        parsed = _extract_json(reply)
    except Exception as exc:  # LlmError or anything else — best-effort
        logger.info("could not parse translation reply: %s", exc)
        return None

    description_en = (parsed.get("translation") or "").strip()
    if not description_en:
        return None
    card_summary_en = (parsed.get("card_summary") or "").strip() or None
    return {
        "description_en": description_en,
        "card_summary_en": card_summary_en,
        "truncated": truncated,
    }


_RESUME_SYSTEM = (
    "You extract job-search terms from a resume. You reply with valid JSON only — "
    "no prose, no markdown fences."
)


async def extract_search_terms(
    resume_text: str, llm: LlmSettings, secrets: Secrets
) -> dict:
    """Read a resume into search keywords/topics.

    Always returns `{"keywords": [...], "topics": [...]}` (empty lists on any
    failure), so the caller can merge it into whatever the user typed without a
    None check. Keywords are capped so a resume cannot explode the search-query
    fan-out downstream.
    """
    empty = {"keywords": [], "topics": []}
    if not resume_text or not resume_text.strip():
        return empty

    user = (
        "From this resume, extract the person's core technical fields for a job "
        "search. Return JSON with exactly this shape and nothing else:\n"
        '{"keywords": ["short search phrases, e.g. \\"computer vision\\", '
        '\\"reinforcement learning\\""], "topics": ["broader field labels"]}\n'
        f"At most {_MAX_KEYWORDS} keywords, most important first.\n\n"
        f"RESUME:\n{resume_text[:_MAX_RESUME_INPUT_CHARS]}"
    )

    reply = await _chat(llm, secrets, _RESUME_SYSTEM, user, max_tokens=400)
    if reply is None:
        return empty

    try:
        parsed = _extract_json(reply)
    except Exception as exc:
        logger.info("could not parse resume reply: %s", exc)
        return empty

    def _clean(value: object) -> list[str]:
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",")]
        if not isinstance(value, list):
            return []
        return [str(v).strip() for v in value if str(v).strip()]

    return {
        "keywords": _clean(parsed.get("keywords"))[:_MAX_KEYWORDS],
        "topics": _clean(parsed.get("topics")),
    }


PROFILE_PROMPT_VERSION = 1
"""Bump when a change here would change the extracted profile shape, so a stored
profile records which rubric produced it (Phase 3, mirrors PROMPT_VERSION)."""

_MAX_PROFILE_INPUT_CHARS = 12000

_PROFILE_SYSTEM = (
    "You extract a structured academic profile from a résumé. You NEVER invent "
    "anything: a fact that is not in the résumé stays absent. You do not guess a "
    "GPA, a publication, a skill or a date the person did not state. You reply with "
    "valid JSON only — no prose, no markdown fences."
)


async def extract_profile(resume_text: str, llm: LlmSettings, secrets: Secrets) -> dict | None:
    """Read a résumé into a structured `AcademicProfile`-shaped dict.

    Best-effort: returns None when the endpoint is down or the reply is unusable,
    so the caller can fall back to an empty, hand-editable profile. The prompt is
    strict about never fabricating — an unknown field comes back empty, and the
    user fills it in themselves.
    """
    if not resume_text or not resume_text.strip():
        return None

    user = (
        "Extract a structured academic profile from this résumé. Use ONLY facts "
        "present in the text; leave anything not stated empty. Return JSON with "
        "exactly this shape and nothing else:\n"
        "{\n"
        '  "name": "", "headline": "", "summary": "",\n'
        '  "research_interests": [],\n'
        '  "education": [{"degree":"","level":"bachelor|master|phd|other",'
        '"institution":"","field":"","start":"","end":"","grade":""}],\n'
        '  "experience": [{"title":"","organization":"",'
        '"kind":"work|research|teaching|other","description":"","start":"","end":""}],\n'
        '  "projects": [{"name":"","description":"","technologies":[]}],\n'
        '  "publications": [], "awards": [],\n'
        '  "skills": {"programming":[],"technical":[],"research_methods":[],"languages":[]},\n'
        '  "links": {"github":"","scholar":"","orcid":"","portfolio":"","linkedin":"","email":""}\n'
        "}\n"
        "summary: 2-3 sentences in the third person, only from the résumé.\n\n"
        f"RÉSUMÉ:\n{resume_text[:_MAX_PROFILE_INPUT_CHARS]}"
    )

    reply = await _chat(llm, secrets, _PROFILE_SYSTEM, user, max_tokens=3000)
    if reply is None:
        return None
    try:
        parsed = _extract_json(reply)
    except Exception as exc:
        logger.info("could not parse profile reply: %s", exc)
        return None
    return parsed if isinstance(parsed, dict) else None


MATCH_PROMPT_VERSION = 1
"""Bump when a change here would change a match verdict, so a cached match records
which rubric produced it (Phase 4)."""

_MATCH_SYSTEM = (
    "You judge how well a student's academic profile fits a specific opportunity. "
    "EVERY claim you make must cite evidence from the profile AND/OR the posting — "
    "never invent a skill, a requirement or an experience that is not written down. "
    "You do NOT output a numeric score; you choose one honest category and back it "
    "with concrete, cited points. You reply with valid JSON only — no prose."
)


async def analyze_match(
    profile_json: str, job_block: str, llm: LlmSettings, secrets: Secrets
) -> dict | None:
    """Analyse how a profile fits one opportunity. `profile_json` is the compact
    profile; `job_block` is the posting flattened for the prompt. Best-effort:
    None when the endpoint is down or the reply is unusable."""
    user = (
        "Compare this STUDENT PROFILE against this OPPORTUNITY and judge the fit.\n"
        "Cite evidence from BOTH sides in every point (e.g. \"You built a SLAM system "
        "in ROS — the role asks for robotics software\"). Do not invent anything: if "
        "the profile lacks something the posting requires, list it under "
        "missing_requirements honestly. If the posting is thin, say so and lower "
        "confidence.\n\n"
        "category — pick ONE:\n"
        "  strong   : clearly qualified; most requirements evidenced.\n"
        "  good     : a solid fit with a few gaps.\n"
        "  stretch  : plausible but several requirements are missing.\n"
        "  unlikely : wrong level/field, or key requirements absent.\n\n"
        "Return JSON with exactly this shape and nothing else:\n"
        "{\n"
        '  "category": "strong|good|stretch|unlikely",\n'
        '  "summary": "one honest sentence",\n'
        '  "strong_matches": [], "partial_matches": [], "missing_requirements": [],\n'
        '  "relevant_projects": [], "relevant_experience": [], "relevant_skills": [],\n'
        '  "suggested_emphasis": [], "concerns": [],\n'
        '  "level_compatible": true, "language_compatible": true,\n'
        '  "confidence": "low|medium|high"\n'
        "}\n\n"
        f"STUDENT PROFILE:\n{profile_json}\n\n"
        f"OPPORTUNITY:\n{job_block}"
    )
    reply = await _chat(llm, secrets, _MATCH_SYSTEM, user, max_tokens=2000)
    if reply is None:
        return None
    try:
        parsed = _extract_json(reply)
    except Exception as exc:
        logger.info("could not parse match reply: %s", exc)
        return None
    return parsed if isinstance(parsed, dict) else None
