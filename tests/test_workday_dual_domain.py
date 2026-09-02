"""Workday serves the same CXS contract from two public domains.

`myworkdayjobs.com` puts the tenant in the subdomain; `myworkdaysite.com` puts
it in the path. The domain was hardcoded, so every board on the second one was
unreachable — including Snap's, which answers with 184 postings.

The load-bearing constraint is that NOTHING about the first domain may change:
both apply-URL shapes this repo has minted are stored as `canonical_url` on
hundreds of thousands of postings, and re-shaping them would re-hash the whole
Workday inventory.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared" / "python"))

from services import workday_cxs_fetcher as w   # noqa: E402


class TestTheApiOrigin:
    def test_the_default_domain_is_unchanged(self):
        assert w.cxs_base("dexcom", "wd1") == "https://dexcom.wd1.myworkdayjobs.com"

    def test_an_empty_domain_falls_back_to_the_default(self):
        assert w.cxs_base("dexcom", "wd1", "") == w.cxs_base("dexcom", "wd1")

    def test_myworkdaysite_puts_no_tenant_in_the_subdomain(self):
        assert w.cxs_base("snapchat", "wd1", "myworkdaysite.com") \
            == "https://wd1.myworkdaysite.com"


class TestTheApplyUrlShapesAreFrozen:
    def test_the_live_search_shape_keeps_its_locale_segment(self):
        assert w.apply_base("dexcom", "wd1", "Dexcom", locale=True) \
            == "https://dexcom.wd1.myworkdayjobs.com/en-US/Dexcom"

    def test_the_inventory_shape_keeps_having_none(self):
        """The two callers have always minted different shapes and both are
        stored. Unifying them would re-hash the whole Workday inventory."""
        assert w.apply_base("dexcom", "wd1", "Dexcom") \
            == "https://dexcom.wd1.myworkdayjobs.com/Dexcom"

    def test_myworkdaysite_puts_the_tenant_in_the_path(self):
        assert w.apply_base("snapchat", "wd1", "snap", "myworkdaysite.com") \
            == "https://wd1.myworkdaysite.com/recruiting/snapchat/snap"


class TestTheEnricherCanParseBothBack:
    """A detail URL the enricher cannot parse means the posting arrives with no
    description, which starves the scorer."""

    def test_the_original_shape_still_parses(self):
        m = w._APPLY_URL_RE.match(
            "https://dexcom.wd1.myworkdayjobs.com/en-US/Dexcom/job/San-Diego/x_JR1")
        assert m and m.group(1) == "dexcom" and m.group(3) == "Dexcom"

    def test_the_original_shape_without_the_locale_still_parses(self):
        assert w._APPLY_URL_RE.match(
            "https://dexcom.wd1.myworkdayjobs.com/Dexcom/job/San-Diego/x_JR1")

    def test_the_site_shape_parses_into_tenant_host_site_path(self):
        m = w._APPLY_URL_SITE_RE.match(
            "https://wd1.myworkdaysite.com/recruiting/snapchat/snap/"
            "job/London-United-Kingdom/Account-Manager_R0046668-1")
        assert m
        host, tenant, site, path = m.groups()
        assert (host, tenant, site) == ("wd1", "snapchat", "snap")
        assert path.startswith("job/")

    def test_a_url_from_neither_domain_matches_neither(self):
        for pat in (w._APPLY_URL_RE, w._APPLY_URL_SITE_RE):
            assert not pat.match("https://boards.greenhouse.io/acme/jobs/1")


class TestTheSpecStaysBackwardCompatible:
    def test_a_four_element_spec_gets_the_default_domain(self):
        assert w.spec_domain(("t", "wd1", "site", "Display")) == w.WD_DOMAIN_JOBS

    def test_a_fifth_element_selects_the_other_domain(self):
        assert w.spec_domain(("t", "wd1", "site", "D", "myworkdaysite.com")) \
            == w.WD_DOMAIN_SITE

    def test_a_blank_fifth_element_is_the_default(self):
        assert w.spec_domain(("t", "wd1", "site", "D", "")) == w.WD_DOMAIN_JOBS


class TestTheCatalogRow:
    def test_snap_is_registered_on_the_site_domain(self):
        from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
        rows = [r for r in WORKDAY_TENANT_CATALOG if r["tenant"] == "snapchat"]
        assert rows and rows[0]["wd_domain"] == "myworkdaysite.com"

    def test_every_other_row_omits_the_domain_and_is_therefore_unchanged(self):
        from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
        odd = [r for r in WORKDAY_TENANT_CATALOG
               if r.get("wd_domain") and r["wd_domain"] != "myworkdaysite.com"]
        assert odd == []
