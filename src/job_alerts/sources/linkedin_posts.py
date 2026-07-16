"""Jobs posted as ordinary LinkedIn posts, rather than in the jobs section.

WHY THIS IS A SEPARATE SOURCE
    A lot of German lab hiring never reaches LinkedIn's job board. Someone writes
    "wir suchen eine studentische Hilfskraft, meldet euch per Mail" and that is
    the whole advert. `search_api` cannot see those: a web search returns a
    title, a URL and ~160 characters, and the outbound link and the email live in
    the post *body*, which is the one part a search result does not carry.

WE STILL DO NOT TOUCH LINKEDIN
    No login, no cookies, no session reuse — the rule the project has always had,
    and `http.py` refuses the host outright to make it more than a promise. The
    post bodies here come from a vendor's API. The vendor does the fetching; we
    call an HTTP endpoint with our own key, exactly as with a search provider.

    That is a real distinction and not a fig leaf: no account of ours exists to
    be banned, and nothing here degrades into fingerprint evasion or proxy
    rotation the moment LinkedIn pushes back. It is also not free of judgement —
    the vendor is scraping a company that prohibits scraping, and consuming that
    is a choice made deliberately rather than by accident.

    Cookie-based actors are deliberately NOT supported. An actor that wants an
    `li_at` session cookie is a burner account with extra steps: same ban, same
    arms race, now with a subscription.

WHAT TO EXPECT — measured, not guessed
    A live search for `"werkstudent" machine learning`, sorted by date, over the
    past week, returned TWO posts. One of them was a newsletter roundup ("63 open
    positions in Munich"), not a job. That is the shape of this source: thin, and
    noisy in a way keywords cannot fix. The verification step is what makes it
    usable, and the German keywords are what keep it German — "werkstudent" is
    not a word anywhere else, so the query filters the country for free.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from ..config import Secrets, SourceConfig
from ..enrich import apply_url_from_text, contact_email_from_text, outbound_links_from_text
from ..http import PoliteClient
from ..models import JobCandidate, SearchQuery
from .base import BaseSource

logger = logging.getLogger(__name__)

_MAX_TITLE_CHARS = 110


class PostBackendError(RuntimeError):
    pass


class PostsUnavailable(RuntimeError):
    """No backend is configured. Reported as *skipped*, not failed — running
    without a vendor key is a supported mode, exactly like `SearchUnavailable`."""


class Post:
    """One post, normalised away from any one vendor's field names."""

    __slots__ = ("author", "posted_at", "text", "url")

    def __init__(
        self, url: str, text: str, author: str | None = None, posted_at: datetime | None = None
    ) -> None:
        self.url = url
        self.text = text
        self.author = author
        self.posted_at = posted_at


class PostBackend(ABC):
    """One way of getting post bodies.

    Deliberately not folded into `search_api`'s `_PROVIDERS`: that registry means
    "a web search engine", and an Apify actor is not one. Keeping them apart is
    what lets a rotted actor be swapped without touching search.
    """

    name: str

    def __init__(self, config: SourceConfig, client: PoliteClient, secrets: Secrets) -> None:
        self.config = config
        self.client = client
        self.secrets = secrets

    @abstractmethod
    async def search(self, keyword: str, limit: int) -> list[Post]: ...


class ApifyPostBackend(PostBackend):
    """apimaestro/linkedin-posts-search-scraper-no-cookies, or any actor with the
    same input shape — the id is config, so replacing a broken actor does not
    need a release.

    Costs $5 per 1000 results against Apify's $5/month free credit: roughly a
    thousand posts a month for nothing. `limit` is capped at 50 by the actor.
    """

    name = "apify"
    endpoint = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"

    async def search(self, keyword: str, limit: int) -> list[Post]:
        actor = str(self.config.model_extra.get("actor") or "").strip()
        if not actor:
            raise PostBackendError(f"source {self.config.name!r} has backend 'apify' but no actor")

        payload = {
            "keyword": keyword,
            # Freshness beats relevance here: a hiring post is dead within days,
            # and `date_filter` enforces the age window at the vendor rather than
            # paying for results we would only throw away.
            "sort_type": str(self.config.model_extra.get("sort_type") or "date_posted"),
            "date_filter": str(self.config.model_extra.get("date_filter") or "past-week"),
            "limit": min(limit, 50),
            "page_number": 1,
        }
        items = await self.client.post_json(
            self.endpoint.format(actor=actor.replace("/", "~")),
            json_body=payload,
            # Bearer, not `?token=` as the docs suggest: a key in a URL lands in
            # every proxy log, and this project already tests that keys never
            # reach one.
            headers={
                "Authorization": f"Bearer {self.secrets.apify_token}",
                "Content-Type": "application/json",
            },
        )
        if not isinstance(items, list):  # pragma: no cover — defensive
            raise PostBackendError(f"apify returned {type(items).__name__}, expected a list")
        return [p for p in (_post_from_apify(i) for i in items) if p]


def _post_from_apify(item: dict) -> Post | None:
    url = (item.get("post_url") or "").strip()
    text = (item.get("text") or "").strip()
    if not url or not text:
        return None
    author = ((item.get("author") or {}).get("name") or "").strip() or None
    return Post(url=url, text=text, author=author, posted_at=_apify_posted_at(item))


