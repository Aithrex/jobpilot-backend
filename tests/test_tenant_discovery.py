"""Vendor-first tenant discovery.

Offline throughout: the archives are stubbed, because the point under test is
the filtering and merging, not whether Common Crawl was up today.
"""
import json

import pytest

from services import tenant_discovery as td


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing in this module may touch the network during tests."""
    def _boom(*args, **kwargs):
        raise AssertionError("unexpected network call")
    monkeypatch.setattr(td, "_get", _boom)
    monkeypatch.setattr(td, "_INDEXES_CACHE", ["CC-MAIN-2026-30",
                                               "CC-MAIN-2026-25"])


def _stub(monkeypatch, *, cc=(), certs=()):
    monkeypatch.setattr(td, "commoncrawl_hosts", lambda *a, **k: set(cc))
    monkeypatch.setattr(td, "certspotter_hosts", lambda *a, **k: set(certs))


def test_infrastructure_subdomains_are_not_employers(monkeypatch):
    """`analytics-euw1-1.bamboohr.com` is BambooHR's, not a customer's.

    Certificate Transparency returns a vendor's own hosts alongside its
    tenants', and an unfiltered label manufactures a bogus endpoint on every
    vendor.
    """
    _stub(monkeypatch, certs=[
        "www.bamboohr.com", "api.bamboohr.com", "marketplace.bamboohr.com",
        "analytics.bamboohr.com", "smetrics.bamboohr.com",
        "24slg.bamboohr.com",
    ])
    found = td.discover_vendor_tenants("bamboohr", "bamboohr.com",
                                       use_commoncrawl=False)
    assert [c.label for c in found] == ["24slg"]


def test_deeper_names_are_not_tenant_hostnames(monkeypatch):
    """`a.b.icims.com` is not `<tenant>.icims.com`."""
    _stub(monkeypatch, cc=["careers-acme.icims.com", "a.b.icims.com"])
    found = td.discover_vendor_tenants("icims", "icims.com",
                                       use_certspotter=False)
    assert [c.label for c in found] == ["careers-acme"]


def test_a_host_seen_by_both_archives_is_ranked_first(monkeypatch):
    """Validation budget is finite, so the likeliest candidates go first."""
    _stub(monkeypatch,
          cc=["one.bamboohr.com", "two.bamboohr.com"],
          certs=["two.bamboohr.com", "three.bamboohr.com"])
    found = td.discover_vendor_tenants("bamboohr", "bamboohr.com")
    assert found[0].label == "two"
    assert sorted(found[0].sources) == ["certspotter", "commoncrawl"]
    # Weighted, not counted: two sources of DIFFERENT quality still beat one,
    # but a crawled host beats a certificate-only one on its own.
    others = {c.label: c.confidence for c in found}
    assert others["two"] > others["one"] > others["three"]


def test_one_dead_archive_does_not_lose_the_vendor(monkeypatch):
    """Sources are independent; a 504 from one must not zero the result."""
    def _fail(*a, **k):
        raise td.DiscoveryError("504")
    monkeypatch.setattr(td, "commoncrawl_hosts", _fail)
    monkeypatch.setattr(td, "certspotter_hosts",
                        lambda *a, **k: {"24slg.bamboohr.com"})
    found = td.discover_vendor_tenants("bamboohr", "bamboohr.com")
    assert [c.label for c in found] == ["24slg"]


def test_excluded_labels_are_honoured(monkeypatch):
    _stub(monkeypatch, cc=["keepme.bamboohr.com", "dropme.bamboohr.com"])
    found = td.discover_vendor_tenants("bamboohr", "bamboohr.com",
                                       use_certspotter=False,
                                       exclude=["dropme"])
    assert [c.label for c in found] == ["keepme"]


def test_nothing_is_synthesised_from_a_company_name(monkeypatch):
    """Every hostname returned must have been OBSERVED by a third party.

    This is the line between discovery and guessing. `*.icims.com` is wildcard
    DNS, so a manufactured hostname would resolve, fetch, and look real.
    """
    _stub(monkeypatch, cc=["careers-acme.icims.com"])
    found = td.discover_vendor_tenants("icims", "icims.com",
                                       use_certspotter=False)
    assert len(found) == 1
    assert found[0].sources == {"commoncrawl"}


def test_page_count_of_none_is_unknown_not_zero(monkeypatch):
    """The index times out on large domains; that is not "no pages"."""
    def _fail(*a, **k):
        raise td.DiscoveryError("504")
    monkeypatch.setattr(td, "_get", _fail)
    assert td.commoncrawl_page_count("*.icims.com") is None


# ---------------------------------------------------------------------------
# Ranking and path-scoped enumeration
# ---------------------------------------------------------------------------

def test_board_path_hosts_outrank_certificate_only_hosts(monkeypatch):
    """A host seen under the vendor's BOARD path is a board by construction.

    Ranking by source COUNT alone made every single-source host equal, so the
    alphabetical tie-break sent an iCIMS validation budget through `academy`,
    `agents`, `api-dev`, `autodiscover`, `billing`… and burned eight minutes
    without reaching one employer.
    """
    calls = []

    def _cc(pattern, **kwargs):
        calls.append(pattern)
        if pattern.endswith("/jobs/*"):
            return {"careers-zzz.icims.com"}
        return {"aaa-infra.icims.com"}

    monkeypatch.setattr(td, "commoncrawl_hosts", _cc)
    monkeypatch.setattr(td, "certspotter_hosts",
                        lambda *a, **k: {"aaa-infra.icims.com"})
    found = td.discover_vendor_tenants("icims", "icims.com",
                                       path_pattern="/jobs/*")
    assert found[0].host == "careers-zzz.icims.com", \
        "a board-path host must be validated before an infrastructure host"


def test_the_scoped_pattern_is_tried_first_and_alone(monkeypatch):
    """The broad pattern is what times out; don't ask for it when scoped works."""
    calls = []

    def _cc(pattern, **kwargs):
        calls.append(pattern)
        return {"careers-x.icims.com"}

    monkeypatch.setattr(td, "commoncrawl_hosts", _cc)
    td.discover_vendor_tenants("icims", "icims.com", path_pattern="/jobs/*",
                               use_certspotter=False)
    assert calls == ["*.icims.com/jobs/*"]


def test_snapshots_accumulate_across_monthly_crawls(monkeypatch):
    """Each monthly crawl samples independently, so more snapshots = more hosts."""
    seen = {"CC-MAIN-2026-30": {"a.icims.com"},
            "CC-MAIN-2026-25": {"b.icims.com"}}
    monkeypatch.setattr(td, "commoncrawl_hosts",
                        lambda p, index=None, **k: seen.get(index, set()))
    found = td.discover_vendor_tenants("icims", "icims.com",
                                       path_pattern="/jobs/*",
                                       cc_snapshots=2, use_certspotter=False)
    assert {c.label for c in found} == {"a", "b"}


def test_broad_pattern_is_used_when_no_path_is_configured(monkeypatch):
    calls = []
    monkeypatch.setattr(td, "commoncrawl_hosts",
                        lambda p, **k: (calls.append(p) or {"x.recruitee.com"}))
    td.discover_vendor_tenants("recruitee", "recruitee.com",
                               use_certspotter=False)
    assert calls == ["*.recruitee.com"]


def test_subdomain_vendor_table_is_well_formed():
    from services.ats_providers import registry as ats_registry

    for row in td.SUBDOMAIN_VENDORS:
        assert len(row) == 3, row
        vendor, domain, path = row
        assert ats_registry.get(vendor) is not None, vendor
        assert "." in domain
        assert path == "" or path.startswith("/"), row
