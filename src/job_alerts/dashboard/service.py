"""Gradio-free glue between the dashboard UI and the existing pipeline.

Every function here opens its **own** short-lived `Database` and returns plain
data (strings, lists, dicts). Two reasons:

  * `sqlite3` connections are bound to the thread that created them, and Gradio
    serves handlers from a threadpool — a shared connection would raise. Opening
    per call is cheap and WAL already lets it coexist with the scheduler.
  * Keeping this layer free of any Gradio import makes it unit-testable against a
    `:memory:` database with a mocked notifier.

Nothing here auto-sends to Discord. A dashboard search stores jobs with
`max_per_run = 0`; publishing is a separate, explicit, per-job action.
"""

from __future__ import annotations

import asyncio
import html
import io
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from ..config import (
    ConfigError,
    Secrets,
    Settings,
    SourcesConfig,
    load_settings,
    load_sources,
)
from ..database import Database
from ..enrich import apply_url_from_text, extract, outbound_links_from_text
from ..http import FetchError, PoliteClient, host_is_denied
from ..llm.assist import endpoint_online, extract_search_terms, translate_job_text
from ..models import Job, JobStatus, Language
from ..normalization import content_hash as compute_content_hash
from ..notifications.discord import (
    DiscordNotifier,
    _color_for,
    org_image_url,
)
from ..profile import AcademicProfile
from ..scheduler import RunLockedError, run_once
from .queries import build_site_queries, domains_for

logger = logging.getLogger(__name__)

_SEARCH_DISCOVERY = "search_discovery"


# ---------------------------------------------------------------------------
# Config (loaded once per process; the dashboard is a single-user tool)
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    settings: Settings
    sources: SourcesConfig
    secrets: Secrets

    @property
    def db_path(self):
        return self.settings.database.path


_config: AppConfig | None = None

# Secret fields the Settings UI may overlay (stored in app_config, applied over
# .env). The LLM tunnel URL is a *setting*, not a secret, and is handled
# separately below. Keep in step with `EDITABLE_SETTINGS`.
_OVERLAY_SECRET_FIELDS = (
    "discord_webhook_url",
    "search_api_provider",
    "search_api_key",
    "google_cse_id",
    "colab_api_key",
    "apify_token",
)


def get_config() -> AppConfig:
    global _config
    if _config is None:
        # First pass with plain env to learn the database path — the overlay
        # lives in that database, so it cannot itself change where the DB is.
        base_secrets = Secrets()
        base_settings = load_settings(secrets=base_secrets)

        overlay: dict[str, str] = {}
        try:
            with Database(base_settings.database.path) as db:
                overlay = db.get_config_overlay()
        except Exception:  # a missing/locked DB must not stop config loading
            logger.debug("could not read config overlay", exc_info=True)

        secret_overrides = {k: v for k, v in overlay.items() if k in _OVERLAY_SECRET_FIELDS}
        if secret_overrides:
            # Passing init kwargs makes pydantic-settings prefer them over .env
            # AND run field validators (e.g. the provider normalizer).
            secrets = Secrets(**secret_overrides)
            settings = load_settings(secrets=secrets)
        else:
            secrets, settings = base_secrets, base_settings

        if overlay.get("colab_base_url"):
            settings.llm.colab_base_url = overlay["colab_base_url"]

        _config = AppConfig(
            settings=settings,
            sources=load_sources(secrets=secrets),
            secrets=secrets,
        )
    return _config


def reload_config() -> None:
    """Drop the cached config so the next `get_config()` re-reads .env AND the
    overlay. Called after the Settings UI writes a secret."""
    global _config
    _config = None


def topic_choices() -> list[str]:
    return list(get_config().settings.keywords.topics)


def location_choices() -> list[str]:
    return list(get_config().settings.locations.include)


def source_choices() -> list[str]:
    with Database(get_config().db_path) as db:
        return sorted(db.stats()["by_source"].keys())


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

ROW_HEADERS = ["Score", "Title", "Organization", "Location", "Source", "Lang", "Status", "Sent"]


def _filtered_jobs(
    *,
    status: str | None = None,
    min_score: int | None = None,
    source: str | None = None,
    text: str | None = None,
    show_hidden: bool = False,
) -> list[tuple[Job, bool]]:
    """The jobs matching the dashboard filters, each paired with whether it is
    soft-hidden. The single source of truth for both the Gradio table
    (`list_rows`) and the JSON API (`list_jobs_json`) — filtering lives here once.
    """
    cfg = get_config()
    with Database(cfg.db_path) as db:
        jobs = db.list_jobs(
            status=JobStatus(status) if status else None,
            min_score=min_score,
            limit=10000,
        )
        hidden = db.hidden_ids()

    needle = (text or "").strip().casefold()
    result: list[tuple[Job, bool]] = []
    for job in jobs:
        is_hidden = job.id in hidden
        if is_hidden and not show_hidden:
            continue
        if source and job.source != source:
            continue
        if needle and needle not in _haystack(job):
            continue
        result.append((job, is_hidden))
    return result


