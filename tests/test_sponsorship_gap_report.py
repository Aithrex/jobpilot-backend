"""The gap list ranks demand we can prove against inventory we actually hold.

The failure modes worth a test are all about over-claiming: a bucket that says
"covered" when we hold nothing, a domain we invented from a name, a software
weight that promotes a hospital because it filed under NAICS 54, and the join
itself — ``employer_registry.name_key`` and
``sponsorship_service.normalize_employer`` disagree about "&", which silently
loses Ernst & Young, AT&T and Johnson & Johnson.
"""
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared" / "python"))

gap = importlib.import_module("scripts.sponsorship_gap_report")


def _sponsor(norm, display=None, approvals=100, denials=0, naics="54",
             mix=None, entities=()):
    return {
        "employer_norm": norm,
        "employer_display": display or norm.upper(),
        "approvals_5y": approvals,
        "denials_5y": denials,
        "naics": {"primary": naics, "label": "",
                  "mix": mix if mix is not None else [{"code": naics, "approvals": approvals}]},
        "location": {"city": "AUSTIN", "state": "TX"},
        "rollup": {"basis": "single_entity", "entity_count": len(entities) or 1,
                   "entities": [{"employer_norm": e} for e in entities]},
        "data_window": {"years": 1, "fiscal_years": [2026], "label": "FY2026",
                        "service_claim_years": 5, "service_claim_mismatch": True},
    }


def _employer(employer_id, name_key, **over):
    row = {"employer_id": employer_id, "name_key": name_key,
           "display_name": name_key.title(), "aliases": [],
           "identity_confidence": 0.95, "operating_status": "active",
           "employer_categories": ["other"], "verified_domains": [],
           "canonical_domain": None, "parent_employer_id": None}
    row.update(over)
    return row


def _index(employers):
    return gap.build_name_index(list(employers))


def _rows(sponsors, employers=(), sources=None, ats=None, canonical=None, names=None):
    by_id, by_name, by_token = _index(employers)
    return gap.build_rows(sponsors, by_id, by_name, by_token, {},
                          sources or {}, ats or {}, canonical or {}, names or {})


class TestTheJoinFindsTheEmployerWeAlreadyKnow:
    def test_the_two_normalizers_disagree_about_ampersand_and_the_join_handles_it(self):
        # normalize_employer("Ernst & Young") -> "ernst and young"
        # employer_registry.name_key("Ernst & Young") -> "ernst young"
        assert gap.match_keys("Ernst & Young LLP") == ["ernst and young", "ernst young"]
        assert gap.match_keys("Widget Inc") == ["widget"]

    def test_an_ampersand_employer_still_matches_its_registry_row(self):
        rows = _rows([_sponsor("ernst and young")],
                     employers=[_employer("emp_ey", "ernst young")])
        assert rows[0]["employer_id"] == "emp_ey"
        assert rows[0]["bucket"] == gap.BUCKET_NO_SOURCE

    def test_an_unknown_employer_is_never_given_one(self):
        rows = _rows([_sponsor("some tiny consultancy")])
        assert rows[0]["employer_id"] == ""
        assert rows[0]["bucket"] == gap.BUCKET_NO_EMPLOYER
        assert rows[0]["domain"] == ""
        assert rows[0]["domain_source"] == "none"

    def test_a_prefix_lookalike_is_a_hint_and_never_a_match(self):
        rows = _rows([_sponsor("cgi technologies and solutions")],
                     employers=[_employer("emp_cgi", "cgi")])
        assert rows[0]["employer_id"] == ""            # not merged
        assert rows[0]["bucket"] == gap.BUCKET_NO_EMPLOYER
        assert rows[0]["registry_hint_unverified"] == "cgi"
        assert rows[0]["registry_hint_employer_id"] == "emp_cgi"

    def test_a_domain_is_emitted_only_with_registry_evidence(self):
        verified = _rows([_sponsor("widget")],
                         employers=[_employer("emp_w", "widget",
                                              verified_domains=["widget.com"])])
        assert verified[0]["domain"] == "widget.com"
        assert verified[0]["domain_source"] == "employer_registry_verified"

        weaker = _rows([_sponsor("widget")],
                       employers=[_employer("emp_w", "widget",
                                            canonical_domain="widget.io")])
        assert weaker[0]["domain"] == "widget.io"
        assert weaker[0]["domain_source"] == "employer_registry_unverified"


