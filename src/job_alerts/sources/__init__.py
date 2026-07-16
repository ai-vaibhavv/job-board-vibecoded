"""Source adapters and the registry that builds them from configuration."""

from __future__ import annotations

import logging

from ..config import Secrets, SourceConfig
from ..http import PoliteClient
from .base import BaseSource, JobSource
from .generic_html import GenericHtmlSource, SelectorError
from .linkedin_posts import LinkedInPostsSource, PostsUnavailable
from .mock import MockSource
from .research_sources import JsonApiSource
from .rss import RssSource
from .search_api import SearchApiSource, SearchUnavailable

logger = logging.getLogger(__name__)

__all__ = [
    "BaseSource",
    "GenericHtmlSource",
    "JobSource",
    "JsonApiSource",
    "LinkedInPostsSource",
    "MockSource",
    "PostsUnavailable",
    "RssSource",
    "SearchApiSource",
    "SearchUnavailable",
    "SelectorError",
    "build_source",
    "build_sources",
]


def build_source(config: SourceConfig, client: PoliteClient, secrets: Secrets) -> BaseSource:
    """Instantiate one adapter from its config block."""
    match config.type:
        case "mock":
            return MockSource(config, client)
        case "rss":
            return RssSource(config, client)
        case "html":
            return GenericHtmlSource(config, client)
        case "json_api":
            return JsonApiSource(config, client)
        case "search_api":
            return SearchApiSource(config, client, secrets)
        case "linkedin_posts":
            return LinkedInPostsSource(config, client, secrets)
        case _:  # pragma: no cover — pydantic constrains `type` already
            raise ValueError(f"unknown source type {config.type!r} for source {config.name!r}")


def build_sources(
    configs: list[SourceConfig], client: PoliteClient, secrets: Secrets
) -> list[BaseSource]:
    """Build every active source.

    A source whose *construction* fails (bad config) is logged and skipped
    rather than aborting the run — same isolation principle as a source whose
    fetch fails.
    """
    sources: list[BaseSource] = []
    for config in configs:
        if config.forbidden:
            logger.info("source %s skipped: its terms disallow automated access", config.name)
            continue
        try:
            sources.append(build_source(config, client, secrets))
        except Exception as exc:
            logger.error("could not build source %s: %s", config.name, exc)
    return sources