def list_rows(
    *,
    status: str | None = None,
    min_score: int | None = None,
    source: str | None = None,
    text: str | None = None,
    show_hidden: bool = False,
) -> tuple[list[list], list[str]]:
    """Rows for the master table plus the parallel list of job ids.

    Returns (display_rows, ids). The UI keeps `ids` in state and maps a selected
    row index back to a job id — the id is never shown as a column.
    """
    rows: list[list] = []
    ids: list[str] = []
    for job, _hidden in _filtered_jobs(
        status=status, min_score=min_score, source=source, text=text, show_hidden=show_hidden
    ):
        rows.append(
            [
                job.relevance_score,
                job.title,
                job.organization or "—",
                job.location or job.city or "—",
                job.source,
                job.language.value,
                job.status.value,
                "✅" if job.notified_at else "",
            ]
        )
        ids.append(job.id)
    return rows, ids


def _job_summary(job: Job, *, hidden: bool) -> dict:
    """The compact per-job payload a job card needs — no description body."""
    return {
        "id": job.id,
        "title": job.title,
        "organization": job.organization,
        "location": job.location or job.city,
        "city": job.city,
        "country": job.country,
        "source": job.source,
        "language": job.language.value,
        "status": job.status.value,
        "relevance_score": job.relevance_score,
        "score_color": f"#{_color_for(job.relevance_score):06x}",
        "matched_keywords": job.matched_keywords,
        "opportunity_type": job.opportunity_type,
        "applicant_level": job.applicant_level,
        "academic_field": job.academic_field,
        "remote_status": job.remote_status.value,
        "published_at": job.published_at.isoformat() if job.published_at else None,
        "notified_at": job.notified_at.isoformat() if job.notified_at else None,
        "url": job.url,
        "logo": org_image_url(job),
        "hidden": hidden,
    }


def list_jobs_json(
    *,
    status: str | None = None,
    min_score: int | None = None,
    source: str | None = None,
    text: str | None = None,
    show_hidden: bool = False,
) -> list[dict]:
    """The filtered job list as JSON-ready summary dicts for the React UI."""
    return [
        _job_summary(job, hidden=hidden)
        for job, hidden in _filtered_jobs(
            status=status, min_score=min_score, source=source, text=text, show_hidden=show_hidden
        )
    ]


def _haystack(job: Job) -> str:
    return " ".join(
        filter(None, [job.title, job.organization, job.location, job.city, job.source])
    ).casefold()


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def translate_job(job_id: str) -> dict | None:
    """Cache-first English translation for a job. Returns the cached/new dict
    (`description_en`, `card_summary_en`, `truncated`) or None if unavailable."""
    cfg = get_config()
    with Database(cfg.db_path) as db:
        job = db.get(job_id)
        if job is None:
            return None
        cached = db.get_translation(job_id, job.content_hash)
        if cached is not None:
            return cached
        if not (job.description or "").strip():
            return None

    result = asyncio.run(translate_job_text(job.description or "", cfg.settings.llm, cfg.secrets))
    if result is None:
        return None

    with Database(cfg.db_path) as db:
        db.save_translation(
            job_id,
            job.content_hash,
            result["description_en"],
            result.get("card_summary_en"),
            result.get("truncated", False),
        )
    return result


# ---------------------------------------------------------------------------
# Detail card
# ---------------------------------------------------------------------------


@dataclass
class Detail:
    html: str
    needs_confirm: bool
    confirm_label: str
    is_german: bool
    exists: bool = True


def job_detail(job_id: str | None) -> Detail:
    """Full detail card for one job. For a German job this auto-translates
    (cache-first), rendering English first with the German original collapsible."""
    if not job_id:
        return Detail(html="<p>Select a job to see details.</p>", needs_confirm=False,
                      confirm_label="", is_german=False, exists=False)

    cfg = get_config()
    with Database(cfg.db_path) as db:
        job = db.get(job_id)
    if job is None:
        return Detail(html="<p>Job not found.</p>", needs_confirm=False,
                      confirm_label="", is_german=False, exists=False)

    is_german = job.language == Language.DE
    translation = translate_job(job_id) if is_german else None

    needs_confirm = False
    confirm_label = ""
    if job.status == JobStatus.REJECTED:
        needs_confirm = True
        reason = _rejection_reason(job)
        confirm_label = f"⚠️ This job was filtered out ({reason}). Tick to publish anyway."
    elif job.notified_at is not None:
        needs_confirm = True
        when = job.notified_at.strftime("%Y-%m-%d %H:%M")
        confirm_label = f"⚠️ Already sent on {when}. Tick to send again."

    return Detail(
        html=_detail_html(job, translation),
        needs_confirm=needs_confirm,
        confirm_label=confirm_label,
        is_german=is_german,
    )


