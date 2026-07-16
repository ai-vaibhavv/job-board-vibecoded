"""Search-engine discovery — the compliant route to LinkedIn and friends.

WHAT THIS DOES
    Sends queries like `site:linkedin.com/jobs/view "research assistant" Germany`
    to a legitimate search API and turns the results into job candidates. The
    stored URL points at the public posting.

THE PROVIDER'S DOMAIN FILTER IS NOT A FILTER
    An earlier version of this docstring claimed "Tavily is built for
    programmatic/agent use, so `site:` queries work as expected." That is false,
    and believing it is what filled the database with the wrong jobs. Measured:

        site:linkedin.com/jobs/view ... Germany  -> 4x ng.linkedin.com (Nigeria),
                                                    1x lk.linkedin.com (Sri Lanka)
        site:fraunhofer.de jobs student research -> 0/10 on fraunhofer.de; instead
                                                    americasjobcenterofkern.com,
                                                    azjobconnection.gov, worknola.com
        include_domains: ["fraunhofer.de"]       -> 0/10; Wikipedia, dict.cc, pons.com
        include_domains: ["de.linkedin.com"]     -> 10/10 correct
        site:mpg.de ...                          -> 10/10 correct

    So it works sometimes, which is worse than never: when the provider cannot
    satisfy a domain constraint it does not return an empty list, it returns
    plausible-looking unfiltered semantic matches, and nothing downstream can
    tell the difference. `site:` and `include_domains` are still sent — they help
    when they work — but the enforcement is `config.allowed_domains`, checked
    here against the URLs that actually came back.

WHAT THIS DELIBERATELY DOES NOT DO
    No LinkedIn login. No cookies, no session reuse, no CAPTCHA solving, no
    fingerprint evasion, no proxy rotation, no scraping of authenticated pages.
    This adapter never contacts linkedin.com at all — it only talks to the
    search provider's official API and stores the links it returns. That is the
    whole point: LinkedIn coverage without touching LinkedIn's anti-bot
    protections.

    Consequently the metadata is thin: a search result gives a title, a URL and
    a snippet. Organization and location are parsed out of the title/snippet
    where the format makes that reliable, and left empty where it does not. A
    guessed employer is worse than a missing one.

PROVIDERS
    tavily | brave | bing | google_cse | serpapi — chosen with
    SEARCH_API_PROVIDER. None is mandatory and none is hard-coded as required:
    with no key the source disables itself and the run continues on RSS/HTML
    sources.

    `tavily` is the recommended starting point — its free tier needs no credit
    card, whereas Brave's asks for one.

    Every provider is called with our own API key on its own documented
    endpoint, so none of them go through the robots.txt check: that governs
    crawlers, not authorized API clients.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

from ..config import Secrets, SourceConfig
from ..http import PoliteClient
from ..models import JobCandidate, SearchQuery
from .base import BaseSource

logger = logging.getLogger(__name__)


def host_allowed(url: str, allowed: list[str]) -> bool:
    """Is `url` on one of `allowed`? Empty `allowed` means no restriction.

    Matches a host exactly, or as a subdomain of an allowed domain, so
    `fraunhofer.de` admits `jobs.fraunhofer.de`. That same rule is why LinkedIn
    must be declared as `de.linkedin.com`: `linkedin.com` legitimately admits
    `ng.linkedin.com`, and a Nigerian posting is not a German one.
    """
    if not allowed:
        return True
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in (a.lower().strip().lstrip(".") for a in allowed if a.strip())
    )


class SearchProviderError(RuntimeError):
    pass


class SearchProvider(ABC):
    """One search back end. Returns (title, url, snippet) triples.

    `include_domains` and `country` are hints, not guarantees: a provider may
    ignore either, and Tavily demonstrably does (see the module docstring).
    Providers that have no equivalent parameter accept and ignore them — the
    real enforcement is `host_allowed` on the results.
    """

    name: str

    def __init__(self, secrets: Secrets, client: PoliteClient) -> None:
        self.secrets = secrets
        self.client = client

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int,
        *,
        include_domains: list[str] | None = None,
        country: str | None = None,
    ) -> list[dict[str, str]]: ...


class TavilyProvider(SearchProvider):
    """Tavily Search API.

    The friendliest option to get started: the free tier does not ask for a
    credit card, unlike Brave's.

    Do not trust its domain filtering — `site:` in the query and the
    `include_domains` parameter are both advisory in practice, and it answers
    semantically when it cannot satisfy them. Both are sent because they help
    when they work; the module docstring has the measurements.

    Differs from the others in two ways worth knowing:
      * it is a POST with a JSON body, not a GET with query params;
      * the snippet field is `content`, not `description`/`snippet`.
    """

    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    async def search(
        self,
        query: str,
        limit: int,
        *,
        include_domains: list[str] | None = None,
        country: str | None = None,
    ) -> list[dict[str, str]]:
        body: dict[str, object] = {
            "query": query,
            # Tavily caps basic search at 20 results per call.
            "max_results": min(limit, 20),
            "search_depth": "basic",
            # `country` is only accepted when topic is "general".
            "topic": "general",
        }
        if include_domains:
            body["include_domains"] = include_domains
        if country:
            body["country"] = country

        payload = await self.client.post_json(
            self.endpoint,
            json_body=body,
            # Bearer auth keeps the key out of the URL, so it cannot leak into
            # a log line or a proxy access log the way a query param would.
            headers={
                "Authorization": f"Bearer {self.secrets.search_api_key}",
                "Content-Type": "application/json",
            },
        )
        results = payload.get("results") or []
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
            }
            for r in results
        ]


class BraveProvider(SearchProvider):
    """Brave Search API.

    Note: the "free" tier still requires a credit card at signup. Use Tavily if
    you would rather not hand over card details.
    """

    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    async def search(
        self,
        query: str,
        limit: int,
        *,
        include_domains: list[str] | None = None,
        country: str | None = None,
    ) -> list[dict[str, str]]:
        # No `include_domains` equivalent; relies on `site:` in the query and on
        # `host_allowed` afterwards. The German market is already pinned below.
        del include_domains, country
        payload = await self.client.get_json(
            self.endpoint,
            params={"q": query, "count": str(min(limit, 20)), "country": "de"},
            check_robots=False,
            headers={
                "X-Subscription-Token": self.secrets.search_api_key,
                "Accept": "application/json",
            },
        )
        results = (payload.get("web") or {}).get("results") or []
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("description", ""),
            }
            for r in results
        ]


class BingProvider(SearchProvider):
    name = "bing"
    endpoint = "https://api.bing.microsoft.com/v7.0/search"

    async def search(
        self,
        query: str,
        limit: int,
        *,
        include_domains: list[str] | None = None,
        country: str | None = None,
    ) -> list[dict[str, str]]:
        # No `include_domains` equivalent; relies on `site:` in the query and on
        # `host_allowed` afterwards. The German market is already pinned below.
        del include_domains, country
        payload = await self.client.get_json(
            self.endpoint,
            params={"q": query, "count": str(min(limit, 50)), "mkt": "de-DE"},
            check_robots=False,
            headers={"Ocp-Apim-Subscription-Key": self.secrets.search_api_key},
        )
        results = (payload.get("webPages") or {}).get("value") or []
        return [
            {
                "title": r.get("name", ""),
                "url": r.get("url", ""),
                "snippet": r.get("snippet", ""),
            }
            for r in results
        ]


class GoogleCseProvider(SearchProvider):
    """Google Programmable Search. Needs both an API key and an engine id (cx),
    and caps `num` at 10 per request."""

    name = "google_cse"
    endpoint = "https://www.googleapis.com/customsearch/v1"

    async def search(
        self,
        query: str,
        limit: int,
        *,
        include_domains: list[str] | None = None,
        country: str | None = None,
    ) -> list[dict[str, str]]:
        # No `include_domains` equivalent; relies on `site:` in the query and on
        # `host_allowed` afterwards. The German market is already pinned below.
        del include_domains, country
        collected: list[dict[str, str]] = []
        start = 1
        while len(collected) < limit and start <= 91:
            payload = await self.client.get_json(
                self.endpoint,
                params={
                    "key": self.secrets.search_api_key,
                    "cx": self.secrets.google_cse_id,
                    "q": query,
                    "num": str(min(10, limit - len(collected))),
                    "start": str(start),
                    "gl": "de",
                },
                check_robots=False,
            )
            items = payload.get("items") or []
            if not items:
                break
            collected.extend(
                {
                    "title": i.get("title", ""),
                    "url": i.get("link", ""),
                    "snippet": i.get("snippet", ""),
                }
                for i in items
            )
            start += len(items)
        return collected[:limit]


class SerpApiProvider(SearchProvider):
    name = "serpapi"
    endpoint = "https://serpapi.com/search"

    async def search(
        self,
        query: str,
        limit: int,
        *,
        include_domains: list[str] | None = None,
        country: str | None = None,
    ) -> list[dict[str, str]]:
        # No `include_domains` equivalent; relies on `site:` in the query and on
        # `host_allowed` afterwards. The German market is already pinned below.
        del include_domains, country
        payload = await self.client.get_json(
            self.endpoint,
            params={
                "q": query,
                "api_key": self.secrets.search_api_key,
                "num": str(min(limit, 100)),
                "engine": "google",
                "gl": "de",
                "hl": "en",
            },
            check_robots=False,
        )
        results = payload.get("organic_results") or []
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "snippet": r.get("snippet", ""),
            }
            for r in results
        ]


_PROVIDERS: dict[str, type[SearchProvider]] = {
    "tavily": TavilyProvider,
    "brave": BraveProvider,
    "bing": BingProvider,
    "google_cse": GoogleCseProvider,
    "serpapi": SerpApiProvider,
}


def build_provider(secrets: Secrets, client: PoliteClient) -> SearchProvider | None:
    """The configured provider, or None when no key is set."""
    if not secrets.has_search_api:
        return None
    provider_class = _PROVIDERS.get(secrets.search_api_provider)
    if provider_class is None:
        raise SearchProviderError(
            f"unknown SEARCH_API_PROVIDER={secrets.search_api_provider!r}; "
            f"expected one of: {', '.join(_PROVIDERS)}"
        )
    return provider_class(secrets, client)


class SearchApiSource(BaseSource):
    """Runs configured discovery queries against a search provider."""

    def __init__(
        self,
        config: SourceConfig,
        client: PoliteClient,
        secrets: Secrets,
        provider: SearchProvider | None = None,
    ) -> None:
        super().__init__(config, client)
        self.secrets = secrets
        self._provider = provider or build_provider(secrets, client)

    @property
    def available(self) -> bool:
        return self._provider is not None

    async def search(self, query: SearchQuery) -> list[JobCandidate]:
        if self._provider is None:
            # Not an error: running without a search key is a supported mode.
            raise SearchUnavailable(
                "no search API configured (set SEARCH_API_PROVIDER and SEARCH_API_KEY in .env); "
                "RSS and HTML sources are unaffected"
            )

        limit = self.config.max_results_per_query
        candidates: list[JobCandidate] = []
        seen: set[str] = set()
        failures: list[str] = []
        returned = 0
        off_domain = 0

        for raw_query in self.config.queries:
            try:
                results = await self._provider.search(
                    raw_query,
                    limit,
                    include_domains=self.config.allowed_domains or None,
                    country=self.config.search_country,
                )
            except Exception as exc:
                logger.warning("query %r failed on %s: %s", raw_query, self._provider.name, exc)
                failures.append(str(exc))
                continue

            for result in results:
                url = result.get("url", "").strip()
                # Cheap in-source dedup; the pipeline dedups properly later, but
                # the same posting turns up in several queries by design.
                if not url or url in seen:
                    continue
                seen.add(url)
                returned += 1

                if not host_allowed(url, self.config.allowed_domains):
                    logger.debug(
                        "%s: dropping off-domain result %s (expected %s)",
                        self.name,
                        url,
                        ", ".join(self.config.allowed_domains),
                    )
                    off_domain += 1
                    continue
                if self._denied(url):
                    logger.debug("%s: dropping denied url %s", self.name, url)
                    continue

                candidate = self._to_candidate(result)
                if candidate:
                    candidates.append(candidate)

        # A provider that ignores the domain constraint looks exactly like a
        # provider that honoured it and found nothing, so say so out loud. A high
        # ratio here means the queries are being answered semantically and the
        # results are not what was asked for.
        if off_domain:
            logger.info(
                "%s: dropped %d/%d results as off-domain (provider did not honour "
                "the domain filter)",
                self.name,
                off_domain,
                returned,
            )

        if failures and not candidates:
            raise SearchProviderError(
                f"all {len(self.config.queries)} queries failed; first error: {failures[0]}"
            )
        return candidates

    def _denied(self, url: str) -> bool:
        """Does this URL match one of the source's `denied_url_patterns`?"""
        return any(re.search(p, url, re.IGNORECASE) for p in self.config.denied_url_patterns)

    def _to_candidate(self, result: dict[str, str]) -> JobCandidate | None:
        title = _clean_title(result.get("title", ""))
        url = result.get("url", "").strip()
        if not title or not url:
            return None
        if looks_like_listing_page(title, url):
            logger.debug("dropping listing/index page: %s (%s)", title, url)
            return None
        try:
            return JobCandidate(
                source=self.name,
                title=title,
                organization=_guess_organization(result.get("title", "")),
                location=_guess_location(f"{result.get('title', '')} {result.get('snippet', '')}"),
                description=result.get("snippet") or None,
                url=url,
            )
        except ValueError:
            return None


