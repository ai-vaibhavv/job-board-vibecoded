"""Per-source health tracking: streaks, flagging, and the pipeline hook."""

from __future__ import annotations

from job_alerts.database import Database
from job_alerts.models import SourceResult
from job_alerts.pipeline import Pipeline


class TestHealthStreaks:
    def test_ok_resets_streaks_and_sets_last_ok(self, tmp_path):
        with Database(tmp_path / "h.db") as db:
            db.record_source_health("s", "empty", 0)
            db.record_source_health("s", "empty", 0)
            db.record_source_health("s", "ok", 12)
            h = db.get_source_health()[0]
            assert h["consecutive_empty"] == 0
            assert h["consecutive_errors"] == 0
            assert h["last_candidates"] == 12
            assert h["last_ok_at"] is not None
            assert h["total_runs"] == 3

    def test_empty_runs_accumulate(self, tmp_path):
        with Database(tmp_path / "h.db") as db:
            for _ in range(3):
                db.record_source_health("s", "empty", 0)
            assert db.get_source_health()[0]["consecutive_empty"] == 3

    def test_errors_accumulate_and_store_message(self, tmp_path):
        with Database(tmp_path / "h.db") as db:
            db.record_source_health("s", "error", 0, "HTTP 500")
            db.record_source_health("s", "error", 0, "timeout")
            h = db.get_source_health()[0]
            assert h["consecutive_errors"] == 2
            assert h["last_error"] == "timeout"

    def test_skipped_leaves_streaks_untouched(self, tmp_path):
        with Database(tmp_path / "h.db") as db:
            db.record_source_health("s", "empty", 0)
            db.record_source_health("s", "skipped", 0)  # e.g. no API key
            h = db.get_source_health()[0]
            assert h["consecutive_empty"] == 1  # not reset, not advanced
            assert h["last_status"] == "skipped"

    def test_ok_after_error_clears_it(self, tmp_path):
        with Database(tmp_path / "h.db") as db:
            db.record_source_health("s", "error", 0, "boom")
            db.record_source_health("s", "ok", 5)
            assert db.get_source_health()[0]["consecutive_errors"] == 0

    def test_worst_source_sorts_first(self, tmp_path):
        with Database(tmp_path / "h.db") as db:
            db.record_source_health("good", "ok", 10)
            db.record_source_health("bad", "error", 0, "x")
            db.record_source_health("bad", "error", 0, "x")
            assert db.get_source_health()[0]["source"] == "bad"


class TestPipelineFlagsAilingSources:
    def _pipe(self, settings, sources_config, secrets, db):
        return Pipeline(settings, sources_config, secrets, db)

    def test_three_empty_runs_flags_the_source(
        self, settings, sources_config, secrets, db, summary_factory
    ):
        pipe = self._pipe(settings, sources_config, secrets, db)
        empty = [SourceResult(source="dept_feed", candidates=[])]
        for _ in range(2):
            s = summary_factory()
            pipe._record_source_health(empty, s)
            assert "dept_feed" not in s.unhealthy_sources  # not yet
        s = summary_factory()
        pipe._record_source_health(empty, s)
        assert "dept_feed" in s.unhealthy_sources
        assert "selector" in s.unhealthy_sources["dept_feed"]

    def test_a_healthy_source_is_never_flagged(
        self, settings, sources_config, secrets, db, summary_factory
    ):
        pipe = self._pipe(settings, sources_config, secrets, db)
        from job_alerts.models import JobCandidate

        ok = [SourceResult(source="uni", candidates=[JobCandidate(source="uni", title="X", url="u")])]
        for _ in range(4):
            s = summary_factory()
            pipe._record_source_health(ok, s)
        assert "uni" not in s.unhealthy_sources
