"""Direct-employer onboarding: one outcome per lead, exact identities, and the
rules that keep a researched company from being onboarded wrongly.

Offline: no network (every provider call goes through a fake context) and no
database beyond the suite's mongomock.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

from services import career_source_service as css
from services import job_ingest_worker as jw
from services.ats_providers import registry
from services.ats_providers.base import Endpoint

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    for p in (ROOT / "scripts", ROOT / "scripts" / "data"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
    spec = importlib.util.spec_from_file_location(
        "onboard_researched_companies",
        ROOT / "scripts" / "onboard_researched_companies.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ob = _load_script()
import direct_employer_leads as leads_mod   # noqa: E402
import direct_employer_plan as plan_mod     # noqa: E402


# ---------------------------------------------------------------------------
# One outcome per lead
# ---------------------------------------------------------------------------

def _ok_validation(n=5):
    return {"status": ob.V_COMPLETE, "requisitions": n, "parsed": n,
            "at": "2026-10-21T00:00:00Z"}


def test_every_one_of_the_174_leads_gets_exactly_one_outcome():
    names = [l["company"] for l in leads_mod.LEADS]
    assert len(names) == 174 == len(set(names))
    vals = {name: _ok_validation() for name in plan_mod.LEAD_PLAN}
    report = ob.build_report(validations=vals, cohorts=("leads",))
    rows = report["cohorts"]["leads"]["rows"]
    assert [r["company"] for r in rows] == names
    assert all(r["outcome"] in ob.OUTCOMES for r in rows)
    counts = report["cohorts"]["leads"]["counts"]
    assert sum(counts.values()) == 174
    # Research dispositions map straight through.
    assert counts["excluded"] == 61          # 33 staffing + 20 consultancy + 8 intermediary
    assert counts["review"] == 31            # 26 mixed + 5 unresolved identities


def test_every_eligible_lead_has_a_plan():
    eligible = {l["company"] for l in leads_mod.LEADS
                if l["disposition"] == "eligible_direct"}
    assert len(eligible) == 82
    assert eligible <= set(plan_mod.LEAD_PLAN)


def test_pending_companies_stay_pending_and_never_carry_an_endpoint():
    for name in ("American Airlines", "Atlys", "Carvana", "DataVisor",
                 "LendingTree", "Reflex", "Verisk", "Upwind Security"):
        entry = plan_mod.LEAD_PLAN[name]
        assert entry["plan"] == "pending" and not entry.get("endpoint"), name
        res = ob.outcome_for(entry, disposition="eligible_direct",
                             validation=None, members={})
        assert res["outcome"] == ob.PENDING


def test_providers_another_agent_builds_are_named_not_rowed():
    expected = {"Destro": "breezyhr", "GM Performance Power Units": "dayforce",
                "Inspira Financial": "adp_myjobs",
                "Lincoln Institute of Land Policy": "paycom",
                "Universal Background Screening": "appone",
                "RevSpring": "newton_paycor", "Spring Arbor University": "newton_paycor",
                "Epic": "avature", "Frequentis": "avature",
                "TikTok USDS Joint Venture": "tiktok_usds"}
    for name, need in expected.items():
        entry = plan_mod.LEAD_PLAN[name]
        assert not entry.get("endpoint"), name
        res = ob.outcome_for(entry, disposition="eligible_direct",
                             validation=None, members={})
        assert res["outcome"] == ob.UNSUPPORTED
        assert res["why"] == f"unsupported_pending_provider:{need}"


def test_every_added_row_is_served_by_the_crawl_tables_with_its_exact_identity():
    """A5: an "added" row must be in the member tables the Oracle runner
    reads — the same tables, the same vendor/tenant/site/pod/host."""
    members = ob.crawl_members()
    added = {n: e for n, e in plan_mod.LEAD_PLAN.items() if e["plan"] == "added"}
    assert len(added) >= 40
    for name, entry in added.items():
        m = ob.member_for(entry["endpoint"], members)
        assert m is not None, name
        assert m["source"] == jw.source_of_vendor(entry["endpoint"]["vendor"]), name


def test_added_row_without_a_recorded_validation_is_not_new_validated():
    members = ob.crawl_members()
    entry = plan_mod.LEAD_PLAN["Swift"]
    res = ob.outcome_for(entry, disposition="eligible_direct", validation=None,
                         members=members)
    assert res["outcome"] == ob.BLOCKED
    res = ob.outcome_for(entry, disposition="eligible_direct",
                         validation={"status": ob.V_BLOCKED, "requisitions": 0},
                         members=members)
    assert res["outcome"] == ob.BLOCKED
    res = ob.outcome_for(entry, disposition="eligible_direct",
                         validation=_ok_validation(), members=members)
    assert res["outcome"] == ob.NEW_VALIDATED


def test_amazon_business_units_are_amazon_not_new_employers():
    members = ob.crawl_members()
    for name in ("Amazon Web Services (AWS)", "Annapurna Labs"):
        entry = plan_mod.LEAD_PLAN[name]
        assert entry["plan"] == "existing" and entry["business_unit_of"] == "Amazon"
        res = ob.outcome_for(entry, disposition="eligible_direct", validation=None,
                             members=members)
        assert res["outcome"] == ob.EXISTING_COVERED
        assert res["member_key"].startswith("amazon")


def test_health_checked_rows_are_not_duplicated():
    keys = [m["member_key"] for src in ob.crawl_members().values() for m in src]
    assert keys.count("greenhouse-duolingo") == 1
    assert sum(1 for k in keys if k in ("nordstrom",)) == 1


# ---------------------------------------------------------------------------
# Workday identity: tenant, pod, site case — no wd1 guessing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant,pod,site", [
    ("openlane", "wd115", "OPENLANE_Careers"),
    ("vrtx", "wd501", "vertex_careers"),
    ("maxar", "wd1", "Vantor"),
    ("homedepot", "wd5", "CareerDepot"),
    ("swift", "wd3", "Join-Swift"),
])
def test_workday_rows_keep_pod_and_site_case(tenant, pod, site):
    members = {m["tenant"]: m for m in jw._load_workday_members()}
    m = members[tenant]
    assert m["wd_host"] == pod and m["site"] == site
    ep = jw._endpoint_from_member(m)
    url = registry.get("workday")._jobs_url(ep)
    assert url == (f"https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/"
                   f"{tenant}/{site}/jobs")


def test_workday_member_identity_requires_the_pod():
    members = ob.crawl_members()
    right = plan_mod.LEAD_PLAN["OPENLANE"]["endpoint"]
    assert ob.member_for(right, members) is not None
    wrong_pod = dict(right, extra={"wd_host": "wd1"})
    assert ob.member_for(wrong_pod, members) is None
    wrong_case_site = dict(right, site="OPENLANE_CAREERS")
    # identity compares case-insensitively, but the stored row keeps case.
    assert ob.member_for(wrong_case_site, members)["site"] == "OPENLANE_Careers"


def test_workday_board_url_does_not_guess_a_pod():
    wd = registry.get("workday")
    assert wd.board_url(Endpoint(vendor="workday", tenant="openlane",
                                 site="OPENLANE_Careers")) == ""
    assert wd.board_url(Endpoint(vendor="workday", tenant="openlane",
                                 site="OPENLANE_Careers",
                                 extra={"wd_host": "wd115"})) == \
        "https://openlane.wd115.myworkdayjobs.com/OPENLANE_Careers"


def test_openlane_sample_filter_is_not_part_of_the_source():
    ep = plan_mod.LEAD_PLAN["OPENLANE"]["endpoint"]
    assert "q" not in (ep.get("extra") or {})
    assert "?" not in ep["site"]


# ---------------------------------------------------------------------------
# Ownership: a same-name board without evidence is not owned
# ---------------------------------------------------------------------------

def test_same_name_board_without_ownership_evidence_is_not_owned():
    rec = css.build_source(
        employer_id="emp_test", ats_vendor="greenhouse", tenant="carvana",
        ownership_evidence=[css.evidence(css.SIGNAL_SLUG_SIMILARITY,
                                         value="carvana ~ Carvana"),
                            css.evidence(css.SIGNAL_ENDPOINT_RESPONDS,
                                         value="200")],
        content_validation_evidence=[css.evidence("content_fetch", value="12")],
        job_count_last=12)
    assert rec["ownership_verified"] is False
    assert rec["state"] not in css.COVERED_STATES


def test_registration_records_carry_the_official_chain_as_ownership():
    report = ob.build_report(validations={"Swift": _ok_validation(47)},
                             cohorts=("leads",))
    row = next(r for r in report["cohorts"]["leads"]["rows"]
               if r["company"] == "Swift")
    assert row["outcome"] == ob.NEW_VALIDATED
    recs = ob.registration_records(row, plan_mod.LEAD_PLAN["Swift"])
    src = recs["career_source"]
    assert src["ownership_verified"] is True
    assert src["state"] == css.STATE_VALIDATED
    assert src["tenant"] == "swift" and src["member_key"] == "workday-swift-join-swift"
    assert src["canonical_endpoint"] == \
        "https://swift.wd3.myworkdayjobs.com/Join-Swift"
    assert recs["candidate"]["owner_domain"] == "swift.com"


def test_apply_writes_ownership_through_existing_services(mock_db, monkeypatch):
    monkeypatch.setattr(ob, "_db", lambda: mock_db)
    report = ob.build_report(validations={"Swift": _ok_validation(47)},
                             cohorts=("leads",))
    written = ob.apply_rows(report)
    assert len(written) == 1 and "validated" in written[0]
    assert mock_db.company_discovery_candidates.count_documents(
        {"owner_domain": "swift.com"}) == 1
    assert mock_db.career_sources.count_documents(
        {"tenant": "swift", "state": "validated"}) == 1
    # The crawl registry half is deliberately NOT written (see docstring).
    assert mock_db.company_ats_endpoints.count_documents({}) == 0


# ---------------------------------------------------------------------------
# Requisitions vs. talent communities / General Application
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "General Application", "General Application (insert job you excel at)",
    "Join our Talent Community", "Talent Network - Engineering",
    "Don't see the role you're looking for?", "Future Opportunities",
])
def test_non_requisitions_are_not_counted_as_roles(title):
    assert ob.is_non_requisition(title)


@pytest.mark.parametrize("title", [
    "Software Engineer", "Talent Acquisition Partner", "Community Manager",
    "Applications Engineer", "General Manager",
])
def test_real_roles_are_requisitions(title):
    assert not ob.is_non_requisition(title)


class _FakeProvider:
    vendor = "fakeboard"
    has_detail = False

    def __init__(self, rows):
        self.rows = rows

    def list_jobs(self, endpoint, ctx):
        ctx.get_json("https://example.invalid/jobs")
        for r in self.rows:
            yield r


class _Ctx:
    def __init__(self, status=200, payload=None, text=""):
        self.status, self.payload, self.text = status, payload, text
        self.listing_truncated = False
        self.post_json = None

    def get_json(self, url, params=None, timeout=None, vendor="", **kw):
        return self.status, self.payload

    def get_text(self, url, params=None, timeout=None, vendor="", **kw):
        return self.status, self.text


def _raw(rid, title, dept=""):
    from services.ats_providers.base import RawJob
    return RawJob(source_job_id=rid, title=title, apply_url="", department=dept)


def _validate(monkeypatch, rows, ctx, **kw):
    prov = _FakeProvider(rows)
    monkeypatch.setattr(registry, "get", lambda v: prov if v == "fakeboard" else None)
    return ob.validate_endpoint({"vendor": "fakeboard", "tenant": "t"}, ctx=ctx, **kw)


def test_validation_counts_requisitions_duplicates_and_talent_rows(monkeypatch):
    rows = [_raw("1", "Software Engineer"), _raw("1", "Software Engineer"),
            _raw("2", "General Application"), _raw("3", "Data Scientist")]
    v = _validate(monkeypatch, rows, _Ctx(payload={"total": 4}))
    assert v["parsed"] == 4 and v["unique_ids"] == 3 and v["duplicate_rows"] == 1
    assert v["requisitions"] == 2 and v["filtered_non_requisition"] == 1
    assert v["status"] == ob.V_BUDGET   # 3 distinct ids < 4 the vendor reported
    assert not ob.validation_ok({"status": ob.V_COMPLETE, "requisitions": 0})


def test_board_of_only_general_applications_does_not_validate(monkeypatch):
    v = _validate(monkeypatch, [_raw("9", "General Application")],
                  _Ctx(payload={"total": 1}))
    assert v["requisitions"] == 0
    assert not ob.validation_ok(v)


# ---------------------------------------------------------------------------
# Shared boards must filter to the hiring entity
# ---------------------------------------------------------------------------

def test_shared_board_without_entity_filter_is_unsupported():
    entry = plan_mod.LEAD_PLAN["SoftWriters"]
    res = ob.outcome_for(entry, disposition="eligible_direct",
                         validation=_ok_validation(), members=ob.crawl_members())
    assert res["outcome"] == ob.UNSUPPORTED
    assert "SoftWriters" in res["why"]
    # And it is never in the crawl tables: Roper's board is not SoftWriters'.
    assert ob.member_for(entry["endpoint"], ob.crawl_members()) is None
    assert all(n != "SoftWriters" for n, _ in ob._validation_targets("all", ()))


def test_shared_board_with_entity_filter_counts_only_the_entity(monkeypatch):
    rows = [_raw("1", "Pharmacy Software Engineer", dept="SoftWriters"),
            _raw("2", "Accountant", dept="Deltek")]
    v = _validate(monkeypatch, rows, _Ctx(payload={"total": 2}),
                  entity_filter="SoftWriters")
    assert v["requisitions"] == 1 and v["filtered_other_entity"] == 1


# ---------------------------------------------------------------------------
# A static empty template is not zero
# ---------------------------------------------------------------------------

def test_static_no_jobs_found_template_is_unverified_not_zero():
    assert ob.classify_zero(parsed=0, source_reported=None, source_kind="html",
                            statuses=[200],
                            body_text="<div>No jobs found</div>") == ob.V_BLOCKED


def test_only_a_json_api_reporting_zero_is_empty():
    assert ob.classify_zero(parsed=0, source_reported=0, source_kind="json",
                            statuses=[200]) == ob.V_EMPTY
    assert ob.classify_zero(parsed=0, source_reported=None, source_kind="json",
                            statuses=[200]) == ob.V_BLOCKED
    for code in (403, 429, 503):
        assert ob.classify_zero(parsed=0, source_reported=None,
                                source_kind="json", statuses=[code]) == ob.V_BLOCKED
    assert ob.classify_zero(parsed=0, source_reported=None, source_kind="json",
                            statuses=[]) == ob.V_FAILED


def test_zero_rows_from_an_html_page_never_validates(monkeypatch):
    class _HtmlProv(_FakeProvider):
        def list_jobs(self, endpoint, ctx):
            ctx.get_text("https://example.invalid/careers")
            return iter(())
    prov = _HtmlProv([])
    monkeypatch.setattr(registry, "get", lambda v: prov)
    v = ob.validate_endpoint({"vendor": "fakeboard", "tenant": "t"},
                             ctx=_Ctx(text="<p>No jobs found</p>"))
    assert v["status"] == ob.V_BLOCKED and not ob.validation_ok(v)


def test_provider_exception_is_a_failed_run_not_a_crash(monkeypatch):
    class _Boom(_FakeProvider):
        def list_jobs(self, endpoint, ctx):
            raise KeyError("schema changed")
            yield  # pragma: no cover
    monkeypatch.setattr(registry, "get", lambda v: _Boom([]))
    v = ob.validate_endpoint({"vendor": "fakeboard", "tenant": "t"}, ctx=_Ctx())
    assert v["status"] == ob.V_FAILED and "KeyError" in v["detail"]


# ---------------------------------------------------------------------------
# Providers whose paging this change touched: truncation + duplicates
# ---------------------------------------------------------------------------

class _PostCtx:
    """post_json returns the next scripted page."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.listing_truncated = False
        self.cutoff_iso = None
        self.options = {}
        self.calls = 0

    def should_stop(self):
        return False

    def log(self, *a, **k):
        pass

    def post_json(self, url, params=None, json_body=None, timeout=None,
                  vendor="", **kw):
        self.calls += 1
        if not self.pages:
            return 200, {}
        return self.pages.pop(0)


