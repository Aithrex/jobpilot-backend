"""Phenom + Eightfold providers: ENTERPRISE_CATALOG becomes crawlable."""
from services.ats_providers import registry as ats_registry
from services.ats_providers.base import (DATE_EXACT, Endpoint, FetchContext)
from services.ats_providers.enterprise import (EightfoldProvider,
                                               PhenomProvider)


def _ctx(*, get_json=None, post_json=None, cutoff_iso=None):
    return FetchContext(
        get_json=get_json or (lambda *a, **k: (200, {})),
        get_text=lambda *a, **k: (200, ""),
        post_json=post_json,
        cutoff_iso=cutoff_iso,
    )


def test_phenom_and_eightfold_are_registered_termless():
    vendors = ats_registry.vendors()
    assert "phenom" in vendors
    assert "eightfold" in vendors
    assert "phenom" in ats_registry.termless_vendors()
    assert "eightfold" in ats_registry.termless_vendors()
    assert len(vendors) >= 14


def test_phenom_parses_widgets_refine_search():
    def post_json(url, *, json_body=None, timeout=None, vendor=""):
        assert url == "https://careers.usbank.com/widgets"
        assert json_body["keywords"] == ""
        assert json_body["ddoKey"] == "refineSearch"
        return 200, {"refineSearch": {"data": {"jobs": [{
            "title": "Staff Engineer",
            "jobSeqNo": "R123",
            "cityState": "Minneapolis, MN",
            "country": "United States",
            "postedDate": "2026-08-10T00:00:00Z",
            "applyUrl": "https://careers.usbank.com/us/en/job/R123",
            "descriptionTeaser": "<p>Build payments</p>",
        }]}}}

    ep = Endpoint(vendor="phenom", tenant="usbank",
                  host="careers.usbank.com")
    jobs = list(PhenomProvider().list_jobs(ep, _ctx(post_json=post_json)))
    assert len(jobs) == 1
    assert jobs[0].title == "Staff Engineer"
    assert jobs[0].source_job_id == "R123"
    assert jobs[0].date_confidence == DATE_EXACT
    assert "Build payments" in jobs[0].description


def test_eightfold_classic_parses_positions():
    def get_json(url, params=None, timeout=None, vendor=""):
        assert "/api/apply/v2/jobs" in url
        assert params["query"] == ""
        return 200, {"positions": [{
            "id": "nf-1", "name": "Senior Engineer",
            "locations": ["Los Gatos, CA"],
            "postedTs": 1754784000,
            "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/nf-1",
        }]}

    ep = Endpoint(vendor="eightfold", tenant="netflix",
                  host="explore.jobs.netflix.net",
                  extra={"domain": "netflix.com", "variant": "classic"})
    jobs = list(EightfoldProvider().list_jobs(ep, _ctx(get_json=get_json)))
    assert jobs[0].title == "Senior Engineer"
    assert jobs[0].source_job_id == "nf-1"
    assert jobs[0].posted_at


def test_eightfold_pcsx_uses_search_endpoint_and_skips_detail():
    def get_json(url, params=None, timeout=None, vendor=""):
        assert "/api/pcsx/search" in url
        return 200, {"data": {"positions": [{
            "id": "ms-1", "name": "Cloud Engineer",
            "location": "Redmond, WA",
            "postedTs": 1754784000000,
        }]}}

    ep = Endpoint(vendor="eightfold", tenant="microsoft",
                  host="apply.careers.microsoft.com",
                  extra={"domain": "microsoft.com", "variant": "pcsx"})
    jobs = list(EightfoldProvider().list_jobs(ep, _ctx(get_json=get_json)))
    assert jobs[0].title == "Cloud Engineer"
    detailed = EightfoldProvider().fetch_detail(
        ep, jobs[0], _ctx(get_json=lambda *a, **k: (200, {"job_description": "nope"})))
    assert detailed is None


def test_phenompeople_fingerprint_captures_tenant():
    hit = ats_registry.match_url("https://usbank.phenompeople.com/careers")
    assert hit is not None
    assert hit[0].vendor == "phenom"
    assert hit[1]["tenant"] == "usbank"


def test_enterprise_catalog_rows_reach_the_career_crawler():
    from services import job_ingest_worker as worker

    worker.reset_registry_cache()
    members = worker.registry_members(worker.SOURCE_CAREER)
    phenom = [m for m in members if m.get("vendor") == "phenom"]
    eightfold = [m for m in members if m.get("vendor") == "eightfold"]
    assert len(phenom) >= 8, f"only {len(phenom)} Phenom members reached the crawler"
    assert len(eightfold) >= 2, f"only {len(eightfold)} Eightfold members reached the crawler"
    usbank = next(m for m in phenom if m["slug"] == "usbank")
    assert usbank["host"] == "careers.usbank.com"
    ms = next(m for m in eightfold if m["slug"] == "microsoft")
    assert (ms.get("extra") or {}).get("variant") == "pcsx"


def test_feed_labels_phenom_and_eightfold():
    from services import inventory_feed as feed
    assert feed._ATS_LABEL["phenom"] == "Phenom"
    assert feed._ATS_LABEL["eightfold"] == "Eightfold"


def test_migrate_indexes_covers_the_employer_graph():
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import migrate_indexes as mi

    names = {(coll, opts["name"]) for coll, _keys, opts in mi.INDEX_SPECS}
    assert ("companies", "company_id_uniq") in names
    assert ("company_ats_endpoints", "member_key_uniq") in names
    assert ("discovery_queue", "claim_order") in names
