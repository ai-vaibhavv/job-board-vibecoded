"""Discord webhook notifier.

Three rules shape this module:

1. **Never claim delivery that did not happen.** Jobs are reported as delivered
   only after Discord returns 2xx for the message containing them. A partial
   failure marks only the batches that actually landed, so the rest are retried
   next run rather than lost.
2. **Never exceed Discord's limits.** They are hard API errors, not warnings.
   Every string is truncated to its documented cap before sending.
3. **Never trust job text.** Titles and descriptions come from third-party
   websites, so markdown and mention syntax are neutralised before they reach a
   channel.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Any

import httpx

from ..config import NotificationSettings
from ..models import Job
from .base import DeliveryResult

logger = logging.getLogger(__name__)

# Documented Discord limits. Exceeding any of them is a 400.
MAX_EMBEDS_PER_MESSAGE = 10
MAX_EMBED_TITLE = 256
MAX_EMBED_DESCRIPTION = 4096
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FOOTER_TEXT = 2048
MAX_EMBED_TOTAL = 6000
MAX_CONTENT = 2000
MAX_FIELDS_PER_EMBED = 25

# The card description is capped to one uniform length for EVERY job, whether it
# comes from the LLM's `card_summary` or a fallback excerpt. This is the fix for
# "some descriptions were huge": a card is a scannable teaser, not the posting.
# Well under Discord's 4096 embed-description cap on purpose.
CARD_SUMMARY_MAX = 300

# Known employers -> the domain whose favicon is their recognisable logo. A job
# URL's own host is often a job-board subdomain (jobs.fraunhofer.de,
# stellenticket.de) whose favicon is generic or missing, so for the employers we
# see most we pin the canonical domain. Keys are matched as case-folded
# substrings of the organization name, so only unambiguous ones are listed
# (bare "tum"/"kit" would match inside unrelated words).
_ORG_LOGO_DOMAINS: dict[str, str] = {
    "fraunhofer": "fraunhofer.de",
    "max planck": "mpg.de",
    "max-planck": "mpg.de",
    "technische universität münchen": "tum.de",
    "tu münchen": "tum.de",
    "technische universität berlin": "tu-berlin.de",
    "tu berlin": "tu-berlin.de",
    "dfki": "dfki.de",
    "helmholtz": "helmholtz.de",
    "karlsruher institut": "kit.edu",
    "rwth": "rwth-aachen.de",
}


def _favicon(domain: str) -> str:
    """Google's favicon service — a stable, no-auth logo for any domain."""
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"


def org_image_url(job: Job) -> str | None:
    """A small logo for the card: a curated domain for employers we recognise,
    otherwise the favicon of the posting's own host. None only when the job URL
    has no usable host at all."""
    org = (job.organization or "").casefold()
    for needle, domain in _ORG_LOGO_DOMAINS.items():
        if needle in org:
            return _favicon(domain)
    netloc = urllib.parse.urlparse(job.url).netloc.removeprefix("www.")
    return _favicon(netloc) if netloc else None

# Score -> embed colour. Purely cosmetic, but makes a channel scannable.
_COLOR_EXCELLENT = 0x2ECC71  # green,  >= 80
_COLOR_GOOD = 0x3498DB  # blue,   >= 65
_COLOR_OK = 0xF39C12  # orange, below that

_MARKDOWN_RE = re.compile(r"([*_~`>|\\])")
_MENTION_RE = re.compile(r"@(everyone|here)", re.IGNORECASE)


def sanitize(text: str | None) -> str:
    """Neutralise markdown and mass mentions from third-party text.

    A job description containing `@everyone` must not ping a server, and stray
    backticks must not wreck the embed. Escaping beats stripping: the text
    stays readable.
    """
    if not text:
        return ""
    cleaned = _MENTION_RE.sub(lambda m: f"@​{m.group(1)}", text)
    cleaned = _MARKDOWN_RE.sub(r"\\\1", cleaned)
    return " ".join(cleaned.split())


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _embed_length(embed: dict[str, Any]) -> int:
    """Discord's 6000-char budget counts these fields together."""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    total += len(embed.get("author", {}).get("name", ""))
    for field in embed.get("fields", []):
        total += len(field.get("name", "")) + len(field.get("value", ""))
    return total