def job_detail_json(job_id: str | None) -> dict:
    """Structured detail for one job, for the React UI to render itself.

    Read-only and LLM-independent: it returns the *cached* translation if one
    exists but never triggers a live translation or page fetch — that is what
    `refresh_job_detail` is for, which the UI calls on open. Includes the last
    on-demand fetch outcome (link status, best apply link) so the UI can render
    the posting link and any "expired" state without another round trip.
    """
    if not job_id:
        return {"exists": False}

    cfg = get_config()
    with Database(cfg.db_path) as db:
        job = db.get(job_id)
        if job is None:
            return {"exists": False}
        is_german = job.language == Language.DE
        translation = db.get_translation(job_id, job.content_hash) if is_german else None
        fetch = db.get_detail_fetch(job_id)
        hidden = job_id in db.hidden_ids()

    needs_confirm = False
    confirm_label = ""
    rejection_reason: str | None = None
    if job.status == JobStatus.REJECTED:
        needs_confirm = True
        rejection_reason = _rejection_reason(job)
        confirm_label = f"⚠️ This job was filtered out ({rejection_reason}). Tick to publish anyway."
    elif job.notified_at is not None:
        needs_confirm = True
        when = job.notified_at.strftime("%Y-%m-%d %H:%M")
        confirm_label = f"⚠️ Already sent on {when}. Tick to send again."

    data = job.model_dump(mode="json")
    data["logo"] = org_image_url(job)
    data["score_color"] = f"#{_color_for(job.relevance_score):06x}"

    link_status = fetch["link_status"] if fetch else None
    apply_url = (fetch["apply_url"] if fetch and fetch.get("apply_url") else None) or job.url

    return {
        "exists": True,
        "job": data,
        "translation": translation,
        "is_german": is_german,
        "translation_unavailable": is_german and translation is None,
        "needs_confirm": needs_confirm,
        "confirm_label": confirm_label,
        "rejection_reason": rejection_reason,
        "hidden": hidden,
        "link_status": link_status,
        "apply_url": apply_url,
        "alternate_links": fetch["alternate_links"] if fetch else [],
        "detail_fetched_at": fetch["fetched_at"] if fetch else None,
    }


# ---------------------------------------------------------------------------
# On-demand full fetch + link check
# ---------------------------------------------------------------------------

_DEAD_STATUSES = {404, 410}
_MAX_DETAIL_DESCRIPTION_CHARS = 20_000


def _candidate_links(job: Job) -> list[str]:
    """Alternate apply links for a job whose primary URL is dead: an apply form
    the source recorded, a form URL named in the description, then any other
    outbound link in the text. Ordered, de-duplicated, primary URL excluded."""
    candidates: list[str] = []
    if job.contact_url:
        candidates.append(job.contact_url)
    apply = apply_url_from_text(job.description or "")
    if apply:
        candidates.append(apply)
    candidates.extend(outbound_links_from_text(job.description or ""))

    seen: set[str] = set()
    out: list[str] = []
    for link in candidates:
        if link and link != job.url and link not in seen:
            seen.add(link)
            out.append(link)
    return out


async def _fetch_and_check(http_settings, job: Job) -> dict:
    """Fetch the posting page and classify the link. Returns link_status plus,
    on success, the `Extracted` page content. Never raises.

    link_status: "alive" | "moved" (primary dead, a live alternate found) |
    "dead" (404/410, no live alternate) | "unverifiable" (LinkedIn / robots /
    timeout / transport / 5xx — not confirmed gone, so never auto-hidden)."""
    result: dict = {
        "link_status": "unverifiable",
        "apply_url": job.url,
        "alternate_links": [],
        "extracted": None,
    }
    if host_is_denied(job.url):
        return result  # LinkedIn et al. — cannot fetch, cannot verify

    async with PoliteClient(http_settings) as client:
        try:
            body = await client.get_text(job.url)
        except FetchError as exc:
            if getattr(exc, "status_code", None) not in _DEAD_STATUSES:
                return result  # transient / robots — leave as unverifiable
            alts = _candidate_links(job)
            result["alternate_links"] = alts
            for alt in alts:
                if host_is_denied(alt):
                    continue
                try:
                    await client.get_text(alt)
                except FetchError:
                    continue
                result["link_status"] = "moved"
                result["apply_url"] = alt
                return result
            result["link_status"] = "dead"
            return result

        result["link_status"] = "alive"
        result["extracted"] = extract(body)
        return result


def refresh_job_detail(job_id: str) -> dict:
    """Fetch the real posting page for one job, store the fuller description,
    act on the link status, (re)translate a German posting, and return the
    updated detail.

    A definitively dead link (404/410) with no live alternate is soft-hidden
    (reversible); a transient failure or an unverifiable host hides nothing."""
    cfg = get_config()
    with Database(cfg.db_path) as db:
        job = db.get(job_id)
    if job is None:
        return {"exists": False}

    res = asyncio.run(_fetch_and_check(cfg.settings.http, job))
    status = res["link_status"]
    description_changed = False

    with Database(cfg.db_path) as db:
        if status == "alive" and res["extracted"] is not None:
            ex = res["extracted"]
            if ex.description and len(ex.description) > len(job.description or ""):
                job.description = ex.description[:_MAX_DETAIL_DESCRIPTION_CHARS]
                description_changed = True
            if job.published_at is None and ex.published_at:
                job.published_at = ex.published_at
            if job.location is None and ex.location:
                job.location = ex.location
            if job.contact_email is None and ex.contact_email:
                job.contact_email = ex.contact_email
            if job.contact_url is None and ex.contact_url:
                job.contact_url = ex.contact_url
            if description_changed:
                # The description grew; content_hash truncates to 500 chars so it
                # may not change, but the translation cache must miss regardless.
                job.content_hash = compute_content_hash(
                    job.title, job.organization, job.location, job.description
                )
                job.enriched_at = datetime.now(UTC)
                db.upsert(job)
                db.delete_translation(job_id)
        elif status == "dead":
            db.hide_job(job_id)

        db.save_detail_fetch(
            job_id,
            link_status=status,
            apply_url=res["apply_url"],
            alternate_links=res["alternate_links"],
            description_len=len(job.description or ""),
        )

    # Re-translate a German posting on the fuller text (cache-first; the cache
    # was cleared above if the description grew).
    if job.language == Language.DE:
        translate_job(job_id)

    return job_detail_json(job_id)


