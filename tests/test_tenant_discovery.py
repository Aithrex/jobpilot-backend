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
    monkeypatch.setattr(td, "_INDEX_CACHE", "CC-MAIN-2026-30")


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
    assert found[0].confidence == 2
    assert sorted(found[0].sources) == ["certspotter", "commoncrawl"]


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


def test_every_enumerable_vendor_has_a_registered_provider():
    """A vendor we can enumerate but cannot fetch is a list going nowhere."""
    from services.ats_providers import registry as ats_registry

    for vendor, domain in td.SUBDOMAIN_VENDORS:
        assert ats_registry.get(vendor) is not None, vendor
        assert domain and "." in domain


def test_page_count_of_none_is_unknown_not_zero(monkeypatch):
    """The index times out on large domains; that is not "no pages"."""
    def _fail(*a, **k):
        raise td.DiscoveryError("504")
    monkeypatch.setattr(td, "_get", _fail)
    assert td.commoncrawl_page_count("*.icims.com") is None
