"""A board URL, per vendor, and "" rather than a guess.

The defect this fixes
---------------------
`career_source_ownership._endpoint_url` built the board URL as
`f"https://{portal_hosts[0].lstrip('.')}/{tenant}"`. `portal_hosts` entries
are SUFFIXES for every vendor that gives each customer its own hostname, so
`.icims.com` became `icims.com` and the URL became
`https://icims.com/careers-rivian` — a page that is nobody's board.

That URL is the only input to `backlink_signal` and to
`candidate_domains_from_board`, which between them are the ONLY route to the
`employer_domain` evidence class. So iCIMS, BambooHR, Cornerstone, Taleo,
Recruitee, Workday and Oracle could never obtain it, and those are the biggest
vendors in the uncovered queue. Measured on the last pass-1 sweep: iCIMS 240
sources checked and 0 ownerships proven, BambooHR 95 and 0.
"""
from __future__ import annotations

import pytest

from services import career_source_ownership as cso
from services.ats_providers import registry as ats_registry
from services.ats_providers.base import Endpoint


def _ep(vendor, tenant, host="", site="", extra=None):
    return Endpoint(vendor=vendor, tenant=tenant, host=host, site=site,
                    extra=dict(extra or {}))


class TestSuffixHostsBecomeSubdomains:
    """`.icims.com` is a suffix. `<tenant>.icims.com` is a board."""

    @pytest.mark.parametrize("vendor,tenant,expected", [
        ("icims", "careers-rivian", "https://careers-rivian.icims.com"),
        ("bamboohr", "acme", "https://acme.bamboohr.com"),
        ("cornerstone", "beckman", "https://beckman.csod.com"),
        ("taleo", "crowley", "https://crowley.taleo.net"),
        ("recruitee", "acme", "https://acme.recruitee.com"),
    ])
    def test_the_tenant_becomes_the_subdomain(self, vendor, tenant, expected):
        assert ats_registry.require(vendor).board_url(_ep(vendor, tenant)) == expected

    def test_the_broken_shape_is_gone(self):
        """`https://icims.com/<tenant>` is nobody's board."""
        url = cso._endpoint_url(_ep("icims", "careers-rivian"))
        assert "//icims.com/" not in url
        assert url.startswith("https://careers-rivian.icims.com")


class TestRealHostnamesTakeATenantPath:

    @pytest.mark.parametrize("vendor,tenant,expected", [
        ("greenhouse", "stripe", "https://boards.greenhouse.io/stripe"),
        ("lever", "forward", "https://jobs.lever.co/forward"),
        ("ashby", "stripe", "https://jobs.ashbyhq.com/stripe"),
        ("smartrecruiters", "acme", "https://jobs.smartrecruiters.com/acme"),
    ])
    def test_the_tenant_becomes_a_path(self, vendor, tenant, expected):
        assert ats_registry.require(vendor).board_url(_ep(vendor, tenant)) == expected


class TestAStoredHostWins:

    def test_workday_uses_its_pod(self):
        url = ats_registry.require("workday").board_url(
            _ep("workday", "aaon", host="aaon.wd108.myworkdayjobs.com",
                site="AAON", extra={"wd_host": "wd108"}))
        assert url == "https://aaon.wd108.myworkdayjobs.com/AAON"

    def test_workday_on_the_second_domain_puts_the_tenant_in_the_PATH(self):
        url = ats_registry.require("workday").board_url(
            _ep("workday", "snapchat", host="wd1.myworkdaysite.com", site="snap",
                extra={"wd_host": "wd1", "wd_domain": "myworkdaysite.com"}))
        assert url == "https://wd1.myworkdaysite.com/recruiting/snapchat/snap"

    def test_oracle_uses_its_pod_hostname(self):
        url = ats_registry.require("oracle_orc").board_url(
            _ep("oracle_orc", "cbdt", host="cbdt.fa.us2.oraclecloud.com",
                site="CX_1"))
        assert url.startswith("https://cbdt.fa.us2.oraclecloud.com/hcmUI/")


class TestRefusingIsBetterThanGuessing:
    """A wrong board URL does not fail — it fetches a STRANGER's page and
    offers whatever it finds there as evidence about this employer."""

    def test_workday_without_a_pod_returns_nothing(self):
        assert ats_registry.require("workday").board_url(
            _ep("workday", "livenation")) == ""

    def test_oracle_without_a_pod_hostname_returns_nothing(self):
        assert ats_registry.require("oracle_orc").board_url(
            _ep("oracle_orc", "fa-exty-saasfaprod1")) == ""

    def test_an_endpoint_with_no_tenant_returns_nothing(self):
        assert ats_registry.require("greenhouse").board_url(
            _ep("greenhouse", "")) == ""

    def test_an_unknown_vendor_returns_nothing_rather_than_raising(self):
        assert cso._endpoint_url(_ep("not-a-vendor", "acme")) == ""


class TestEveryRegisteredVendorAnswers:

    def test_no_vendor_produces_a_suffix_shaped_url(self):
        """The regression, checked across the whole registry rather than on
        the four vendors somebody remembered."""
        for provider in ats_registry.all_providers(enabled_only=False):
            url = provider.board_url(_ep(provider.vendor, "sometenant"))
            if not url:
                continue
            host = url.split("//", 1)[1].split("/", 1)[0]
            assert not host.startswith("."), (provider.vendor, url)
            assert host not in {"icims.com", "bamboohr.com", "csod.com",
                                "taleo.net", "recruitee.com",
                                "oraclecloud.com", "myworkdayjobs.com"}, \
                (provider.vendor, url)
