"""Shared fixtures.

Every test here is offline. No test may touch a live website — the spec
requires it, and a test suite that depends on a job board being up is a test
suite that fails at 3am for no reason.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from job_alerts.config import (
    FilteringSettings,
    HttpSettings,
    KeywordSettings,
    LlmSettings,
    LocationSettings,
    NotificationSettings,
    ScoringSettings,
    Secrets,
    Settings,
    SourceConfig,
    SourcesConfig,
)
from job_alerts.database import Database
from job_alerts.models import Job, JobCandidate
from job_alerts.normalization import content_hash


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Realistic settings mirroring config/settings.example.yaml."""
    return Settings(
        keywords=KeywordSettings(
            positive=[
                "research assistant",
                "research intern",
                "student researcher",
                "working student research",
                "student assistant",
                "wissenschaftliche hilfskraft",
                "studentische hilfskraft",
                "hiwi",
                "werkstudent forschung",
                "master thesis",
                "masterarbeit",
                "machine learning intern",
            ],
            negative=[
                "professor",
                "postdoc",
                "postdoctoral",
                "senior",
                "principal",
                "director",
                "head of",
                "mandatory completed PhD",
                "doctorate required",
                "ausbildung",
            ],
            topics=[
                "artificial intelligence",
                "machine learning",
                "deep learning",
                "NLP",
                "natural language processing",
                "computer vision",
                "data science",
                "robotics",
                "software engineering",
            ],
        ),
        filtering=FilteringSettings(
            phd_requires_explicit_signal=True,
            # Kept in sync with config/settings.example.yaml. Real postings
            # phrase this as "a completed PhD is required", so the list must
            # cover the "is" variants or the nuance rule silently under-fires.
            phd_requirement_signals=[
                "completed phd required",
                "mandatory completed phd",
                "phd is required",
                "phd required",
                "doctorate required",
                "doctoral degree required",
                "requires a completed phd",
                "must hold a phd",
                "abgeschlossene promotion",
                "promotion vorausgesetzt",
                "promotion erforderlich",
            ],
            word_boundary_matching=True,
        ),
        # Scoring lists must be populated here, not patched in by a separate
        # fixture: the pipeline reads them straight off `settings`, so leaving
        # them empty would silently score every job far too low and make the
        # end-to-end tests meaningless.
        scoring=ScoringSettings(
            min_score_to_notify=55,
            exact_titles=[
                "research assistant",
                "student research assistant",
                "research intern",
                "research internship",
                "working student research",
                "wissenschaftliche hilfskraft",
                "studentische hilfskraft",
                "hiwi",
                "werkstudent forschung",
                "master thesis",
                "masterarbeit",
            ],
            masters_signals=[
                "master student",
                "master's student",
                "masterstudent",
                "masterand",
                "msc student",
                "graduate student",
                "enrolled student",
                "students enrolled",
                "immatrikuliert",
                "eingeschriebene studierende",
            ],
            unrelated_disciplines=["nursing", "dentistry", "theology", "veterinary", "pflege"],
        ),
        locations=LocationSettings(
            all_germany=True,
            include=[
                "Berlin",
                "Munich",
                "München",
                "Hamburg",
                "Frankfurt",
                "Darmstadt",
                "Tübingen",
                "Bonn",
                "Germany",
                "Deutschland",
            ],
        ),
        # No real sleeping in tests: pacing/backoff are verified explicitly in
        # tests/test_llm.py rather than paid for in every pipeline test.
        llm=LlmSettings(min_request_interval=0.0, retry_base_delay=0.0, max_retries=0),
        notifications=NotificationSettings(max_per_run=10, embeds_per_message=5),
        http=HttpSettings(per_domain_delay=0.0, cache_ttl_seconds=0, max_retries=2),
    )


@pytest.fixture
def scoring_settings(settings) -> ScoringSettings:
    return settings.scoring


@pytest.fixture
def secrets() -> Secrets:
    """Secrets with nothing configured.

    `_env_file=None` stops pydantic-settings reading a developer's real `.env`
    and leaking their webhook into the test run.
    """
    return Secrets(
        _env_file=None,
        discord_webhook_url="",
        search_api_provider="",
        search_api_key="",
    )


# Single source of truth: tests mock this exact URL, so the fixture and the
# respx routes cannot drift apart.
TEST_WEBHOOK = "https://discord.com/api/webhooks/123456789/test-token-abc"


@pytest.fixture
def discord_secrets() -> Secrets:
    return Secrets(_env_file=None, discord_webhook_url=TEST_WEBHOOK)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def sources_config() -> SourcesConfig:
    return SourcesConfig(sources=[SourceConfig(name="mock", type="mock", enabled=True)])


def make_job(**overrides) -> Job:
    """A valid `Job` with sensible defaults; override what a test cares about."""
    base = {
        "id": "test:1",
        "source": "test",
        "title": "Research Assistant",
        "organization": "TU Munich",
        "location": "Munich",
        "country": "Germany",
        "description": "A research assistant position in machine learning.",
        "url": "https://example.de/jobs/1",
        "published_at": datetime.now(UTC) - timedelta(days=2),
        "discovered_at": datetime.now(UTC),
        "content_hash": "abc123",
    }
    base.update(overrides)
    if "content_hash" not in overrides:
        base["content_hash"] = content_hash(
            base["title"], base.get("organization"), base.get("location"), base.get("description")
        )
    return Job(**base)


def make_candidate(**overrides) -> JobCandidate:
    base = {
        "source": "test",
        "title": "Research Assistant",
        "organization": "TU Munich",
        "location": "Munich",
        "description": "A research assistant position.",
        "url": "https://example.de/jobs/1",
    }
    base.update(overrides)
    return JobCandidate(**base)


@pytest.fixture
def job_factory():
    return make_job


@pytest.fixture
def candidate_factory():
    return make_candidate
