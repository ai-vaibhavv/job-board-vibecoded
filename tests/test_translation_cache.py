"""The v6 translation cache: a German posting is translated once, not per view.

Same discipline as the assessment cache — keyed on `content_hash`, so an edited
posting is re-translated, and CASCADE so a translation cannot outlive its job.
"""

from __future__ import annotations

import sqlite3

import pytest

from job_alerts.database import Database


class TestTranslationCache:
    def test_round_trips(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            assert db.get_translation("j1", "h1") is None

            db.save_translation("j1", "h1", "English text", "EN blurb", truncated=True)
            got = db.get_translation("j1", "h1")
            assert got == {
                "description_en": "English text",
                "card_summary_en": "EN blurb",
                "truncated": True,
            }

    def test_an_edited_posting_invalidates_its_translation(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_translation("j1", "h1", "old English")
            # Same job, new content hash -> stale translation must not be served.
            assert db.get_translation("j1", "h2") is None

    def test_retranslating_replaces_rather_than_duplicates(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_translation("j1", "h1", "first")
            db.save_translation("j1", "h2", "second")
            assert db.get_translation("j1", "h2")["description_en"] == "second"
            count = db._conn.execute("SELECT count(*) FROM job_translations").fetchone()[0]
            assert count == 1

    def test_a_translation_cannot_outlive_its_job(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_translation("j1", "h1", "English")
            with db._tx() as conn:
                conn.execute("DELETE FROM jobs WHERE id = 'j1'")
            orphans = db._conn.execute("SELECT count(*) FROM job_translations").fetchone()[0]
            assert orphans == 0

    def test_a_translation_for_an_unknown_job_is_refused(self, tmp_path):
        with Database(tmp_path / "jobs.db") as db, pytest.raises(sqlite3.IntegrityError):
            db.save_translation("ghost", "h1", "English")

    def test_missing_card_summary_reads_back_as_none(self, tmp_path, job_factory):
        with Database(tmp_path / "jobs.db") as db:
            db.upsert(job_factory(id="j1"))
            db.save_translation("j1", "h1", "English only")
            assert db.get_translation("j1", "h1")["card_summary_en"] is None