async def _is_definitely_dead(client: PoliteClient, job: Job) -> bool:
    """True only when the primary URL is 404/410 AND no alternate link resolves.
    A transient/unverifiable failure returns False — the sweep never hides on
    doubt."""
    try:
        await client.get_text(job.url)
        return False
    except FetchError as exc:
        if getattr(exc, "status_code", None) not in _DEAD_STATUSES:
            return False
    for alt in _candidate_links(job):
        if host_is_denied(alt):
            continue
        try:
            await client.get_text(alt)
            return False
        except FetchError:
            continue
    return True


def check_all_links() -> str:
    """Link-check every non-hidden, non-LinkedIn job and soft-hide the ones whose
    posting is definitively gone. Returns a human summary. Runs as a background
    task (it makes one polite request per job)."""
    cfg = get_config()
    with Database(cfg.db_path) as db:
        jobs = db.list_jobs(limit=1_000_000)
        hidden = db.hidden_ids()
    targets = [j for j in jobs if j.id not in hidden and not host_is_denied(j.url)]

    async def _run() -> list[str]:
        dead: list[str] = []
        async with PoliteClient(cfg.settings.http) as client:
            for job in targets:
                if await _is_definitely_dead(client, job):
                    dead.append(job.id)
        return dead

    dead_ids = asyncio.run(_run())
    with Database(cfg.db_path) as db:
        for jid in dead_ids:
            db.hide_job(jid)
            db.save_detail_fetch(
                jid, link_status="dead", apply_url=None, alternate_links=[], description_len=0
            )
    skipped = len(jobs) - len(targets) - len(hidden)
    return (
        f"Checked {len(targets)} link(s); hid {len(dead_ids)} expired posting(s). "
        f"{max(skipped, 0)} LinkedIn posting(s) skipped (cannot be verified)."
    )


# ---------------------------------------------------------------------------
# Settings (secrets overlay)
# ---------------------------------------------------------------------------

# Secret keys the Settings UI may write; masked on read. The search provider and
# the LLM tunnel URL are also editable but shown in the clear (not secrets).
EDITABLE_SECRET_KEYS = (
    "discord_webhook_url",
    "search_api_key",
    "google_cse_id",
    "colab_api_key",
    "apify_token",
)
_SEARCH_PROVIDERS = ("", "tavily", "brave", "bing", "google_cse", "serpapi")


def _mask(value: str | None) -> dict:
    v = (value or "").strip()
    if not v:
        return {"set": False, "hint": ""}
    return {"set": True, "hint": f"…{v[-4:]}" if len(v) >= 4 else "set"}


def get_settings_status() -> dict:
    """What the Settings page shows: which secrets are set (masked, never the raw
    value) plus the editable non-secret values."""
    cfg = get_config()
    s = cfg.secrets
    return {
        "secrets": {k: _mask(getattr(s, k)) for k in EDITABLE_SECRET_KEYS},
        "search_api_provider": s.search_api_provider,
        "colab_base_url": cfg.settings.llm.colab_base_url,
        "providers": list(_SEARCH_PROVIDERS),
    }


def save_settings(values: dict) -> dict:
    """Persist overlay values (empty string clears a key back to .env) and apply
    them to the live config in place, so the change takes effect immediately
    without re-reading the config files. Returns the new masked status."""
    allowed = set(EDITABLE_SECRET_KEYS) | {"search_api_provider", "colab_base_url"}
    clean: dict[str, str] = {}
    for key, value in values.items():
        if key not in allowed:
            continue
        val = "" if value is None else str(value).strip()
        if key == "search_api_provider" and val not in _SEARCH_PROVIDERS:
            raise ValueError(f"unknown search provider: {val!r}")
        clean[key] = val

    cfg = get_config()
    with Database(cfg.db_path) as db:
        db.set_config_overlay(clean)

    # Apply in place. The from-scratch path in `get_config()` applies the same
    # overlay on a fresh process; this keeps a long-running process in step
    # without discarding sources/settings already loaded.
    for key in _OVERLAY_SECRET_FIELDS:
        if key in clean:
            setattr(cfg.secrets, key, clean[key])
    if "colab_base_url" in clean:
        cfg.settings.llm.colab_base_url = clean["colab_base_url"]

    return get_settings_status()


def _rejection_reason(job: Job) -> str:
    for line in reversed(job.score_explanation):
        if line.lower().startswith("rejected:"):
            return line.split(":", 1)[1].strip()
    return "did not pass filtering"


