"""The ownership walk reaches boards it could not reach before.

Two defects, both measured on live employers, both silent:

1. **It never looked at the careers HOST.** `CAREER_PATHS` only ever tried
   `www.<domain>/<path>`, and that is not where a large employer's board
   lives: an enterprise runs a white-labelled board on `careers.<domain>` or
   `jobs.<domain>`, which usually redirects straight onto the ATS. Akamai,
   Rivian and Capgemini all read "no ATS fingerprint found" while their board
   sat one DNS label from their homepage.

2. **It fingerprinted a page's LINKS and never the page's own address.** A
   careers host that 302s onto the ATS — `careers.rivian.com` ->
   `careers-rivian.icims.com/jobs/search` — has the endpoint in its OWN URL
   and, very often, nowhere in its body. The walk read that page, looked only
   at what it linked to, found nothing, and reported failure while standing on
   the board.

Neither produced an error. There was simply no row.
"""
from __future__ import annotations

import pytest

from services import career_source_ownership as cso
from services import career_source_service as css
from services import employer_registry as er


class ScriptedFetcher:
    """A scripted internet that can also REDIRECT, which is the point here."""

    def __init__(self, pages, redirects=None):
        self.pages = pages
        self.redirects = redirects or {}
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        for key, dest in self.redirects.items():
            if key in url:
                body = ""
                for pkey, pbody in self.pages.items():
                    if pkey in dest:
                        body = pbody
                return {"status": 200, "text": body, "chain": [url, dest],
                        "final_url": dest, "error": ""}
        # LONGEST key first: `careers.acme.example` must win over
        # `acme.example`, or the scripted internet serves the homepage for
        # every subdomain and the test measures the fixture.
        for key in sorted(self.pages, key=len, reverse=True):
            if key in url:
                return {"status": 200, "text": self.pages[key], "chain": [url],
                        "final_url": url, "error": ""}
        return {"status": 404, "text": "", "chain": [url], "final_url": url,
                "error": ""}


def _employer(name="Rivian", domain="rivian.com"):
    return er.build_employer(display_name=name, canonical_domain=domain)


class TestTheCareersSubdomain:

    def test_the_careers_host_is_tried_before_paths_on_the_homepage(self):
        fetcher = ScriptedFetcher({
            "rivian.com": "<html><body>Rivian</body></html>",
            "careers.rivian.com": (
                "<html><body>Rivian careers "
                '<a href="https://careers-rivian.icims.com/jobs/search">Jobs</a>'
                "</body></html>"),
        })
        out = cso.walk_to_endpoints(fetcher, display_name="Rivian",
                                   verified_domain="rivian.com")
        assert [f"{e.vendor}:{e.tenant}" for e in out["endpoints"]] \
            == ["icims:careers-rivian"]
        # And it was tried EARLY, not after eight fruitless paths.
        assert any("careers.rivian.com" in u for u in fetcher.requested[:4])

    def test_the_jobs_host_is_tried_too(self):
        fetcher = ScriptedFetcher({
            "acme.example": "<html><body>Acme</body></html>",
            "jobs.acme.example": (
                '<html><body>Acme <a href="https://boards.greenhouse.io/acme">'
                "Roles</a></body></html>"),
        })
        out = cso.walk_to_endpoints(fetcher, display_name="Acme",
                                   verified_domain="acme.example")
        assert [f"{e.vendor}:{e.tenant}" for e in out["endpoints"]] \
            == ["greenhouse:acme"]

    def test_only_two_subdomains_are_tried(self):
        """Each one is a request against a host that usually does not exist."""
        assert cso.CAREER_SUBDOMAINS == ("careers", "jobs")


class TestTheRedirectIsTheEvidence:

    def test_a_careers_host_that_redirects_onto_the_ats_is_found(self):
        """The endpoint is in the URL and nowhere in the body."""
        fetcher = ScriptedFetcher(
            pages={
                "rivian.com": "<html><body>Rivian</body></html>",
                "careers-rivian.icims.com": "<html><body>Search jobs</body></html>",
            },
            redirects={"careers.rivian.com":
                       "https://careers-rivian.icims.com/jobs/search"},
        )
        out = cso.walk_to_endpoints(fetcher, display_name="Rivian",
                                    verified_domain="rivian.com")
        assert [f"{e.vendor}:{e.tenant}" for e in out["endpoints"]] \
            == ["icims:careers-rivian"]
        assert "resolves to" in out["detail"]

    def test_a_redirect_is_evidence_even_when_the_destination_refuses(self):
        """An employer host that 301s onto their board has NAMED it. Whether
        the ATS then served us a bot wall is a fact about our request."""
        class Blocked(ScriptedFetcher):
            def get(self, url):
                if "icims.com" in url:
                    return {"status": 403, "text": "", "chain": [url],
                            "final_url": url, "error": ""}
                return super().get(url)

        fetcher = Blocked(
            pages={"rivian.com": "<html><body>Rivian</body></html>"},
            redirects={"careers.rivian.com":
                       "https://careers-rivian.icims.com/jobs/search"},
        )
        out = cso.walk_to_endpoints(fetcher, display_name="Rivian",
                                    verified_domain="rivian.com")
        assert [f"{e.vendor}:{e.tenant}" for e in out["endpoints"]] \
            == ["icims:careers-rivian"]

    def test_a_page_with_neither_a_link_nor_a_redirect_still_reports_honestly(self):
        fetcher = ScriptedFetcher({
            "opaque.example": "<html><body>Opaque Inc</body></html>"})
        out = cso.walk_to_endpoints(fetcher, display_name="Opaque Inc",
                                    verified_domain="opaque.example")
        assert out["endpoints"] == []
        assert "no ATS fingerprint found" in out["detail"]


class TestTheSafetyPropertiesSurvive:
    """The walk got wider. It must not have got less careful."""

    def test_every_fetch_still_goes_through_the_fetcher(self):
        """No path builds its own request. The `Fetcher` is where the SSRF
        check, robots.txt, the pace, the timeout and the size cap live, and a
        second request path would bypass all five."""
        import inspect
        source = inspect.getsource(cso.walk_to_endpoints)
        assert "requests." not in source
        assert "urlopen" not in source
        assert source.count("fetcher.get(") >= 1

    def test_the_page_budget_is_still_bounded(self):
        """A wider queue must not become an unbounded crawl of one employer."""
        calls = {"n": 0}

        class Counting(ScriptedFetcher):
            def get(self, url):
                calls["n"] += 1
                return {"status": 200, "chain": [url], "final_url": url,
                        "error": "",
                        "text": '<html><body>Acme <a href="/careers/x">more</a>'
                                '<a href="/jobs/y">more</a></body></html>'}

        cso.walk_to_endpoints(Counting({}), display_name="Acme",
                              verified_domain="acme.example")
        # The queue is 2 subdomains + 6 paths + homepage, and each page may
        # follow up to 4 of its own career links, all under MAX_CAREER_PAGES.
        assert calls["n"] <= cso.MAX_CAREER_PAGES * 4, calls["n"]

    def test_the_ssrf_wrapper_still_unpacks_the_two_tuple(self):
        """`url_is_safe` returns `(ok, reason)`. A non-empty tuple is always
        truthy, and `if not url_is_safe(url)` is how that guard came to have
        never blocked anything."""
        import inspect
        source = inspect.getsource(cso._ssrf_ok)
        assert "ok, reason = url_is_safe(url)" in source
        assert "return bool(ok)" in source
