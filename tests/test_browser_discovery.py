"""Browser-rendered discovery: JS-shell careers pages via Fetcher.render.

The HTML walk cannot see an XHR to boards-api.greenhouse.io. The browser
pass captures that request, fingerprints it through the SAME
`endpoints_from_page` path, and feeds ownership through
`adjudicate_ownership`. A bot challenge stays `blocked` — never solved.
"""
from __future__ import annotations

import pytest

from services import career_source_ownership as cso
from services import career_source_service as css
from services import employer_registry as er
from services.ats_providers import registry as ats_registry


class ScriptedRenderFetcher:
    """A scripted Fetcher whose `render` returns canned DOM + network URLs."""

    def __init__(self, pages=None, renders=None, challenges=None):
        self.pages = pages or {}
        self.renders = renders or {}
        self.challenges = set(challenges or ())
        self.requested = []
        self.rendered = []
        self.enable_browser = True

    def get(self, url):
        self.requested.append(url)
        for key in sorted(self.pages, key=len, reverse=True):
            if key in url:
                return {"status": 200, "text": self.pages[key], "chain": [url],
                        "final_url": url, "error": "", "network_urls": [],
                        "rendered": False}
        return {"status": 404, "text": "", "chain": [url], "final_url": url,
                "error": "", "network_urls": [], "rendered": False}

    def render(self, url):
        self.rendered.append(url)
        if any(c in url for c in self.challenges):
            return {"status": 403, "text": "<html><title>Just a moment...</title>"
                    "<body>Checking your browser</body></html>",
                    "chain": [url], "final_url": url, "error": "bot_challenge",
                    "network_urls": [], "rendered": True}
        for key in sorted(self.renders, key=len, reverse=True):
            if key in url:
                body, network = self.renders[key]
                return {"status": 200, "text": body, "chain": [url],
                        "final_url": url, "error": "",
                        "network_urls": list(network), "rendered": True}
        return {"status": 404, "text": "", "chain": [url], "final_url": url,
                "error": "", "network_urls": [], "rendered": True}

    def close(self):
        pass


class TestApiGreenhouseFingerprint:
    """JS shells XHR api.greenhouse.io, not only boards-api."""

    def test_api_greenhouse_io_names_the_board(self):
        url = "https://api.greenhouse.io/v1/boards/digitalocean98/embed/departments"
        eps = ats_registry.endpoints_from_urls([url])
        assert [(e.vendor, e.tenant) for e in eps] == [
            ("greenhouse", "digitalocean98")]

    def test_boards_api_still_works(self):
        url = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        eps = ats_registry.endpoints_from_urls([url])
        assert [(e.vendor, e.tenant) for e in eps] == [("greenhouse", "acme")]


class TestRenderedNetworkIsTheEvidence:

    def test_an_xhr_only_board_is_found(self):
        """The board appears ONLY in the network list — nowhere in the HTML."""
        fetcher = ScriptedRenderFetcher(
            pages={"digitalocean.com": "<html><body>DigitalOcean</body></html>"},
            renders={
                "digitalocean.com/careers": (
                    "<html><body>Careers at DigitalOcean</body></html>",
                    ["https://api.greenhouse.io/v1/boards/digitalocean98/embed/departments",
                     "https://www.digitalocean.com/assets/app.js"],
                ),
            },
        )
        out = cso.walk_to_endpoints(
            fetcher, display_name="DigitalOcean",
            verified_domain="digitalocean.com",
            start_url="https://www.digitalocean.com/careers",
            render=True)
        assert [(e.vendor, e.tenant) for e in out["endpoints"]] == [
            ("greenhouse", "digitalocean98")]
        assert out["career_landing_url"]
        assert fetcher.rendered  # used render, not get, for the careers page

    def test_a_challenge_is_blocked_not_solved(self):
        fetcher = ScriptedRenderFetcher(
            renders={"cognizant.com": ("<html><body>x</body></html>", [])},
            challenges=["cognizant.com"],
        )
        out = cso.walk_to_endpoints(
            fetcher, display_name="Cognizant",
            verified_domain="cognizant.com",
            start_url="https://www.cognizant.com/careers",
            render=True)
        assert out["endpoints"] == []
        assert out["blocked"] is True
        assert "challenge" in out["detail"].lower() or out["blocked"]

    def test_plain_walk_does_not_see_the_xhr(self):
        """Without render, the same page yields nothing — that is why the
        fifth pass exists."""
        fetcher = ScriptedRenderFetcher(
            pages={
                "digitalocean.com/careers":
                    "<html><body>Careers at DigitalOcean</body></html>",
                "digitalocean.com": "<html><body>DigitalOcean</body></html>",
            },
        )
        out = cso.walk_to_endpoints(
            fetcher, display_name="DigitalOcean",
            verified_domain="digitalocean.com",
            start_url="https://www.digitalocean.com/careers",
            render=False)
        assert out["endpoints"] == []


