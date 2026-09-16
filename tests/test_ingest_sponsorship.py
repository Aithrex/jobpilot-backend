"""The sponsorship ingest parses a government file and refuses to guess.

Three things here are load-bearing and easy to get wrong:

* USCIS has changed its outcome columns at least twice, and summing a
  hard-coded subset silently under-reports every employer;
* a rollup that merges on a name resemblance invents an employer (the USCIS
  file really does contain a Florida stone importer whose legal name starts
  with "AMAZON"); and
* a field with no source must be ABSENT, because ``median_wage: 0`` is a claim
  about wages and ``None`` is not.
"""
import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared" / "python"))

ingest = importlib.import_module("scripts.ingest_sponsorship")
rollups = importlib.import_module("scripts.data.sponsorship_rollups")
sponsorship_service = importlib.import_module("services.sponsorship_service")

HEADER = [
    "Line by line", "Fiscal Year   ", "Employer (Petitioner) Name", "Tax ID",
    "Industry (NAICS) Code", "Petitioner City", "Petitioner State",
    "Petitioner Zip Code",
    "New Employment Approval", "New Employment Denial",
    "Continuation Approval", "Continuation Denial",
    "Change with Same Employer Approval", "Change with Same Employer Denial",
    "New Concurrent Approval", "New Concurrent Denial",
    "Change of Employer Approval", "Change of Employer Denial",
    "Amended Approval", "Amended Denial",
]

SOFTWARE = "54 - Professional, Scientific, and Technical Services"
INFO = "51 - Information"


def _row(line, name, tax="1234", naics=SOFTWARE, city="SEATTLE", state="WA",
         approvals=(0, 0, 0, 0, 0, 0), denials=(0, 0, 0, 0, 0, 0), fy="2026"):
    out = [line, fy, name, tax, naics, city, state, "98101"]
    for i in range(6):
        out.append(approvals[i])
        out.append(denials[i])
    return out


def _workbook(tmp_path, rows, name="datahub.xlsx"):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet 1"
    ws.append(HEADER)
    for row in rows:
        ws.append(row)
    path = tmp_path / name
    wb.save(path)
    return path


class TestTheParserReadsWhatTheFileActuallySays:
    def test_every_approval_column_is_summed_not_just_the_first(self, tmp_path):
        path = _workbook(tmp_path, [
            _row(1, "WIDGET SOFTWARE INC", approvals=(3, 5, 1, 0, 2, 4),
                 denials=(1, 0, 0, 0, 1, 0)),
        ])
        entities = {}
        stats = ingest.ingest_uscis(path, entities, 0)
        assert stats["approvals"] == 15      # 3+5+1+0+2+4
        assert stats["denials"] == 2
        assert entities["widget software"].approvals == 15

    def test_the_older_two_column_layout_parses_with_no_code_change(self):
        approvals, denials = ingest.resolve_outcome_columns(
            ["Employer", "Fiscal Year", "Initial Approval", "Initial Denial",
             "Continuing Approvals", "Continuing Denials"])
        assert approvals == ["Initial Approval", "Continuing Approvals"]
        assert denials == ["Initial Denial", "Continuing Denials"]

    def test_a_future_outcome_column_is_picked_up_by_its_suffix(self):
        approvals, _ = ingest.resolve_outcome_columns(
            ["Some Brand New Category Approval", "Employer"])
        assert approvals == ["Some Brand New Category Approval"]

    def test_rows_across_naics_and_city_fold_into_one_employer(self, tmp_path):
        path = _workbook(tmp_path, [
            _row(1, "WIDGET SOFTWARE INC", naics=SOFTWARE, approvals=(10, 0, 0, 0, 0, 0)),
            _row(2, "WIDGET SOFTWARE INC", naics=INFO, city="AUSTIN", state="TX",
                 approvals=(4, 0, 0, 0, 0, 0)),
        ])
        entities = {}
        ingest.ingest_uscis(path, entities, 0)
        ent = entities["widget software"]
        assert ent.approvals == 14
        assert ent.rows == 2
        assert ent.top_naics[0] == "54"          # approval-weighted, not first-seen
        assert ent.top_place == ("SEATTLE", "WA")

    def test_a_blank_petitioner_name_is_counted_and_dropped(self, tmp_path):
        path = _workbook(tmp_path, [
            _row(1, "", approvals=(1, 0, 0, 0, 0, 0)),
            _row(2, None, approvals=(1, 0, 0, 0, 0, 0)),
            _row(3, "LLC", approvals=(1, 0, 0, 0, 0, 0)),   # normalizes to ""
            _row(4, "REAL EMPLOYER INC", approvals=(2, 0, 0, 0, 0, 0)),
        ])
        entities = {}
        stats = ingest.ingest_uscis(path, entities, 0)
        assert stats["skipped_blank_name"] == 2
        assert stats["skipped_unnormalizable_name"] == 1
        assert set(entities) == {"real employer"}

    def test_the_fiscal_years_come_from_the_data_not_from_an_assumption(self, tmp_path):
        path = _workbook(tmp_path, [
            _row(1, "A INC", fy="2025", approvals=(1, 0, 0, 0, 0, 0)),
            _row(2, "B INC", fy="2026", approvals=(1, 0, 0, 0, 0, 0)),
        ])
        stats = ingest.ingest_uscis(path, {}, 0)
        assert stats["fiscal_years"] == [2025, 2026]

    def test_a_duplicated_header_cell_never_collapses_two_columns(self):
        assert ingest._dedupe_header(["A", None, "A"]) == ["A", "column_2", "A__2"]

    def test_the_display_name_is_deterministic_across_runs(self, tmp_path):
        path = _workbook(tmp_path, [
            _row(1, "ACME", approvals=(1, 0, 0, 0, 0, 0)),
            _row(2, "ACME INCORPORATED", approvals=(1, 0, 0, 0, 0, 0)),
        ])
        entities = {}
        ingest.ingest_uscis(path, entities, 0)
        assert entities["acme"].display == "ACME INCORPORATED"


