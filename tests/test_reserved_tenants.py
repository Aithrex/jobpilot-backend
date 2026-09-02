"""A vendor's CDN is not an employer's job board.

Fingerprints run over EVERY url a careers page mentions, and a careers page
mentions its vendor's asset hosts far more often than it mentions its own
board. Before this guard, one `verify_official_career_urls.py --register` run
created these as OWNER-VERIFIED career sources:

    adobe.com     phenom:assets, phenom:pp-cdn, phenom:content-us,
                  phenom:cdn-prod-static
    cisco.com     the same four, plus phenom:cdn-bot
    splunk.com    the same five
    deno.com      ashby:noindex, ashby:Jobs
    booking.com   icims:cookie-policy-scripts,
                  icims:cookie-policy-scripts (careers- prefixed)

Every one names infrastructure. They inflate the source count, they inflate
`ownership_verified`, and each one is an endpoint we would go and fetch.

This is a DISCOVERY guard: a board already in a catalog is crawled by its
stored endpoint and never passes through `endpoint_from_match`, so a false
positive costs a discovery candidate and never a real board.
"""
from __future__ import annotations

import pytest

from services import career_source_ownership as cso
from services.ats_providers import registry as reg


def _tenants(urls):
    return {(e.vendor, e.tenant) for e in cso.endpoints_from_page(urls)}


class TestTheObservedJunk:

    def test_phenoms_four_shared_asset_hosts_are_refused(self):
        found = _tenants([
            "https://assets.phenompeople.com/CareerConnectResources/x.js",
            "https://pp-cdn.phenompeople.com/b.png",
            "https://content-us.phenompeople.com/social/x.png",
            "https://cdn-prod-static.phenompeople.com/a.css",
            "https://cdn-bot.phenompeople.com/x",
        ])
        assert found == set()

    def test_a_real_phenom_tenant_on_the_same_host_still_matches(self):
        assert ("phenom", "adobe") in _tenants(
            ["https://adobe.phenompeople.com/careers"])

    def test_ashbys_noindex_is_not_a_company(self):
        found = _tenants(["https://jobs.ashbyhq.com/noindex",
                          "https://jobs.ashbyhq.com/DeepL"])
        assert ("ashby", "DeepL") in found
        assert not any(t.lower() == "noindex" for _v, t in found)

    def test_a_slug_a_company_could_OWN_is_left_alone(self):
        """`greenhouse:css` is CloudKitchens (City Storage Systems) and
        `greenhouse:manifest` is Manifest Cyber — both live, both returning
        real postings, both refused by an earlier draft of this guard.
        Refusing a plausible slug silently loses an employer, which is the
        failure this whole effort exists to fix."""
        found = _tenants(["https://boards.greenhouse.io/public",
                          "https://boards.greenhouse.io/support",
                          "https://boards.greenhouse.io/help",
                          "https://boards.greenhouse.io/css",
                          "https://boards.greenhouse.io/manifest"])
        assert found == {("greenhouse", "public"), ("greenhouse", "support"),
                         ("greenhouse", "help"), ("greenhouse", "css"),
                         ("greenhouse", "manifest")}

    def test_a_cookie_banner_host_is_not_an_icims_portal(self):
        found = _tenants([
            "https://cookie-policy-scripts.icims.com/jobs",
            "https://careers-cookie-policy-scripts.icims.com/jobs",
            "https://us-careers-rivian.icims.com/jobs",
        ])
        assert found == {("icims", "us-careers-rivian")}

    def test_the_icims_portal_distinctions_that_MUST_survive(self):
        """`careers-rivian`, `us-careers-rivian` and bare `rivian` are three
        different portals with different job sets. The guard must not tidy
        them into one."""
        found = _tenants(["https://careers-rivian.icims.com/jobs",
                          "https://us-careers-rivian.icims.com/jobs",
                          "https://rivian.icims.com/jobs"])
        assert found == {("icims", "careers-rivian"),
                         ("icims", "us-careers-rivian"),
                         ("icims", "rivian")}


