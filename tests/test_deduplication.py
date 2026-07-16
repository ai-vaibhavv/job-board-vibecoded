"""Deduplication — across tracking-URL variants, sources and runs.

This is the behaviour that decides whether the tool is pleasant or spammy, so
it is tested from several angles.
"""

from __future__ import annotations

from job_alerts.database import Database
from job_alerts.models import JobCandidate, JobStatus
from job_alerts.normalization import normalize_candidate
from job_alerts.pipeline import Pipeline

from .conftest import make_job


class TestUrlVariantDeduplication:
    def test_tracking_url_variants_collapse_to_one_job(self):
        """The spec's headline dedup case: the same posting reached through
        different tracking URLs must be one job."""
        variants = [
            "https://uni-berlin.de/jobs/hiwi-ml?utm_source=newsletter&utm_medium=email",
            "http://www.uni-berlin.de/jobs/hiwi-ml/",
            "https://uni-berlin.de/jobs/hiwi-ml#apply",
            "https://www.uni-berlin.de/jobs/hiwi-ml?trk=public_jobs&refId=abc",
        ]
        jobs = [
            normalize_candidate(
                JobCandidate(
                    source="s", title="HiWi ML", organization="FU", location="Berlin", url=v
                )
            )
            for v in variants
        ]
        assert len({j.url for j in jobs}) == 1
        # No source_job_id, so identity comes from the hash — which must also agree.
        assert len({j.id for j in jobs}) == 1

    def test_linkedin_variants_collapse(self):
        variants = [
            "https://www.linkedin.com/jobs/view/research-assistant-at-tum-4012345678?trk=x",
            "https://de.linkedin.com/jobs/view/4012345678?refId=y",
        ]
        jobs = [normalize_candidate(JobCandidate(source="s", title="RA", url=v)) for v in variants]
        assert len({j.url for j in jobs}) == 1


class TestDatabaseDeduplication:
    def test_same_id_is_not_new_twice(self, db: Database):
        job = make_job(id="s:1")
        assert db.upsert(job) is True
        assert db.upsert(job) is False

    def test_duplicate_detected_by_url_across_different_sources(self, db: Database):
        """The key cross-source case: an RSS feed and a search engine find the
        same posting. Their ids differ, but the normalized URL does not."""
        rss_job = make_job(id="rss:123", source="rss", url="https://uni.de/jobs/1")
        search_job = make_job(id="search:h:abc", source="search", url="https://uni.de/jobs/1")

        assert db.upsert(rss_job) is True
        assert db.is_duplicate(search_job) is True

    def test_different_urls_are_not_duplicates(self, db: Database):
        db.upsert(make_job(id="s:1", url="https://uni.de/jobs/1"))
        assert db.is_duplicate(make_job(id="s:2", url="https://uni.de/jobs/2")) is False

    def test_upsert_preserves_notified_state(self, db: Database):
        """Re-finding a job must never resurrect it into the unsent set."""
        job = make_job(id="s:1")
        db.upsert(job)
        db.mark_notified(["s:1"])

        refreshed = make_job(id="s:1", title="Research Assistant (updated)")
        db.upsert(refreshed)

        stored = db.get("s:1")
        assert stored.notified_at is not None
        assert stored.status is JobStatus.NOTIFIED
        assert stored.title == "Research Assistant (updated)"  # content did refresh

    def test_upsert_refreshes_score_and_description(self, db: Database):
        db.upsert(make_job(id="s:1", relevance_score=40, description="old"))
        db.upsert(make_job(id="s:1", relevance_score=80, description="new"))
        stored = db.get("s:1")
        assert stored.relevance_score == 80
        assert stored.description == "new"


class TestDuplicateDiscoveryQueries:
    """Two different discovery queries returning the same job — the spec calls
    this out specifically."""

    def test_same_job_from_two_queries_is_stored_once(self, db: Database):
        from_query_1 = normalize_candidate(
            JobCandidate(
                source="search_discovery",
                title="Research Assistant Machine Learning",
                url="https://linkedin.com/jobs/view/4012345678?trk=query1",
            )
        )
        from_query_2 = normalize_candidate(
            JobCandidate(
                source="search_discovery",
                title="Research Assistant Machine Learning",
                url="https://www.linkedin.com/jobs/view/ra-ml-at-tum-4012345678?refId=query2",
            )
        )
        assert from_query_1.url == from_query_2.url
        assert db.upsert(from_query_1) is True
        assert db.upsert(from_query_2) is False
        assert len(db.list_jobs(limit=10)) == 1


class TestPipelineDeduplication:
    async def test_in_run_duplicates_are_collapsed(self, settings, sources_config, secrets, db):
        """The mock source ships a deliberate duplicate (mock-001 reached via a
        tracking URL). One run must not notify it twice."""
        pipeline = Pipeline(settings, sources_config, secrets, db)
        summary = await pipeline.run(dry_run=True)

        assert summary.candidates_found == 8  # fixture count
        assert summary.after_dedup == 7  # the duplicate folded in

    async def test_second_run_notifies_nothing_new(
        self, settings, sources_config, discord_secrets, db, monkeypatch
    ):
        """Across runs: the same jobs must not be re-sent."""
        sent: list[list] = []

        async def fake_send_jobs(self, jobs, *, extra_stored=0):
            from job_alerts.notifications.base import DeliveryResult

            sent.append(jobs)
            return DeliveryResult(delivered_ids=[j.id for j in jobs], messages_sent=1)

        monkeypatch.setattr(
            "job_alerts.notifications.discord.DiscordNotifier.send_jobs", fake_send_jobs
        )

        pipeline = Pipeline(settings, sources_config, discord_secrets, db)
        first = await pipeline.run()
        assert first.notified > 0

        second = await pipeline.run()
        assert second.notified == 0
        assert second.newly_stored == 0
        assert len(sent) == 1  # no second Discord call


class TestDryRunIsolation:
    async def test_dry_run_does_not_write_to_the_database(
        self, settings, sources_config, secrets, db
    ):
        """If a dry run stored jobs, the first real run would think it had
        already seen them and stay silent — the worst possible failure."""
        pipeline = Pipeline(settings, sources_config, secrets, db)
        await pipeline.run(dry_run=True)
        assert db.list_jobs(limit=100) == []
        assert db.stats()["total_jobs"] == 0
