"""Canned-payload tests for WorkdayProvider and GreenhouseProvider.

These are a PORT of the listing contracts already proven in
`test_job_ingest_worker` (relative dates, first_published vs updated_at).
They pin the provider modules so switching a source onto this path later
cannot silently change what a posting's date or identity is.
"""
from datetime import datetime, timezone

from services.ats_providers.base import (DATE_EXACT, DATE_RELATIVE,
                                         DATE_UNKNOWN, Endpoint, FetchContext)
from services.ats_providers.boards import GreenhouseProvider
from services.ats_providers.workday import WorkdayProvider


def _ctx(*, get_json=None, post_json=None, cutoff_iso=None):
    return FetchContext(
        get_json=get_json or (lambda *a, **k: (200, {})),
        get_text=lambda *a, **k: (200, ""),
        post_json=post_json,
        cutoff_iso=cutoff_iso,
    )


def test_workday_maps_relative_posted_on_and_requisition_id():
    def post_json(url, *, json_body=None, timeout=None, vendor=""):
        assert "/wday/cxs/acme/External/jobs" in url
        assert json_body["searchText"] == ""
        assert json_body["limit"] == 20
        return 200, {"jobPostings": [{
            "title": "Software Engineer",
            "externalPath": "job/123",
            "bulletFields": ["R-99"],
            "postedOn": "Posted 2 Days Ago",
            "locationsText": "Austin, TX",
            "timeType": "Full time",
        }]}

    ep = Endpoint(vendor="workday", tenant="acme", site="External",
                  host="acme.wd1.myworkdayjobs.com", extra={"wd_host": "wd1"})
    jobs = list(WorkdayProvider().list_jobs(ep, _ctx(post_json=post_json)))
    assert len(jobs) == 1
    assert jobs[0].title == "Software Engineer"
    assert jobs[0].source_job_id == "R-99"
    assert jobs[0].date_confidence == DATE_RELATIVE
    assert jobs[0].posted_at
    assert jobs[0].apply_url.endswith("/External/job/123")
    assert "wday/cxs/acme/External/job/123" in jobs[0].detail_url


def test_workday_undated_row_is_unknown_not_today():
    def post_json(url, *, json_body=None, timeout=None, vendor=""):
        return 200, {"jobPostings": [{
            "title": "Mystery Role",
            "externalPath": "job/undated",
            "bulletFields": ["R-0"],
            "postedOn": "",
            "locationsText": "Remote",
        }]}

    ep = Endpoint(vendor="workday", tenant="acme", site="External",
                  extra={"wd_host": "wd1"})
    jobs = list(WorkdayProvider().list_jobs(ep, _ctx(post_json=post_json)))
    assert jobs[0].posted_at == ""
    assert jobs[0].date_confidence == DATE_UNKNOWN


def test_workday_termless_listing_stops_at_the_freshness_boundary():
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    pages = [{
        "jobPostings": [
            {"title": "Fresh", "externalPath": "job/new",
             "bulletFields": ["N"], "postedOn": "Posted Today",
             "locationsText": "NY"},
            {"title": "Stale", "externalPath": "job/old",
             "bulletFields": ["O"], "postedOn": "Posted 30+ Days Ago",
             "locationsText": "NY"},
            {"title": "ShouldNotYield", "externalPath": "job/later",
             "bulletFields": ["L"], "postedOn": "Posted Today",
             "locationsText": "NY"},
        ],
    }]
    called = {"n": 0}

    def post_json(url, *, json_body=None, timeout=None, vendor=""):
        called["n"] += 1
        return 200, pages[0]

    ep = Endpoint(vendor="workday", tenant="acme", site="External",
                  extra={"wd_host": "wd1"})
    jobs = list(WorkdayProvider().list_jobs(
        ep, _ctx(post_json=post_json, cutoff_iso=cutoff)))
    titles = [j.title for j in jobs]
    assert "ShouldNotYield" not in titles
    assert "Stale" not in titles
    assert "Fresh" in titles
    assert called["n"] == 1


def test_greenhouse_uses_first_published_never_updated_at():
    def get_json(url, params=None, timeout=None, vendor=""):
        assert "content" in (params or {})
        return 200, {"jobs": [{
            "id": 11, "title": "Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/11",
            "first_published": "2026-08-10T00:00:00Z",
            "updated_at": "2026-08-16T00:00:00Z",
            "location": {"name": "New York, NY"},
        }, {
            "id": 12, "title": "No Date Role",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/12",
            "updated_at": "2026-08-16T00:00:00Z",
            "location": {"name": "Boston, MA"},
        }]}

    ep = Endpoint(vendor="greenhouse", tenant="acme")
    jobs = list(GreenhouseProvider().list_jobs(ep, _ctx(get_json=get_json)))
    by_id = {j.source_job_id: j for j in jobs}
    assert by_id["11"].date_confidence == DATE_EXACT
    assert by_id["11"].posted_at.startswith("2026-08-10")
    assert by_id["12"].posted_at == ""
    assert by_id["12"].date_confidence == DATE_UNKNOWN


def test_workday_fingerprint_captures_pod_and_site():
    from services.ats_providers import registry as ats_registry

    url = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    hit = ats_registry.match_url(url)
    assert hit is not None
    provider, fields, _conf = hit
    assert provider.vendor == "workday"
    ep = provider.endpoint_from_match(fields, url)
    assert ep.tenant == "acme"
    assert ep.extra.get("wd_host") == "wd5"
    assert ep.site == "External"