def _detail_html(job: Job, translation: dict | None) -> str:
    color = f"#{_color_for(job.relevance_score):06x}"
    logo = org_image_url(job)
    esc = html.escape

    logo_html = (
        f'<img src="{esc(logo)}" style="width:40px;height:40px;border-radius:6px;'
        f'vertical-align:middle;margin-right:10px">' if logo else ""
    )

    def field(label: str, value: str | None) -> str:
        if not value:
            return ""
        return (
            f'<div style="margin:2px 0"><span style="opacity:.6">{esc(label)}:</span> '
            f'{esc(value)}</div>'
        )

    meta = "".join(
        [
            field("Organization", job.organization),
            field("Location", job.location or job.city),
            field("Source", job.source),
            field("Status", job.status.value),
            field("Employment", job.employment_type),
            field("Deadline", job.application_deadline.strftime("%Y-%m-%d")
                  if job.application_deadline else None),
            field("Apply to", job.contact_email),
            field("Language", job.language.value),
        ]
    )

    kw = ""
    if job.matched_keywords:
        chips = "".join(
            f'<span style="background:{color}22;border:1px solid {color}55;'
            f'border-radius:10px;padding:1px 8px;margin:2px;font-size:.85em;'
            f'display:inline-block">{esc(k)}</span>'
            for k in job.matched_keywords[:8]
        )
        kw = f'<div style="margin:8px 0">{chips}</div>'

    # Description block. For a German job: English first, German collapsible.
    if translation is not None:
        excerpt = " (excerpt)" if translation.get("truncated") else ""
        desc_html = (
            f'<h4 style="margin:14px 0 4px">English translation{excerpt}</h4>'
            f'<div style="white-space:pre-wrap;line-height:1.5">'
            f'{esc(translation["description_en"])}</div>'
            f'<details style="margin-top:10px"><summary style="cursor:pointer;opacity:.7">'
            f'Original German</summary>'
            f'<div style="white-space:pre-wrap;line-height:1.5;margin-top:6px;opacity:.85">'
            f'{esc(job.description or "")}</div></details>'
        )
    elif job.language == Language.DE:
        desc_html = (
            '<div style="color:#c0392b;margin:14px 0 4px">⚠️ English translation '
            "unavailable (LLM endpoint not reachable). Showing the original German.</div>"
            f'<div style="white-space:pre-wrap;line-height:1.5">'
            f'{esc(job.description or "")}</div>'
        )
    else:
        desc_html = (
            f'<div style="white-space:pre-wrap;line-height:1.5;margin-top:14px">'
            f'{esc(job.description or "(no description)")}</div>'
        )

    return f"""
<div style="padding:4px 2px">
  <div style="display:flex;align-items:center">
    {logo_html}
    <div>
      <div style="font-size:1.2em;font-weight:600">
        <a href="{esc(job.url)}" target="_blank" style="color:{color};text-decoration:none">
          {esc(job.title)}</a>
      </div>
      <div style="opacity:.7">
        <span style="background:{color};color:#fff;border-radius:8px;padding:1px 8px;
              font-size:.85em">score {job.relevance_score}/100</span>
      </div>
    </div>
  </div>
  <div style="margin-top:10px">{meta}</div>
  {kw}
  {desc_html}
</div>
"""


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def publish_job(job_id: str | None, confirm: bool) -> str:
    """Publish one job to Discord in English. Returns a status string."""
    if not job_id:
        return "No job selected."

    cfg = get_config()
    with Database(cfg.db_path) as db:
        job = db.get(job_id)
    if job is None:
        return "Job not found."

    if job.status == JobStatus.REJECTED and not confirm:
        return (
            f"Blocked: this job was filtered out ({_rejection_reason(job)}). "
            "Tick confirm to send."
        )
    if job.notified_at is not None and not confirm:
        when = job.notified_at.strftime("%Y-%m-%d %H:%M")
        return f"Blocked: already sent on {when}. Tick confirm to send again."

    try:
        webhook = cfg.secrets.require_discord()
    except ConfigError as exc:
        return f"Discord not configured: {exc}"

    note = ""
    to_send = job
    if job.language == Language.DE:
        translation = translate_job(job_id)
        if translation is not None:
            to_send = job.model_copy(
                update={
                    "description": translation["description_en"],
                    "card_summary": translation.get("card_summary_en"),
                }
            )
        else:
            note = " (translation unavailable — sent the original German)"

    result = asyncio.run(_send(webhook, cfg.settings, to_send))
    if result.delivered_ids:
        with Database(cfg.db_path) as db:
            db.mark_notified(result.delivered_ids)
        return f"✅ Published “{job.title}” to Discord{note}."
    err = result.errors[0] if result.errors else "unknown error"
    return f"❌ Discord did not accept the message: {err}"


async def _send(webhook: str, settings: Settings, job: Job):
    async with DiscordNotifier(webhook, settings.notifications) as notifier:
        return await notifier.send_jobs([job])


# ---------------------------------------------------------------------------
# Hide / unhide
# ---------------------------------------------------------------------------


