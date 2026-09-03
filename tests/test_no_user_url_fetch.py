"""No user-supplied string becomes a server-side fetch target.

The verification subsystem fetches employer domains and ATS boards, and it is
careful about it — SSRF on every hop, robots per origin, a per-host pace, a
12s timeout and a 1.5MB cap. All of that protects a URL we CHOSE. The thing
that must never happen is a URL we were HANDED: a request body or query
parameter that reaches `requests.get` would turn every one of those guards
into a speed bump on somebody else's internal network.

So this test is structural rather than behavioural. It asserts a property of
the code — no route reads a URL from the request and hands it to a fetcher —
because the behavioural version can only test the URLs somebody thought of.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JOBS_BLUEPRINT = ROOT / "services" / "jobs" / "blueprints" / "jobs.py"

#: The tech-feed routes added on 2026-09-02. Each one is named so a NEW route
#: has to be added here deliberately rather than inheriting the guarantee.
NEW_TECH_FEED_ROUTES = (
    "/tech-feed/companies",
    "/tech-feed/company-categories",
    "/tech-feed/saved-searches",
    "/tech-feed/telemetry",
    "/tech-feed/company-requests",
)


def _blueprint_source() -> str:
    return JOBS_BLUEPRINT.read_text(encoding="utf-8")


class TestTheNewRoutesTakeNoUrls:

    def test_every_new_route_exists(self):
        source = _blueprint_source()
        for route in NEW_TECH_FEED_ROUTES:
            assert f'"{route}"' in source, route

    def test_no_new_route_reads_a_url_from_the_request(self):
        """`q`, `ids`, `limit`, `categories`, `cohorts`, a saved-search name
        and a telemetry batch. Not one of them is a URL, and none is fetched."""
        source = _blueprint_source()
        start = source.index("# --- Tech feed telemetry")
        end = source.index("# --- Tech feed feedback")
        block = source[start:end]
        for forbidden in ("requests.get", "requests.post", "urlopen",
                          "Fetcher(", "fetcher.get", "url_is_safe"):
            assert forbidden not in block, forbidden
        # And nothing in the block reads a request field NAMED like a URL.
        assert not re.search(r'request\.\w+\.get\(\s*["\'](url|uri|href|endpoint|'
                             r'callback|redirect|webhook)', block)


class TestTheVerificationFetchersAreOperatorDriven:

    def test_the_official_url_script_takes_its_urls_from_the_registry_or_argv(self):
        """`--url 'Name|domain|url'` is an OPERATOR argument on a CLI that is
        dry-run by default. It is not reachable from any request."""
        source = (ROOT / "scripts" / "verify_official_career_urls.py").read_text(
            encoding="utf-8")
        assert "ocr.OFFICIAL_CAREER_URLS" in source
        assert 'ap.add_argument("--url"' in source
        # The script is not importable from a blueprint.
        assert "verify_official_career_urls" not in _blueprint_source()

    def test_every_verification_fetch_goes_through_the_guarded_fetcher(self):
        from services import career_source_ownership as cso
        source = inspect.getsource(cso)
        # `requests` is imported INSIDE `Fetcher.get` and `RobotsGate._load`
        # and nowhere else, so there is no second request path.
        users = [line for line in source.splitlines()
                 if re.search(r"\brequests\.(get|post|put|request)\b", line)]
        assert len(users) <= 2, users

    def test_the_ssrf_check_runs_on_every_redirect_hop(self):
        from services import career_source_ownership as cso
        source = inspect.getsource(cso.Fetcher.get)
        assert "for hop in chain:" in source
        assert "_ssrf_ok(hop)" in source
        assert "ssrf_redirect" in source

    def test_the_response_is_size_capped_while_it_streams(self):
        from services import career_source_ownership as cso
        source = inspect.getsource(cso.Fetcher.get)
        assert "iter_content" in source
        assert "MAX_HTML_BYTES" in source
        assert cso.MAX_HTML_BYTES <= 5_000_000

    def test_robots_is_consulted_before_the_fetch(self):
        from services import career_source_ownership as cso
        source = inspect.getsource(cso.Fetcher.get)
        assert source.index("self.robots.allows(url)") < source.index("import requests")


class TestTelemetryCannotBeTurnedIntoAFetch:

    def test_no_dimension_can_hold_a_url(self):
        """The dimension allowlist has no URL-shaped key, and the scalar
        cleaner strips `:` and `/` — so even a value that arrived as a URL
        cannot leave as one."""
        from services import tech_feed_telemetry as tft
        assert not (tft.ALLOWED_DIMENSIONS
                    & {"url", "uri", "href", "endpoint", "callback", "redirect"})
        cleaned = tft._clean_dimensions({"ats": "https://169.254.169.254/latest"})
        assert "://" not in cleaned.get("ats", "")
        assert "/" not in cleaned.get("ats", "")
