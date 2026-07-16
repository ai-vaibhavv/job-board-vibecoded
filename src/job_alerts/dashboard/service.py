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
import logging
from dataclasses import dataclass

from ..config import (
    ConfigError,
    Secrets,
    Settings,
    SourcesConfig,
    load_settings,
    load_sources,
)
from ..database import Database
from ..llm.assist import extract_search_terms, translate_job_text
from ..models import Job, JobStatus, Language
from ..notifications.discord import (
    DiscordNotifier,
    _color_for,
    org_image_url,
)
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


def get_config() -> AppConfig:
    global _config
    if _config is None:
        secrets = Secrets()
        _config = AppConfig(
            settings=load_settings(secrets=secrets),
            sources=load_sources(secrets=secrets),
            secrets=secrets,
        )
    return _config


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
    cfg = get_config()
    with Database(cfg.db_path) as db:
        jobs = db.list_jobs(
            status=JobStatus(status) if status else None,
            min_score=min_score,
            limit=10000,
        )
        hidden = set() if show_hidden else db.hidden_ids()

    needle = (text or "").strip().casefold()
    rows: list[list] = []
    ids: list[str] = []
    for job in jobs:
        if job.id in hidden:
            continue
        if source and job.source != source:
            continue
        if needle and needle not in _haystack(job):
            continue
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
            run_once(settings, sources, cfg.secrets, dry_run=False, use_lock=True)
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