def _entity(norm, display, approvals=0, denials=0, tax="", city="", state="",
            naics=("54", "Prof")):
    ent = ingest.Entity(norm)
    ent.display = display
    ent.approvals = approvals
    ent.denials = denials
    ent.rows = 1
    ent.fiscal_years = {2026}
    if tax:
        ent.tax_ids[tax] = approvals + 1
    if city or state:
        ent.places[(city, state)] = approvals + 1
    ent.naics[naics] = approvals + 1
    return ent


class TestTheRollupMergesOnEvidenceAndNothingElse:
    def test_the_shipped_table_is_structurally_valid(self):
        assert rollups.validate_groups() == []

    def test_a_member_claimed_by_two_parents_is_rejected(self):
        bad = [
            {"key": "a", "display": "A", "members": ["shared name"], "aliases": [], "excluded": []},
            {"key": "b", "display": "B", "members": ["shared name"], "aliases": [], "excluded": []},
        ]
        problems = rollups.validate_groups(bad)
        assert any("claimed by both" in p for p in problems)

    def test_a_member_that_is_not_normalized_is_rejected(self):
        bad = [{"key": "a", "display": "A", "members": ["Widget Software, Inc."],
                "aliases": [], "excluded": []}]
        assert any("not normalized" in p for p in rollups.validate_groups(bad))

    def test_curated_members_become_one_employer(self):
        entities = {
            "amazon com services": _entity("amazon com services", "AMAZON COM SERVICES LLC",
                                           9337, 82, "4687", "ARLINGTON", "VA"),
            "amazon web services": _entity("amazon web services", "AMAZON WEB SERVICES INC",
                                           1435, 17, "8068", "ARLINGTON", "VA"),
        }
        groups, stats, _ = build(entities)
        amazon = _by_key(groups)["amazon"]
        assert amazon.approvals == 9337 + 1435
        assert amazon.basis == rollups.BASIS_CURATED
        assert len(amazon.members) == 2

    def test_a_refused_lookalike_stays_its_own_employer(self):
        """The Delray Beach stone importer is not Amazon."""
        entities = {
            "amazon com services": _entity("amazon com services", "AMAZON COM SERVICES LLC",
                                           9337, 0, "4687", "ARLINGTON", "VA"),
            "amazon usa enterprise": _entity("amazon usa enterprise",
                                             "AMAZON USA ENTERPRISE CORP D B A GRANITE",
                                             1, 0, "7566", "DELRAY BEACH", "FL"),
        }
        groups, stats, candidates = build(entities)
        by_key = _by_key(groups)
        assert by_key["amazon"].approvals == 9337
        assert "amazon usa enterprise" in by_key
        assert by_key["amazon usa enterprise"].approvals == 1
        refused = {r["employer_norm"] for r in by_key["amazon"].refused}
        assert "amazon usa enterprise" in refused
        assert any(c["employer_norm"] == "amazon usa enterprise"
                   and c["reason"] == "curated_exclusion" for c in candidates)

    def test_a_shared_leading_token_alone_never_merges(self):
        entities = {
            "meta platforms": _entity("meta platforms", "META PLATFORMS INC", 2563, 0,
                                      "5019", "MENLO PARK", "CA"),
            "meta gadgets": _entity("meta gadgets", "META GADGETS INC", 4, 0,
                                    "9999", "ROSWELL", "GA"),
        }
        groups, _, candidates = build(entities)
        by_key = _by_key(groups)
        assert by_key["meta"].approvals == 2563
        assert by_key["meta gadgets"].approvals == 4
        hit = [c for c in candidates if c["employer_norm"] == "meta gadgets"]
        assert hit and hit[0]["reason"] == "name_prefix_only_no_corroborating_evidence"
        assert hit[0]["would_join"] == "meta"

    def test_the_same_ein_last4_brand_and_state_merges_one_entity_spelled_twice(self):
        entities = {
            "aptiv us services general partnership d b a aptiv": _entity(
                "aptiv us services general partnership d b a aptiv",
                "APTIV US SERVICES GENERAL PARTNERSHIP D B A APTIV", 42, 0,
                "0316", "TROY", "MI"),
            "aptiv us services general partnership dba aptiv": _entity(
                "aptiv us services general partnership dba aptiv",
                "APTIV US SERVICES GENERAL PARTNERSHIP DBA APTIV", 30, 0,
                "0316", "TROY", "MI"),
        }
        groups, stats, _ = build(entities)
        assert len(groups) == 1
        assert groups[0].basis == rollups.BASIS_TAX_ID
        assert groups[0].approvals == 72

    def test_the_same_ein_last4_in_a_different_state_does_not_merge(self):
        entities = {
            "acme systems": _entity("acme systems", "ACME SYSTEMS INC", 10, 0,
                                    "0316", "TROY", "MI"),
            "acme services": _entity("acme services", "ACME SERVICES LLC", 8, 0,
                                     "0316", "AUSTIN", "TX"),
        }
        groups, _, _ = build(entities)
        assert len(groups) == 2

    def test_the_registry_merges_only_a_strong_identity(self):
        entities = {
            "intuit": _entity("intuit", "INTUIT INC", 573, 0, "4661", "MOUNTAIN VIEW", "CA"),
            "credit karma": _entity("credit karma", "CREDIT KARMA LLC", 67, 0,
                                    "1111", "OAKLAND", "CA"),
        }
        groups, _, _ = build(entities, registry_map={"intuit": "emp_x", "credit karma": "emp_x"})
        assert _by_key(groups)["intuit"].approvals == 640

        # ... and with no registry evidence they stay two employers.
        groups, _, _ = build(entities, registry_map={})
        assert _by_key(groups)["intuit"].approvals == 573

    def test_a_registry_group_spanning_two_curated_groups_is_refused(self):
        entities = {
            "microsoft": _entity("microsoft", "MICROSOFT CORPORATION", 3688, 0,
                                 "4442", "REDMOND", "WA"),
            "linkedin": _entity("linkedin", "LINKEDIN CORPORATION", 542, 0,
                                "2023", "SUNNYVALE", "CA"),
        }
        groups, _, _ = build(entities,
                             registry_map={"microsoft": "emp_m", "linkedin": "emp_m"})
        by_key = _by_key(groups)
        assert by_key["microsoft"].approvals == 3688
        assert by_key["linkedin"].approvals == 542

    def test_an_alias_never_overwrites_an_employer_that_actually_filed(self):
        """'aws' is a curated lookup alias; if some company IS named AWS, it wins."""
        entities = {
            "amazon com services": _entity("amazon com services", "AMAZON COM SERVICES LLC",
                                           9337, 0, "4687", "ARLINGTON", "VA"),
            "aws": _entity("aws", "AWS INC", 3, 0, "5555", "TAMPA", "FL"),
        }
        groups, _, _ = build(entities)
        by_key = _by_key(groups)
        assert "aws" not in by_key["amazon"].aliases
        assert by_key["aws"].approvals == 3


