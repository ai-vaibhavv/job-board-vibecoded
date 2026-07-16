"""Soft-hide: 'drop' a job from the dashboard without deleting it.

The row stays in `jobs` with its REJECTED status and cached verdict, so a hidden
job is still deduplicated and never re-fetched or re-judged. Deleting it instead
would make it re-appear and re-cost LLM quota on the next run — the whole reason
this is a side table and not a DELETE.
"""

from __future__ import annotations

from job_alerts.database import Database


class TestHidden:
    def test_hide_then_list(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            assert db.hidden_ids() == set()
            db.hide_job("j1")
            assert db.hidden_ids() == {"j1"}

    def test_hide_is_idempotent(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.hide_job("j1")
            db.hide_job("j1")  # must not raise on the duplicate PK
            assert db.hidden_ids() == {"j1"}

    def test_unhide(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.hide_job("j1")
            db.unhide_job("j1")
            assert db.hidden_ids() == set()

    def test_hiding_does_not_touch_the_job_row(self, tmp_path, job_factory):
        """A hidden job keeps its status and history — it is only hidden, not
        forgotten, which is what preserves dedup and the verdict cache."""
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.mark_notified(["j1"])
            db.hide_job("j1")
            job = db.get("j1")
            assert job is not None
            assert job.notified_at is not None
            assert db.is_duplicate(job) is True

    def test_hidden_flag_cascades_on_delete(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.hide_job("j1")
            with db._tx() as conn:
                conn.execute("DELETE FROM jobs WHERE id = 'j1'")
            assert db.hidden_ids() == set()
