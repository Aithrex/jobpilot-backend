"""The sponsorship badge may only say what the ROW can support.

Two live defects are pinned here.

**The provenance was a constant, not a measurement.** The module advertised
five fiscal years of "DOL OFLC LCA Disclosure Data + USCIS H-1B Employer Data
Hub" in every headline, summary and disclaimer, and reported `data_vintage` as
the moment our ingest ran. The loaded rows are ONE fiscal year of USCIS with no
DOL component, so the product was describing evidence it did not have.

**A miss and a zero looked identical.** No row and "filed nothing" both
produced `None`. The USCIS Data Hub is a census of employers who filed, so
absence from it is a finding — Northrop Grumman holds 833 active postings in
our inventory and filed zero H-1B petitions in FY2026 — and a `census_absent`
row is how that gets said.
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared" / "python"))

svc = importlib.import_module("services.sponsorship_service")


def _doc(**over):
    doc = {
        "employer_norm": "acme",
        "employer_display": "Acme",
        "approvals_5y": 100,
        "denials_5y": 0,
        "computed_at": "2026-09-16T06:52:28+00:00",
        "ingested_at": "2026-09-16T06:52:28+00:00",
        "source_label": "USCIS H-1B Employer Data Hub",
        "data_window": {"fiscal_years": [2026], "years": 1,
                        "label": "USCIS fiscal year 2026 (single fiscal year)",
                        "dol_lca_included": False},
    }
    doc.update(over)
    return doc


class TestProvenanceComesFromTheRow:
    def test_the_window_is_the_rows_window_not_the_module_default(self):
        s = svc._score_doc(_doc())
        assert s["data_window_years"] == 1 != svc.DATA_WINDOW_YEARS
        assert s["fiscal_years"] == [2026]
        assert "5" not in s["headline"]
        assert "FY2026" in s["headline"]

    def test_the_source_is_the_rows_source_not_both_datasets(self):
        s = svc._score_doc(_doc())
        assert s["source"] == "USCIS H-1B Employer Data Hub"
        assert "DOL" not in s["source"]
        assert "DOL" not in s["disclaimer"]
        assert "USCIS H-1B Employer Data Hub" in s["disclaimer"]

    def test_the_disclaimer_still_carries_the_mandated_framing(self):
        s = svc._score_doc(_doc())
        for phrase in ("NOT a prediction", "not legal or immigration advice",
                       "not a guarantee"):
            assert phrase in s["disclaimer"]

    def test_the_vintage_is_the_fiscal_period_not_our_import_time(self):
        s = svc._score_doc(_doc())
        assert s["data_vintage"] == "FY2026"
        # The import moment is still available, under a name that says so.
        assert s["data_imported_at"].startswith("2026-09-16")

    def test_a_multi_year_row_states_its_span(self):
        s = svc._score_doc(_doc(data_window={
            "fiscal_years": [2022, 2023, 2024, 2025, 2026], "years": 5}))
        assert s["data_vintage"] == "FY2022-FY2026"
        assert "FY2022-FY2026" in s["summary"]

    def test_a_legacy_row_with_no_window_falls_back_to_the_constants(self):
        legacy = {"employer_norm": "acme", "employer_display": "Acme",
                  "approvals_5y": 50, "denials_5y": 0}
        s = svc._score_doc(legacy)
        assert s["data_window_years"] == svc.DATA_WINDOW_YEARS
        assert s["source"] == svc.SOURCE_LABEL
        assert f"the last {svc.DATA_WINDOW_YEARS} fiscal years" in s["headline"]

    def test_the_measured_window_is_passed_through_for_fit_score(self):
        """`fit_score._SPONSORSHIP_PASSTHROUGH` reads `data_window` off the
        signal to state the measured window; it can only do that if we send it."""
        s = svc._score_doc(_doc())
        assert s["data_window"]["years"] == 1
        assert s["data_window"]["fiscal_years"] == [2026]


class TestBandsMeanTheSameThingAtEveryVintage:
    """Saturation is per YEAR. Held fixed at the five-year constant, an
    employer's band would move when we imported more history rather than when
    their behaviour changed."""

    def _band(self, approvals, years, fiscal_years):
        return svc._score_doc(_doc(approvals_5y=approvals, data_window={
            "fiscal_years": fiscal_years, "years": years}))["band"]

    def test_the_five_year_calibration_is_unchanged(self):
        five = [2022, 2023, 2024, 2025, 2026]
        assert self._band(12, 5, five) == svc.BAND_LOW
        assert self._band(13, 5, five) == svc.BAND_MODERATE
        assert self._band(203, 5, five) == svc.BAND_MODERATE
        assert self._band(204, 5, five) == svc.BAND_STRONG

    def test_one_year_of_data_is_judged_at_the_same_rate(self):
        one = [2026]
        assert self._band(0, 1, one) == svc.BAND_NONE
        assert self._band(7, 1, one) == svc.BAND_LOW
        assert self._band(8, 1, one) == svc.BAND_MODERATE
        assert self._band(65, 1, one) == svc.BAND_MODERATE
        assert self._band(66, 1, one) == svc.BAND_STRONG

    def test_the_same_annual_rate_bands_the_same_at_both_vintages(self):
        # ~1,000 approvals over five years is ~200 a year; both read strong.
        assert self._band(1000, 5, [2022, 2023, 2024, 2025, 2026]) == svc.BAND_STRONG
        assert self._band(200, 1, [2026]) == svc.BAND_STRONG

    def test_denials_still_temper_without_erasing(self):
        clean = svc._score_doc(_doc(approvals_5y=70))
        heavy = svc._score_doc(_doc(approvals_5y=70, denials_5y=70))
        assert clean["band"] == svc.BAND_STRONG
        assert heavy["score"] < clean["score"]


class TestAMissAndAZeroAreDifferentFacts:
    def test_a_census_absent_row_says_they_filed_nothing(self):
        s = svc._score_doc(_doc(approvals_5y=0, employer_display="Northrop Grumman",
                                census_absent=True))
        assert s["band"] == svc.BAND_NONE
        assert s["score"] == 0
        assert s["evidence"] == "absent_from_filing_census"
        assert "Filed no H-1B petitions in FY2026" == s["headline"]
        assert "every employer that filed" in s["summary"]

    def test_a_row_with_activity_is_marked_as_such(self):
        assert svc._score_doc(_doc())["evidence"] == "filed_in_window"

    def test_a_zero_row_that_is_not_a_census_claim_does_not_make_one(self):
        s = svc._score_doc(_doc(approvals_5y=0, denials_5y=3))
        assert s["evidence"] == "row_with_no_activity"
        assert "filed no H-1B petitions" not in s["summary"]

    def test_lookup_status_distinguishes_the_two(self, monkeypatch):
        rows = {"acme": _doc(),
                "northrop grumman": _doc(employer_norm="northrop grumman",
                                         approvals_5y=0, census_absent=True)}

        class _Coll:
            def find_one(self, flt, _proj=None):
                return rows.get(flt.get("employer_norm"))

        monkeypatch.setattr(svc, "_collection", lambda: _Coll())
        assert svc.lookup_status("Acme")["reason"] == "employer_row"
        assert svc.lookup_status("Northrop Grumman")["reason"] == "census_absent"
        never = svc.lookup_status("Some Company We Never Loaded")
        assert never["matched"] is False and never["reason"] == "no_row"
        assert svc.lookup_status("   ")["reason"] == "unnormalizable_name"


class TestTheNormalizerStillProtectsTheQuery:
    def test_the_key_can_never_reach_an_operator_position(self):
        # A dict stringifies to a braced literal, which the parenthetical
        # stripper removes outright — an operator cannot survive as a key.
        assert svc.normalize_employer({"$ne": None}) == ""
        assert all(c.isalnum() or c == " "
                   for c in svc.normalize_employer("A&B, Inc. $where"))
