"""A career source knows WHERE its board is. The content pass must ask it.

Pass 3 rebuilt the endpoint as `Endpoint(vendor, tenant)` and threw away
everything else the source had recorded. For Greenhouse and Lever that is
harmless — a tenant IS the address. For the three vendors that carry most of
the enterprise inventory it is fatal:

    Adobe      adobe.wd5.myworkdayjobs.com/external_experienced
    Accenture  accenture.wd103.myworkdayjobs.com/AccentureCareers
    Snap       wd1.myworkdaysite.com/recruiting/snapchat/snap
    Rivian     us-careers-rivian.icims.com

`workday:adobe` addresses none of those. Every one of them came back
`unparseable_response`, which reads as "their board is broken" and meant "we
asked the wrong URL" — and the failure was concentrated in exactly the vendors
that need a host, invisible in the ones that do not.
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

verify = importlib.import_module("scripts.verify_career_sources")


class TestTheCatalogIsPreferred:

    def test_a_company_ats_endpoints_row_supplies_host_site_and_extra(self):
        ep = verify.hydrate_endpoint(
            {"ats_vendor": "workday", "tenant": "adobe",
             "member_key": "workday-adobe-external_experienced",
             "canonical_endpoint": "https://ignored.example/x"},
            {"workday-adobe-external_experienced": {
                "vendor": "workday", "tenant": "adobe",
                "host": "adobe.wd5.myworkdayjobs.com",
                "site": "external_experienced",
                "extra": {"wd_host": "wd5"}}})
        assert ep.host == "adobe.wd5.myworkdayjobs.com"
        assert ep.site == "external_experienced"
        assert ep.extra["wd_host"] == "wd5"


class TestTheFallbackReachesWhatTheCatalogCannot:
    """A source discovered by the ownership walk or by `--register` has a
    canonical endpoint and NO `company_ats_endpoints` row, because nothing has
    crawled it yet — which is precisely the population this pass exists for."""

    @pytest.mark.parametrize("url,tenant,host,site", [
        ("https://adobe.wd5.myworkdayjobs.com/external_experienced",
         "adobe", "adobe.wd5.myworkdayjobs.com", "external_experienced"),
        ("https://accenture.wd103.myworkdayjobs.com/AccentureCareers",
         "accenture", "accenture.wd103.myworkdayjobs.com", "AccentureCareers"),
    ])
    def test_a_workday_pod_and_site_are_recovered_from_the_url(
            self, url, tenant, host, site):
        ep = verify.hydrate_endpoint(
            {"ats_vendor": "workday", "tenant": tenant,
             "canonical_endpoint": url}, {})
        assert ep.host == host
        assert ep.site == site

    def test_the_myworkdaysite_form_is_recovered_too(self):
        """Tenant in the PATH, not the host. It is a different URL shape and
        it is the one Snap uses."""
        ep = verify.hydrate_endpoint(
            {"ats_vendor": "workday", "tenant": "snapchat",
             "canonical_endpoint":
                 "https://wd1.myworkdaysite.com/recruiting/snapchat/snap"}, {})
        assert ep.host == "wd1.myworkdaysite.com"
        assert ep.tenant == "snapchat"
        assert ep.site == "snap"
        assert ep.extra.get("wd_domain") == "myworkdaysite.com"

    def test_an_icims_subdomain_survives_verbatim(self):
        ep = verify.hydrate_endpoint(
            {"ats_vendor": "icims", "tenant": "us-careers-rivian",
             "canonical_endpoint": "https://us-careers-rivian.icims.com"}, {})
        assert ep.host == "us-careers-rivian.icims.com"
        assert ep.tenant == "us-careers-rivian"


class TestItNeverGuesses:

    def test_a_url_for_ANOTHER_vendor_is_refused(self):
        """A mismatched fingerprint is not a hydration. Falling back to the
        bare tenant is wrong; silently switching vendor is worse."""
        ep = verify.hydrate_endpoint(
            {"ats_vendor": "workday", "tenant": "acme",
             "canonical_endpoint": "https://boards.greenhouse.io/acme"}, {})
        assert ep.vendor == "workday"
        assert ep.host == ""

    def test_no_catalog_row_and_no_url_still_yields_the_bare_endpoint(self):
        ep = verify.hydrate_endpoint(
            {"ats_vendor": "greenhouse", "tenant": "anthropic"}, {})
        assert (ep.vendor, ep.tenant) == ("greenhouse", "anthropic")
        assert ep.host == ""

    def test_an_unparseable_stored_url_does_not_raise(self):
        ep = verify.hydrate_endpoint(
            {"ats_vendor": "workday", "tenant": "acme",
             "canonical_endpoint": "http://["}, {})
        assert ep.tenant == "acme"
