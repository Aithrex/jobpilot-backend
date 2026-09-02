"""One ATS tenant must resolve to ONE employer name.

Measured on live `aspirely_db` (2026-09-01): 129 (ats, tenant) pairs carried
more than one `employer_name`, covering 28,386 active postings - 12.5% of the
whole active inventory. `oracle_orc/jpmc` alone was published under
'NUTMEG CONTAINER CORPORATION', 'CHASE FAMILY LIMITED PARTNERSHIP NO. 5' and
'JPMorgan Chase'. Each case is a real live row.
"""
from __future__ import annotations

import pytest

from services import employer_identity as ei


@pytest.fixture(autouse=True)
def _fresh_cache():
    ei.reset_cache()
    yield
    ei.reset_cache()


class TestNameKey:
    @pytest.mark.parametrize("a, b", [
        ("Boeing", "BOEING CO"),
        ("Boeing", "The Boeing Company"),
        ("Alarm.com Holdings, Inc.", "Alarm.com"),
        ("Tyson Foods, Inc.", "Tyson Foods Inc"),
        ("SAP SE", "SAP"),
    ])
    def test_legal_suffixes_do_not_make_two_employers(self, a, b):
        assert ei.name_key(a) == ei.name_key(b)

    def test_genuinely_different_names_stay_different(self):
        assert ei.name_key("Boeing") != ei.name_key("M.J. Williams Company")


class TestResolution:
    def test_the_curated_catalog_wins(self):
        got = ei.resolve_employer_name(
            ats="workday", tenant="boeing",
            candidates=["BOEING CO", "THE BOEING COMPANY", "M.J. WILLIAMS COMPANY"])
        assert got["source"] == "curated_catalog"
        assert ei.name_key(got["name"]) == ei.name_key("Boeing")
        assert "M.J. WILLIAMS COMPANY" in got["variants"]

    def test_an_acronym_tenant_resolves(self):
        """`jpmc` is neither a prefix nor a substring of 'JPMorgan Chase' - it
        is the capital letters. This single tenant is 3,998 live postings."""
        got = ei.resolve_employer_name(
            ats="oracle_orc", tenant="jpmc",
            candidates=["NUTMEG CONTAINER CORPORATION",
                        "CHASE FAMILY LIMITED PARTNERSHIP NO. 5",
                        "JPMorgan Chase"])
        assert got["name"] == "JPMorgan Chase"
        assert got["confidence"] >= 0.8

    def test_an_icims_careers_prefix_is_not_part_of_the_identity(self):
        got = ei.resolve_employer_name(
            ats="icims", tenant="careers-coraltreehospitality",
            candidates=["CoralTree Hospitality Group LLC", "CHRISTIANIA, INC."])
        assert got["name"] == "CoralTree Hospitality Group LLC"

    def test_a_shared_first_word_is_not_ownership(self):
        """`ryan` must not resolve to 'RYAN OIL COMPANY' just because the name
        starts with it - containment on a short slug matched half the registry."""
        got = ei.resolve_employer_name(
            ats="workday", tenant="ryan",
            candidates=["RYAN OIL COMPANY, INC.", "SOME OTHER LLC"])
        assert got["source"] == "unresolved_conflict"
        assert got["confidence"] == 0.0

    def test_a_raw_tenant_slug_is_not_an_employer_name(self):
        got = ei.resolve_employer_name(
            ats="icims", tenant="careers-superioraluminumalloys",
            candidates=["careers-superioraluminumalloys",
                        "Superior Aluminum Alloys LLC"])
        assert got["name"] == "Superior Aluminum Alloys LLC"

    def test_an_unadjudicable_tenant_is_flagged_not_guessed(self):
        """An opaque Oracle pod id with two unrelated names carries no
        evidence. Picking one would be a confident wrong answer, which is the
        failure mode this module exists to undo."""
        got = ei.resolve_employer_name(
            ats="oracle_orc", tenant="ibwsjb",
            candidates=["GENE F. LANG & CO.", "COBAS SERVICES LLC"])
        assert got["source"] == "unresolved_conflict"
        assert got["confidence"] == 0.0
        assert len(got["variants"]) == 1

    def test_a_sole_candidate_is_kept_at_moderate_confidence(self):
        got = ei.resolve_employer_name(
            ats="oracle_orc", tenant="ibwsjb",
            candidates=["ENCOMPASS HEALTH CORPORATION"])
        assert got["name"] == "ENCOMPASS HEALTH CORPORATION"
        assert got["source"] == "sole_candidate"

    def test_mixed_case_beats_shouting_for_the_same_entity(self):
        got = ei.resolve_employer_name(
            ats="oracle_orc", tenant="zzz",
            candidates=["TYSON FOODS, INC.", "Tyson Foods"])
        assert got["name"] == "Tyson Foods"

    def test_no_candidates_and_no_fallback_is_empty_not_invented(self):
        got = ei.resolve_employer_name(ats="workday", tenant="zzz", candidates=[])
        assert got["name"] == "" and got["source"] == "none"

    def test_blank_candidates_are_ignored(self):
        got = ei.resolve_employer_name(ats="workday", tenant="zzz",
                                       candidates=["", "  ", None, "Acme"])
        assert got["name"] == "Acme"


class TestApplyToRows:
    def test_a_row_is_renamed_and_the_original_recorded(self):
        rmap = ei.build_resolution_map([
            {"ats": "workday", "tenant": "boeing", "employer_name": "BOEING CO"},
            {"ats": "workday", "tenant": "boeing",
             "employer_name": "M.J. WILLIAMS COMPANY"},
        ])
        row = {"ats": "workday", "tenant": "boeing",
               "company": "M.J. WILLIAMS COMPANY"}
        ei.apply_resolution(row, rmap)
        assert ei.name_key(row["company"]) == ei.name_key("Boeing")
        assert row["employer_name_original"] == "M.J. WILLIAMS COMPANY"
        assert row["employer_name_confidence"] > 0.9

    def test_an_unresolved_row_keeps_its_name_and_is_flagged(self):
        rmap = ei.build_resolution_map([
            {"ats": "oracle_orc", "tenant": "ibwsjb", "employer_name": "A LLC"},
            {"ats": "oracle_orc", "tenant": "ibwsjb", "employer_name": "B INC"},
        ])
        row = {"ats": "oracle_orc", "tenant": "ibwsjb", "company": "A LLC"}
        ei.apply_resolution(row, rmap)
        assert row["company"] == "A LLC"
        assert row["employer_name_conflict"] is True
        assert row["employer_name_confidence"] == 0.0

    def test_an_unknown_tenant_is_left_completely_alone(self):
        row = {"ats": "workday", "tenant": "nobody", "company": "Whatever"}
        before = dict(row)
        ei.apply_resolution(row, {})
        assert row == before

    def test_rows_with_no_tenant_are_skipped_not_dropped(self):
        rmap = ei.build_resolution_map([
            {"ats": "jsearch", "tenant": "", "employer_name": "Acme"}])
        assert rmap == {}