class TestVerifyEmployerBrowserPath:

    def test_browser_discovery_produces_owned_source(self, monkeypatch):
        """Rendered network URL -> domain link evidence -> ownership."""
        employer = er.build_employer(
            display_name="DigitalOcean", canonical_domain="digitalocean.com")
        # Domain verify needs the homepage to name them.
        fetcher = ScriptedRenderFetcher(
            pages={"digitalocean.com":
                   "<html><title>DigitalOcean</title><body>DigitalOcean cloud</body></html>"},
            renders={
                "digitalocean.com/careers": (
                    "<html><body>Careers at DigitalOcean</body></html>",
                    ["https://boards-api.greenhouse.io/v1/boards/digitalocean98/jobs"],
                ),
            },
        )
        recs = cso.verify_employer_source(
            employer=employer, fetcher=fetcher, use_browser=True,
            official_url="https://www.digitalocean.com/careers")
        assert recs
        owned = [r for r in recs if r.get("ats_vendor") == "greenhouse"]
        assert owned, recs
        assert owned[0]["ownership_verified"] is True
        assert owned[0]["tenant"] == "digitalocean98"
        # Rule 4a: verified-domain link is sufficient alone.
        kinds = {e.get("kind") for e in owned[0]["ownership_evidence"]
                 if e.get("confirmed")}
        assert css.SIGNAL_DOMAIN_LINK in kinds

    def test_challenge_without_endpoint_is_blocked(self):
        employer = er.build_employer(
            display_name="Cognizant", canonical_domain="cognizant.com")
        fetcher = ScriptedRenderFetcher(
            pages={"cognizant.com":
                   "<html><title>Cognizant</title><body>Cognizant</body></html>"},
            challenges=["cognizant.com/careers", "careers.cognizant"],
        )
        recs = cso.verify_employer_source(
            employer=employer, fetcher=fetcher, use_browser=True,
            official_url="https://www.cognizant.com/careers")
        assert len(recs) == 1
        assert recs[0]["state"] == css.STATE_BLOCKED
        assert recs[0]["failure_code"] == css.FAIL_CHALLENGE
        assert not recs[0].get("ats_vendor")


class TestFetcherRenderGates:
    """render refuses before Playwright when the gates say no."""

    def test_render_refuses_when_browser_disabled(self):
        f = cso.Fetcher(enable_browser=False)
        got = f.render("https://example.com/careers")
        assert got["error"] == "browser_disabled"
        assert got["rendered"] is True

    def test_render_refuses_ssrf_before_browser(self, monkeypatch):
        f = cso.Fetcher(enable_browser=True)
        monkeypatch.setattr(cso, "_ssrf_ok", lambda url: (False, "private"))
        # Must not attempt to launch Chromium.
        def _boom():
            raise AssertionError("browser must not launch on SSRF fail")
        f._ensure_browser = _boom  # type: ignore[method-assign]
        got = f.render("https://127.0.0.1/careers")
        assert got["error"].startswith("ssrf:")