class SearchUnavailable(RuntimeError):
    """No search API key configured. The pipeline reports this as *skipped*
    rather than failed — it is a configuration choice, not a fault."""


# Search results title jobs as "Research Assistant - TU Berlin | LinkedIn".
# These patterns pull that apart; anything they cannot parse confidently is
# left as None rather than guessed.
_SITE_SUFFIX_RE = re.compile(
    r"\s*[|\-–—]\s*(LinkedIn|Indeed|StepStone|Glassdoor|Xing|academics\.de|jobs?)\s*$",
    re.IGNORECASE,
)
_ORG_RE = re.compile(r"\s+(?:at|bei|@)\s+([^|\-–—]{2,60})", re.IGNORECASE)
_HIRING_RE = re.compile(r"^\s*([^|\-–—]{2,60}?)\s+(?:hiring|is hiring|sucht)\s+", re.IGNORECASE)

_GERMAN_CITIES = (
    "Berlin",
    "Munich",
    "München",
    "Hamburg",
    "Frankfurt",
    "Stuttgart",
    "Cologne",
    "Köln",
    "Düsseldorf",
    "Bonn",
    "Aachen",
    "Karlsruhe",
    "Darmstadt",
    "Heidelberg",
    "Mannheim",
    "Tübingen",
    "Freiburg",
    "Dresden",
    "Leipzig",
    "Jena",
    "Potsdam",
    "Saarbrücken",
    "Erlangen",
    "Nuremberg",
    "Nürnberg",
    "Bremen",
    "Hanover",
    "Hannover",
    "Göttingen",
    "Münster",
    "Dortmund",
    "Bochum",
    "Essen",
    "Kiel",
)


