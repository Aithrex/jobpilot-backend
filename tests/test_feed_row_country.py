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
        assert inv.normalize_location("Indianapolis, IN")["country_code"] == "US"

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

    def test_the_repair_places_rows_the_first_pass_left_unresolved(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "repair", Path(__file__).resolve().parents[1] / "scripts" / "repair_state_code_countries.py")
        repair = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(repair)
        town = {"raw": "Whitestown, IN", "city": "Whitestown", "region": "IN", "country": "",
                "country_code": "", "key": "whitestown-in"}
        peru = {"raw": "Peru, IN", "city": "Peru", "region": "IN", "country": "",
                "country_code": "PE", "key": "peru-in"}
        new, changes = repair.repaired([town, peru])
        assert [l["country_code"] for l in new] == ["US", "US"]
        assert len(changes) == 2


class TestIndianaAndIndia:
    """"City, IN" is ambiguous, so the resolver lets every Indian signal speak
    first and only then reads Indiana. Live, 656 such rows covered 127 places
    and all but Nashik were in Indiana (2026-10-03)."""

    def test_indiana_by_elimination(self):
        from services import job_inventory_service as inv
        for raw in ("Whitestown, IN", "Bloomington, IN", "Jeffersonville, IN", "Indianapolis, IN"):
            assert inv.normalize_location(raw)["country_code"] == "US", raw

    def test_india_still_wins_on_evidence(self):
        from services import job_inventory_service as inv
        for raw in ("Pune, IN", "Nashik, IN", "Bangalore, IN", "Hyderabad IN", "India, IN"):
            assert inv.normalize_location(raw)["country_code"] == "IN", raw

    def test_us_towns_named_for_countries(self):
        from services import job_inventory_service as inv
        for raw in ("Peru, IN", "Brazil, IN", "Mexico, MO", "Lebanon, PA"):
            assert inv.normalize_location(raw)["country_code"] == "US", raw


class TestSeveralPlacesInOneString:
    """Boards list several offices in one string. Split on commas alone,
    "San Francisco, CA; Mountain View, CA" put the trailing "CA" in the country
    slot and filed the posting in Canada: 15 of the 128 in-inventory roles
    missing from real users' boards on 2026-10-06 (Waymo, DoorDash, Robinhood)."""

    def test_a_list_of_us_offices_is_in_the_us(self):
        from services import job_inventory_service as inv
        for raw in ("San Francisco, CA; Mountain View, CA",
                    "San Francisco, CA; Sunnyvale, CA; Seattle, WA",
                    "Menlo Park, CA | New York, NY",
                    "San Francisco, CA • New York, NY"):
            loc = inv.normalize_location(raw)
            assert loc["country_code"] == "US", raw
            assert loc["raw"] == raw and loc["region"] == "CA", raw
            row = inventory_feed._row_from_posting(_doc(loc), tz_offset=0)
            assert us_geo.keep_us_row(row) is True, raw

    def test_a_us_office_anywhere_in_the_list_counts(self):
        from services import job_inventory_service as inv
        assert inv.normalize_location("Toronto, ON; San Francisco, CA")["country_code"] == "US"

    def test_a_list_with_no_us_office_is_not_made_american(self):
        from services import job_inventory_service as inv
        assert inv.normalize_location("Vancouver, BC; Toronto, ON")["country_code"] == "CA"
        assert inv.normalize_location("London, UK; Dublin, Ireland")["country_code"] == "GB"

    def test_three_parts_ending_in_a_state(self):
        from services import job_inventory_service as inv
        assert inv.normalize_location("Building 5, Santa Clara, CA")["country_code"] == "US"
        assert inv.normalize_location("Toronto, ON, CA")["country_code"] == "CA"
        assert inv.normalize_location("Austin, TX, USA")["country_code"] == "US"

    def test_a_province_or_state_spelled_out_before_the_code(self):
        """The dry run of the repair would have moved "Toronto, Ontario, CA"
        and "Mississauga, Ontario, CA" to the US."""
        from services import job_inventory_service as inv
        for raw in ("Toronto, Ontario, CA", "Mississauga, Ontario, CA", "Vancouver, British Columbia, CA"):
            assert inv.normalize_location(raw)["country_code"] == "CA", raw
        assert inv.normalize_location("Bloomington, Veterans Parkway, Bloomington, IL")["country_code"] == "US"
        assert inv.normalize_location("US, Adelanto, CA")["country_code"] == "US"
        assert inv.normalize_location("Huntsville, AL; Littleton, CO")["country_code"] == "US"


def test_the_repair_sends_a_province_named_row_back_to_canada():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "repair2", Path(__file__).resolve().parents[1] / "scripts" / "repair_state_code_countries.py")
    repair = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair)
    wrong = {"raw": "Toronto, Ontario, CA", "city": "Toronto", "region": "CA", "country": "",
             "country_code": "US", "key": "toronto-ca"}
    fine = {"raw": "Kearny Mesa, San Diego, CA", "city": "Kearny Mesa", "region": "CA", "country": "",
            "country_code": "US", "key": "kearny-mesa-ca"}
    new, changes = repair.repaired([wrong, fine])
    assert new[0]["country_code"] == "CA" and new[1] is fine
    assert changes == [("US", "CA", "Toronto, Ontario, CA")]
