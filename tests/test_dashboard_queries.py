"""Injected search queries: bounded, on-domain, OR-joined.

`SearchApiSource` searches its `config.queries` and drops any result off the
`allowed_domains`. So a generated query must stay on an allowed host and must not
fan out into a call per keyword — both are enforced here.
"""

from __future__ import annotations

from job_alerts.dashboard.queries import (
    PRIMARY_QUERY_DOMAINS,
    build_site_queries,
    domains_for,
    normalize_terms,
)


class TestNormalizeTerms:
    def test_dedupes_case_insensitively_and_caps(self):
        terms = ["Computer Vision", "computer vision", "  ", "NLP", "RL", "robotics", "sixth"]
        assert normalize_terms(terms, max_terms=5) == [
            "Computer Vision",
            "NLP",
            "RL",
            "robotics",
            "sixth",
        ]

    def test_collapses_whitespace(self):
        assert normalize_terms(["  deep   learning "]) == ["deep learning"]


class TestBuildSiteQueries:
    def test_one_query_per_domain_or_joined(self):
        qs = build_site_queries(["reinforcement learning", "computer vision"])
        assert len(qs) == len(PRIMARY_QUERY_DOMAINS)
        assert qs[0] == (
            'site:de.linkedin.com/jobs/view '
            '("reinforcement learning" OR "computer vision") Germany'
        )

    def test_no_terms_yields_no_queries(self):
        assert build_site_queries([]) == []
        assert build_site_queries(["  ", ""]) == []

    def test_terms_are_capped(self):
        many = [f"t{i}" for i in range(20)]
        q = build_site_queries(many, ["fraunhofer.de"])[0]
        # 5 quoted terms max in the OR group.
        assert q.count(" OR ") == 4

    def test_custom_domains_and_suffix(self):
        qs = build_site_queries(["nlp"], ["example.edu"], suffix="")
        assert qs == ['site:example.edu ("nlp")']


class TestDomainsFor:
    def test_filters_to_allowed_hosts(self):
        assert domains_for(["fraunhofer.de"]) == ["fraunhofer.de"]

    def test_linkedin_path_host_is_matched(self):
        # The primary domain carries a /jobs/view path, but its HOST is what the
        # allowlist checks.
        assert "de.linkedin.com/jobs/view" in domains_for(["de.linkedin.com"])

    def test_empty_allowlist_means_no_restriction(self):
        assert domains_for([]) == list(PRIMARY_QUERY_DOMAINS)

    def test_unlisted_domains_are_dropped(self):
        assert domains_for(["nowhere.example"]) == []