def _color_for(score: int) -> int:
    if score >= 80:
        return _COLOR_EXCELLENT
    if score >= 65:
        return _COLOR_GOOD
    return _COLOR_OK


# Acronyms that should not be title-cased when a taxonomy value is displayed.
_LABEL_ACRONYMS = {
    "ml": "ML", "ai": "AI", "nlp": "NLP", "hiwi": "HiWi", "ee": "EE", "me": "ME",
    "phd": "PhD",
}


def _pretty_label(value: str) -> str:
    """Turn a taxonomy value ("master_thesis", "ml") into a display label
    ("Master Thesis", "ML"). Purely cosmetic; the stored value is unchanged."""
    words = value.replace("_", " ").split()
    return " ".join(_LABEL_ACRONYMS.get(w, w.capitalize()) for w in words)


def build_embed(job: Job, settings: NotificationSettings) -> dict[str, Any]:
    """One job -> one Discord embed, guaranteed within limits."""
    title = truncate(sanitize(job.title) or "(untitled position)", MAX_EMBED_TITLE)

    # The LLM's uniform blurb when we have one, else a trimmed posting excerpt —
    # both capped to the SAME length so every card reads consistently and a
    # thousand-character description never lands in the channel.
    raw_summary = job.card_summary or job.short_description(settings.description_excerpt_chars)
    description = truncate(sanitize(raw_summary), CARD_SUMMARY_MAX)

    fields: list[dict[str, Any]] = []

    def add_field(name: str, value: str, inline: bool = True) -> None:
        if value and len(fields) < MAX_FIELDS_PER_EMBED:
            fields.append(
                {
                    "name": truncate(name, MAX_FIELD_NAME),
                    "value": truncate(value, MAX_FIELD_VALUE),
                    "inline": inline,
                }
            )

    # Organization is shown in the embed `author` block (with its logo) instead
    # of a field, so it is not repeated here.
    # LabScout academic taxonomy first — it is the whole point of the board.
    if job.opportunity_type:
        add_field("🎓 Opportunity", _pretty_label(job.opportunity_type))
    if job.applicant_level:
        add_field("🧑‍🎓 Level", _pretty_label(job.applicant_level))
    if job.academic_field:
        add_field("🔬 Field", _pretty_label(job.academic_field))
    add_field("📍 Location", sanitize(job.location) or "—")
    add_field("⭐ Relevance", f"{job.relevance_score}/100")
    add_field("🔎 Source", sanitize(job.source))

    when = job.published_at or job.discovered_at
    label = "📅 Published" if job.published_at else "📅 Discovered"
    add_field(label, f"<t:{int(when.timestamp())}:R>")

    if job.application_deadline:
        add_field("⏳ Deadline", f"<t:{int(job.application_deadline.timestamp())}:D>")
    if job.employment_type:
        add_field("💼 Type", sanitize(job.employment_type))
    if job.salary:
        add_field("💶 Salary", sanitize(job.salary))

    # How to apply, when the posting named a person rather than an apply button.
    # Shown, never used: the app does not send anything on your behalf. An
    # untailored auto-application to a research lab reads as automated, gets
    # ignored, and burns a contact you only get one of.
    if job.contact_email:
        add_field("✉️ Apply to", sanitize(job.contact_email))
    if job.contact_url:
        add_field("📝 Apply via", sanitize(job.contact_url), inline=False)

    if job.matched_keywords:
        add_field("🏷️ Matched", sanitize(", ".join(job.matched_keywords[:6])), inline=False)

    embed: dict[str, Any] = {
        "title": title,
        "url": job.url,
        "description": description,
        "color": _color_for(job.relevance_score),
        "fields": fields,
        "footer": {
            "text": truncate(f"{job.source} • score {job.relevance_score}/100", MAX_FOOTER_TEXT)
        },
    }
    if job.published_at:
        embed["timestamp"] = job.published_at.isoformat()

    # Organization identity: a small logo + name at the top (author) and the same
    # logo top-right (thumbnail). Every card gets both — that uniform framing is
    # what makes the channel look like one product rather than scraped rows.
    # Image URLs do not count against the 6000-char budget; `author.name` does,
    # and `_embed_length` already accounts for it.
    image = org_image_url(job)
    author_name = sanitize(job.organization) or job.source
    embed["author"] = {"name": truncate(author_name, MAX_FIELD_NAME), "url": job.url}
    if image:
        embed["author"]["icon_url"] = image
        embed["thumbnail"] = {"url": image}

    # Last line of defence for the 6000-char budget: drop the description
    # before dropping structured fields, since fields carry the key facts.
    while _embed_length(embed) > MAX_EMBED_TOTAL and embed["description"]:
        embed["description"] = truncate(
            embed["description"], max(0, len(embed["description"]) - 500)
        )
        if len(embed["description"]) <= 1:
            embed["description"] = ""
    while _embed_length(embed) > MAX_EMBED_TOTAL and embed["fields"]:
        embed["fields"].pop()

    return embed