def _phenom_page(ids, total):
    return 200, {"refineSearch": {"totalHits": total, "data": {"jobs": [
        {"jobSeqNo": i, "title": f"Role {i}", "postedDate": "2026-10-20"}
        for i in ids]}}}


def test_phenom_page_cap_is_reported_as_truncation():
    ph = registry.get("phenom")
    size, cap = ph.policy.page_size, ph.policy.max_pages
    pages = [_phenom_page([f"J{p}-{i}" for i in range(size)], 5000)
             for p in range(cap)]
    ctx = _PostCtx(pages)
    rows = list(ph.list_jobs(Endpoint(vendor="phenom", tenant="baesystems",
                                      host="jobs.baesystems.com"), ctx))
    assert len(rows) == size * cap
    assert ctx.listing_truncated is True


def test_phenom_reads_to_the_reported_total_and_dedupes():
    ph = registry.get("phenom")
    size = ph.policy.page_size
    p1 = [f"A{i}" for i in range(size)]
    p2 = [p1[0]] + [f"B{i}" for i in range(size - 1)]   # one repeat across pages
    ctx = _PostCtx([_phenom_page(p1, 2 * size), _phenom_page(p2, 2 * size)])
    rows = list(ph.list_jobs(Endpoint(vendor="phenom", tenant="acrisure",
                                      host="careers.acrisure.com"), ctx))
    ids = [r.source_job_id for r in rows]
    assert len(ids) == len(set(ids)) == 2 * size - 1
    assert ctx.listing_truncated is False and ctx.calls == 2