def hide_job(job_id: str | None) -> str:
    if not job_id:
        return "No job selected."
    with Database(get_config().db_path) as db:
        db.hide_job(job_id)
    return "🙈 Hidden from the dashboard (still in the database)."


def unhide_all() -> str:
    with Database(get_config().db_path) as db:
        for jid in list(db.hidden_ids()):
            db.unhide_job(jid)
    return "Unhid all jobs."


# ---------------------------------------------------------------------------
# Resume → terms
# ---------------------------------------------------------------------------


def prefill_from_resume(resume_path: str | None) -> tuple[str, list[str], str]:
    """Extract keywords/topics from a resume PDF. Returns (keywords_csv, topics, status)."""
    if not resume_path:
        return "", [], "No resume uploaded."

    try:
        from pypdf import PdfReader

        reader = PdfReader(resume_path)
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        logger.info("could not read resume pdf: %s", exc)
        return "", [], f"Could not read the PDF: {exc}"

    if not text.strip():
        return "", [], "No selectable text found (is it a scanned image?)."

    cfg = get_config()
    terms = asyncio.run(extract_search_terms(text, cfg.settings.llm, cfg.secrets))
    keywords = terms.get("keywords", [])
    topics = terms.get("topics", [])
    if not keywords and not topics:
        return "", [], "LLM endpoint unavailable — type your keywords manually."
    return ", ".join(keywords), topics, f"Extracted {len(keywords)} keyword(s)."


# ---------------------------------------------------------------------------
# Central academic profile (Phase 3)
# ---------------------------------------------------------------------------


def _resume_text(raw: bytes, filename: str) -> str:
    """Best-effort plain text from an uploaded résumé.

    PDF via pypdf; .txt/.md/.tex/.markdown decoded directly. DOCX/ZIP are not yet
    supported — the caller shows a clear message rather than storing junk.
    """
    name = (filename or "").lower()
    if name.endswith(".pdf") or raw[:5] == b"%PDF-":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if name.endswith((".txt", ".md", ".markdown", ".tex", ".rst")):
        return raw.decode("utf-8", errors="replace")
    if name.endswith((".docx", ".zip")):
        raise ValueError("DOCX/ZIP résumés are not supported yet — upload a PDF or text/Markdown.")
    # Unknown extension: try to decode as text; a binary file yields junk that the
    # extractor will simply fail to read, which the caller reports honestly.
    return raw.decode("utf-8", errors="replace")


def save_and_extract_profile(raw: bytes, filename: str, content_type: str | None) -> dict:
    """Persist the original résumé immutably, extract a structured profile from it,
    and store that as the canonical profile. Returns the profile payload (same
    shape as `get_profile_json`) plus a `message`."""
    cfg = get_config()
    try:
        text = _resume_text(raw, filename)
    except Exception as exc:
        return {"exists": False, "message": str(exc)}
    if not text.strip():
        return {"exists": False, "message": "No selectable text found (is it a scanned image?)."}

    from ..llm.assist import PROFILE_PROMPT_VERSION, extract_profile

    extracted = asyncio.run(extract_profile(text, cfg.settings.llm, cfg.secrets))
    if extracted is None:
        return {
            "exists": False,
            "message": "LLM endpoint unavailable — could not read the résumé. Try again later.",
        }
    try:
        profile = AcademicProfile.model_validate(extracted)
    except Exception as exc:
        logger.info("profile validation failed: %s", exc)
        return {"exists": False, "message": "The extracted profile could not be parsed."}
    if profile.is_empty():
        return {"exists": False, "message": "Could not extract a meaningful profile from the file."}

    profile_json = profile.model_dump_json()
    with Database(cfg.db_path) as db:
        upload_id = db.save_profile_upload(filename, content_type, raw)
        db.save_profile(
            profile_json,
            profile_json,
            source_upload_id=upload_id,
            model_version=cfg.settings.llm.colab_model,
            prompt_version=PROFILE_PROMPT_VERSION,
            user_edited=False,
        )
    result = get_profile_json()
    result["message"] = "Profile extracted. Review and edit anything the résumé didn't spell out."
    return result


def get_profile_json() -> dict:
    """The current profile for the UI: the editable copy, the immutable extracted
    copy (for a 'what changed' view), and the source-upload metadata."""
    cfg = get_config()
    with Database(cfg.db_path) as db:
        row = db.get_profile()
        upload = db.latest_profile_upload()
    if row is None:
        return {"exists": False, "profile": AcademicProfile().model_dump(mode="json")}
    try:
        profile = json.loads(row["profile_json"])
        extracted = json.loads(row["extracted_json"])
    except json.JSONDecodeError:
        return {"exists": False, "profile": AcademicProfile().model_dump(mode="json")}
    return {
        "exists": True,
        "profile": profile,
        "extracted": extracted,
        "user_edited": bool(row["user_edited"]),
        "model_version": row["model_version"],
        "extracted_at": row["extracted_at"],
        "updated_at": row["updated_at"],
        "source": (
            {
                "filename": upload["filename"],
                "size_bytes": upload["size_bytes"],
                "uploaded_at": upload["uploaded_at"],
            }
            if upload
            else None
        ),
    }