def build_messages(
    jobs: list[Job], settings: NotificationSettings, *, extra_stored: int = 0
) -> list[tuple[dict[str, Any], list[str]]]:
    """Batch jobs into webhook payloads.

    Returns (payload, job_ids) pairs so the caller knows exactly which jobs a
    given message carries — that mapping is what makes per-batch delivery
    marking honest.
    """
    per_message = min(settings.embeds_per_message, MAX_EMBEDS_PER_MESSAGE)
    messages: list[tuple[dict[str, Any], list[str]]] = []

    for index in range(0, len(jobs), per_message):
        batch = jobs[index : index + per_message]
        embeds = [build_embed(job, settings) for job in batch]
        header = (
            f"🎓 **{len(jobs)} new research position(s) in Germany**"
            if index == 0
            else f"… continued ({index + 1}–{index + len(batch)} of {len(jobs)})"
        )
        payload: dict[str, Any] = {
            "content": truncate(header, MAX_CONTENT),
            "embeds": embeds,
            # Belt and braces alongside `sanitize`: even if a mention slipped
            # through, Discord is told to resolve none of them.
            "allowed_mentions": {"parse": []},
        }
        messages.append((payload, [job.id for job in batch]))

    if extra_stored > 0 and messages:
        note = (
            f"\n_+{extra_stored} more matching job(s) were stored but not sent "
            f"(max_per_run={settings.max_per_run}). See `python -m job_alerts list --new`._"
        )
        last_payload = messages[-1][0]
        last_payload["content"] = truncate(last_payload["content"] + note, MAX_CONTENT)

    return messages