def build(entities, registry_map=None):
    return ingest.build_groups(entities, use_registry=registry_map is not None,
                               registry_map=registry_map)


def _by_key(groups):
    return {g.key: g for g in groups}


SOURCES = [{"dataset": ingest.USCIS_DATASET, "label": ingest.USCIS_LABEL,
            "file": "f.xlsx", "sha256": "abc", "rows": 2, "rows_used": 2,
            "fiscal_years": [2026]}]


def _docs(entities, window_years=(2026,)):
    groups, _, _ = build(entities)
    window = ingest.window_block(window_years, has_lca=False)
    return ingest.build_documents(groups, sources=SOURCES, window=window,
                                  run_id="run1",
                                  now=datetime(2026, 9, 15, tzinfo=timezone.utc))


class TestTheDocumentTellsTheTruthAboutWhatWeHave:
    def test_fields_with_no_source_are_absent_not_zero(self):
        docs = _docs({"widget": _entity("widget", "WIDGET INC", 10, 1)})
        doc = docs[0]
        for field in ("lca_filings_5y", "median_wage", "top_titles", "last_filed_at"):
            assert field not in doc, f"{field} must be ABSENT with no DOL source"
        assert doc["approvals_5y"] == 10
        assert doc["denials_5y"] == 1

    def test_the_window_is_read_from_the_data_and_the_overclaim_is_flagged(self):
        window = ingest.window_block([2026], has_lca=False)
        assert window["years"] == 1
        assert window["fiscal_years"] == [2026]
        assert "2026" in window["label"]
        # The service's copy says five years; one year is not five.
        assert window["service_claim_years"] == sponsorship_service.DATA_WINDOW_YEARS
        assert window["service_claim_mismatch"] is True

        five = ingest.window_block([2022, 2023, 2024, 2025, 2026], has_lca=True)
        assert five["years"] == 5
        assert five["service_claim_mismatch"] is False

    def test_every_document_carries_its_vintage_and_provenance(self):
        doc = _docs({"widget": _entity("widget", "WIDGET INC", 10)})[0]
        assert doc["data_window"]["fiscal_years"] == [2026]
        assert doc["source_label"] == ingest.USCIS_LABEL
        assert doc["ingest_run_id"] == "run1"
        assert doc["provenance"]["sources"][0]["sha256"] == "abc"
        assert doc["ingested_at"] == datetime(2026, 9, 15, tzinfo=timezone.utc)

    def test_alias_documents_carry_the_parents_numbers_and_the_parents_name(self):
        entities = {
            "amazon com services": _entity("amazon com services", "AMAZON COM SERVICES LLC",
                                           9337, 82, "4687", "ARLINGTON", "VA"),
            "amazon web services": _entity("amazon web services", "AMAZON WEB SERVICES INC",
                                           1435, 17, "8068", "ARLINGTON", "VA"),
        }
        docs = {d["employer_norm"]: d for d in _docs(entities)}
        assert docs["amazon"]["rollup"]["is_alias"] is False
        for alias in ("amazon web services", "amazon com services", "aws"):
            assert docs[alias]["approvals_5y"] == 9337 + 1435
            # The sentence the service renders uses employer_display, so an alias
            # row must say "Amazon", never "AWS had 10,772 approvals".
            assert docs[alias]["employer_display"] == "Amazon"
            assert docs[alias]["rollup"]["is_alias"] is True
            assert docs[alias]["rollup"]["canonical_norm"] == "amazon"

    def test_the_entities_array_is_bounded(self):
        entities = {f"acme unit {i}": _entity(f"acme unit {i}", f"ACME UNIT {i} INC",
                                              i, 0, "0001", "TROY", "MI")
                    for i in range(80)}
        groups, _, _ = build(entities)
        big = max(groups, key=lambda g: len(g.members))
        doc = ingest.build_documents([big], sources=SOURCES,
                                     window=ingest.window_block([2026], False),
                                     run_id="r")[0]
        assert len(doc["rollup"]["entities"]) <= ingest._MAX_ENTITIES_ON_DOC
        assert doc["rollup"]["entities_truncated"] == len(big.members) - ingest._MAX_ENTITIES_ON_DOC

    def test_a_denial_only_employer_is_written_and_reads_as_no_activity(self):
        docs = _docs({"widget": _entity("widget", "WIDGET INC", 0, 5)})
        scored = sponsorship_service._score_doc(docs[0])
        assert scored["approvals_5y"] == 0
        assert scored["denials_5y"] == 5
        assert scored["band"] == sponsorship_service.BAND_NONE
        assert scored["score"] == 0


