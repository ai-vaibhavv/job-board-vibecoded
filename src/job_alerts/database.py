"""SQLite persistence and deduplication.

Plain `sqlite3` rather than an ORM: there is one table, the queries are simple,
and every statement here is parameterized and auditable at a glance.

Schema changes go through `_MIGRATIONS`, keyed by `PRAGMA user_version`. On
startup `migrate()` applies every migration above the stored version, so an
existing database upgrades in place and a fresh one is built from scratch by
the same code path.
"""

from __future__ import annotations

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
        """
        is_new = not self.is_duplicate(job)
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    id, source, source_job_id, title, organization, location, country,
                    remote_status, description, url, published_at, discovered_at,
                    application_deadline, employment_type, language, salary,
                    relevance_score, matched_keywords, score_explanation, content_hash,
                    notified_at, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title             = excluded.title,
                    organization      = excluded.organization,
                    location          = excluded.location,
                    description       = excluded.description,
                    relevance_score   = excluded.relevance_score,
                    matched_keywords  = excluded.matched_keywords,
                    score_explanation = excluded.score_explanation,
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
                    job.remote_status.value,
                    job.description,
                    job.url,
                    _dt(job.published_at),
                    _dt(job.discovered_at),
                    _dt(job.application_deadline),
                    job.employment_type,
                    job.language.value,
                    job.salary,
                    job.relevance_score,
                    json.dumps(job.matched_keywords),
                    json.dumps(job.score_explanation),
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

    def record_run(self, started_at: datetime, finished_at: datetime, summary_json: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO runs (started_at, finished_at, summary_json) VALUES (?,?,?)",
                (_dt(started_at), _dt(finished_at), summary_json),
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
        country=row["country"] or "Germany",
        remote_status=row["remote_status"] or "unknown",
        description=row["description"],
        url=row["url"],
        published_at=_parse_dt(row["published_at"]),
        discovered_at=_parse_dt(row["discovered_at"]) or datetime.now(UTC),
        application_deadline=_parse_dt(row["application_deadline"]),
        employment_type=row["employment_type"],
        language=row["language"] or "unknown",
        salary=row["salary"],
        relevance_score=row["relevance_score"],
        matched_keywords=json.loads(row["matched_keywords"] or "[]"),
        score_explanation=json.loads(row["score_explanation"] or "[]"),
        content_hash=row["content_hash"],
        notified_at=_parse_dt(row["notified_at"]),
        status=row["status"],
    )