class DiscordNotifier:
    """Sends jobs to a Discord channel via an incoming webhook."""

    name = "discord"

    def __init__(
        self,
        webhook_url: str,
        settings: NotificationSettings,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        timeout: float = 15.0,
    ) -> None:
        self.webhook_url = webhook_url
        self.settings = settings
        self.max_retries = max_retries
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> DiscordNotifier:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def send_jobs(self, jobs: list[Job], *, extra_stored: int = 0) -> DeliveryResult:
        result = DeliveryResult()
        if not jobs:
            return result

        for payload, job_ids in build_messages(jobs, self.settings, extra_stored=extra_stored):
            try:
                await self._post(payload)
            except Exception as exc:
                logger.error("Discord delivery failed for %d job(s): %s", len(job_ids), exc)
                result.failed_ids.extend(job_ids)
                result.errors.append(str(exc))
                # Keep going: a later batch may well succeed, and each batch is
                # marked independently.
                continue
            result.delivered_ids.extend(job_ids)
            result.messages_sent += 1

        return result

    async def send_test(self) -> bool:
        payload = {
            "content": "✅ **LabScout** — test message",
            "embeds": [
                {
                    "title": "Webhook configured correctly",
                    "description": (
                        "If you can read this, your `DISCORD_WEBHOOK_URL` works and real "
                        "job alerts will arrive in this channel."
                    ),
                    "color": _COLOR_EXCELLENT,
                    "fields": [
                        {
                            "name": "Next step",
                            "value": "`python -m job_alerts search --dry-run`",
                            "inline": False,
                        },
                    ],
                }
            ],
            "allowed_mentions": {"parse": []},
        }
        try:
            await self._post(payload)
        except Exception as exc:
            logger.error("Discord test message failed: %s", exc)
            return False
        return True

    async def _post(self, payload: dict[str, Any]) -> None:
        """POST with backoff. Raises on final failure.

        Discord's rate limit is communicated by 429 + `Retry-After`, and it is
        strict enough that honouring it is mandatory rather than polite.
        """
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = await self._client.post(self.webhook_url, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                await self._backoff(attempt, f"network error: {exc}")
                continue

            status = response.status_code
            if 200 <= status < 300:
                return

            if status == 429:
                retry_after = _retry_after_seconds(response)
                logger.info("Discord rate limited; waiting %.1fs", retry_after)
                await asyncio.sleep(min(retry_after, 60.0))
                last_error = RuntimeError("HTTP 429 rate limited")
                continue

            if 400 <= status < 500:
                # A malformed payload or a revoked webhook will fail identically
                # forever; retrying only delays the error.
                body = _safe_body(response)
                raise RuntimeError(f"Discord rejected the message (HTTP {status}): {body}")

            last_error = RuntimeError(f"HTTP {status}")
            await self._backoff(attempt, f"HTTP {status}")

        raise RuntimeError(
            f"Discord delivery failed after {self.max_retries} attempts: {last_error}"
        )

    async def _backoff(self, attempt: int, reason: str) -> None:
        if attempt >= self.max_retries:
            return
        delay = min(2.0 ** (attempt - 1), 30.0)
        logger.debug("Discord attempt %d failed (%s); retrying in %.1fs", attempt, reason, delay)
        await asyncio.sleep(delay)


def _retry_after_seconds(response: httpx.Response) -> float:
    """Discord sends `retry_after` in the JSON body and/or a Retry-After header."""
    try:
        body = response.json()
        if isinstance(body, dict) and "retry_after" in body:
            return float(body["retry_after"])
    except (ValueError, TypeError):
        pass
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return 5.0


def _safe_body(response: httpx.Response) -> str:
    """A short, secret-free excerpt of an error body for logging.

    The webhook URL is the secret here and it is never in the body, but the
    excerpt is capped anyway to keep logs readable.
    """
    try:
        return truncate(response.text.replace("\n", " "), 300)
    except Exception:  # pragma: no cover
        return "<unreadable response body>"


def render_dry_run(
    jobs: list[Job], settings: NotificationSettings, *, extra_stored: int = 0
) -> str:
    """Exactly what would be sent, as text. Makes no network call whatsoever."""
    if not jobs:
        return "No jobs would be sent."

    messages = build_messages(jobs, settings, extra_stored=extra_stored)
    lines: list[str] = [
        "",
        "──────────────────────────────────────────────────────────",
        f" DRY RUN — {len(jobs)} job(s) in {len(messages)} Discord message(s)",
        "          Nothing was sent. No request reached Discord.",
        "──────────────────────────────────────────────────────────",
    ]
    for index, (payload, _) in enumerate(messages, start=1):
        lines.append(f"\n▼ Message {index}/{len(messages)} — {len(payload['embeds'])} embed(s)")
        lines.append(f"  content: {payload['content']}")
        for embed in payload["embeds"]:
            lines.append(f"\n  ┌─ {embed['title']}")
            lines.append(f"  │  {embed['url']}")
            for field in embed["fields"]:
                lines.append(f"  │  {field['name']}: {field['value']}")
            if embed["description"]:
                lines.append(f"  │  {truncate(embed['description'], 200)}")
            lines.append(f"  └─ ({_embed_length(embed)} chars, limit {MAX_EMBED_TOTAL})")
    lines.append("")
    return "\n".join(lines)
