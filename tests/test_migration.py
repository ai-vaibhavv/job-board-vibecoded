"""Migrations: schema changes must not cost you your notification history.

The live database holds months of "already told you about this job". Losing it
does not look like data loss — it looks like every old job being announced to
Discord again, at once. These tests exist to make that impossible to ship.
"""

from __future__ import annotations

import sqlite3

import pytest

from job_alerts.database import _MIGRATIONS, Database


def _make_v1_db(path, *, rows: int = 3) -> None:
    """A database as it existed at schema v1, with real notification history."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_MIGRATIONS[0])
    conn.execute("PRAGMA user_version = 1")
    for i in range(rows):
        conn.execute(
            """
            INSERT INTO jobs (id, source, title, url, discovered_at, content_hash,
                              relevance_score, status, notified_at, country)
            VALUES (?, 'fraunhofer', ?, ?, '2026-07-01T08:00:00+00:00', ?, 72,
                    'notified', '2026-07-01T08:05:00+00:00', 'Germany')
            """,
            (f"fraunhofer:{i}", f"Studentische Hilfskraft {i}", f"https://f.de/{i}", f"hash{i}"),
        )
    conn.commit()
    conn.close()


class TestMigrationSafety:
    def test_a_v1_database_upgrades_without_losing_rows(self, tmp_path):
        path = tmp_path / "jobs.db"
        _make_v1_db(path, rows=5)

        with Database(path) as db:
            assert db.migrate() == len(_MIGRATIONS)
            jobs = db.list_jobs(limit=100)

        assert len(jobs) == 5

    def test_notification_history_survives(self, tmp_path):
        """The one that matters. If `notified_at` or `status` is lost, the next
        run re-announces every job in the database to Discord."""
        path = tmp_path / "jobs.db"
        _make_v1_db(path, rows=5)

        with Database(path) as db:
            jobs = db.list_jobs(limit=100)

        assert len(jobs) == 5
        assert all(j.status == "notified" for j in jobs)
        assert all(j.notified_at is not None for j in jobs)

    def test_a_backup_is_written_before_migrating(self, tmp_path, monkeypatch):
        """Simulates a pending migration rather than waiting for a real one, so
        the safety net is proven now and not on the day it is first needed."""
        path = tmp_path / "jobs.db"
        _make_v1_db(path, rows=3)

        monkeypatch.setattr(
            "job_alerts.database._MIGRATIONS",
            [*_MIGRATIONS, "ALTER TABLE jobs ADD COLUMN _probe TEXT;"],
        )
        with Database(path) as db:
            assert db.migrate() == len(_MIGRATIONS) + 1

        backups = list(tmp_path.glob("jobs.db.v1.bak"))
        assert backups, f"no backup written; found {[p.name for p in tmp_path.iterdir()]}"

        # The backup must be a real, readable database with the rows in it —
        # not a zero-byte file that only looks like insurance.
        conn = sqlite3.connect(str(backups[0]))
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        # It is the *pre*-migration state: the new column must not be in it.
        columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert "_probe" not in columns
        conn.close()

    def test_no_backup_when_there_is_nothing_pending(self, tmp_path):
        """An already-current database has no migration to protect against, so
        it must not litter a fresh .bak on every single run."""
        path = tmp_path / "jobs.db"
        with Database(path):
            pass  # brings a fresh file up to the current version
        for stale in tmp_path.glob("*.bak"):
            stale.unlink()

        with Database(path):
            pass
        assert not list(tmp_path.glob("*.bak"))

    def test_a_fresh_database_is_not_backed_up(self, tmp_path):
        """Nothing to lose, so no litter."""
        path = tmp_path / "jobs.db"
        with Database(path):
            pass
        assert not list(tmp_path.glob("*.bak"))

    def test_migrating_an_already_current_database_is_a_no_op(self, tmp_path):
        path = tmp_path / "jobs.db"
        with Database(path) as db:
            first = db.migrate()
            assert db.migrate() == first
        # No backup: there were no pending migrations to protect against.
        assert not list(tmp_path.glob("*.bak"))

    def test_foreign_keys_are_on_once_migration_has_finished(self, tmp_path):
        """Off during migrate() so a table rebuild cannot have its REFERENCES
        clauses silently repointed at the dropped table; on afterwards, because
        ON DELETE CASCADE has to work."""
        path = tmp_path / "jobs.db"
        with Database(path) as db:
            assert db._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    def test_a_failing_backup_does_not_stop_the_run(self, tmp_path, monkeypatch):
        """Insurance that refuses to be written must not also refuse the run."""
        path = tmp_path / "jobs.db"
        _make_v1_db(path, rows=2)

        monkeypatch.setattr(
            "job_alerts.database._MIGRATIONS",
            [*_MIGRATIONS, "ALTER TABLE jobs ADD COLUMN _probe TEXT;"],
        )
        monkeypatch.setattr("job_alerts.database.sqlite3.connect", _only_for_backup())

        with Database(path) as db:
            assert db.migrate() == len(_MIGRATIONS) + 1
            assert len(db.list_jobs(limit=10)) == 2
        assert not list(tmp_path.glob("*.bak"))


def _only_for_backup():
    """Break sqlite3.connect only for `.bak` targets, so the database under test
    still opens normally."""
    real = sqlite3.connect

    def fake(target, *args, **kwargs):
        if str(target).endswith(".bak"):
            raise OSError("disk full")
        return real(target, *args, **kwargs)

    return fake


class TestV2Enrichment:
    """v2 adds enrichment columns and the assessment cache. Additive only: no
    table is rebuilt, so no existing row can be lost."""

    def test_v1_rows_survive_and_keep_their_notification_history(self, tmp_path):
        path = tmp_path / "jobs.db"
        _make_v1_db(path, rows=4)

        with Database(path) as db:
            jobs = db.list_jobs(limit=100)
            assert len(jobs) == 4
            assert all(j.status == "notified" for j in jobs)
            assert all(j.notified_at is not None for j in jobs)
            # New columns exist and are empty, not invented.
            assert all(j.city is None for j in jobs)
            assert all(j.enriched_at is None for j in jobs)
            assert all(j.contact_email is None for j in jobs)

    def test_a_null_country_reads_back_as_none_not_germany(self, tmp_path):
        """`_row_to_job` used to do `row["country"] or "Germany"`, so an unknown
        country was re-invented on every read. The model default was only half
        the bug."""
        path = tmp_path / "jobs.db"
        _make_v1_db(path, rows=1)
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE jobs SET country = NULL")
        conn.commit()
        conn.close()

        with Database(path) as db:
            assert db.list_jobs(limit=1)[0].country is None

    def test_the_assessment_cache_round_trips(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            assert db.get_assessment("j1", "hash1", 1) is None

            db.save_assessment("j1", "hash1", 1, {"score": 80, "role_type": "hiwi"}, "gemini")
            assert db.get_assessment("j1", "hash1", 1) == {"score": 80, "role_type": "hiwi"}

    def test_an_edited_posting_invalidates_its_verdict(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_assessment("j1", "hash1", 1, {"score": 80}, "gemini")
            # Same job, new content -> the old verdict must not be reused.
            assert db.get_assessment("j1", "hash2", 1) is None

    def test_changing_the_prompt_invalidates_old_verdicts(self, tmp_path, job_factory):
        """Otherwise two rubrics coexist in one database and scores stop being
        reproducible."""
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_assessment("j1", "hash1", 1, {"score": 80}, "gemini")
            assert db.get_assessment("j1", "hash1", 2) is None

    def test_reassessing_replaces_rather_than_duplicates(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_assessment("j1", "hash1", 1, {"score": 80}, "gemini")
            db.save_assessment("j1", "hash2", 1, {"score": 40}, "groq")
            assert db.get_assessment("j1", "hash2", 1) == {"score": 40}
            count = db._conn.execute("SELECT count(*) FROM job_assessments").fetchone()[0]
            assert count == 1

    def test_an_assessment_cannot_outlive_its_job(self, tmp_path, job_factory):
        """ON DELETE CASCADE, which only works because foreign_keys is enabled
        after migrate() rather than never."""
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_assessment("j1", "hash1", 1, {"score": 80}, "gemini")

            with db._tx() as conn:
                conn.execute("DELETE FROM jobs WHERE id = 'j1'")

            orphans = db._conn.execute("SELECT count(*) FROM job_assessments").fetchone()[0]
            assert orphans == 0

    def test_a_verdict_for_an_unknown_job_is_refused(self, tmp_path):
        """The cache is keyed to real jobs; a dangling verdict is a bug, and the
        foreign key is what says so instead of letting it rot in the table."""
        with Database(tmp_path / "jobs.db") as db, pytest.raises(sqlite3.IntegrityError):
            db.save_assessment("ghost", "hash1", 1, {"score": 80}, "gemini")


class TestV3CountryRetraction:
    """v3 withdraws the "Germany" claim from rows that never earned it."""

    def _seed(self, path, source: str, country: str) -> None:
        conn = sqlite3.connect(str(path))
        conn.executescript(_MIGRATIONS[0])
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            """
            INSERT INTO jobs (id, source, title, url, discovered_at, content_hash, country)
            VALUES (?, ?, 'T', ?, '2026-07-01T08:00:00+00:00', 'h', ?)
            """,
            (f"{source}:1", source, f"https://x.de/{source}", country),
        )
        conn.commit()
        conn.close()

    def test_search_discovered_jobs_stop_claiming_germany(self, tmp_path):
        """These are the rows that said Germany while being Nigerian."""
        path = tmp_path / "jobs.db"
        self._seed(path, "search_discovery", "Germany")
        with Database(path) as db:
            assert db.list_jobs(limit=1)[0].country is None

    def test_a_source_that_knows_its_country_keeps_it(self, tmp_path):
        """fraunhofer only ever lists German jobs and says so through
        `defaults:`. That is an assertion, not a default, and it survives."""
        path = tmp_path / "jobs.db"
        self._seed(path, "fraunhofer", "Germany")
        with Database(path) as db:
            assert db.list_jobs(limit=1)[0].country == "Germany"


@pytest.mark.parametrize("version", range(len(_MIGRATIONS)))
def test_every_migration_is_applied_in_order(tmp_path, version):
    """Upgrading from any historical version lands on the current schema."""
    path = tmp_path / f"v{version}.db"
    conn = sqlite3.connect(str(path))
    for script in _MIGRATIONS[:version]:
        conn.executescript(script)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()

    with Database(path) as db:
        assert db.migrate() == len(_MIGRATIONS)