class TestTheServiceScoresAUscisOnlyRowSanely:
    """Pins the bands a USCIS-only row lands in, because the ingest chose to
    leave the DOL fields absent and ``_recency_multiplier`` then returns 0.85.

    These thresholds are a CONSEQUENCE of that choice, not an independent
    decision, and a change to either side should show up here."""

    # One fiscal year of data, judged at the same per-year rate a five-year
    # file is judged at (see tests/test_sponsorship_service.py).
    @pytest.mark.parametrize("approvals,band", [
        (0, sponsorship_service.BAND_NONE),
        (1, sponsorship_service.BAND_LOW),
        (7, sponsorship_service.BAND_LOW),
        (8, sponsorship_service.BAND_MODERATE),
        (65, sponsorship_service.BAND_MODERATE),
        (66, sponsorship_service.BAND_STRONG),
        (13069, sponsorship_service.BAND_STRONG),
    ])
    def test_band_boundaries(self, approvals, band):
        doc = _docs({"x": _entity("x", "X INC", approvals)})[0]
        assert sponsorship_service._score_doc(doc)["band"] == band

    def test_denials_temper_the_signal_without_erasing_it(self):
        clean = sponsorship_service._score_doc(_docs({"x": _entity("x", "X", 70)})[0])
        heavy = sponsorship_service._score_doc(_docs({"x": _entity("x", "X", 70, 70)})[0])
        assert clean["band"] == sponsorship_service.BAND_STRONG
        assert heavy["score"] < clean["score"]
        assert heavy["band"] == sponsorship_service.BAND_MODERATE

    def test_the_absent_dol_fields_read_as_unknown_not_as_zero_wages(self):
        scored = sponsorship_service._score_doc(_docs({"x": _entity("x", "X", 50)})[0])
        assert scored["median_wage"] is None
        assert scored["top_titles"] == []
        assert scored["last_filed_at"] is None
        # The vintage still has to render: computed_at is the fallback.
        assert scored["data_vintage"] is not None


