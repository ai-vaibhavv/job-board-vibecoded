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
            [*_MIGRATIONS, "ALTER TABLE jobs ADD COLUMN city TEXT;"],
        )
        with Database(path) as db:
            assert db.migrate() == 2

        backups = list(tmp_path.glob("jobs.db.v1.bak"))
        assert backups, f"no backup written; found {[p.name for p in tmp_path.iterdir()]}"

        # The backup must be a real, readable database with the rows in it —
        # not a zero-byte file that only looks like insurance.
        conn = sqlite3.connect(str(backups[0]))
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        # It is the *pre*-migration state: the new column must not be in it.
        columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        assert "city" not in columns
        conn.close()

    def test_no_backup_when_there_is_nothing_pending(self, tmp_path):
        """A v1 database against a v1 schema has no migration to protect against,
        so it must not litter a .bak on every single run."""
        path = tmp_path / "jobs.db"
        _make_v1_db(path, rows=3)
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
            [*_MIGRATIONS, "ALTER TABLE jobs ADD COLUMN city TEXT;"],
        )
        monkeypatch.setattr("job_alerts.database.sqlite3.connect", _only_for_backup())

        with Database(path) as db:
            assert db.migrate() == 2
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