def update_profile(payload: dict) -> dict:
    """Save a user-edited profile over the working copy (the immutable extracted
    copy is preserved). Validates against the model; returns the fresh payload."""
    cfg = get_config()
    try:
        profile = AcademicProfile.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"invalid profile: {exc}") from exc
    with Database(cfg.db_path) as db:
        if not db.update_profile_json(profile.model_dump_json()):
            raise ValueError("no profile to update — upload a résumé first")
    result = get_profile_json()
    result["message"] = "Profile saved."
    return result


def delete_profile_data() -> dict:
    """Erase the profile and every stored résumé — the 'delete my data' action."""
    with Database(get_config().db_path) as db:
        db.delete_profile()
    return {"exists": False, "message": "Profile and uploaded résumés deleted."}


def export_profile() -> dict:
    """The full profile as a JSON-serialisable dict, for download."""
    data = get_profile_json()
    data.pop("message", None)
    return data


def get_original_upload() -> dict | None:
    """The immutable original résumé bytes + metadata, for download. None if none."""
    with Database(get_config().db_path) as db:
        latest = db.latest_profile_upload()
        if latest is None:
            return None
        return db.get_profile_upload(latest["id"])


def _job_match_block(job: Job) -> str:
    """Flatten a job for the match prompt — the taxonomy first, then the text."""
    bits = [f"Title: {job.title}"]
    if job.organization:
        bits.append(f"Organization: {job.organization}")
    if job.location or job.city:
        bits.append(f"Location: {job.location or job.city}")
    for label, value in (
        ("Opportunity type", job.opportunity_type),
        ("Applicant level", job.applicant_level),
        ("Academic field", job.academic_field),
    ):
        if value:
            bits.append(f"{label}: {value}")
    if job.matched_keywords:
        bits.append(f"Topics/skills: {', '.join(job.matched_keywords[:10])}")
    if job.description:
        # Kept short on purpose: the self-hosted model is slow, and the match only
        # needs the gist of the role, not the whole posting.
        bits.append(f"Description: {job.description[:1200]}")
    return "\n".join(bits)


def match_job(job_id: str) -> dict:
    """Analyse how the stored profile fits one opportunity, cache-first.

    Returns `{"available": bool, ...}`. `available` is False (with a `reason`)
    when there is no profile yet, the job is unknown, or the LLM is down — the UI
    shows the reason rather than a broken panel."""
    import hashlib

    from ..llm.assist import MATCH_PROMPT_VERSION, analyze_match
    from ..profile import MatchAnalysis

    cfg = get_config()
    with Database(cfg.db_path) as db:
        prow = db.get_profile()
        if prow is None:
            return {"available": False, "reason": "no_profile"}
        job = db.get(job_id)
        if job is None:
            return {"available": False, "reason": "unknown_job"}

        profile_json = prow["profile_json"]
        profile_hash = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
        cached = db.get_match(job_id, job.content_hash, profile_hash, MATCH_PROMPT_VERSION)
        if cached is not None:
            return {"available": True, "cached": True, "match": cached}

    result = asyncio.run(
        analyze_match(profile_json, _job_match_block(job), cfg.settings.llm, cfg.secrets)
    )
    if result is None:
        return {"available": False, "reason": "llm_unavailable"}
    try:
        match = MatchAnalysis.model_validate({**result, "job_id": job_id})
    except Exception as exc:
        logger.info("match validation failed: %s", exc)
        return {"available": False, "reason": "unparseable"}

    payload = match.model_dump(mode="json")
    with Database(cfg.db_path) as db:
        try:
            db.save_match(job_id, job.content_hash, profile_hash, MATCH_PROMPT_VERSION, payload)
        except Exception as exc:  # a cache write must not fail the request
            logger.debug("could not cache match for %s: %s", job_id, exc)
    return {"available": True, "cached": False, "match": payload}


def tailor_job(job_id: str) -> dict:
    """Suggest how to tailor the stored profile's résumé to one opportunity,
    cache-first. Same `{"available": bool, ...}` contract as `match_job`."""
    import hashlib

    from ..llm.assist import TAILORING_PROMPT_VERSION, suggest_tailoring
    from ..profile import TailoringPlan

    cfg = get_config()
    with Database(cfg.db_path) as db:
        prow = db.get_profile()
        if prow is None:
            return {"available": False, "reason": "no_profile"}
        job = db.get(job_id)
        if job is None:
            return {"available": False, "reason": "unknown_job"}

        profile_json = prow["profile_json"]
        profile_hash = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
        cached = db.get_tailoring(job_id, job.content_hash, profile_hash, TAILORING_PROMPT_VERSION)
        if cached is not None:
            return {"available": True, "cached": True, "plan": cached}

    result = asyncio.run(
        suggest_tailoring(profile_json, _job_match_block(job), cfg.settings.llm, cfg.secrets)
    )
    if result is None:
        return {"available": False, "reason": "llm_unavailable"}
    try:
        plan = TailoringPlan.model_validate({**result, "job_id": job_id})
    except Exception as exc:
        logger.info("tailoring validation failed: %s", exc)
        return {"available": False, "reason": "unparseable"}

    payload = plan.model_dump(mode="json")
    with Database(cfg.db_path) as db:
        try:
            db.save_tailoring(
                job_id, job.content_hash, profile_hash, TAILORING_PROMPT_VERSION, payload
            )
        except Exception as exc:
            logger.debug("could not cache tailoring for %s: %s", job_id, exc)
    return {"available": True, "cached": False, "plan": payload}


