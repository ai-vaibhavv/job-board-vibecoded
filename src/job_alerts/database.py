"""SQLite persistence and deduplication.

Plain `sqlite3` rather than an ORM: there is one table, the queries are simple,
and every statement here is parameterized and auditable at a glance.

Schema changes go through `_MIGRATIONS`, keyed by `PRAGMA user_version`. On
startup `migrate()` applies every migration above the stored version, so an
existing database upgrades in place and a fresh one is built from scratch by
the same code path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import Job, JobStatus

logger = logging.getLogger(__name__)

_MIGRATIONS: list[str] = [
    # v1 — initial schema.
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id                  TEXT PRIMARY KEY,
        source              TEXT NOT NULL,
        source_job_id       TEXT,
        title               TEXT NOT NULL,
        organization        TEXT,
        location            TEXT,
        country             TEXT,
        remote_status       TEXT,
        description         TEXT,
        url                 TEXT NOT NULL,
        published_at        TEXT,
        discovered_at       TEXT NOT NULL,
        application_deadline TEXT,
        employment_type     TEXT,
        language            TEXT,
        salary              TEXT,
        relevance_score     INTEGER NOT NULL DEFAULT 0,
        matched_keywords    TEXT NOT NULL DEFAULT '[]',
        score_explanation   TEXT NOT NULL DEFAULT '[]',
        content_hash        TEXT NOT NULL,
        notified_at         TEXT,
        status              TEXT NOT NULL DEFAULT 'new'
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
    CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(relevance_score DESC);
    CREATE INDEX IF NOT EXISTS idx_jobs_discovered ON jobs(discovered_at DESC);
    -- Dedup across sources: the same posting found via RSS and via a search
    -- engine normalizes to one URL, so a UNIQUE index catches it even when the
    -- two ids differ.
    CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);

    CREATE TABLE IF NOT EXISTS runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        summary_json  TEXT
    );
    """,
    # v2 — enrichment and the assessment cache.
    #
    # Purely additive: no table is rebuilt, so nothing can be lost and every
    # existing row keeps its `status` and `notified_at`. Adding a column to
    # `jobs` means touching the `Job` model, the INSERT column list and
    # `_row_to_job` in the same change — the model is `extra="forbid"`, which is
    # what makes a half-done version fail loudly instead of silently.
    """
    -- Where the job is, in words. Not coordinates: there is no radius search,
    -- only a country allowlist, and a city is for reading.
    ALTER TABLE jobs ADD COLUMN city TEXT;

    -- How to apply when the posting is a person saying "email me". Extracted,
    -- shown, never mailed automatically.
    ALTER TABLE jobs ADD COLUMN contact_email TEXT;
    ALTER TABLE jobs ADD COLUMN contact_url TEXT;

    -- When we last fetched the real posting page. This is what lets the recency
    -- rule tell "no date, we never looked" from "no date, we looked properly" —
    -- only the second is grounds for dropping a job.
    ALTER TABLE jobs ADD COLUMN enriched_at TEXT;

    -- One LLM verdict per job, kept so a run costs its providers only the jobs
    -- it has never seen. Without this, every run re-judges the whole database:
    -- a live run against 108 surviving candidates exhausted the free tiers of
    -- BOTH Gemini and Groq and fell back to keyword scoring for 48 jobs.
    --
    -- Keyed on content_hash so an edited posting is re-assessed, and on
    -- prompt_version so changing the prompt invalidates verdicts made under the
    -- old one rather than silently mixing two rubrics in one database.
    CREATE TABLE IF NOT EXISTS job_assessments (
        job_id          TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        content_hash    TEXT NOT NULL,
        prompt_version  INTEGER NOT NULL,
        assessment_json TEXT NOT NULL,
        provider        TEXT,
        assessed_at     TEXT NOT NULL
    );
    """,
    # v3 — retract the country claim on rows that were never entitled to it.
    #
    # This belongs to v2 by rights, and started life there. It is a separate
    # migration because v2 had already run against a live database by the time
    # the omission was noticed, and an applied migration is history: editing one
    # means every database that already ran it silently never gets the fix.
    # Append, never amend.
    #
    # `Job.country` defaulted to "Germany" and `_row_to_job` re-applied it on
    # read, so every search-discovered job asserted Germany on no evidence at
    # all — including the Nigerian and Sri Lankan postings a broken `site:`
    # filter let in. Those rows still say "Germany" on disk. NULL destroys
    # nothing here: the value was never observed, and NULL is what "we do not
    # know" is supposed to look like. Sources that assert their own country via
    # `defaults:` (fraunhofer, tum_hiwi) genuinely know, and keep theirs.
    """
    UPDATE jobs SET country = NULL WHERE source = 'search_discovery';
    """,
    # v4 — clear locations that are taxonomies wearing a location's clothes.
    #
    # Fraunhofer's job pages keep their tag list in `<p class="job-location">`,
    # so enrichment trusted the class name and wrote "Job Segment: Research
    # Assistant, Industrial Engineer, Mechanical Engineer, Intern, ..." into the
    # location of five jobs — and into a Discord alert. `looks_like_a_place` now
    # checks the value instead of believing the selector; this clears what the
    # earlier version already stored. NULL, because we never learned the place.
    """
    UPDATE jobs SET location = NULL
    WHERE location LIKE 'Job Segment:%'
       OR location LIKE 'Stellensegment:%'
       OR length(location) > 60;
    """,
    # v5 — the LLM-written Discord card blurb.
    #
    # Additive, same discipline as v2: adding a column to `jobs` means touching
    # the `Job` model, the INSERT column list and `_row_to_job` in one change.
    # The model is `extra="forbid"`, so a half-done version fails loudly.
    """
    ALTER TABLE jobs ADD COLUMN card_summary TEXT;
    """,
    # v6 — dashboard side tables: on-demand English translations, and soft-hide.
    #
    # Both are side tables, not columns on `jobs`, and deliberately so. A column
    # would ride the hot `upsert` ON-CONFLICT path, where a normal pipeline run
    # (which knows nothing about translations or hiding) would overwrite it with
    # NULL on every re-discovery. A referencing table with ON DELETE CASCADE keeps
    # `upsert`, the `Job` model and `_row_to_job` untouched — same discipline as
    # `job_assessments`.
    #
    # `job_translations` is keyed on content_hash like the assessment cache, so an
    # edited posting is re-translated rather than served a stale translation.
    # `job_hidden` records a soft-hide: the row stays in `jobs` with its REJECTED
    # status and its cached verdict, so a hidden job is still deduplicated and
    # never re-fetched or re-judged. Deleting the row instead would make it
    # re-appear and re-cost LLM quota on the next run.
    """
    CREATE TABLE IF NOT EXISTS job_translations (
        job_id          TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        content_hash    TEXT NOT NULL,
        description_en  TEXT NOT NULL,
        card_summary_en TEXT,
        truncated       INTEGER NOT NULL DEFAULT 0,
        translated_at   TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS job_hidden (
        job_id     TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        hidden_at  TEXT NOT NULL
    );
    """,
    # v7 — on-demand detail fetch record, and a writable config overlay.
    #
    # `job_detail_fetch` is another side table (same discipline as v6): the
    # dashboard fetches the full posting page on first open, and records the
    # outcome here — when it was fetched (so a re-open is instant), whether the
    # link is alive/dead/moved/unverifiable, and the best apply link plus any
    # alternates found. The `jobs.description` improvement itself rides the normal
    # `upsert` path; only this fetch metadata is a side table, so it never gets
    # clobbered by a pipeline re-discovery.
    #
    # `app_config` is a plain key/value overlay written by the Settings UI. It is
    # the ONLY writable config path in Docker (`.env` is not mounted, `config/` is
    # read-only), and it lives in the persistent `/data` volume so a UI-set secret
    # survives a container restart. Values are applied over `.env`/env in
    # `service.get_config()`.
    """
    CREATE TABLE IF NOT EXISTS job_detail_fetch (
        job_id           TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        fetched_at       TEXT NOT NULL,
        link_status      TEXT NOT NULL,
        apply_url        TEXT,
        alternate_links  TEXT NOT NULL DEFAULT '[]',
        description_len  INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS app_config (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    """,
    # v8 — LabScout academic taxonomy: coarse fields on `jobs`, plus a Pass-2
    # detail cache side table.
    #
    # `opportunity_type`, `applicant_level` and `academic_field` are the three
    # high-value taxonomy fields the two-pass LLM classifier fills. They ride the
    # normal `upsert` path — same additive discipline as v5's `card_summary`:
    # adding a column to `jobs` means touching the `Job` model, the INSERT column
    # list and `_row_to_job` in one change, and the model is `extra="forbid"` so a
    # half-done version fails loudly. Richer fields (institution, PI, weekly hours)
    # stay LLM-only for now rather than becoming a table of perma-null columns.
    #
    # `job_opportunity_details` caches the Pass-2 verdict, keyed on content_hash +
    # prompt_version exactly like `job_assessments`, so a run pays for the detail
    # pass only on jobs it has never fine-classified. A side table, so it never
    # rides the hot `upsert` conflict path.
    """
    ALTER TABLE jobs ADD COLUMN opportunity_type TEXT;
    ALTER TABLE jobs ADD COLUMN applicant_level TEXT;
    ALTER TABLE jobs ADD COLUMN academic_field TEXT;

    CREATE TABLE IF NOT EXISTS job_opportunity_details (
        job_id          TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        content_hash    TEXT NOT NULL,
        prompt_version  INTEGER NOT NULL,
        detail_json     TEXT NOT NULL,
        provider        TEXT,
        classified_at   TEXT NOT NULL
    );
    """,
    # v9 — per-source health, so a source that silently rots (a renamed CSS
    # selector, a moved feed) is noticed instead of just quietly returning zero.
    #
    # A side table keyed by source name — not tied to `jobs`, and updated once per
    # run from the SourceResults. `consecutive_empty` catches a broken parser (the
    # request still 200s, but nothing matches), `consecutive_errors` catches an
    # outright failure; either crossing a threshold is surfaced in the run summary
    # and by `source-health`. A skipped source (no API key) never counts as
    # unhealthy — that is a configuration choice, not a fault.
    """
    CREATE TABLE IF NOT EXISTS source_health (
        source              TEXT PRIMARY KEY,
        last_run_at         TEXT NOT NULL,
        last_status         TEXT NOT NULL,
        last_candidates     INTEGER NOT NULL DEFAULT 0,
        last_error          TEXT,
        last_ok_at          TEXT,
        consecutive_empty   INTEGER NOT NULL DEFAULT 0,
        consecutive_errors  INTEGER NOT NULL DEFAULT 0,
        total_runs          INTEGER NOT NULL DEFAULT 0
    );
    """,
    # v10 — the central academic profile (Phase 3).
    #
    # `profile_uploads` keeps every uploaded résumé BYTE-FOR-BYTE and immutable —
    # the original is never mutated, only re-extracted from. `profile` is a
    # singleton (CHECK id = 1): one user, one canonical profile. It stores BOTH
    # the immutable LLM output (`extracted_json`) and the editable working copy
    # (`profile_json`), so "what the machine read" and "what the user corrected"
    # are always both recoverable — that is the whole provenance story, kept at the
    # row level instead of smeared across per-field flags.
    """
    CREATE TABLE IF NOT EXISTS profile_uploads (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        filename      TEXT NOT NULL,
        content_type  TEXT,
        content_hash  TEXT NOT NULL,
        size_bytes    INTEGER NOT NULL DEFAULT 0,
        raw           BLOB NOT NULL,
        uploaded_at   TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS profile (
        id               INTEGER PRIMARY KEY CHECK (id = 1),
        profile_json     TEXT NOT NULL,
        extracted_json   TEXT NOT NULL,
        source_upload_id INTEGER REFERENCES profile_uploads(id) ON DELETE SET NULL,
        model_version    TEXT,
        prompt_version   INTEGER,
        user_edited      INTEGER NOT NULL DEFAULT 0,
        extracted_at     TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    );
    """,
    # v11 — profile↔opportunity match analysis cache (Phase 4).
    #
    # A match depends on BOTH sides, so it is keyed on the job's content_hash AND
    # a hash of the profile: edit the résumé and every match re-computes, edit the
    # posting and just that one does. `prompt_version` invalidates on a rubric
    # change, exactly like the assessment cache. A side table with ON DELETE
    # CASCADE, so a deleted job takes its match with it.
    """
    CREATE TABLE IF NOT EXISTS job_match (
        job_id         TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        content_hash   TEXT NOT NULL,
        profile_hash   TEXT NOT NULL,
        prompt_version INTEGER NOT NULL,
        match_json     TEXT NOT NULL,
        analyzed_at    TEXT NOT NULL
    );
    """,
    # v12 — résumé-tailoring plan cache (Phase 5). Same keying as the match cache
    # (job content + profile + prompt version); cleared when the profile is deleted.
    """
    CREATE TABLE IF NOT EXISTS job_tailoring (
        job_id         TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        content_hash   TEXT NOT NULL,
        profile_hash   TEXT NOT NULL,
        prompt_version INTEGER NOT NULL,
        plan_json      TEXT NOT NULL,
        tailored_at    TEXT NOT NULL
    );
    """,
    # v13 — research-group intelligence cache (Phase 6, OpenAlex).
    #
    # Keyed on job_id plus `query_key` (a hash of the institution+field looked up),
    # so it survives an org change and a caller-side TTL decides staleness — a
    # group's research profile drifts over months, not minutes, so there is no
    # content_hash here. Not profile-dependent, so it is NOT cleared on profile
    # delete.
    """
    CREATE TABLE IF NOT EXISTS job_research (
        job_id       TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        query_key    TEXT NOT NULL,
        research_json TEXT NOT NULL,
        fetched_at   TEXT NOT NULL
    );
    """,
]


