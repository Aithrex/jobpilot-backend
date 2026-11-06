"""Board census regressions, using injected responses and mongomock only."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

import pytest

from services.ats_providers.base import Endpoint, FetchContext
from services.ats_providers.oracle_orc import OracleOrcProvider
from services.ats_providers.icims import IcimsProvider
from services.ats_providers.taleo import TaleoProvider
from services.ats_providers.enterprise import PhenomProvider, EightfoldProvider
from services.ats_providers.boards import SmartRecruitersProvider, WorkableProvider


def _response(vendor, ids, total):
    if vendor == "oracle_orc":
        return {"items": [{"TotalJobsCount": total, "requisitionList": [
            {"Id": i, "Title": f"Engineer {i}", "PostedDate": "2026-11-04"}
            for i in ids]}]}
    if vendor == "taleo":
        return {"pagingData": {"totalCount": total}, "requisitionList": [
            {"jobId": i, "column": [f"Engineer {i}", "Nov 4, 2026"],
             "linkedColumn": 0} for i in ids]}
    if vendor == "icims":
        return (f"<div>Results 1-{len(ids)} of {total}</div><ul>" + "".join(
            f'<li class="iCIMS_JobCardItem"><a href="/jobs/{i}/engineer/job">'
            f'<h3>Engineer {i}</h3></a></li>' for i in ids) + "</ul>")
    if vendor == "phenom":
        return {"refineSearch": {"totalHits": total, "data": {"jobs": [
            {"jobSeqNo": i, "title": f"Engineer {i}", "postedDate": "2026-11-04"}
            for i in ids]}}}
    if vendor == "eightfold":
        return {"count": total, "positions": [
            {"id": i, "name": f"Engineer {i}"} for i in ids]}
    return {"totalFound": total, "content": [
        {"id": i, "name": f"Engineer {i}", "releasedDate": "2026-11-04"}
        for i in ids]}


def _board(provider_cls, pages, *, max_pages=1, deadline=float("inf")):
    provider = provider_cls()
    provider.policy = replace(provider.policy, max_pages=max_pages)
    endpoint = Endpoint(vendor=provider.vendor, tenant="acme",
                        host="careers-acme.icims.com" if provider.vendor == "icims"
                        else "acme.fa.us2.oraclecloud.com", extra={"portal": "123"})
    calls = []

    def fetch(url, **kwargs):
        if provider.vendor == "icims" and "pr" not in (kwargs.get("params") or {}):
            return 200, "<html>Career opportunities</html>"
        calls.append(kwargs)
        return pages[min(len(calls) - 1, len(pages) - 1)]

    ctx = FetchContext(get_json=fetch, post_json=fetch, get_text=fetch, deadline=deadline)
    return provider, endpoint, ctx, calls


PAGED = [OracleOrcProvider, IcimsProvider, TaleoProvider, PhenomProvider,
         SmartRecruitersProvider, EightfoldProvider]


@pytest.mark.parametrize("provider_cls, filename, expected", [
    (IcimsProvider, "icims_emory.html", 2),
    (TaleoProvider, "taleo_aarcorp.json", 3),
    (PhenomProvider, "phenom_baesystems.json", 3),
    (EightfoldProvider, "eightfold_netflix.json", 3),
])
def test_trimmed_live_page_below_its_reported_total_is_truncated(provider_cls, filename, expected):
    content = (Path(__file__).parent / "fixtures/ats_providers/under_reads" / filename).read_text(encoding="utf-8")
    payload = json.loads(content) if filename.endswith(".json") else content
    provider, endpoint, ctx, _ = _board(provider_cls, [(200, payload)])
    assert len(list(provider.list_jobs(endpoint, ctx))) == expected
    assert ctx.listing_truncated


def test_workable_live_trexquant_repeated_shortcode_merges_its_places():
    payload = json.loads((Path(__file__).parent / "fixtures/ats_providers/under_reads/workable_trexquant.json")
                         .read_text(encoding="utf-8"))
    ctx = FetchContext(get_json=lambda *a, **k: (200, payload), get_text=lambda *a, **k: (200, ""))
    jobs = list(WorkableProvider().list_jobs(Endpoint(vendor="workable", tenant="trexquant"), ctx))
    assert len(jobs) == 1 < len(payload["jobs"])
    assert len(jobs[0].extra["locations"]) == len(payload["jobs"])


@pytest.mark.parametrize("provider_cls", PAGED)
def test_capped_listing_is_not_a_census(provider_cls):
    provider = provider_cls()
    size = provider.policy.page_size
    page = _response(provider.vendor, [str(i + 100) for i in range(size)], size + 1)
    provider, endpoint, ctx, _ = _board(provider_cls, [(200, page)])
    assert len(list(provider.list_jobs(endpoint, ctx))) == size
    assert ctx.listing_truncated is True


@pytest.mark.parametrize("provider_cls", PAGED)
def test_deadline_before_first_page_is_not_an_empty_census(provider_cls):
    provider, endpoint, ctx, _ = _board(provider_cls, [(200, {})], deadline=0)
    assert list(provider.list_jobs(endpoint, ctx)) == []
    assert ctx.listing_truncated is True


@pytest.mark.parametrize("provider_cls", PAGED)
def test_short_page_below_the_total_is_not_a_census(provider_cls):
    provider = provider_cls()
    page = _response(provider.vendor, ["101"], 20)
    provider, endpoint, ctx, _ = _board(provider_cls, [(200, page)])
    assert len(list(provider.list_jobs(endpoint, ctx))) == 1
    assert ctx.listing_truncated is True


@pytest.mark.parametrize("provider_cls", PAGED)
def test_complete_short_page_can_be_a_census(provider_cls):
    provider = provider_cls()
    page = _response(provider.vendor, ["101"], 1)
    provider, endpoint, ctx, _ = _board(provider_cls, [(200, page)])
    assert len(list(provider.list_jobs(endpoint, ctx))) == 1
    assert ctx.listing_truncated is False


@pytest.mark.parametrize("provider_cls", PAGED)
def test_failed_later_page_is_not_a_census(provider_cls):
    provider = provider_cls()
    size = provider.policy.page_size
    page = _response(provider.vendor, [str(i + 100) for i in range(size)], size + 1)
    provider, endpoint, ctx, _ = _board(provider_cls, [(200, page), (500, None)], max_pages=2)
    assert len(list(provider.list_jobs(endpoint, ctx))) == size
    assert ctx.listing_truncated is True


@pytest.mark.parametrize("provider_cls", PAGED)
@pytest.mark.parametrize("overlap", [1, "all"])
def test_overlapping_pages_do_not_prove_the_reported_total(provider_cls, overlap):
    provider = provider_cls()
    size = provider.policy.page_size
    ids = [str(i + 100) for i in range(size)]
    second = ids if overlap == "all" else [ids[-1]] + [str(i + 1000) for i in range(size - 1)]
    pages = [(200, _response(provider.vendor, batch, size * 2)) for batch in (ids, second)]
    provider, endpoint, ctx, _ = _board(provider_cls, pages, max_pages=2)
    rows = list(provider.list_jobs(endpoint, ctx))
    assert len({row.source_job_id for row in rows}) < size * 2
    assert ctx.listing_truncated is True


@pytest.mark.parametrize("provider_cls", [OracleOrcProvider, TaleoProvider, PhenomProvider,
                                         SmartRecruitersProvider, EightfoldProvider])
@pytest.mark.parametrize("new_tail", [False, True])
def test_short_overlapping_page_without_a_total_is_not_a_census(provider_cls, new_tail):
    provider = provider_cls()
    ids = [str(i + 100) for i in range(provider.policy.page_size)]
    tail = [ids[-1]] + (["99999"] if new_tail else [])
    pages = [(200, _response(provider.vendor, batch, None)) for batch in (ids, tail)]
    provider, endpoint, ctx, _ = _board(provider_cls, pages, max_pages=2)
    list(provider.list_jobs(endpoint, ctx))
    assert ctx.listing_truncated


@pytest.mark.parametrize("provider_cls", PAGED)
def test_a_growing_reported_total_is_not_frozen_to_the_first_page(provider_cls):
    provider = provider_cls()
    size = provider.policy.page_size
    pages = [(200, _response(provider.vendor, [str(i + 100) for i in range(size)], size + 1)),
             (200, _response(provider.vendor, [str(i + 1000) for i in range(size)], 2 * size + 1))]
    provider, endpoint, ctx, _ = _board(provider_cls, pages, max_pages=2)
    assert len(list(provider.list_jobs(endpoint, ctx))) == size * 2
    assert ctx.listing_truncated


@pytest.mark.parametrize("provider_cls", PAGED)
def test_a_shrinking_total_does_not_hide_unreached_jobs_in_a_changing_board(provider_cls):
    provider = provider_cls()
    size = provider.policy.page_size
    pages = [(200, _response(provider.vendor, [str(i + 100) for i in range(size)], 2 * size + 1)),
             (200, _response(provider.vendor, [str(i + 1000) for i in range(size)], 2 * size))]
    provider, endpoint, ctx, _ = _board(provider_cls, pages, max_pages=2)
    assert len(list(provider.list_jobs(endpoint, ctx))) == size * 2
    assert ctx.listing_truncated


@pytest.mark.parametrize("provider_cls", [OracleOrcProvider, TaleoProvider, PhenomProvider,
                                         SmartRecruitersProvider, EightfoldProvider])
@pytest.mark.parametrize("later_page", [False, True])
def test_malformed_row_list_is_not_an_empty_census(provider_cls, later_page):
    provider = provider_cls()
    payload = _response(provider.vendor, [], None)
    if provider.vendor == "oracle_orc":
        payload["items"][0]["requisitionList"] = ["changed schema"]
    elif provider.vendor == "taleo":
        payload["requisitionList"] = ["changed schema"]
    elif provider.vendor == "phenom":
        payload["refineSearch"]["data"]["jobs"] = ["changed schema"]
    elif provider.vendor == "eightfold":
        payload["positions"] = ["changed schema"]
    else:
        payload["content"] = ["changed schema"]
    pages = [(200, payload)]
    size = provider.policy.page_size
    if later_page:
        pages.insert(0, (200, _response(provider.vendor, [str(i + 100) for i in range(size)], None)))
    provider, endpoint, ctx, _ = _board(provider_cls, pages, max_pages=2)
    assert len(list(provider.list_jobs(endpoint, ctx))) == (size if later_page else 0)
    assert ctx.listing_truncated


def test_taleo_expired_deadline_does_not_discover_a_portal():
    provider, endpoint, ctx, calls = _board(TaleoProvider, [(200, {})], deadline=0)
    endpoint.extra = {}
    assert list(provider.list_jobs(endpoint, ctx)) == []
    assert calls == []
    assert ctx.listing_truncated


def test_oracle_cutoff_cannot_hide_a_fresh_row_that_failed_to_parse():
    payload = _response("oracle_orc", ["101", "102"], 2)
    payload["items"][0]["requisitionList"][0].pop("Title")
    payload["items"][0]["requisitionList"][1]["PostedDate"] = "2026-10-01"
    provider, endpoint, ctx, _ = _board(OracleOrcProvider, [(200, payload)])
    ctx.cutoff_iso = "2026-11-01T00:00:00+00:00"
    assert list(provider.list_jobs(endpoint, ctx)) == []
    assert ctx.listing_truncated


@pytest.mark.parametrize("bad_row", [None, {"title": "missing identifier"}, {"shortcode": "missing-title"}])
def test_workable_malformed_rows_do_not_make_a_complete_census(bad_row):
    ctx = FetchContext(get_json=lambda *a, **k: (200, {"jobs": _workable_rows() + [bad_row]}),
                       get_text=lambda *a, **k: (200, ""))
    jobs = list(WorkableProvider().list_jobs(Endpoint(vendor="workable", tenant="acme"), ctx))
    assert len(jobs) == 1
    assert ctx.listing_truncated


@pytest.mark.parametrize("bad_row", [None, {"name": "missing identifier"}, {"uuid": "missing-title"}])
def test_rippling_malformed_rows_do_not_make_a_complete_census(bad_row):
    from services.ats_providers.payroll import RipplingProvider
    ctx = FetchContext(get_json=lambda *a, **k: (200, [{"uuid": "1", "name": "Engineer"}, bad_row]),
                       get_text=lambda *a, **k: (200, ""))
    jobs = list(RipplingProvider().list_jobs(Endpoint(vendor="rippling", tenant="acme"), ctx))
    assert len(jobs) == 1
    assert ctx.listing_truncated


@pytest.fixture
def inventory_db(mock_db):
    for name in ("job_postings", "job_source_state", "job_ingestion_runs"):
        mock_db[name].delete_many({})
    yield mock_db
    for name in ("job_postings", "job_source_state", "job_ingestion_runs"):
        mock_db[name].delete_many({})


@pytest.mark.parametrize("provider_cls", [OracleOrcProvider, IcimsProvider, TaleoProvider])
def test_partial_provider_listing_does_not_close_an_unreached_posting(
        provider_cls, monkeypatch, inventory_db):
    """Real provider -> worker census gate -> real mongomock closure path."""
    from services import job_ingest_worker as worker, job_inventory_service as inventory
    from services.ats_providers import registry

    provider = provider_cls()
    size = provider.policy.page_size
    page = _response(provider.vendor, [str(i + 100) for i in range(size)], size + 1)
    provider, endpoint, ctx, _ = _board(provider_cls, [(200, page)])
    source = worker.source_of_vendor(provider.vendor)
    now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    member = dict(endpoint.to_doc(), ats=provider.vendor, slug=endpoint.tenant, source=source)
    monkeypatch.setitem(registry._PROVIDERS, provider.vendor, provider)
    monkeypatch.setattr(worker, "_provider_fetch_context", lambda *a, **k: ctx)
    row = worker._listing_row(member, "not-reached", title="Engineer beyond page cap",
                              url="https://example.org/jobs/not-reached",
                              posted_at=now - timedelta(days=1), location="Austin, TX, US")
    inventory.upsert_posting(row, now=now - timedelta(days=1))
    assert worker._list_via_provider(member, cutoff=now - timedelta(days=7),
                                     deadline=time.monotonic() + 30)
    censused = {member["member_key"]} if not member["_listing_truncated"] else set()
    closed = worker._close_stale_in_shard(
        source, censused, cutoff_seen=now - timedelta(hours=1),
        fresh_cutoff=now - timedelta(days=7), run_id="test-census", now=now)
    assert closed == 0
    assert inventory.get_posting(source, row["source_job_id"])["status"] == "active"


def test_phenom_can_read_more_than_the_old_five_page_cap():
    provider = PhenomProvider()
    size = provider.policy.page_size
    pages = [(200, _response("phenom", [str(p * size + i) for i in range(size)], 6 * size))
             for p in range(6)]
    provider, endpoint, ctx, calls = _board(PhenomProvider, pages,
                                           max_pages=provider.policy.max_pages)
    assert len(list(provider.list_jobs(endpoint, ctx))) == 6 * size
    assert len(calls) == 6
    assert not ctx.listing_truncated


def test_smartrecruiters_reads_the_reported_450_rows():
    provider = SmartRecruitersProvider()
    pages = [(200, _response("smartrecruiters", [str(i) for i in range(offset, min(450, offset + 100))], 450))
             for offset in range(0, 450, 100)]
    provider, endpoint, ctx, calls = _board(SmartRecruitersProvider, pages,
                                           max_pages=provider.policy.max_pages)
    assert len(list(provider.list_jobs(endpoint, ctx))) == 450
    assert [call["params"]["offset"] for call in calls] == [0, 100, 200, 300, 400]
    assert not ctx.listing_truncated


def _workable_rows():
    base = {"shortcode": "ABC", "title": "Engineer", "url": "https://apply.workable.com/j/ABC",
            "published_on": "2026-11-04", "country": "United States",
            "description": "<p>Build reliable services.</p>"}
    return [dict(base, city="New York", state="NY"), dict(base, city="Boston", state="MA"),
            dict(base, city="New York", state="NY")]


def test_workable_widget_merges_locations_for_the_same_shortcode():
    ctx = FetchContext(get_json=lambda *a, **k: (200, {"jobs": _workable_rows()}),
                       get_text=lambda *a, **k: (200, ""))
    jobs = list(WorkableProvider().list_jobs(Endpoint(vendor="workable", tenant="acme"), ctx))
    assert len(jobs) == 1
    assert jobs[0].location.count("New York") == 1
    assert "Boston" in jobs[0].location
    assert jobs[0].source_job_id == "ABC"


def test_workable_id_fallback_accepted_by_grouping_is_emitted():
    payload = _workable_rows()[:1]
    payload[0]["id"] = payload[0].pop("shortcode")
    ctx = FetchContext(get_json=lambda *a, **k: (200, {"jobs": payload}),
                       get_text=lambda *a, **k: (200, ""))
    jobs = list(WorkableProvider().list_jobs(Endpoint(vendor="workable", tenant="acme"), ctx))
    assert [job.source_job_id for job in jobs] == ["ABC"]
    assert not ctx.listing_truncated


def test_workable_legacy_crawl_keeps_both_locations(monkeypatch):
    from services import job_ingest_worker as worker
    monkeypatch.setattr(worker, "http_get_json", lambda *a, **k: {"jobs": _workable_rows()})
    member = {"source": worker.SOURCE_CAREER, "ats": "workable", "slug": "acme",
              "member_key": "workable-acme", "display_name": "Acme"}
    rows = worker._list_career(member, cutoff=datetime(2026, 11, 1, tzinfo=timezone.utc),
                              policy=worker.HostPolicy(min_interval=0, jitter=0),
                              deadline=time.monotonic() + 30)
    assert len(rows) == 1
    assert "New York" in str(rows[0]["locations"])
    assert "Boston" in str(rows[0]["locations"])


def test_eightfold_classic_does_not_invent_dates_from_edit_timestamps():
    row = EightfoldProvider._to_raw("explore.jobs.netflix.net", "netflix.com", {
        "id": "123", "name": "Engineer", "t_update": 1793836800, "t_create": 1793836800})
    assert row.posted_at == ""
    assert row.date_confidence == "unknown"
    assert EightfoldProvider.date_confidence == "unknown"


@pytest.mark.parametrize("deadline", [0, float("inf")])
def test_eightfold_cap_and_deadline_are_not_a_census(deadline):
    provider = EightfoldProvider()
    provider.policy = replace(provider.policy, max_pages=1)
    payload = {"positions": [{"id": str(i), "name": "Engineer"}
                              for i in range(provider.policy.page_size)]}
    ctx = FetchContext(get_json=lambda *a, **k: (200, payload),
                       get_text=lambda *a, **k: (200, ""), deadline=deadline)
    list(provider.list_jobs(Endpoint(vendor="eightfold", tenant="netflix",
                                    host="explore.jobs.netflix.net"), ctx))
    assert ctx.listing_truncated


@pytest.mark.parametrize("vendor_body, expected", [
    ('<script type="application/ld+json">{"@type":"JobPosting",'
     '"description":"<p>Maintain the electricity grid.</p>","jobLocation":'
     '[{"address":{"addressLocality":"Newark","addressRegion":"NJ",'
     '"addressCountry":{"@type":"Country","name":"US"}}},'
     '{"address":{"addressLocality":"Bethpage","addressRegion":"NY",'
     '"addressCountry":"US"}}]}</script>', ("Newark", "Bethpage", "US")),
    ('<span itemprop="jobLocation"><span itemprop="addressLocality">Newark</span>'
     '<span itemprop="addressRegion">NJ</span><span itemprop="addressCountry">US</span>'
     '</span><span itemprop="description">Maintain the electricity grid.</span>',
     ("Newark", "NJ", "US")),
])
def test_successfactors_detail_supplies_missing_locations(vendor_body, expected):
    from services.ats_providers.base import RawJob
    from services.ats_providers.successfactors import SuccessFactorsProvider
    ctx = FetchContext(get_json=lambda *a, **k: (200, {}),
                       get_text=lambda *a, **k: (200, vendor_body))
    job = RawJob(source_job_id="123", title="Engineer", apply_url="https://jobs.pseg.com/job/123")
    detail = SuccessFactorsProvider().fetch_detail(
        Endpoint(vendor="successfactors", tenant="pseg", host="jobs.pseg.com"), job, ctx)
    assert detail is not None
    assert "electricity grid" in detail.description
    assert all(part in detail.location for part in expected)


def test_successfactors_detail_location_reaches_the_worker_row(monkeypatch):
    from services import job_ingest_worker as worker
    body = ('<span itemprop="description">Maintain the electricity grid.</span>'
            '<span itemprop="jobLocation"><span itemprop="addressLocality">Newark</span>'
            '<span itemprop="addressRegion">NJ</span><span itemprop="addressCountry">US</span></span>')
    ctx = FetchContext(get_json=lambda *a, **k: (200, {}),
                       get_text=lambda *a, **k: (200, body))
    monkeypatch.setattr(worker, "_provider_fetch_context", lambda *a, **k: ctx)
    member = {"source": worker.SOURCE_CAREER, "ats": "successfactors", "slug": "pseg",
              "host": "jobs.pseg.com", "member_key": "successfactors-pseg", "display_name": "PSEG"}
    row = worker._listing_row(member, "123", title="Engineer", location="",
                              posted_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
                              url="https://jobs.pseg.com/job/123", detail_ref="123")
    text = worker._detail_via_provider(row, member, deadline=time.monotonic() + 30)
    assert "electricity grid" in text
    assert "Newark" in str(row["locations"])


@pytest.mark.parametrize("tenant, host, city, requisition", [
    ("pseg", "jobs.pseg.com", "Newark", "83984"),
    ("psegliny", "jobs.psegliny.com", "Bethpage", "82651"),
])
def test_real_successfactors_detail_locations_reach_inventory(
        monkeypatch, tenant, host, city, requisition):
    from services import job_ingest_worker as worker, job_inventory_service as inventory
    fixture = Path(__file__).parent / "fixtures/ats_providers/under_reads"
    body = (fixture / f"successfactors_{tenant}_detail.html").read_text(encoding="utf-8")
    ctx = FetchContext(get_json=lambda *a, **k: (200, {}),
                       get_text=lambda *a, **k: (200, body))
    monkeypatch.setattr(worker, "_provider_fetch_context", lambda *a, **k: ctx)
    member = {"source": worker.SOURCE_CAREER, "ats": "successfactors", "slug": tenant,
              "host": host, "member_key": f"successfactors-{tenant}", "display_name": "PSEG"}
    row = worker._listing_row(member, requisition, title="Engineer", location="",
                              posted_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
                              url=f"https://{host}/job/{requisition}", detail_ref=requisition)
    description = worker._detail_via_provider(row, member, deadline=time.monotonic() + 30)
    assert requisition in description
    assert city in str(row["locations"])
    assert {place["country_code"] for place in inventory.normalize_locations(row["locations"])} == {"US"}


def test_detail_location_does_not_overwrite_a_known_multilocation_listing(monkeypatch):
    from services import job_ingest_worker as worker
    from services.ats_providers.base import RawJob
    from services.ats_providers import registry
    provider = registry.get("successfactors")
    monkeypatch.setattr(provider, "fetch_detail", lambda *a: RawJob(
        source_job_id="123", title="Engineer", apply_url="https://jobs.pseg.com/job/123",
        description="Grid engineering", location="Newark, NJ, US"))
    member = {"source": worker.SOURCE_CAREER, "ats": "successfactors", "slug": "pseg",
              "host": "jobs.pseg.com", "member_key": "successfactors-pseg", "display_name": "PSEG"}
    row = worker._listing_row(member, "123", title="Engineer", location="Newark, NJ, US; Bethpage, NY, US",
                              posted_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
                              url="https://jobs.pseg.com/job/123", detail_ref="123")
    original = row["locations"]
    worker._detail_via_provider(row, member, deadline=time.monotonic() + 30)
    assert row["locations"] == original


def test_rippling_duplicate_locations_survive_the_actual_worker_path(monkeypatch):
    """Trimmed MATS response captured 2026-11-05T22:46:09Z: one id, three countries."""
    from services import job_ingest_worker as worker, job_inventory_service as inventory
    payload = json.loads((Path(__file__).parent / "fixtures/ats_providers/rippling/mats_locations.json")
                         .read_text(encoding="utf-8"))
    ctx = FetchContext(get_json=lambda *a, **k: (200, payload),
                       get_text=lambda *a, **k: (200, ""))
    monkeypatch.setattr(worker, "_provider_fetch_context", lambda *a, **k: ctx)
    member = {"source": worker.SOURCE_CAREER, "ats": "rippling", "slug": "mats-research",
              "member_key": "rippling-mats-research", "display_name": "MATS Research"}
    rows = worker.list_member(member, cutoff=datetime(2026, 11, 1, tzinfo=timezone.utc),
                              policy=worker.HostPolicy(min_interval=0, jitter=0),
                              deadline=time.monotonic() + 30)
    assert len(rows) == 1
    places = inventory.normalize_locations(rows[0]["locations"])
    assert {place["country_code"] for place in places} == {"US", "CA", "GB"}
    assert not member["_listing_truncated"]


def test_workable_mixed_countries_preserve_each_location(monkeypatch):
    from services import job_inventory_service as inventory, job_ingest_worker as worker
    payload = _workable_rows()[:1] + [dict(_workable_rows()[0], city="London", state="", country="United Kingdom")]
    ctx = FetchContext(get_json=lambda *a, **k: (200, {"jobs": payload}),
                       get_text=lambda *a, **k: (200, ""))
    job = list(WorkableProvider().list_jobs(Endpoint(vendor="workable", tenant="acme"), ctx))[0]
    assert job.country == ""
    assert "London" in job.location and "New York" in job.location
    member = {"source": worker.SOURCE_CAREER, "ats": "workable", "slug": "acme",
              "member_key": "workable-acme", "display_name": "Acme"}
    row = worker._raw_job_to_listing(member, job, cutoff=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert {p["country_code"] for p in inventory.normalize_locations(row["locations"])} == {"US", "GB"}
