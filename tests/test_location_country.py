"""Unit gates for `services.location_country`.

Pinned to the live failures that opened the work: Santander CDMX, Michelin
Bangkok, Baker Hughes German street address, and EU gender markers on titles.
Never defaults to US. Multi-location rollups stay unknown.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))

from services import location_country as lc  # noqa: E402
from services import job_inventory_service as inv  # noqa: E402


def _loc(raw, **extra):
    base = {"raw": raw, "city": raw, "region": "", "country": "",
            "country_code": ""}
    base.update(extra)
    return base


class TestHandoffFailures:
    def test_cdmx_is_mexico(self):
        assert lc.resolve(_loc("CDMX")) == ("MX", "known_foreign_city")

    def test_bangkok_msg_headquarters_is_thailand(self):
        assert lc.resolve(_loc("Bangkok MSG Headquarters")) == (
            "TH", "known_foreign_city")

    def test_german_street_address_is_germany_not_delaware(self):
        assert lc.resolve(_loc("DE-CELLE-BAKER-HUGHES-STRASSE 1")) == (
            "DE", "foreign_address_marker")

    def test_title_mfd_marker_when_location_empty(self):
        code, basis = lc.resolve(
            _loc(""), title="Intern – Systems & Data Analysis (M/F/D)")
        assert (code, basis) == ("DE", "title_gender_marker")

    def test_title_fhx_marker_is_france(self):
        code, basis = lc.resolve(
            _loc(""), title="Stage Data Analyst (F/H/X) - Paris")
        assert (code, basis) == ("FR", "title_gender_marker")

    def test_remote_uk_is_britain(self):
        assert lc.resolve(_loc("Remote - UK"))[0] == "GB"

    def test_india_remote_is_india(self):
        assert lc.resolve(_loc("India Remote"))[0] == "IN"

    def test_manila_and_toulouse(self):
        assert lc.resolve(_loc("Manila"))[0] == "PH"
        assert lc.resolve(_loc("Toulouse"))[0] == "FR"

    def test_sydney_cbd_is_australia(self):
        assert lc.resolve(_loc("Sydney CBD Area"))[0] == "AU"

    def test_title_marker_does_not_override_resolved_location(self):
        code, basis = lc.resolve(
            _loc("Austin, TX", city="Austin", region="TX"),
            title="Engineer (M/F/D)")
        assert code == "US"
        assert basis.startswith("us_")


class TestNeverDefaultsToUS:
    def test_empty_stays_unknown(self):
        assert lc.resolve(_loc(""))[0] == ""

    def test_multi_location_stays_unknown(self):
        code, basis = lc.resolve(_loc("10 Locations"))
        assert code == ""
        assert basis == "multi_location"

    def test_work_location_unknown_is_not_foreign(self):
        verdict = lc.work_location_verdict(
            {"locations": [_loc("")], "title_raw": "Analyst"},
            home_country="US")
        assert verdict["verdict"] == lc.LOC_UNKNOWN
        assert verdict["penalty"] == 1.0
        assert verdict["badges"] == []

    def test_undeclared_home_does_not_label_us_foreign(self):
        verdict = lc.work_location_verdict(
            {"locations": [_loc("Austin, TX", city="Austin", region="TX")]},
            home_country="")
        assert verdict["verdict"] == lc.LOC_UNKNOWN
        assert verdict["penalty"] == 1.0
        assert "US" in verdict["country_codes"]
        assert verdict["badges"] == []


class TestWorkLocationDimension:
    def test_foreign_demotes_for_us_home(self):
        verdict = lc.work_location_verdict(
            {"locations": [_loc("CDMX")], "title_raw": "Analyst"},
            home_country="US")
        assert verdict["verdict"] == lc.LOC_FOREIGN
        assert verdict["penalty"] == lc.PENALTY_FOREIGN
        assert lc.BADGE_FOREIGN in verdict["badges"]
        assert verdict["primary_country"] == "MX"

    def test_home_match_is_not_demoted(self):
        verdict = lc.work_location_verdict(
            {"locations": [_loc("Austin, TX", city="Austin", region="TX")]},
            home_country="US")
        assert verdict["verdict"] == lc.LOC_HOME
        assert verdict["penalty"] == 1.0


class TestNormalizeLocationDelegates:
    def test_ingest_path_stamps_cdmx(self):
        got = inv.normalize_location("CDMX")
        assert got["country_code"] == "MX"

    def test_ingest_path_stamps_bangkok(self):
        got = inv.normalize_location("Bangkok MSG Headquarters")
        assert got["country_code"] == "TH"

    def test_ingest_path_stamps_strasse(self):
        got = inv.normalize_location("DE-CELLE-BAKER-HUGHES-STRASSE 1")
        assert got["country_code"] == "DE"