_RESEARCH_MAX_AGE_DAYS = 30


def research_for_job(job_id: str) -> dict:
    """Research-group intelligence (OpenAlex) for an opportunity's institution,
    cache-first. No LLM involved — works regardless of the model's state."""
    import hashlib

    from ..research import research_context

    cfg = get_config()
    with Database(cfg.db_path) as db:
        job = db.get(job_id)
        if job is None:
            return {"available": False, "reason": "unknown_job"}
        org = job.organization
        field = job.academic_field or ""
        query_key = hashlib.sha256(f"{(org or '').lower()}|{field}".encode()).hexdigest()
        cached = db.get_research(job_id, query_key, _RESEARCH_MAX_AGE_DAYS)
        if cached is not None:
            return {**cached, "cached": True}

    result = asyncio.run(research_context(org, field))
    if result.get("available"):
        with Database(cfg.db_path) as db:
            try:
                db.save_research(job_id, query_key, result)
            except Exception as exc:
                logger.debug("could not cache research for %s: %s", job_id, exc)
    return {**result, "cached": False}


# ---------------------------------------------------------------------------
# Search (fetch)
# ---------------------------------------------------------------------------


def _merge_terms(keywords: str, topics: list[str] | None) -> list[str]:
    parts: list[str] = []
    for chunk in (keywords or "").split(","):
        c = chunk.strip()
        if c:
            parts.append(c)
    parts.extend(topics or [])
    return parts


def preview_search(keywords: str, topics: list[str] | None) -> tuple[str, str]:
    """The queries a search would run and a scope/cost line — no side effects."""
    cfg = get_config()
    terms = _merge_terms(keywords, topics)
    sd = next((s for s in cfg.sources.sources if s.name == _SEARCH_DISCOVERY), None)

    if terms and sd is not None:
        queries = build_site_queries(terms, domains_for(sd.allowed_domains))
    elif sd is not None:
        queries = list(sd.queries)
    else:
        queries = []

    active = [s.name for s in cfg.sources.active]
    paid = " (includes the paid Apify source)" if "linkedin_posts" in active else ""
    scope = (
        f"This runs {len(active)} source(s): {', '.join(active)}{paid}. "
        f"search_discovery will run {len(queries)} query(ies). Nothing is sent to Discord."
    )
    query_text = "\n".join(queries) if queries else "(no queries)"
    return query_text, scope


def run_search(keywords: str, topics: list[str] | None, locations: list[str] | None) -> str:
    """Run the full pipeline with injected queries, storing only (no auto-send)."""
    cfg = get_config()
    terms = _merge_terms(keywords, topics)

    settings = cfg.settings.model_copy(deep=True)
    if terms:
        settings.keywords.positive = terms
    if topics:
        settings.keywords.topics = topics
    if locations:
        settings.locations.include = locations
    # Store everything, send nothing — publishing stays a manual per-job action.
    settings.notifications.max_per_run = 0

    sources = cfg.sources.model_copy(deep=True)
    if terms:
        for src in sources.sources:
            if src.name == _SEARCH_DISCOVERY:
                injected = build_site_queries(terms, domains_for(src.allowed_domains))
                if injected:
                    src.queries = injected

    try:
        summary = asyncio.run(
            run_once(
                settings, sources, cfg.secrets, dry_run=False, use_lock=True, incremental=True
            )
        )
    except RunLockedError:
        return "A scheduled or manual run is already in progress. Try again shortly."
    except Exception as exc:  # a source/config failure must not crash the UI
        logger.exception("dashboard search failed")
        return f"Search failed: {exc}"

    return (
        f"Done in {summary.duration_seconds:.0f}s — "
        f"{summary.candidates_found} found, {summary.after_dedup} after dedup, "
        f"{summary.newly_stored} newly stored, {summary.above_threshold} above threshold. "
        f"Nothing sent to Discord; review and publish below."
    )


def llm_online() -> bool:
    """Whether the self-hosted LLM endpoint is reachable right now. Drives the
    UI's 'translation/search disabled' banner; never raises."""
    cfg = get_config()
    try:
        return asyncio.run(endpoint_online(cfg.settings.llm, cfg.secrets))
    except Exception:  # a config/network hiccup means "offline", not a 500
        logger.debug("llm_online check failed", exc_info=True)
        return False


def stats() -> dict:
    """Raw stats dict from the database, for the JSON API."""
    with Database(get_config().db_path) as db:
        return db.stats()


def stats_line() -> str:
    with Database(get_config().db_path) as db:
        s = db.stats()
    by_status = s["by_status"]
    avg = s["average_score"]
    return (
        f"**{s['total_jobs']}** jobs · "
        f"**{by_status.get('new', 0)}** new · "
        f"**{by_status.get('notified', 0)}** notified · "
        f"**{by_status.get('rejected', 0)}** rejected · "
        f"avg score **{avg if avg is not None else '—'}**"
    )
