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


def test_the_row_keeps_its_posting_date():
    """f7f0175 named the country code `iso`, the name this function already
    used for the posting date, so every row's `date_posted` became "US"."""
    row = inventory_feed._row_from_posting(_doc({
        "raw": "Remote", "country": "", "country_code": "US", "key": "remote"}), tz_offset=0)
    assert row["date_posted"].startswith("2026-10-02")


class TestAStateCodeIsNotACountry:
    """"San Francisco, CA" was filed as Canada. Six US state codes are also
    country codes, and the parser promoted the second part of a two-part
    location to the country whenever it parsed as one: 5,653 active US
    postings (CA, IL, CO, DE, ID, AR) left every US-only feed, plus 658
    Indiana rows filed as India (live, 2026-10-03)."""

    def test_colliding_state_codes_resolve_to_the_us(self):
        from services import job_inventory_service as inv
        for raw in ("San Francisco, CA", "Chicago, IL", "Denver, CO",
                    "Wilmington, DE", "Boise, ID", "Fayetteville, AR"):
            loc = inv.normalize_location(raw)
            assert loc["country_code"] == "US", raw
            assert loc["country"] == "" and loc["region"] == raw.split(", ")[1], raw

    def test_the_indexed_key_is_unchanged(self):
        from services import job_inventory_service as inv
        assert inv.normalize_location("San Francisco, CA")["key"] == "san-francisco-ca"

    def test_indiana_is_left_to_the_evidence(self):
        from services import job_inventory_service as inv
        assert inv.normalize_location("Pune, IN")["country_code"] == "IN"
        assert inv.normalize_location("Bangalore, IN")["country_code"] == "IN"
        assert inv.normalize_location("Indianapolis, IN")["country_code"] != "IN"

    def test_real_countries_are_still_promoted(self):
        from services import job_inventory_service as inv
        assert inv.normalize_location("Berlin, Germany")["country_code"] == "DE"
        assert inv.normalize_location("London, UK")["country_code"] == "GB"
        assert inv.normalize_location("Paris, FR")["country_code"] == "FR"
        assert inv.normalize_location("Toronto, ON")["country_code"] == "CA"

    def test_a_california_row_reaches_the_us_only_feed(self):
        from services import job_inventory_service as inv
        loc = inv.normalize_location("San Francisco, CA")
        row = inventory_feed._row_from_posting(_doc(loc), tz_offset=0)
        assert us_geo.keep_us_row(row) is True

    def test_the_repair_touches_only_the_bug_signature(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "repair", Path(__file__).resolve().parents[1] / "scripts" / "repair_state_code_countries.py")
        repair = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(repair)
        broken = {"raw": "Folsom, CA", "city": "Folsom", "region": "", "country": "CA",
                  "country_code": "CA", "key": "folsom-ca"}
        canada = {"raw": "Toronto, ON, CA", "city": "Toronto", "region": "ON", "country": "CA",
                  "country_code": "CA", "key": "toronto-on-ca"}
        new, changes = repair.repaired([broken, canada])
        assert new[0]["country_code"] == "US" and new[0]["key"] == "folsom-ca"
        assert new[1] is canada
        assert changes == [("CA", "US", "Folsom, CA")]