class TestTheGuardItself:

    @pytest.mark.parametrize("tenant", [
        "cdn", "cdn-bot", "www", "noindex", "nofollow", "robots", "sitemap",
        "favicon", "__assets__",
        # Infrastructure spelled as a COMPOUND. A real separator on at least
        # one side is what tells these apart from the bare words below.
        "pp-cdn", "cdn-prod-static", "content-us", "cookie-policy-scripts",
        "static-assets", "assets-us",
        "",
    ])
    def test_reserved(self, tenant):
        assert reg.get("greenhouse").is_reserved_tenant(tenant) is True

    @pytest.mark.parametrize("tenant", [
        "anthropic", "adobe", "assetmark", "staticly", "contentful",
        "us-careers-rivian", "openai", "d-Matrix", "quantinuum",
        "mediatek", "cdnetworks-korea",
        # Live boards that return real postings. The first draft of this
        # guard refused the first four as "page furniture"; the second draft
        # refused `css` (CloudKitchens, City Storage Systems) and `manifest`
        # (Manifest Cyber). Every one is an employer this would have lost.
        "public", "support", "help", "jobs", "css", "manifest", "content",
        "static", "media", "assets",
    ])
    def test_not_reserved(self, tenant):
        assert reg.get("greenhouse").is_reserved_tenant(tenant) is False

    def test_the_guard_is_case_insensitive(self):
        p = reg.get("greenhouse")
        assert p.is_reserved_tenant("CDN-BOT") is True
        assert p.is_reserved_tenant("NoIndex") is True
        assert p.is_reserved_tenant("PP-CDN") is True

    def test_every_registered_provider_answers_the_question(self):
        """A provider that overrides `endpoint_from_match` without calling the
        guard is the hole this closes — at minimum every one must inherit it."""
        for vendor in reg.vendors():
            provider = reg.get(vendor)
            assert provider.is_reserved_tenant("cdn-bot") is True, vendor
            assert provider.is_reserved_tenant("anthropic") is False, vendor


class TestTheAssetFilterIsThePreciseSignal:
    """An extension test has no false positives on a real board.

    No ATS serves a job board at a `.js` or `.png` path, so dropping asset
    urls before fingerprinting removes the whole Phenom CDN family without
    judging a single company name. This is the check that made the tenant
    denylist above short enough to be safe.
    """

    def test_the_phenom_asset_family_never_reaches_a_fingerprint(self):
        assert _tenants([
            "https://assets.phenompeople.com/CareerConnectResources/x.js",
            "https://cdn-prod-static.phenompeople.com/a.css",
            "https://pp-cdn.phenompeople.com/b.png",
            "https://content-us.phenompeople.com/social/x.svg",
        ]) == set()

    def test_the_SAME_host_with_a_page_path_is_kept(self):
        """The host is not what is being refused. Only the file is."""
        assert ("phenom", "adobe") in _tenants(
            ["https://adobe.phenompeople.com/careers"])

    @pytest.mark.parametrize("url", [
        "https://boards.greenhouse.io/acme.js",
        "https://boards.greenhouse.io/acme.css",
        "https://boards.greenhouse.io/acme.png",
        "https://boards.greenhouse.io/acme.woff2",
        "https://boards.greenhouse.io/acme.json",
    ])
    def test_an_asset_under_a_real_board_host_is_still_an_asset(self, url):
        assert cso._is_asset_url(url) is True
        assert _tenants([url]) == set()

    def test_a_query_string_does_not_hide_a_page(self):
        assert cso._is_asset_url(
            "https://boards.greenhouse.io/acme?v=1.js") is False
        assert ("greenhouse", "acme") in _tenants(
            ["https://boards.greenhouse.io/acme?v=1.js"])

    def test_a_malformed_url_is_not_an_asset_and_does_not_raise(self):
        assert cso._is_asset_url("http://[") is False
        assert cso._is_asset_url("") is False
        assert cso._is_asset_url(None) is False


class TestPerVendorReservedTenants:
    """The shared list has to stay short enough to be safe for EVERY vendor,
    so a vendor's own plumbing is declared on that vendor.

    `assets` is not in `_RESERVED_TENANTS` — `greenhouse:assets` could be a
    company. `phenom:assets` cannot: it is `assets.phenompeople.com`, the
    host every Phenom customer's careers page loads, and 44 employers held it
    as their own owner-verified career source.
    """

    def test_phenom_refuses_its_own_asset_hosts(self):
        p = reg.get("phenom")
        for tenant in ("assets", "pp-cdn", "cdn-prod-static", "content-us",
                       "content-ca", "content-ir", "cdn-bot"):
            assert p.is_reserved_tenant(tenant) is True, tenant

    def test_phenom_keeps_its_real_customers(self):
        p = reg.get("phenom")
        for tenant in ("adobe", "united", "lilly", "ecolab", "toyota",
                       "landolakes", "southwest"):
            assert p.is_reserved_tenant(tenant) is False, tenant

    def test_jobvite_refuses_its_asset_segment(self):
        assert reg.get("jobvite").is_reserved_tenant("__assets__") is True
        assert reg.get("jobvite").is_reserved_tenant("acme") is False

    def test_one_vendors_list_does_not_bind_another(self):
        """`greenhouse:assets` stays allowed. A per-vendor declaration is a
        statement about that vendor's hostnames, not about the word."""
        assert reg.get("greenhouse").is_reserved_tenant("assets") is False
        assert reg.get("phenom").is_reserved_tenant("assets") is True
