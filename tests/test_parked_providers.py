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


MYJOBS = "https://myjobs.adp.com/inspiracareers"
WFN = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
       "?cid=5a22940c-3629-44d9-813a-0ccee8ac9aac&ccId=19000101_000001&type=JS&lang=en_US")


def test_adp_myjobs_is_parked_and_never_read_as_workforce_now():
    state = registry.readiness("adp_myjobs")
    assert state["state"] == registry.PARKED and "listing request" in state["detail"]
    # Discovery must not turn a MyJobs link into an ADP Workforce Now board.
    assert registry.match_url(MYJOBS) is None
    myjobs = registry.get("adp_myjobs")
    wfn = registry.get("adp_wfn")
    assert any(f.match(MYJOBS) for f in myjobs.fingerprints)
    assert not any(f.match(MYJOBS) for f in wfn.fingerprints)
    assert not any(f.match(WFN) for f in myjobs.fingerprints)
    provider, fields, _ = registry.match_url(WFN)
    ep = provider.endpoint_from_match(fields, WFN)
    assert (ep.vendor, ep.tenant) == ("adp_wfn", "5a22940c-3629-44d9-813a-0ccee8ac9aac")
