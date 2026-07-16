"""Turn dashboard search terms into bounded `site:` search queries.

`SearchApiSource` searches whatever strings sit in its `config.queries`, not the
settings keywords (see `sources/search_api.py`). So a dashboard search that
should actually *fetch* new postings has to generate those query strings itself
and swap them in.

Two constraints shape this and both come from the code, not preference:

  * **Cost.** Every query is a paid search-API call. `search_discovery` lists a
    dozen `allowed_domains`; one query per domain per search would be a dozen
    calls. So queries are built for a small curated set of the most productive
    domains (LinkedIn job pages, Fraunhofer, MPG), not all of them, and the
    terms are capped.
  * **Domain allowlist.** `SearchApiSource` drops any result whose host is not in
    `allowed_domains`. A generated query must therefore stay on an allowed host,
    or it returns results that are all thrown away. The service filters the
    primary domains against the source's own allowlist before building.
"""

from __future__ import annotations

# The handful of domains worth spending a query on, most productive first. The
# LinkedIn entry carries the `/jobs/view` path deliberately: a bare
# `site:de.linkedin.com` drags in profile and company pages that
# `denied_url_patterns` then has to clean up. The host of each (the part before
# the first `/`) is what must appear in a source's `allowed_domains`.
PRIMARY_QUERY_DOMAINS: list[str] = [
    "de.linkedin.com/jobs/view",
    "fraunhofer.de",
    "mpg.de",
]

_MAX_TERMS = 5


def _host_of(domain: str) -> str:
    """The bare host of a `site:` target, e.g. `de.linkedin.com/jobs/view`
    -> `de.linkedin.com` — what `allowed_domains` is checked against."""
    return domain.split("/", 1)[0]


def normalize_terms(terms: list[str], *, max_terms: int = _MAX_TERMS) -> list[str]:
    """Trim, drop blanks, de-duplicate case-insensitively, cap the count."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for term in terms:
        t = " ".join(str(term).split())
        if not t:
            continue
        key = t.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(t)
        if len(cleaned) >= max_terms:
            break
    return cleaned


def build_site_queries(
    terms: list[str],
    domains: list[str] | None = None,
    *,
    max_terms: int = _MAX_TERMS,
    suffix: str = "Germany",
) -> list[str]:
    """One `site:<domain> ("t1" OR "t2" …) Germany` query per domain.

    Terms are OR-joined (not AND) on purpose: a resume yields several fields and
    ANDing all of them would demand every term appear in one posting, which
    matches almost nothing. OR keeps each query broad while staying a single
    call per domain. Returns [] when there are no usable terms, so the caller can
    decline to run a fetch with nothing to search for.
    """
    clean = normalize_terms(terms, max_terms=max_terms)
    if not clean:
        return []

    domains = domains if domains is not None else PRIMARY_QUERY_DOMAINS
    or_group = " OR ".join(f'"{t}"' for t in clean)
    tail = f" {suffix}" if suffix else ""

    queries: list[str] = []
    for domain in domains:
        queries.append(f"site:{domain} ({or_group}){tail}")
    return queries


def domains_for(allowed_domains: list[str]) -> list[str]:
    """The primary query domains whose host is permitted by `allowed_domains`.

    Keeps generated queries on hosts the source will actually accept. An empty
    `allowed_domains` means the source imposes no restriction, so all primary
    domains are usable.
    """
    if not allowed_domains:
        return list(PRIMARY_QUERY_DOMAINS)
    allowed = {d.casefold() for d in allowed_domains}
    return [d for d in PRIMARY_QUERY_DOMAINS if _host_of(d).casefold() in allowed]
