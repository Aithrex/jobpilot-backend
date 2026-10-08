"""ADP Workforce Now career centers. Shapes copied from live boards, 2026-10-07."""
import pytest

from services.ats_providers import registry
from services.ats_providers.adp import AdpWorkforceNowProvider
from services.ats_providers.base import Endpoint, FetchContext, RawJob

CID = "a6a06e10-3c6c-4409-bb46-d0e8fd89373c"


def _row(item, title, posted, *, job_id="575522"):
    return {"itemID": item, "requisitionTitle": title, "postDate": posted,
            "workLevelCode": {"shortName": "Full Time"},
            "customFieldGroup": {"stringFields": [
                {"stringValue": job_id, "nameCode": {"codeValue": "ExternalJobID"}}]},
            "requisitionLocations": [{"address": {"cityName": "Rowland Heights"},
                                      "nameCode": {"shortName": " Rowland Heights, CA, US"}}]}


def _ctx(pages, sent, cutoff=""):
    def get_json(url, **kwargs):
        sent.append((url, dict(kwargs["params"])))
        if url.endswith("/job-requisitions"):
            index = (kwargs["params"]["$skip"] - 1) // 20
            rows = pages[index] if index < len(pages) else []
            return 200, {"jobRequisitions": rows, "meta": {"totalNumber": sum(map(len, pages))}}
        return 200, {"requisitionDescription": "<p>Process <b>loans</b>.</p>"}
    return FetchContext(get_json=get_json, get_text=None, cutoff_iso=cutoff)


def _endpoint():
    return Endpoint(vendor="adp_wfn", tenant=CID, site="19000101_000001")


@pytest.mark.parametrize("url", [
    f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={CID}",
    f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid&#61;{CID}&amp;ccId&#61;19000101_000001&amp;lang&#61;en_US",
    f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={CID}&#038;ccId=19000101_000001&#038;lang=en_US",
])
def test_the_careers_link_is_recognised_however_the_page_escaped_it(url):
    provider, fields, _ = registry.match_url(url)
    endpoint = provider.endpoint_from_match(fields, url)
    assert (endpoint.vendor, endpoint.tenant, endpoint.site) == ("adp_wfn", CID, "19000101_000001")


def test_a_script_on_the_same_host_is_not_a_board():
    assert registry.match_url(
        "https://static.workforcenow.adp.com/mas/mdf-components/26.29.14/adpComponents.js") is None


def test_listing_pages_by_one_based_start_index():
    page1 = [_row(f"92009950415{i:02d}_1", "Loan Services Specialist", "2026-10-07T09:00:00.000-04:00")
             for i in range(20)]
    page2 = [_row("9200939266083_1", "Teller", "2026-10-06T09:00:00.000-04:00")]
    sent = []
    jobs = list(AdpWorkforceNowProvider().list_jobs(_endpoint(), _ctx([page1, page2], sent)))
    assert len(jobs) == 21
    assert [p["$skip"] for _, p in sent] == [1, 21]
    assert sent[0][1]["cid"] == CID and sent[0][1]["ccId"] == "19000101_000001"
    job = jobs[0]
    assert job.posted_at.startswith("2026-10-07") and job.location == "Rowland Heights, CA, US"
    assert f"cid={CID}" in job.apply_url and "jobId=575522" in job.apply_url


def test_the_walk_stops_at_a_page_with_nothing_fresh():
    fresh = [_row(f"1{i:04d}_1", "A", "2026-10-07T09:00:00.000-04:00") for i in range(20)]
    old = [_row(f"2{i:04d}_1", "B", "2026-08-01T09:00:00.000-04:00") for i in range(20)]
    sent = []
    jobs = list(AdpWorkforceNowProvider().list_jobs(
        _endpoint(), _ctx([fresh, old, fresh], sent, cutoff="2026-10-01T00:00:00+00:00")))
    assert len(jobs) == 20 and len(sent) == 2


def test_detail_reads_the_description():
    sent = []
    job = RawJob(source_job_id="9200995041533_1", title="T", apply_url="https://x")
    out = AdpWorkforceNowProvider().fetch_detail(_endpoint(), job, _ctx([], sent))
    assert out.description.startswith("Process") and "loans" in out.description
    assert "<b>" not in out.description
    assert sent[0][0].endswith("/job-requisitions/9200995041533_1")


def test_a_tenant_that_is_not_a_guid_makes_no_request():
    sent = []
    bad = Endpoint(vendor="adp_wfn", tenant="../../etc", site="x")
    assert list(AdpWorkforceNowProvider().list_jobs(bad, _ctx([], sent))) == []
    job = RawJob(source_job_id="1/../2", title="T", apply_url="https://x")
    assert AdpWorkforceNowProvider().fetch_detail(_endpoint(), job, _ctx([], sent)) is None
    assert sent == []