def _dt(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Database:
    """Thin wrapper over a SQLite file. Safe to construct per run."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        # WAL lets a long read (export) coexist with the scheduled write run.
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self.migrate()
        # Deliberately after migrate(). SQLite rewrites a REFERENCES clause to
        # follow a renamed table, so a future create-copy-drop-rename migration
        # would silently repoint every foreign key at the dropped table if this
        # were on while it ran. It corrupts quietly rather than failing.
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    def migrate(self) -> int:
        """Apply pending migrations. Returns the resulting schema version."""
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        pending = _MIGRATIONS[current:]
        if not pending:
            return current

        self._backup_before_migrating(current)
        for index, script in enumerate(pending, start=current + 1):
            with self._conn:
                self._conn.executescript(script)
                # PRAGMA does not accept parameters; index is loop-controlled.
                self._conn.execute(f"PRAGMA user_version = {index}")
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _backup_before_migrating(self, from_version: int) -> None:
        """Copy the database aside before changing its shape.

        `with self._conn:` looks like a transaction, but `executescript` issues
        an implicit COMMIT before it runs its first statement — so a migration
        that fails halfway leaves the file in whatever state it got to, already
        committed and unrollbackable. The rows here are months of "already told
        you about this job" history; re-notifying all of them is a bad morning.
        A few hundred KB is cheap insurance.

        Never fatal: a backup that cannot be written must not stop the run.
        """
        if str(self.path) == ":memory:" or not self.path.exists():
            return
        if not self._conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
            return  # nothing to lose yet

        target = self.path.with_suffix(f"{self.path.suffix}.v{from_version}.bak")
        try:
            # sqlite3's own backup API, not a file copy: it is consistent even
            # with WAL sidecar files that a naive copy would miss.
            with sqlite3.connect(str(target)) as dest:
                self._conn.backup(dest)
            logger.info("backed up schema v%d to %s before migrating", from_version, target.name)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("could not back up %s before migrating: %s", self.path, exc)

    # -- reads ------------------------------------------------------------

    def exists(self, job_id: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row is not None

    def get(self, job_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def get_by_url(self, url: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        return _row_to_job(row) if row else None

    def known_ids(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT id FROM jobs")}

    def known_urls(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT url FROM jobs")}

    def is_duplicate(self, job: Job) -> bool:
        """Seen before, by id or by normalized URL?

        The URL check is what makes two *different* discovery routes converge:
        an RSS item and a Google result for the same posting produce different
        ids but the same normalized URL.
        """
        return self.exists(job.id) or self.get_by_url(job.url) is not None

    def list_jobs(
        self,
        *,
        status: JobStatus | str | None = None,
        min_score: int | None = None,
        limit: int = 50,
        new_only: bool = False,
    ) -> list[Job]:
        clauses: list[str] = []
        params: list[object] = []
        if new_only:
            clauses.append("notified_at IS NULL")
            clauses.append("status != ?")
            params.append(JobStatus.REJECTED.value)
        if status is not None:
            # Accept a plain string too: JobStatus is a StrEnum, so coercing
            # here turns a silent AttributeError into a clear ValueError on a
            # bad value, and makes `list_jobs(status="notified")` just work.
            clauses.append("status = ?")
            params.append(JobStatus(status).value)
        if min_score is not None:
            clauses.append("relevance_score >= ?")
            params.append(min_score)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY relevance_score DESC, discovered_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_job(r) for r in rows]

    def stats(self) -> dict[str, object]:
        conn = self._conn
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        }
        by_source = {
            r["source"]: r["n"]
            for r in conn.execute(
                "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source ORDER BY n DESC"
            )
        }
        avg = conn.execute(
            "SELECT AVG(relevance_score) FROM jobs WHERE status != ?", (JobStatus.REJECTED.value,)
        ).fetchone()[0]
        last_run = conn.execute(
            "SELECT started_at, finished_at, summary_json FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "total_jobs": total,
            "by_status": by_status,
            "by_source": by_source,
            "average_score": round(avg, 1) if avg is not None else None,
            "notified": by_status.get(JobStatus.NOTIFIED.value, 0),
            "last_run": dict(last_run) if last_run else None,
            "database_path": str(self.path),
        }

    # -- writes -----------------------------------------------------------

    def upsert(self, job: Job) -> bool:
        """Insert a job. Returns True when it was genuinely new.

        On conflict the row is refreshed (score/description may have improved)
        but `notified_at` and `status` are preserved — re-running a search must
        never resurrect an already-sent job into the "to notify" set.

        A posting's identity is its URL, not its id. Two sources derive different
        ids for the same page — an RSS feed uses its guid, a web search has no id
        and falls back to a hash — and `idx_jobs_url` is UNIQUE precisely so that
        one page cannot become two rows. But the upsert only ever conflicted on
        `id`, so a same-URL-different-id job did not update anything: it inserted,
        hit the unique index, and took the whole run down with an IntegrityError.
        That was latent for as long as no two sources overlapped, and became a
        crash the moment the TUM feed started finding pages the search already
        knew about.

        So: adopt the stored id. The row we already have IS this job, and keeping
        its id is what keeps `notified_at` attached to it — rewriting the id
        instead would leave `mark_notified` updating a row that no longer exists,
        and the job would be announced again on every run forever.
        """
        existing = self.get_by_url(job.url) if job.url else None
        if existing and existing.id != job.id:
            logger.debug(
                "same url under two ids (%s -> %s); keeping the stored one: %s",
                job.id,
                existing.id,
                job.url,
            )
            job.id = existing.id

        is_new = not self.is_duplicate(job)
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, source, source_job_id, title, organization, location, country,
                    city, remote_status, description, url, contact_email, contact_url,
                    published_at, discovered_at, enriched_at,
                    application_deadline, employment_type, language, salary,
                    relevance_score, matched_keywords, score_explanation, card_summary,
                    opportunity_type, applicant_level, academic_field,
                    content_hash, notified_at, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title             = excluded.title,
                    organization      = excluded.organization,
                    location          = excluded.location,
                    country           = excluded.country,
                    city              = excluded.city,
                    description       = excluded.description,
                    contact_email     = excluded.contact_email,
                    contact_url       = excluded.contact_url,
                    published_at      = excluded.published_at,
                    enriched_at       = excluded.enriched_at,
                    relevance_score   = excluded.relevance_score,
                    matched_keywords  = excluded.matched_keywords,
                    score_explanation = excluded.score_explanation,
                    card_summary      = excluded.card_summary,
                    -- COALESCE so a re-discovery whose Pass-2 detail call did not
                    -- run (cache miss + LLM down) does not wipe a previously stored
                    -- taxonomy value with NULL.
                    opportunity_type  = COALESCE(excluded.opportunity_type, jobs.opportunity_type),
                    applicant_level   = COALESCE(excluded.applicant_level, jobs.applicant_level),
                    academic_field    = COALESCE(excluded.academic_field, jobs.academic_field),
                    content_hash      = excluded.content_hash,
                    application_deadline = excluded.application_deadline
                """,
                (
                    job.id,
                    job.source,
                    job.source_job_id,
                    job.title,
                    job.organization,
                    job.location,
                    job.country,
                    job.city,
                    job.remote_status.value,
                    job.description,
                    job.url,
                    job.contact_email,
                    job.contact_url,
                    _dt(job.published_at),
                    _dt(job.discovered_at),
                    _dt(job.enriched_at),
                    _dt(job.application_deadline),
                    job.employment_type,
                    job.language.value,
                    job.salary,
                    job.relevance_score,
                    json.dumps(job.matched_keywords),
                    json.dumps(job.score_explanation),
                    job.card_summary,
                    job.opportunity_type,
                    job.applicant_level,
                    job.academic_field,
                    job.content_hash,
                    _dt(job.notified_at),
                    job.status.value,
                ),
            )
        return is_new

    def mark_notified(self, job_ids: list[str], when: datetime | None = None) -> None:
        """Flag jobs as delivered.

        Only ever called after Discord confirms a 2xx — that ordering is what
        stops a failed send from silently swallowing a job forever.
        """
        if not job_ids:
            return
        when = when or datetime.now(UTC)
        with self._tx() as conn:
            conn.executemany(
                "UPDATE jobs SET notified_at = ?, status = ? WHERE id = ?",
                [(_dt(when), JobStatus.NOTIFIED.value, jid) for jid in job_ids],
            )

    def mark_rejected(self, job_id: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET status = ? WHERE id = ?", (JobStatus.REJECTED.value, job_id)
            )

    # -- assessment cache -------------------------------------------------

    def get_assessment(self, job_id: str, content_hash: str, prompt_version: int) -> dict | None:
        """A cached LLM verdict, if one was made for *this* posting text under
        *this* prompt. Otherwise None, and the caller pays for a fresh one.

        Both keys matter. `content_hash` means an edited posting is re-judged;
        `prompt_version` means changing the rubric invalidates old verdicts
        rather than leaving two rubrics mixed in one database, which would show
        up as scores that cannot be reproduced.
        """
        row = self._conn.execute(
            """
            SELECT assessment_json FROM job_assessments
            WHERE job_id = ? AND content_hash = ? AND prompt_version = ?
            """,
            (job_id, content_hash, prompt_version),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["assessment_json"])
        except json.JSONDecodeError:  # pragma: no cover — defensive
            logger.warning("cached assessment for %s is not valid json; ignoring", job_id)
            return None

    def save_assessment(
        self,
        job_id: str,
        content_hash: str,
        prompt_version: int,
        assessment: dict,
        provider: str | None = None,
        when: datetime | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO job_assessments
                    (job_id, content_hash, prompt_version, assessment_json, provider, assessed_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    content_hash    = excluded.content_hash,
                    prompt_version  = excluded.prompt_version,
                    assessment_json = excluded.assessment_json,
                    provider        = excluded.provider,
                    assessed_at     = excluded.assessed_at
                """,
                (
                    job_id,
                    content_hash,
                    prompt_version,
                    json.dumps(assessment),
                    provider,
                    _dt(when or datetime.now(UTC)),
                ),
            )

    # -- opportunity detail cache (Pass 2) --------------------------------

    def get_detail(self, job_id: str, content_hash: str, prompt_version: int) -> dict | None:
        """A cached Pass-2 detail verdict, if one was made for *this* posting text
        under *this* detail prompt. Otherwise None, and the caller pays for a
        fresh classification. Same key discipline as `get_assessment`."""
        row = self._conn.execute(
            """
            SELECT detail_json FROM job_opportunity_details
            WHERE job_id = ? AND content_hash = ? AND prompt_version = ?
            """,
            (job_id, content_hash, prompt_version),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["detail_json"])
        except json.JSONDecodeError:  # pragma: no cover — defensive
            logger.warning("cached detail for %s is not valid json; ignoring", job_id)
            return None

    def save_detail(
        self,
        job_id: str,
        content_hash: str,
        prompt_version: int,
        detail: dict,
        provider: str | None = None,
        when: datetime | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO job_opportunity_details
                    (job_id, content_hash, prompt_version, detail_json, provider, classified_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    content_hash    = excluded.content_hash,
                    prompt_version  = excluded.prompt_version,
                    detail_json     = excluded.detail_json,
                    provider        = excluded.provider,
                    classified_at   = excluded.classified_at
                """,
                (
                    job_id,
                    content_hash,
                    prompt_version,
                    json.dumps(detail),
                    provider,
                    _dt(when or datetime.now(UTC)),
                ),
            )

    # -- translation cache ------------------------------------------------

    def get_translation(self, job_id: str, content_hash: str) -> dict | None:
        """A cached English translation, if one was made for *this* posting text.

        Keyed on `content_hash` like the assessment cache: an edited posting
        (new hash) is re-translated rather than served the old translation.
        Returns a dict with `description_en`, `card_summary_en`, `truncated`, or
        None when nothing is cached for this text.
        """
        row = self._conn.execute(
            """
            SELECT description_en, card_summary_en, truncated FROM job_translations
            WHERE job_id = ? AND content_hash = ?
            """,
            (job_id, content_hash),
        ).fetchone()
        if row is None:
            return None
        return {
            "description_en": row["description_en"],
            "card_summary_en": row["card_summary_en"],
            "truncated": bool(row["truncated"]),
        }

    def delete_translation(self, job_id: str) -> None:
        """Drop a cached translation. Used when the dashboard re-fetches a fuller
        description: `content_hash` truncates to 500 chars so growing the body
        rarely changes it, which would otherwise keep serving the old short
        translation from the cache."""
        with self._tx() as conn:
            conn.execute("DELETE FROM job_translations WHERE job_id = ?", (job_id,))

    def save_translation(
        self,
        job_id: str,
        content_hash: str,
        description_en: str,
        card_summary_en: str | None = None,
        truncated: bool = False,
        when: datetime | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO job_translations
                    (job_id, content_hash, description_en, card_summary_en,
                     truncated, translated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    content_hash    = excluded.content_hash,
                    description_en  = excluded.description_en,
                    card_summary_en = excluded.card_summary_en,
                    truncated       = excluded.truncated,
                    translated_at   = excluded.translated_at
                """,
                (
                    job_id,
                    content_hash,
                    description_en,
                    card_summary_en,
                    1 if truncated else 0,
                    _dt(when or datetime.now(UTC)),
                ),
            )

    # -- soft-hide --------------------------------------------------------

    def hide_job(self, job_id: str, when: datetime | None = None) -> None:
        """Mark a job hidden from the dashboard. The `jobs` row is untouched, so
        the job keeps its status and cached verdict and is never re-fetched."""
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO job_hidden (job_id, hidden_at) VALUES (?, ?)
                ON CONFLICT(job_id) DO NOTHING
                """,
                (job_id, _dt(when or datetime.now(UTC))),
            )

    def unhide_job(self, job_id: str) -> None:
        with self._tx() as conn:
            conn.execute("DELETE FROM job_hidden WHERE job_id = ?", (job_id,))

    def hidden_ids(self) -> set[str]:
        return {r[0] for r in self._conn.execute("SELECT job_id FROM job_hidden")}

    # -- on-demand detail fetch record ------------------------------------

    def get_detail_fetch(self, job_id: str) -> dict | None:
        """The recorded outcome of the last on-demand full-posting fetch for a
        job, or None if it has never been fetched from the dashboard."""
        row = self._conn.execute(
            """
            SELECT fetched_at, link_status, apply_url, alternate_links, description_len
            FROM job_detail_fetch WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "fetched_at": row["fetched_at"],
            "link_status": row["link_status"],
            "apply_url": row["apply_url"],
            "alternate_links": json.loads(row["alternate_links"] or "[]"),
            "description_len": row["description_len"],
        }

    def save_detail_fetch(
        self,
        job_id: str,
        *,
        link_status: str,
        apply_url: str | None,
        alternate_links: list[str] | None = None,
        description_len: int = 0,
        when: datetime | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO job_detail_fetch
                    (job_id, fetched_at, link_status, apply_url, alternate_links, description_len)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    fetched_at      = excluded.fetched_at,
                    link_status     = excluded.link_status,
                    apply_url       = excluded.apply_url,
                    alternate_links = excluded.alternate_links,
                    description_len = excluded.description_len
                """,
                (
                    job_id,
                    _dt(when or datetime.now(UTC)),
                    link_status,
                    apply_url,
                    json.dumps(alternate_links or []),
                    description_len,
                ),
            )

    # -- config overlay (Settings UI) -------------------------------------

    def get_config_overlay(self) -> dict[str, str]:
        """Every key/value the Settings UI has written. Applied over `.env`/env
        in `service.get_config()`; the only writable config path in Docker."""
        return {
            r["key"]: r["value"]
            for r in self._conn.execute("SELECT key, value FROM app_config")
        }

    def set_config_overlay(self, values: dict[str, str], when: datetime | None = None) -> None:
        """Upsert overlay keys. An empty string value clears the key, so the UI
        can revert to whatever `.env`/env provides."""
        stamp = _dt(when or datetime.now(UTC))
        with self._tx() as conn:
            for key, value in values.items():
                if value == "":
                    conn.execute("DELETE FROM app_config WHERE key = ?", (key,))
                else:
                    conn.execute(
                        """
                        INSERT INTO app_config (key, value, updated_at) VALUES (?,?,?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                                       updated_at = excluded.updated_at
                        """,
                        (key, value, stamp),
                    )

    def record_run(self, started_at: datetime, finished_at: datetime, summary_json: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO runs (started_at, finished_at, summary_json) VALUES (?,?,?)",
                (_dt(started_at), _dt(finished_at), summary_json),
            )

    # -- source health ----------------------------------------------------

    def record_source_health(
        self,
        source: str,
        status: str,
        candidates: int = 0,
        error: str | None = None,
        when: datetime | None = None,
    ) -> None:
        """Fold one run's outcome for a source into its rolling health.

        `status` is "ok" (candidates found), "empty" (200 but nothing parsed —
        the tell-tale of a broken selector), "error", or "skipped". A skipped
        source leaves the consecutive counters untouched: it is a config choice,
        not a fault. Everything else advances or resets the streaks.
        """
        now = _dt(when or datetime.now(UTC))
        row = self._conn.execute(
            "SELECT consecutive_empty, consecutive_errors, total_runs, last_ok_at "
            "FROM source_health WHERE source = ?",
            (source,),
        ).fetchone()
        empty = row["consecutive_empty"] if row else 0
        errors = row["consecutive_errors"] if row else 0
        total = (row["total_runs"] if row else 0) + 1
        last_ok = row["last_ok_at"] if row else None

        if status == "ok":
            empty, errors, last_ok = 0, 0, now
        elif status == "empty":
            empty, errors = empty + 1, 0
        elif status == "error":
            errors += 1
        # "skipped": leave streaks as they were.

        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO source_health (
                    source, last_run_at, last_status, last_candidates, last_error,
                    last_ok_at, consecutive_empty, consecutive_errors, total_runs
                ) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                    last_run_at        = excluded.last_run_at,
                    last_status        = excluded.last_status,
                    last_candidates    = excluded.last_candidates,
                    last_error         = excluded.last_error,
                    last_ok_at         = excluded.last_ok_at,
                    consecutive_empty  = excluded.consecutive_empty,
                    consecutive_errors = excluded.consecutive_errors,
                    total_runs         = excluded.total_runs
                """,
                (source, now, status, candidates, error, last_ok, empty, errors, total),
            )

    def get_source_health(self) -> list[dict]:
        """Every source's current health, worst first."""
        rows = self._conn.execute(
            """
            SELECT source, last_run_at, last_status, last_candidates, last_error,
                   last_ok_at, consecutive_empty, consecutive_errors, total_runs
            FROM source_health
            ORDER BY consecutive_errors DESC, consecutive_empty DESC, source
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # -- central academic profile (Phase 3) -------------------------------

    def save_profile_upload(
        self, filename: str, content_type: str | None, raw: bytes, when: datetime | None = None
    ) -> int:
        """Store an uploaded résumé byte-for-byte (immutable). Returns its id."""
        digest = hashlib.sha256(raw).hexdigest()
        with self._tx() as conn:
            cur = conn.execute(
                """
                INSERT INTO profile_uploads
                    (filename, content_type, content_hash, size_bytes, raw, uploaded_at)
                VALUES (?,?,?,?,?,?)
                """,
                (filename, content_type, digest, len(raw), raw, _dt(when or datetime.now(UTC))),
            )
            return int(cur.lastrowid)

    def get_profile_upload(self, upload_id: int) -> dict | None:
        """One stored upload, including its raw bytes (for download/re-extract)."""
        row = self._conn.execute(
            "SELECT id, filename, content_type, content_hash, size_bytes, raw, uploaded_at "
            "FROM profile_uploads WHERE id = ?",
            (upload_id,),
        ).fetchone()
        return dict(row) if row else None

    def latest_profile_upload(self) -> dict | None:
        row = self._conn.execute(
            "SELECT id, filename, content_type, content_hash, size_bytes, uploaded_at "
            "FROM profile_uploads ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def save_profile(
        self,
        profile_json: str,
        extracted_json: str,
        *,
        source_upload_id: int | None,
        model_version: str | None,
        prompt_version: int | None,
        user_edited: bool = False,
        when: datetime | None = None,
    ) -> None:
        """Insert or replace the singleton profile (id = 1)."""
        now = _dt(when or datetime.now(UTC))
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO profile (
                    id, profile_json, extracted_json, source_upload_id,
                    model_version, prompt_version, user_edited, extracted_at, updated_at
                ) VALUES (1,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    profile_json     = excluded.profile_json,
                    extracted_json   = excluded.extracted_json,
                    source_upload_id = excluded.source_upload_id,
                    model_version    = excluded.model_version,
                    prompt_version   = excluded.prompt_version,
                    user_edited      = excluded.user_edited,
                    extracted_at     = excluded.extracted_at,
                    updated_at       = excluded.updated_at
                """,
                (
                    profile_json,
                    extracted_json,
                    source_upload_id,
                    model_version,
                    prompt_version,
                    1 if user_edited else 0,
                    now,
                    now,
                ),
            )

    def update_profile_json(self, profile_json: str, when: datetime | None = None) -> bool:
        """Save a user-edited profile over the working copy (extracted_json is
        left untouched, preserving provenance). Returns False if none exists."""
        if self.get_profile() is None:
            return False
        with self._tx() as conn:
            conn.execute(
                "UPDATE profile SET profile_json = ?, user_edited = 1, updated_at = ? WHERE id = 1",
                (profile_json, _dt(when or datetime.now(UTC))),
            )
        return True

    def get_profile(self) -> dict | None:
        row = self._conn.execute(
            "SELECT profile_json, extracted_json, source_upload_id, model_version, "
            "prompt_version, user_edited, extracted_at, updated_at FROM profile WHERE id = 1"
        ).fetchone()
        return dict(row) if row else None

    def delete_profile(self) -> None:
        """Erase the profile and every stored upload — the user's 'delete my
        data' button. Uploads go too: the résumé bytes are the sensitive part.
        Matches are profile-dependent, so they go as well."""
        with self._tx() as conn:
            conn.execute("DELETE FROM profile WHERE id = 1")
            conn.execute("DELETE FROM profile_uploads")
            conn.execute("DELETE FROM job_match")
            conn.execute("DELETE FROM job_tailoring")

    # -- match analysis cache (Phase 4) -----------------------------------

    def get_match(
        self, job_id: str, content_hash: str, profile_hash: str, prompt_version: int
    ) -> dict | None:
        """A cached match verdict for this exact (posting, profile, rubric), else
        None. Keyed on both hashes: editing the résumé or the posting invalidates."""
        row = self._conn.execute(
            """
            SELECT match_json FROM job_match
            WHERE job_id = ? AND content_hash = ? AND profile_hash = ? AND prompt_version = ?
            """,
            (job_id, content_hash, profile_hash, prompt_version),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["match_json"])
        except json.JSONDecodeError:  # pragma: no cover — defensive
            logger.warning("cached match for %s is not valid json; ignoring", job_id)
            return None

    def save_match(
        self,
        job_id: str,
        content_hash: str,
        profile_hash: str,
        prompt_version: int,
        match: dict,
        when: datetime | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO job_match
                    (job_id, content_hash, profile_hash, prompt_version, match_json, analyzed_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    content_hash   = excluded.content_hash,
                    profile_hash   = excluded.profile_hash,
                    prompt_version = excluded.prompt_version,
                    match_json     = excluded.match_json,
                    analyzed_at    = excluded.analyzed_at
                """,
                (
                    job_id,
                    content_hash,
                    profile_hash,
                    prompt_version,
                    json.dumps(match),
                    _dt(when or datetime.now(UTC)),
                ),
            )

    # -- résumé tailoring cache (Phase 5) ---------------------------------

    def get_tailoring(
        self, job_id: str, content_hash: str, profile_hash: str, prompt_version: int
    ) -> dict | None:
        row = self._conn.execute(
            """
            SELECT plan_json FROM job_tailoring
            WHERE job_id = ? AND content_hash = ? AND profile_hash = ? AND prompt_version = ?
            """,
            (job_id, content_hash, profile_hash, prompt_version),
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["plan_json"])
        except json.JSONDecodeError:  # pragma: no cover — defensive
            logger.warning("cached tailoring for %s is not valid json; ignoring", job_id)
            return None

    def save_tailoring(
        self,
        job_id: str,
        content_hash: str,
        profile_hash: str,
        prompt_version: int,
        plan: dict,
        when: datetime | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO job_tailoring
                    (job_id, content_hash, profile_hash, prompt_version, plan_json, tailored_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    content_hash   = excluded.content_hash,
                    profile_hash   = excluded.profile_hash,
                    prompt_version = excluded.prompt_version,
                    plan_json      = excluded.plan_json,
                    tailored_at    = excluded.tailored_at
                """,
                (
                    job_id,
                    content_hash,
                    profile_hash,
                    prompt_version,
                    json.dumps(plan),
                    _dt(when or datetime.now(UTC)),
                ),
            )

    # -- research-group intelligence cache (Phase 6) ----------------------

    def get_research(self, job_id: str, query_key: str, max_age_days: int) -> dict | None:
        """A cached OpenAlex snapshot for this job, if the same institution/field
        was looked up within `max_age_days`. Otherwise None (re-fetch)."""
        row = self._conn.execute(
            "SELECT research_json, fetched_at FROM job_research WHERE job_id = ? AND query_key = ?",
            (job_id, query_key),
        ).fetchone()
        if row is None:
            return None
        fetched = _parse_dt(row["fetched_at"])
        if fetched and datetime.now(UTC) - fetched > timedelta(days=max_age_days):
            return None
        try:
            return json.loads(row["research_json"])
        except json.JSONDecodeError:  # pragma: no cover — defensive
            return None

    def save_research(
        self, job_id: str, query_key: str, research: dict, when: datetime | None = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO job_research (job_id, query_key, research_json, fetched_at)
                VALUES (?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    query_key     = excluded.query_key,
                    research_json = excluded.research_json,
                    fetched_at    = excluded.fetched_at
                """,
                (job_id, query_key, json.dumps(research), _dt(when or datetime.now(UTC))),
            )

    def purge_old_rejected(self, retention_days: int) -> int:
        """Drop stale rejected jobs. Returns rows removed."""
        cutoff = _dt(datetime.now(UTC) - timedelta(days=retention_days))
        with self._tx() as conn:
            cursor = conn.execute(
                "DELETE FROM jobs WHERE status = ? AND discovered_at < ?",
                (JobStatus.REJECTED.value, cutoff),
            )
        return cursor.rowcount


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        source=row["source"],
        source_job_id=row["source_job_id"],
        title=row["title"],
        organization=row["organization"],
        location=row["location"],
        # `or "Germany"` used to live here too, so a NULL country was re-invented
        # as Germany on every single read. Fixing the model default alone would
        # have left the lie intact one layer down.
        country=row["country"],
        city=row["city"],
        remote_status=row["remote_status"] or "unknown",
        description=row["description"],
        url=row["url"],
        contact_email=row["contact_email"],
        contact_url=row["contact_url"],
        published_at=_parse_dt(row["published_at"]),
        discovered_at=_parse_dt(row["discovered_at"]) or datetime.now(UTC),
        enriched_at=_parse_dt(row["enriched_at"]),
        application_deadline=_parse_dt(row["application_deadline"]),
        employment_type=row["employment_type"],
        language=row["language"] or "unknown",
        salary=row["salary"],
        relevance_score=row["relevance_score"],
        matched_keywords=json.loads(row["matched_keywords"] or "[]"),
        score_explanation=json.loads(row["score_explanation"] or "[]"),
        card_summary=row["card_summary"],
        opportunity_type=row["opportunity_type"],
        applicant_level=row["applicant_level"],
        academic_field=row["academic_field"],
        content_hash=row["content_hash"],
        notified_at=_parse_dt(row["notified_at"]),
        status=row["status"],
    )
