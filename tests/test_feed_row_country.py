"""A feed row's country is the RESOLVED code, not the provider's raw spelling.

Live 2026-10-03: "Cloud Engineer I - IND-Work from Home-Remote" (Rackspace)
reached a US-only feed. The posting was stored correctly as `country_code: IN`,
but `_row_from_posting` read only the raw `country` field - empty for most
providers - and an empty country on a remote row reads as "could be US".
"""
from datetime import datetime, timezone

from services import inventory_feed, us_geo


def _doc(loc):
    return {"job_id": "j1", "title_raw": "Cloud Engineer I", "employer_name": "Rackspace",
            "locations": [loc], "work_mode": "remote", "status": "active",
            "posted_at": datetime(2026, 10, 2, tzinfo=timezone.utc),
            "posted_at_confidence": "exact", "source": "workday"}


def test_a_resolved_non_us_country_is_excluded_from_us_only():
    row = inventory_feed._row_from_posting(_doc({
        "raw": "IND-Work from Home-Remote", "country": "", "country_code": "IN",
        "key": "ind-work-from-home-remote"}), tz_offset=0)
    assert row["country_code"] == "IN"
    assert us_geo.keep_us_row(row) is False


def test_a_resolved_us_country_is_kept():
    row = inventory_feed._row_from_posting(_doc({
        "raw": "Remote", "country": "", "country_code": "US", "key": "remote"}), tz_offset=0)
    assert row["country_code"] == "US"
    assert us_geo.keep_us_row(row) is True


def test_the_raw_spelling_still_works_without_a_code():
    row = inventory_feed._row_from_posting(_doc({
        "raw": "Austin, TX", "country": "United States", "key": "austin-tx"}), tz_offset=0)
    assert row["country_code"] == "US"