class TestTheBucketIsTheHonestOne:
    def test_postings_today_beat_every_other_signal(self):
        rows = _rows([_sponsor("widget")],
                     employers=[_employer("emp_w", "widget")],
                     canonical={"emp_w": {"active": 12, "core": 7}})
        assert rows[0]["bucket"] == gap.BUCKET_COVERED
        assert rows[0]["active_postings"] == 12
        assert rows[0]["core_tech_postings"] == 7
        assert rows[0]["posting_count_basis"] == "canonical_employer_id"

    def test_a_proven_board_holding_nothing_is_its_own_bucket(self):
        rows = _rows([_sponsor("widget")],
                     employers=[_employer("emp_w", "widget")],
                     sources={"emp_w": [{"state": "validated", "member_key": "gh-widget",
                                         "ats_vendor": "greenhouse", "tenant": "widget"}]})
        assert rows[0]["bucket"] == gap.BUCKET_VALIDATED_ZERO

    def test_an_unproven_board_does_not_count_as_coverage(self):
        """ADR-006: only validated/empty count. ownership_verified does not."""
        rows = _rows([_sponsor("widget")],
                     employers=[_employer("emp_w", "widget")],
                     sources={"emp_w": [{"state": "ownership_verified",
                                         "member_key": "gh-widget",
                                         "ats_vendor": "greenhouse", "tenant": "widget"}]})
        assert rows[0]["bucket"] == gap.BUCKET_UNVALIDATED
        assert rows[0]["career_source_covering"] == 0

    def test_postings_are_counted_once_when_both_paths_answer(self):
        """The same posting reached by both joins must not be counted twice."""
        rows = _rows([_sponsor("widget", entities=("widget",))],
                     employers=[_employer("emp_w", "widget")],
                     canonical={"emp_w": {"active": 10, "core": 3}},
                     names={"widget": {"active": 10, "core": 3}})
        assert rows[0]["active_postings"] == 10

    def test_a_posting_the_canonical_join_cannot_see_is_still_found_by_name(self):
        rows = _rows([_sponsor("widget")],
                     names={"widget": {"active": 4, "core": 2}})
        assert rows[0]["active_postings"] == 4
        assert rows[0]["posting_count_basis"] == "employer_name"
        assert rows[0]["bucket"] == gap.BUCKET_COVERED


class TestSoftwareLikelihoodIsEvidenceNotVibes:
    def test_the_sector_mix_is_approval_weighted(self):
        mostly_info, _ = gap.software_likelihood(
            "ACME", [{"code": "51", "approvals": 900}, {"code": "62", "approvals": 100}])
        mostly_health, _ = gap.software_likelihood(
            "ACME", [{"code": "51", "approvals": 100}, {"code": "62", "approvals": 900}])
        assert mostly_info > 0.6 > mostly_health

    def test_a_hospital_that_filed_under_naics_54_is_still_a_hospital(self):
        weight, evidence = gap.software_likelihood(
            "SAINT SOMEWHERE HOSPITAL", [{"code": "54", "approvals": 500}])
        assert weight <= 0.08
        assert "name_non_software_veto" in evidence["signals"]

    def test_a_software_name_lifts_an_uninformative_sector(self):
        weight, evidence = gap.software_likelihood(
            "COMPUNNEL SOFTWARE GROUP INC", [{"code": "56", "approvals": 100}])
        assert weight >= 0.75
        assert "name_strong" in evidence["signals"]

    def test_our_own_registry_category_is_evidence_when_it_has_one(self):
        bare, _ = gap.software_likelihood("APPLE INC", [{"code": "31-33", "approvals": 100}])
        typed, evidence = gap.software_likelihood(
            "APPLE INC", [{"code": "31-33", "approvals": 100}], ["software_saas"])
        assert typed > bare
        assert "employer_registry_category" in evidence["signals"]

    def test_an_uncategorised_employer_is_not_penalised(self):
        bare, _ = gap.software_likelihood("ACME", [{"code": "51", "approvals": 10}])
        other, _ = gap.software_likelihood("ACME", [{"code": "51", "approvals": 10}], ["other"])
        assert bare == other

    def test_no_sector_at_all_is_a_neutral_prior_not_a_zero(self):
        weight, evidence = gap.software_likelihood("ACME", [])
        assert weight == gap.NAICS_WEIGHT_UNKNOWN
        assert evidence["basis"] == "naics_unknown"

    def test_the_ranking_is_approvals_times_likelihood(self):
        rows = _rows([
            _sponsor("big hospital", approvals=1000, naics="62"),
            _sponsor("small software", approvals=200, naics="51"),
        ])
        by_key = {r["employer_norm"]: r for r in rows}
        assert by_key["small software"]["priority_score"] > \
            by_key["big hospital"]["priority_score"]


class TestTheSummaryDoesNotOverstate:
    def test_the_unlock_estimate_is_modelled_from_employers_we_know(self):
        rows = _rows([
            _sponsor("known a", approvals=300),
            _sponsor("known b", approvals=300),
            _sponsor("stranger", approvals=300),
        ], employers=[_employer("emp_a", "known a"), _employer("emp_b", "known b")],
            canonical={"emp_a": {"active": 20, "core": 5}})
        summary = gap.summarize(rows)
        # emp_a holds 20, emp_b holds 0 -> median of the known population is 0 or 20,
        # and the stranger inherits that median rather than emp_a's 20 by fiat.
        est = summary["buckets"][gap.BUCKET_NO_EMPLOYER]["estimated_postings_if_covered"]
        assert est in (0, 20)
        assert "estimate" in summary["buckets"][gap.BUCKET_NO_EMPLOYER]["estimate_method"]

    def test_every_bucket_is_accounted_for(self):
        rows = _rows([_sponsor("a"), _sponsor("b")])
        summary = gap.summarize(rows)
        assert set(summary["buckets"]) == set(gap.BUCKET_ORDER)
        assert sum(b["employers"] for b in summary["buckets"].values()) == 2
