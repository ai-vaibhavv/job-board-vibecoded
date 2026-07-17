"""One polite HTTP client, shared by every source.

Centralising this is what makes "be a good citizen" enforceable rather than a
per-adapter promise: robots.txt, per-domain pacing, caching, timeouts, retry
policy and the identifying user agent all happen here, so a new source adapter
gets correct behaviour for free.

Retry policy follows the spec exactly: temporary failures only. 5xx, timeouts
and connection errors retry with exponential backoff; permanent 4xx do not.
429 is the sole 4xx exception and honours `Retry-After`.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.robotparser
from collections import defaultdict
from dataclasses import dataclass
from types import TracebackType
from urllib.parse import urlsplit

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import HttpSettings

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class FetchError(RuntimeError):
    """A request failed in a way the caller should report, not retry.

    `status_code` is the HTTP status when the failure was a definite response
    (e.g. 404, 410) and None when there was none to read (timeout, DNS/transport
    error, robots/host denial). The on-demand link check uses it to tell a truly
    dead posting (404/410 → safe to auto-hide) from a merely unreachable one.
    """

    def __init__(self, *args: object, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.status_code = status_code


class RobotsDisallowed(FetchError):
    """robots.txt forbids this URL. Never retried, never bypassed."""


class HostDenied(FetchError):
    """This host is on `_DENIED_HOSTS`. Never retried, never bypassed."""


# Hosts this application must never fetch, whatever robots.txt says.
#
# This exists because robots.txt cannot be relied on to say no here.
# linkedin.com/robots.txt declares no `User-agent: *` group at all — only rules
# for 36 named crawlers (LinkedInBot, Googlebot, Bingbot, …). Our UA matches
# none of them, and `urllib.robotparser.can_fetch()` returns True when no group
# matches, because the standard's default is allow. So `is_allowed()` returns a
# green light for every LinkedIn URL, while the file's own header reads:
#
#     # Notice: The use of robots or other automated means to access LinkedIn
#     # without the express permission of LinkedIn is strictly prohibited.
#
# Permission is granted only to approved search engines, via
# whitelist-crawl@linkedin.com. Until now nothing fetched linkedin.com, so the
# gap never mattered; the moment anything follows a link, it does. The project's
# rule ("no LinkedIn login, no cookies, no scraping of authenticated pages") is
# enforced here, explicitly, rather than resting on a check that says yes.
#
# LinkedIn content still reaches us — via a search provider's API, or via a
# vendor actor. Neither is us fetching linkedin.com.
_DENIED_HOSTS: frozenset[str] = frozenset({"linkedin.com"})


def host_is_denied(url: str) -> bool:
    """Is this URL on a host we refuse to fetch? Matches subdomains too, so one
    entry covers `www.`, `de.`, `ng.` and every other country prefix."""
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    return any(host == denied or host.endswith(f".{denied}") for denied in _DENIED_HOSTS)


class _RetryableStatus(RuntimeError):
    """Internal: a response worth retrying. Never escapes this module."""

    def __init__(self, status_code: int, retry_after: float | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(slots=True)
class _CacheEntry:
    body: str
    stored_at: float
    status_code: int


class PoliteClient:
    """Async HTTP client that rate-limits per domain and obeys robots.txt."""

    def __init__(self, settings: HttpSettings, *, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml,"
                    "application/json;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": settings.accept_language,
            },
            timeout=httpx.Timeout(settings.request_timeout),
            follow_redirects=True,
        )
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._domain_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request_at: dict[str, float] = {}
        self._cache: dict[str, _CacheEntry] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- robots -----------------------------------------------------------

    async def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_locks[origin]:
            if origin in self._robots:
                return self._robots[origin]
            parser: urllib.robotparser.RobotFileParser | None = None
            try:
                response = await self._client.get(f"{origin}/robots.txt", timeout=10.0)
                if response.status_code == 200:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.parse(response.text.splitlines())
                else:
                    # No robots.txt is a grant, per the standard.
                    logger.debug("no robots.txt at %s (HTTP %s)", origin, response.status_code)
            except httpx.HTTPError as exc:
                # Unreachable robots.txt must not block the run; the request
                # itself will fail anyway if the host is genuinely down.
                logger.debug("could not fetch robots.txt for %s: %s", origin, exc)
            self._robots[origin] = parser
            return parser

    async def is_allowed(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        parser = await self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.settings.user_agent, url)

    async def crawl_delay(self, url: str) -> float:
        """The site's requested delay, or our configured floor — whichever is
        more generous to the site."""
        configured = self.settings.per_domain_delay
        if not self.settings.respect_robots:
            return configured
        parser = await self._robots_for(url)
        if parser is None:
            return configured
        try:
            delay = parser.crawl_delay(self.settings.user_agent)
        except (AttributeError, ValueError):
            return configured
        return max(configured, float(delay)) if delay else configured

    # -- fetching ---------------------------------------------------------

    async def _pace(self, url: str, *, use_robots_delay: bool = True) -> None:
        """Hold the per-domain lock until this domain is due another request.

        `use_robots_delay=False` still paces the domain — it just uses the
        configured delay instead of asking robots.txt for a `Crawl-delay`.
        Without this, a caller that opted out of robots would still trigger a
        robots.txt fetch from here, which defeats the opt-out entirely.
        """
        domain = urlsplit(url).netloc
        delay = await self.crawl_delay(url) if use_robots_delay else self.settings.per_domain_delay
        async with self._domain_locks[domain]:
            last = self._last_request_at.get(domain)
            if last is not None:
                wait = delay - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request_at[domain] = time.monotonic()

    def _cached(self, key: str) -> str | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.stored_at > self.settings.cache_ttl_seconds:
            del self._cache[key]
            return None
        return entry.body

    async def get_text(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        check_robots: bool = True,
    ) -> str:
        """GET `url` and return the body. Raises `FetchError` on failure.

        `check_robots=False` is for official APIs called with our own key (see
        `post_json`) — robots.txt governs crawlers, not authenticated clients.
        """
        cache_key = f"{url}?{sorted((params or {}).items())}"
        if use_cache and (hit := self._cached(cache_key)) is not None:
            logger.debug("cache hit %s", url)
            return hit

        if check_robots and not await self.is_allowed(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        body = await self._request_with_retries(
            "GET", url, params=params, headers=headers, check_robots=check_robots
        )
        if use_cache:
            self._cache[cache_key] = _CacheEntry(body, time.monotonic(), 200)
        return body

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
        check_robots: bool = True,
    ):
        text = await self.get_text(
            url,
            params=params,
            headers=headers,
            use_cache=use_cache,
            check_robots=check_robots,
        )
        return _parse_json(url, text)

    async def post_json(
        self,
        url: str,
        *,
        json_body: dict[str, object],
        headers: dict[str, str] | None = None,
        check_robots: bool = False,
    ):
        """POST a JSON body and parse the JSON response.

        Not cached: a POST is a request to *do* something, and caching one would
        be wrong even when the endpoint behaves like a search.

        `check_robots` defaults to False because the only caller is a search
        provider hitting its own documented API endpoint with our own API key.
        That is not crawling, so robots.txt does not apply — and honouring it
        there would let a provider's crawler policy silently disable a paid,
        authorized integration.
        """
        text = await self._request_with_retries(
            "POST", url, headers=headers, json_body=json_body, check_robots=check_robots
        )
        return _parse_json(url, text)

    async def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        check_robots: bool = True,
    ) -> str:
        # Checked here rather than beside the robots lookup, because this is the
        # one path every caller funnels through — including `post_json`, which
        # sets check_robots=False. A denied host is denied even then, and it is
        # never retried: the answer will not change.
        if host_is_denied(url):
            raise HostDenied(
                f"refusing to fetch {url}: host is on the denylist "
                f"(its robots.txt may permit this; that is not the point)"
            )

        attempts = max(1, self.settings.max_retries)
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(attempts),
                wait=wait_exponential(multiplier=1, min=1, max=30),
                retry=retry_if_exception_type(
                    (_RetryableStatus, httpx.TimeoutException, httpx.TransportError)
                ),
                reraise=True,
            ):
                with attempt:
                    return await self._request_once(
                        method,
                        url,
                        params=params,
                        headers=headers,
                        json_body=json_body,
                        check_robots=check_robots,
                    )
        except _RetryableStatus as exc:
            raise FetchError(
                f"{url} failed after {attempts} attempts: HTTP {exc.status_code}",
                status_code=exc.status_code,
            ) from exc
        except httpx.TimeoutException as exc:
            raise FetchError(f"{url} timed out after {attempts} attempts") from exc
        except httpx.TransportError as exc:
            raise FetchError(f"{url} unreachable after {attempts} attempts: {exc}") from exc
        raise FetchError(f"{url} failed")  # pragma: no cover — defensive

    async def _request_once(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        check_robots: bool = True,
    ) -> str:
        async with self._semaphore:
            await self._pace(url, use_robots_delay=check_robots)
            try:
                response = await self._client.request(
                    method, url, params=params, headers=headers, json=json_body
                )
            except httpx.HTTPError:
                raise

        status = response.status_code
        if status == 429:
            # Respect Retry-After; fall back to the backoff schedule.
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if retry_after:
                logger.info("429 from %s, honouring Retry-After=%.0fs", url, retry_after)
                await asyncio.sleep(min(retry_after, 60.0))
            raise _RetryableStatus(status, retry_after)
        if status in _RETRYABLE_STATUS:
            raise _RetryableStatus(status)
        if 400 <= status < 500:
            # Permanent client errors: retrying cannot help.
            raise FetchError(f"{url} returned HTTP {status}", status_code=status)
        if status >= 500:  # pragma: no cover — covered by _RETRYABLE_STATUS
            raise _RetryableStatus(status)
        return response.text


def _parse_json(url: str, text: str):
    import json

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{url} did not return valid JSON: {exc}") from exc


def _parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` is either seconds or an HTTP date."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        from datetime import UTC, datetime
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None
