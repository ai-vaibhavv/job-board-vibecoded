"""Configuration: secrets from the environment, everything else from YAML.

The split is deliberate and the spec requires it — a webhook URL or API key in
a YAML file ends up in git sooner or later. `Secrets` reads `.env`/the
environment; `Settings` reads `config/settings.yaml`; `SourcesConfig` reads
`config/sources.yaml`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised for user-fixable configuration problems.

    The CLI catches these and prints the message without a traceback — a
    missing webhook is a typo, not a crash.
    """


# ---------------------------------------------------------------------------
# Secrets (environment / .env)
# ---------------------------------------------------------------------------

SearchProvider = Literal["tavily", "brave", "bing", "google_cse", "serpapi", ""]


class Secrets(BaseSettings):
    """Never log or serialize these. See `.env.example`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    discord_webhook_url: str = ""
    search_api_provider: SearchProvider = ""
    search_api_key: str = ""
    google_cse_id: str = ""

    colab_api_key: str = ""
    """Optional bearer token for a self-hosted Colab/vLLM endpoint. Usually empty
    — a private tunnel needs no auth, and vLLM only checks a key when started
    with `--api-key`. The endpoint URL itself lives in `settings.yaml`
    (`llm.colab_base_url`), not here, because it is not a secret."""

    apify_token: str = ""
    """Apify, for LinkedIn post bodies a web search cannot reach. Optional: no
    token means the posts source disables itself and every other source runs
    exactly as before."""

    job_alerts_settings_file: Path = Path("config/settings.yaml")
    job_alerts_sources_file: Path = Path("config/sources.yaml")
    job_alerts_profile_file: Path = Path("config/profile.yaml")
    job_alerts_database_path: Path | None = None
    job_alerts_log_level: str | None = None
    job_alerts_log_format: Literal["text", "json"] | None = None

    @field_validator("search_api_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @property
    def has_discord(self) -> bool:
        return bool(self.discord_webhook_url.strip())

    @property
    def has_search_api(self) -> bool:
        if not self.search_api_provider or not self.search_api_key.strip():
            return False
        # Google CSE needs both a key and an engine id; a key alone cannot query.
        return not (self.search_api_provider == "google_cse" and not self.google_cse_id.strip())

    @property
    def has_apify(self) -> bool:
        return bool(self.apify_token.strip())

    def require_discord(self) -> str:
        if not self.has_discord:
            raise ConfigError(
                "DISCORD_WEBHOOK_URL is not set.\n"
                "  1. Copy the example file:  cp .env.example .env\n"
                "  2. In Discord: Server Settings -> Integrations -> Webhooks\n"
                "     -> New Webhook -> Copy URL\n"
                "  3. Paste it into .env as DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...\n"
                "Tip: `python -m job_alerts search --dry-run` needs no webhook at all."
            )
        return self.discord_webhook_url.strip()


# ---------------------------------------------------------------------------
# Settings (config/settings.yaml)
# ---------------------------------------------------------------------------


class SearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_age_days: int = 30
    max_results_per_source: int = 100

    enrich: bool = True
    """Fetch each thin job's own page before judging it. Off, and the pipeline is
    back to scoring a title and a 160-character snippet."""

    enrich_below_chars: int = 400
    """A description shorter than this means the source gave a stub, so the page
    is worth a fetch."""

    enrich_timeout: float = 240.0
    """Whole-stage budget. Exceeding it is not an error — whatever finished keeps
    its data, and the rest stay un-enriched, which the pipeline already handles."""

    drop_undated_when_enriched: bool = True
    """Drop a job that has no date *after* we fetched its page and looked.

    Not the same as dropping every undated job: when a fetch never happened, we
    have no opinion and the job is kept. This is what makes `max_age_days` mean
    something — before enrichment existed, 125/125 stored jobs were undated and
    the age filter was inert."""


class LocationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all_germany: bool = True
    include: list[str] = Field(default_factory=list)


class KeywordSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    positive: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)


class FilteringSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phd_requires_explicit_signal: bool = True
    phd_requirement_signals: list[str] = Field(default_factory=list)
    word_boundary_matching: bool = True


class ScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exact_title_match: int = 30
    research_or_student_keyword_in_title: int = 20
    topic_in_title: int = 15
    topic_in_description: int = 10
    location_match: int = 10
    masters_explicitly_eligible: int = 15
    english_speaking_role: int = 5
    recently_published: int = 5
    phd_required: int = -60
    senior_role: int = -40
    unrelated_discipline: int = -25


class ScoringSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_score_to_notify: int = 55
    weights: ScoringWeights = Field(default_factory=ScoringWeights)
    recent_days: int = 7
    exact_titles: list[str] = Field(default_factory=list)
    masters_signals: list[str] = Field(default_factory=list)
    unrelated_disciplines: list[str] = Field(default_factory=list)


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_per_run: int = 10
    embeds_per_message: int = Field(default=5, ge=1, le=10)
    """Discord rejects more than 10 embeds in one message."""
    description_excerpt_chars: int = 400


class HttpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_agent: str = "GermanyResearchJobAlerts/1.0 (personal job-search tool)"
    request_timeout: float = 20.0
    total_run_timeout: float = 600.0
    max_concurrency: int = 5
    per_domain_delay: float = 1.5
    max_retries: int = 3
    respect_robots: bool = True
    cache_ttl_seconds: int = 900


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Path = Path("data/jobs.db")
    rejected_retention_days: int = 60


class SchedulerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str = "Europe/Berlin"
    run_at: list[str] = Field(default_factory=lambda: ["08:00", "18:00"])
    lock_file: Path = Path("data/scheduler.lock")

    @field_validator("run_at")
    @classmethod
    def _validate_times(cls, value: list[str]) -> list[str]:
        for item in value:
            hour, _, minute = item.partition(":")
            if not (
                hour.isdigit()
                and minute.isdigit()
                and 0 <= int(hour) <= 23
                and 0 <= int(minute) <= 59
            ):
                raise ValueError(f"invalid run_at time {item!r}; expected 24h HH:MM")
        return value


class LlmSettings(BaseModel):
    """LLM-based assessment. Entirely optional: with no keys, or with
    `enabled: false`, the keyword scorer runs exactly as before."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    """When true AND a key is present, the LLM judges relevance. Falls back to
    keyword scoring automatically whenever it cannot."""

    providers: list[Literal["colab"]] = Field(default_factory=lambda: ["colab"])
    """Which LLM providers to use. Only the self-hosted `colab` provider exists;
    it runs when `colab_base_url` is set and otherwise the run falls back to
    keyword scoring."""

    colab_base_url: str = ""
    """Base URL of a self-hosted OpenAI-compatible server (e.g. the cloudflared
    tunnel in front of vLLM on Colab), without the `/v1/...` path. Empty means
    the `colab` provider is skipped even if listed in `providers`. Set it in
    `settings.yaml`; it changes every Colab session."""

    colab_model: str = "Qwen/Qwen2.5-7B-Instruct-AWQ"
    """Model name the self-hosted server was started with."""

    batch_size: int = Field(default=10, ge=1, le=25)
    """Jobs per request. Larger = fewer calls (kinder to per-minute request
    limits) but a longer prompt, more tokens per call, and more to lose if one
    batch fails."""

    max_concurrency: int = Field(default=1, ge=1, le=10)
    """Concurrent LLM requests. One by default, and that is not timidity:
    measured against the live free tiers, two concurrent unpaced requests get a
    429 immediately."""

    min_request_interval: float = Field(default=6.0, ge=0.0)
    """Seconds between requests to the SAME provider. Gemini's free tier allows
    ~10 requests/minute, so ~6s keeps us just under it. Set 0 to disable pacing
    (expect 429s on free tiers)."""

    max_retries: int = Field(default=2, ge=0, le=5)
    """Retries after EVERY provider has failed transiently (rate limit, 503,
    timeout). Permanent failures like a bad API key are never retried."""

    retry_base_delay: float = 20.0
    """Seconds before the first retry; doubles each attempt."""

    timeout: float = 60.0

    max_description_chars: int = 700
    """Characters of the description sent per job. This is the single biggest
    lever on token usage: at 1500 an 8-job prompt measured ~4.7k tokens against
    Groq's 12k tokens/minute limit — two requests per minute. 700 roughly halves
    that while keeping enough text to judge a posting."""

    prefilter_with_keywords: bool = True
    """Run the cheap keyword filter first and only send survivors to the LLM.
    Saves most of the quota. Turn off to let the LLM see every candidate."""

    min_score_to_notify: int | None = None
    """Notify threshold for LLM-scored jobs. Defaults to `scoring.min_score_to_notify`
    when unset. LLM scores are calibrated differently from keyword scores, so
    this exists to tune them independently."""


class LoggingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: str = "INFO"
    format: Literal["text", "json"] = "text"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: SearchSettings = Field(default_factory=SearchSettings)
    locations: LocationSettings = Field(default_factory=LocationSettings)
    keywords: KeywordSettings = Field(default_factory=KeywordSettings)
    filtering: FilteringSettings = Field(default_factory=FilteringSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


# ---------------------------------------------------------------------------
# Sources (config/sources.yaml)
# ---------------------------------------------------------------------------

SourceType = Literal["mock", "rss", "html", "search_api", "linkedin_posts"]


class SourceConfig(BaseModel):
    """One entry from sources.yaml. Extra keys are allowed so adapters can take
    their own options without this model needing to know about them."""

    model_config = ConfigDict(extra="allow")

    name: str
    type: SourceType
    enabled: bool = False
    url: str | None = None
    forbidden: bool = False
    """Set for sources whose terms disallow automated access. Hard-blocked even
    if someone flips `enabled` — see academics.de in sources.example.yaml."""

    selectors: dict[str, str] = Field(default_factory=dict)
    defaults: dict[str, str] = Field(default_factory=dict)
    queries: list[str] = Field(default_factory=list)
    max_results_per_query: int = 20

    allowed_domains: list[str] = Field(default_factory=list)
    """Hosts a search result must be on to be kept. Empty means no restriction.

    A search provider's own domain filter cannot be trusted: measured against
    Tavily, `site:fraunhofer.de` and `include_domains: ["fraunhofer.de"]` both
    returned *zero* fraunhofer results and a page of dictionary entries instead.
    It does not fail closed — it quietly returns unfiltered semantic matches. So
    the domain is enforced here, on our side, against what actually came back.

    Declare the exact host you want. `de.linkedin.com` — not `linkedin.com`,
    which legitimately admits `ng.linkedin.com` and `lk.linkedin.com`; that is
    how Nigerian and Sri Lankan jobs ended up in a German job board.
    """

    denied_url_patterns: list[str] = Field(default_factory=list)
    """Regexes; a result whose URL matches any of them is dropped. For paths a
    domain filter cannot exclude — LinkedIn `/in/` profiles and `/company/`
    pages are on the right host but are not job postings."""

    search_country: str | None = None
    """Country name passed to the search provider to bias results, e.g.
    "germany". Advisory only — a hint to the ranker, never a filter."""

    @field_validator("name")
    @classmethod
    def _name_is_slug(cls, value: str) -> str:
        if not value or not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"source name {value!r} must be alphanumeric with - or _")
        return value


class SourcesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[SourceConfig] = Field(default_factory=list)

    @field_validator("sources")
    @classmethod
    def _unique_names(cls, value: list[SourceConfig]) -> list[SourceConfig]:
        seen: set[str] = set()
        for source in value:
            if source.name in seen:
                raise ValueError(f"duplicate source name {source.name!r}")
            seen.add(source.name)
        return value

    @property
    def active(self) -> list[SourceConfig]:
        """Enabled and not contractually off-limits."""
        return [s for s in self.sources if s.enabled and not s.forbidden]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_yaml(path: Path, *, example: str) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"Configuration file not found: {path}\n"
            f"Create it from the example:\n    cp {example} {path}"
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML:\n{exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return raw


class ProfileSettings(BaseModel):
    """Who is looking, and for what — as opposed to how the app runs.

    Everything here is a fact about the person that no job board and no resume
    can supply. It lives in its own file for the same reason: `settings.yaml` is
    machinery, this is intent.
    """

    model_config = ConfigDict(extra="forbid")

    countries: list[str] = Field(default_factory=lambda: ["Germany"])
    """Countries whose jobs are worth seeing. A job the LLM places outside this
    list is rejected; a job whose country it could not determine is kept, because
    "we do not know" has never been grounds to drop anything here.

    Germany only, by default, and that is a statement about permits rather than
    ambition: a non-EU German student residence permit authorises work in
    Germany. An Austrian, Swiss or Czech job needs that country's own permit,
    which in practice means moving there and enrolling there. Listing them would
    surface jobs that cannot be accepted, which is noise, not coverage. Add them
    the day that changes — the sources are already written, just disabled.
    """

    exclude_german_required: bool = True
    """Drop jobs that state fluent German as a requirement.

    Note what this is not: it does not drop jobs *written* in German. Most of TU
    München's HiWi board is German-language advertising for groups that work in
    English, and filtering on language rather than requirement would throw away
    the best source in the config. See `german_required` in the prompt.
    """


def load_profile(path: Path | None = None, secrets: Secrets | None = None) -> ProfileSettings:
    """The profile, or defaults when the file does not exist.

    Deliberately not an error when missing, unlike settings.yaml: a fresh clone
    should run, and "Germany, no German-only jobs" is the right default for the
    person this was built for.
    """
    secrets = secrets or Secrets()
    path = path or secrets.job_alerts_profile_file
    if not Path(path).exists():
        return ProfileSettings()
    data = _read_yaml(path, example="config/profile.example.yaml")
    try:
        return ProfileSettings.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"{path} has invalid profile settings:\n{exc}") from exc


def load_settings(path: Path | None = None, secrets: Secrets | None = None) -> Settings:
    secrets = secrets or Secrets()
    path = path or secrets.job_alerts_settings_file
    data = _read_yaml(path, example="config/settings.example.yaml")
    try:
        settings = Settings.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"{path} has invalid settings:\n{exc}") from exc

    # Environment beats file, so a container can override without a new YAML.
    if secrets.job_alerts_database_path:
        settings.database.path = secrets.job_alerts_database_path
    if secrets.job_alerts_log_level:
        settings.logging.level = secrets.job_alerts_log_level
    if secrets.job_alerts_log_format:
        settings.logging.format = secrets.job_alerts_log_format
    return settings


def load_sources(path: Path | None = None, secrets: Secrets | None = None) -> SourcesConfig:
    secrets = secrets or Secrets()
    path = path or secrets.job_alerts_sources_file
    data = _read_yaml(path, example="config/sources.example.yaml")
    try:
        return SourcesConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"{path} has invalid sources:\n{exc}") from exc


def build_search_query(settings: Settings):
    """Settings -> the `SearchQuery` handed to every source."""
    from .models import SearchQuery

    return SearchQuery(
        keywords=settings.keywords.positive,
        topics=settings.keywords.topics,
        locations=settings.locations.include,
        all_germany=settings.locations.all_germany,
        max_age_days=settings.search.max_age_days,
        max_results=settings.search.max_results_per_source,
    )