# A search engine cannot tell "a job posting" from "a page that lists jobs", so
# it returns both. An index page is worthless in an alert — you cannot apply to
# a search box — and it never deduplicates away, because its URL is stable and
# it reappears on every single run. Found live: a query for
# "studentische hilfskraft machine learning" returned
# `de.indeed.com/q-studentische-hilfskraft-machine-learning-jobs.html`, titled
# "Studentische Hilfskraft Machine Learning Jobs", which scored 55 and would
# have been notified.
_LISTING_URL_RE = re.compile(
    r"""
      /q-[^/]*-jobs\.html          # indeed:  /q-<terms>-jobs.html
    | /(jobs|stellen|stellenangebote|stellenanzeigen)\.html?$
    | /(search|suche|jobsuche|job-search|jobboard|jobbourse|jobboerse)(/|$)
    | [?&](q|query|keywords|suchbegriff|was)=   # a query string = a search page
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Index pages are titled after the search, not after a role: "… Jobs",
# "… Stellenangebote". A real posting is titled after the position.
# `opportunities` and `openings` were added after "AIML Lab - Thesis
# Opportunities" (ml.informatik.tu-darmstadt.de/thesis/proposal/index.html)
# scored 95 and reached a live Discord alert. It is a landing page inviting you
# to come and discuss a thesis, not a position you can apply to.
#
# `positions` is deliberately NOT here. A real advert from Cyber Valley reads
# "Student Assistant (HiWi)/Internship Positions in Vision-Based Autonomous
# Systems" — one applyable page offering several seats. The `<plural> in <place>`
# branch below would have thrown it away. The words differ in practice: a page
# titled "Opportunities" is browsing, a page titled "Positions in X" is often
# hiring.
_LISTING_PLURALS = (
    r"jobs|stellenangebote|stellenanzeigen|stellenb(?:ö|oe)rse"
    r"|jobb(?:ö|oe)rse|vacancies|jobsuche|job\s+search|opportunities|openings"
)
_LISTING_TITLE_RE = re.compile(
    # An index page is titled after the search; a real posting names the role.
    #   "… Machine Learning Jobs"              -> ends in the generic plural
    #   "… Machine Learning-Jobs (Studierende)" -> plural + a trailing qualifier
    #   "Research Assistant Jobs in Berlin"     -> "<plural> in <place>"
    #   "Mehr als 100 Machine Learning-Jobs"    -> advertises a COUNT of jobs
    #
    # The count patterns were added after a live run: Groq scored "Mehr als 100
    # Machine Learning-Jobs (Studierende)" 90/100 as though it were a real
    # posting. No page that counts its jobs is a single job.
    rf"\b(?:{_LISTING_PLURALS})\s*(?:\([^)]*\))?\s*$"
    rf"|\b(?:{_LISTING_PLURALS})\s+(?:in|bei|near|around|und)\b"
    r"|^\s*(?:mehr als|über|ueber|more than|alle|all|top)\s+\d+"
    rf"|^\s*\d{{2,}}\s+.*\b(?:{_LISTING_PLURALS}|stellen)\b",
    re.IGNORECASE,
)


def looks_like_listing_page(title: str, url: str) -> bool:
    """Is this a search/index page rather than one job posting?

    Deliberately conservative: a false positive silently hides a real job, which
    is worse than letting one index page through. Only unambiguous signals count
    — a URL that is plainly a search query, or a title that ends in the generic
    plural ("… Jobs") rather than naming a role.
    """
    if _LISTING_TITLE_RE.search(title or ""):
        return True
    return bool(_LISTING_URL_RE.search(url or ""))


def _clean_title(raw: str) -> str:
    """Strip the search engine's site suffix from a result title."""
    title = _SITE_SUFFIX_RE.sub("", raw or "").strip()
    # "Org hiring Role in City" -> "Role"
    hiring = re.match(r"^.{2,60}?\s+hiring\s+(.+?)(?:\s+in\s+.+)?$", title, re.IGNORECASE)
    if hiring:
        title = hiring.group(1).strip()
    return " ".join(title.split())


def _guess_organization(raw: str) -> str | None:
    """Employer from a result title, only when the format is unambiguous."""
    if not raw:
        return None
    cleaned = _SITE_SUFFIX_RE.sub("", raw).strip()
    if match := _HIRING_RE.match(cleaned):
        return match.group(1).strip() or None
    if match := _ORG_RE.search(cleaned):
        org = match.group(1).strip()
        # "at Berlin" is a location, not an employer.
        if org and not any(city.lower() == org.lower() for city in _GERMAN_CITIES):
            return org
    return None


def _guess_location(text: str) -> str | None:
    """First German city named in the title/snippet, if any."""
    if not text:
        return None
    for city in _GERMAN_CITIES:
        if re.search(rf"\b{re.escape(city)}\b", text, re.IGNORECASE):
            return city
    if re.search(r"\b(Germany|Deutschland)\b", text, re.IGNORECASE):
        return "Germany"
    return None