def _apify_posted_at(item: dict) -> datetime | None:
    """`posted_at.timestamp` is milliseconds; `posted_at.date` is a naive local
    string. Prefer the timestamp — it is unambiguous."""
    posted = item.get("posted_at") or {}
    if isinstance(posted, dict):
        ts = posted.get("timestamp")
        if isinstance(ts, int | float) and ts > 0:
            return datetime.fromtimestamp(ts / 1000, tz=UTC)
        raw = posted.get("date")
        if isinstance(raw, str) and raw.strip():
            from ..normalization import parse_datetime

            return parse_datetime(raw)
    return None


_BACKENDS: dict[str, type[PostBackend]] = {"apify": ApifyPostBackend}


def build_backend(
    config: SourceConfig, client: PoliteClient, secrets: Secrets
) -> PostBackend | None:
    name = str(config.model_extra.get("backend") or "apify")
    backend_class = _BACKENDS.get(name)
    if backend_class is None:
        raise PostBackendError(
            f"unknown backend {name!r} for source {config.name!r}; "
            f"expected one of: {', '.join(_BACKENDS)}"
        )
    if name == "apify" and not secrets.has_apify:
        return None
    return backend_class(config, client, secrets)


class LinkedInPostsSource(BaseSource):
    """Runs configured keywords against a post backend."""

    def __init__(
        self,
        config: SourceConfig,
        client: PoliteClient,
        secrets: Secrets,
        backend: PostBackend | None = None,
    ) -> None:
        super().__init__(config, client)
        self.secrets = secrets
        self._backend = backend or build_backend(config, client, secrets)

    @property
    def available(self) -> bool:
        return self._backend is not None

    async def search(self, query: SearchQuery) -> list[JobCandidate]:
        if self._backend is None:
            raise PostsUnavailable(
                "no APIFY_TOKEN set — LinkedIn posts are skipped; every other source is unaffected"
            )

        keywords = self.config.queries
        if not keywords:
            logger.warning("source %s has no queries; nothing to search", self.name)
            return []

        limit = self.config.max_results_per_query
        candidates: list[JobCandidate] = []
        seen: set[str] = set()
        failures: list[str] = []

        for keyword in keywords:
            try:
                posts = await self._backend.search(keyword, limit)
            except Exception as exc:
                # One dead keyword must not cost the others. A community-run
                # actor scraping a company that fights scrapers WILL break;
                # this source has to degrade rather than take the run down.
                logger.warning("keyword %r failed on %s: %s", keyword, self._backend.name, exc)
                failures.append(str(exc))
                continue

            for post in posts:
                if not post.url or post.url in seen:
                    continue
                seen.add(post.url)
                if candidate := _to_candidate(post, self.name):
                    candidates.append(candidate)

        if failures and not candidates:
            raise PostBackendError(
                f"all {len(keywords)} keyword(s) failed; first error: {failures[0]}"
            )
        return candidates


def _to_candidate(post: Post, source: str) -> JobCandidate | None:
    title = synthesize_title(post.text)
    if not title:
        return None
    try:
        candidate = JobCandidate(
            source=source,
            title=title,
            # Deliberately not the author: the person who posted is usually not
            # the employer — a recruiter, a newsletter account, a professor
            # sharing a colleague's opening. Guessing here would put "DACHpulse"
            # in the Organization field of somebody else's job. The LLM reads the
            # body and does better.
            organization=None,
            description=_describe(post),
            url=post.url,
            published_at=post.posted_at,
        )
    except ValueError:
        return None

    # How to apply, if the post says so. This is the whole reason posts are worth
    # having: "meldet euch per Mail an hiwi@lab.de" is an advert that exists
    # nowhere else, and the address is the only way to answer it. A post that
    # offers a concrete route is also, in practice, the one most likely to be
    # real — the route is itself the evidence.
    candidate.contact_email = contact_email_from_text(post.text)
    candidate.contact_url = apply_url_from_text(post.text) or _first_outbound(post.text)
    return candidate


def _first_outbound(text: str) -> str | None:
    """The first link out of the post — usually the actual advert.

    `lnkd.in` counts: it is LinkedIn's shortener, and a request to it answers
    with a redirect to somewhere else. The page we end up reading belongs to a
    university or an ATS. Links back to linkedin.com itself are already excluded
    upstream, and the denylist would refuse them anyway.
    """
    links = outbound_links_from_text(text)
    return links[0] if links else None


def _describe(post: Post) -> str:
    """The post body, with the author noted as context rather than as a claim."""
    if post.author:
        return f"[posted by {post.author}]\n\n{post.text}"
    return post.text


def synthesize_title(text: str) -> str:
    """A title for something that has none.

    `JobCandidate` requires a title, and a post does not have one — it is a
    paragraph someone typed. The first meaningful line is the closest thing, and
    it is provisional: the LLM's `role_type` and reasoning are what actually
    describe the role. Better a rough title than dropping the post.
    """
    for line in (text or "").splitlines():
        cleaned = " ".join(line.split())
        # Skip lines that are only emoji/punctuation — LinkedIn posts open with
        # them constantly ("👀", "🚀🚀🚀").
        if len(cleaned) < 12 or not any(ch.isalnum() for ch in cleaned):
            continue
        if len(cleaned) > _MAX_TITLE_CHARS:
            cleaned = cleaned[:_MAX_TITLE_CHARS].rsplit(" ", 1)[0] + "…"
        return cleaned
    return ""
