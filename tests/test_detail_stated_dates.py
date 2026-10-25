"""An exact date a detail page states reaches the stored posting.

2026-10-24: `_detail_via_provider` kept the description and dropped the
date, so own-site pages that state "Date Posted: December 12, 2025" (AGAP
Technologies) and Workday's `startDate` behind "Posted 30+ Days Ago" changed
nothing in production.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from services import job_ingest_worker as w
from services import job_inventory_service as inv
from services.ats_providers import workday
from services.ats_providers.base import DATE_EXACT, DATE_RELATIVE, Endpoint, RawJob


def _detail(posted_at, confidence=DATE_EXACT, text="Date Posted: December 12, 2025"):
    return SimpleNamespace(posted_at=posted_at, date_confidence=confidence, raw_posted_text=text)


def test_an_undated_listing_takes_the_date_its_page_states():
    row = {"posted_at": None, "posted_at_confidence": inv.CONFIDENCE_UNKNOWN, "raw_posted_text": ""}
    w._adopt_detail_date(row, _detail("2025-12-12T00:00:00+00:00"))
    assert row["posted_at"] == datetime(2025, 12, 12, tzinfo=timezone.utc)
    assert row["posted_at_confidence"] == inv.CONFIDENCE_EXACT
    assert "Date Posted" in row["raw_posted_text"]


def test_a_relative_listing_date_yields_to_an_exact_one():
    row = {"posted_at": datetime(2026, 9, 24, tzinfo=timezone.utc),
           "posted_at_confidence": inv.CONFIDENCE_EXACT,
           "raw_posted_text": "Posted 30+ Days Ago"}
    w._adopt_detail_date(row, _detail("2025-12-23T00:00:00+00:00", text="startDate 2025-12-23"))
    assert row["posted_at"] == datetime(2025, 12, 23, tzinfo=timezone.utc)


def test_an_exact_listing_date_is_never_overwritten():
    exact = datetime(2026, 10, 20, tzinfo=timezone.utc)
    row = {"posted_at": exact, "posted_at_confidence": inv.CONFIDENCE_EXACT,
           "raw_posted_text": "2026-10-20"}
    w._adopt_detail_date(row, _detail("2025-01-01T00:00:00+00:00"))
    assert row["posted_at"] == exact


def test_a_relative_detail_date_is_ignored():
    row = {"posted_at": None, "raw_posted_text": ""}
    w._adopt_detail_date(row, _detail("2026-10-01T00:00:00+00:00", confidence=DATE_RELATIVE))
    assert row["posted_at"] is None


def test_workday_detail_reads_start_date():
    payload = {"jobPostingInfo": {"jobDescription": "<p>Build things.</p>" * 20,
                                  "startDate": "2025-12-23", "postedOn": "Posted 30+ Days Ago",
                                  "location": "New York, NY", "timeType": "Full time"}}
    ctx = SimpleNamespace(get_json=lambda url, **k: (200, payload))
    provider = workday.WorkdayProvider() if hasattr(workday, "WorkdayProvider") else None
    if provider is None:
        from services.ats_providers import registry
        provider = registry.get("workday")
    job = RawJob(source_job_id="R1", title="Engineer", apply_url="https://x",
                 detail_url="https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/ext/job/R1")
    out = provider.fetch_detail(Endpoint(vendor="workday", tenant="acme"), job, ctx)
    assert out.posted_at.startswith("2025-12-23") and out.date_confidence == DATE_EXACT