def test_phenom_schema_change_yields_fewer_rows_not_a_crash():
    ph = registry.get("phenom")
    ctx = _PostCtx([(200, {"refineSearch": {"hits": []}}), (200, {"x": 1})])
    assert list(ph.list_jobs(Endpoint(vendor="phenom", tenant="a",
                                      host="careers.acrisure.com"), ctx)) == []
    ctx = _PostCtx([(200, {"refineSearch": "not-a-dict"})])
    assert list(ph.list_jobs(Endpoint(vendor="phenom", tenant="a",
                                      host="careers.acrisure.com"), ctx)) == []


def _ukg_page(ids, total):
    return 200, {"totalCount": total, "opportunities": [
        {"Id": i, "Title": f"Role {i}", "PostedDate": "2026-10-20T00:00:00Z"}
        for i in ids]}


def test_ukg_page_cap_is_reported_as_truncation():
    ukg = registry.get("ukg")
    size, cap = ukg.policy.page_size, ukg.policy.max_pages
    pages = [_ukg_page([f"{p}-{i}" for i in range(size)], 2115) for p in range(cap)]
    ctx = _PostCtx(pages)
    ep = Endpoint(vendor="ukg", tenant="TRO1001TROO",
                  site="2b13054b-60bb-410d-9136-e52c1c7d9720",
                  host="recruiting2.ultipro.com")
    rows = list(ukg.list_jobs(ep, ctx))
    assert len(rows) == size * cap and ctx.listing_truncated is True


def test_ukg_failed_later_page_is_truncation():
    ukg = registry.get("ukg")
    size = ukg.policy.page_size
    ctx = _PostCtx([_ukg_page([str(i) for i in range(size)], 3 * size),
                    (500, None)])
    ep = Endpoint(vendor="ukg", tenant="SCH1012SHRZ",
                  site="813f24f9-0154-46d6-bbb4-966dd708b1ff")
    rows = list(ukg.list_jobs(ep, ctx))
    assert len(rows) == size and ctx.listing_truncated is True


def test_new_hosts_are_named_not_wildcards():
    for vendor, host in (("radancy", "jobs.echostar.com"),
                         ("radancy", "careers.ice.com"),
                         ("radancy", "jobs.firstcitizens.com"),
                         ("phenom", "careers.acrisure.com"),
                         ("successfactors", "jobs.psegliny.com")):
        assert host in registry.get(vendor).api_hosts
        assert jw.host_allowed(jw.source_of_vendor(vendor), f"https://{host}/api/jobs")
    assert not jw.host_allowed(jw.SOURCE_CAREER, "https://jobs.example-unknown.com/api/jobs")