class TestFilingNothingIsAnAnswerAndIsNotNoData:
    """The USCIS Data Hub lists every employer that filed, so absence from it is
    evidence. Northrop Grumman holds 833 active postings in our inventory and
    filed zero petitions in FY2026 — until now that was indistinguishable from
    an employer we had simply never looked at."""

    WINDOW = {"fiscal_years": [2026], "years": 1, "label": "FY2026"}

    def _run(self, inventory, entities=None):
        entities = entities or {
            "fiserv solutions": _entity("fiserv solutions", "FISERV SOLUTIONS LLC", 232),
            "acme widgets": _entity("acme widgets", "ACME WIDGETS INC", 5),
        }
        return ingest.census_absent_documents(
            entities, set(entities), inventory, sources=SOURCES,
            window=self.WINDOW, run_id="r")

    def test_an_employer_with_no_token_in_the_census_filed_nothing(self):
        docs, _ = self._run({"northrop grumman": {"active": 833, "core": 408,
                                                  "display": "Northrop Grumman"}})
        assert len(docs) == 1
        doc = docs[0]
        assert doc["employer_norm"] == "northrop grumman"
        assert doc["approvals_5y"] == 0 and doc["census_absent"] is True
        assert doc["census"]["tokens_checked"] == ["northrop", "grumman"]
        assert doc["census"]["inventory_active_postings"] == 833
        scored = sponsorship_service._score_doc(doc)
        assert scored["band"] == sponsorship_service.BAND_NONE
        assert scored["evidence"] == "absent_from_filing_census"

    def test_a_token_present_in_the_census_is_a_curation_task_not_a_zero(self):
        """Fiserv filed as FISERV SOLUTIONS LLC. Claiming it filed nothing
        because our feed spells it 'Fiserv' would be a false negative."""
        docs, skipped = self._run({"fiserv": {"active": 201, "core": 93,
                                              "display": "Fiserv"}})
        assert docs == []
        assert skipped[0]["reason"] == "token_present_in_census"
        assert skipped[0]["tokens"] == ["fiserv"]

    def test_a_name_of_only_common_words_proves_nothing(self):
        docs, skipped = self._run({"td bank": {"active": 877, "core": 181,
                                               "display": "TD Bank"}})
        assert docs == []
        assert skipped[0]["reason"] == "no_distinctive_token"

    def test_an_aggregator_is_not_an_employer_and_gets_no_claim(self):
        docs, skipped = self._run({"jobgether": {"active": 4478, "core": 2151,
                                                 "display": "Jobgether"},
                                   "uscareers oslrs": {"active": 180, "core": 0,
                                                       "display": "uscareers-oslrs"}})
        assert docs == []
        assert {s["reason"] for s in skipped} == {"not_an_employer"}

    def test_an_employer_we_already_have_a_row_for_is_left_alone(self):
        entities = {"acme widgets": _entity("acme widgets", "ACME WIDGETS INC", 5)}
        docs, _ = ingest.census_absent_documents(
            entities, {"acme widgets"},
            {"acme widgets": {"active": 10, "core": 2, "display": "Acme Widgets"}},
            sources=SOURCES, window=self.WINDOW, run_id="r")
        assert docs == []

    def test_we_only_speak_about_employers_we_carry_postings_for(self):
        docs, _ = self._run({"northrop grumman": {"active": 0, "core": 0,
                                                  "display": "Northrop Grumman"}})
        assert docs == []
