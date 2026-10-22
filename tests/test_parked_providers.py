"""Observed-but-unreadable career surfaces are PARKED with evidence, never
crawled and never reported empty."""
from services.ats_providers import registry
from services.ats_providers.base import STATUS_BLOCKED, Endpoint, FetchContext


def _ctx(sent):
    def fetch(url, **kwargs):
        sent.append(url)
        return 200, "{}"
    return FetchContext(get_json=fetch, get_text=fetch, post_json=fetch)


def test_dayforce_is_parked_with_its_evidence():
    state = registry.readiness("dayforce")
    assert state["state"] == registry.PARKED and not state["crawlable"]
    assert "403" in state["detail"]
    assert "dayforce" not in registry.termless_vendors()
    assert "dayforce" not in registry.vendors()


def test_a_parked_vendor_makes_no_request_and_is_never_a_verified_zero():
    provider = registry.get("dayforce")
    sent = []
    ctx = _ctx(sent)
    ep = Endpoint(vendor="dayforce", tenant="gmppu", site="CANDIDATEPORTAL")
    assert list(provider.list_jobs(ep, ctx)) == []
    assert ctx.listing_truncated and sent == []
    assert provider.validate(ep, ctx).status == STATUS_BLOCKED


def test_a_parked_vendor_is_not_matched_by_discovery():
    assert registry.match_url("https://jobs.dayforcehcm.com/en-US/gmppu/CANDIDATEPORTAL") is None
