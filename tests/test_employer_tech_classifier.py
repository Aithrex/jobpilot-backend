"""Employer classification, kept separate from occupation classification.

Every case here is drawn from a live `companies` or `job_postings` row. The
recurring theme is that a company NAME is not evidence: the registry contains
"NUROTOCO OF MASSACHUSETTS, INC.", "SLACK TIDE CRUISES LLC" and
"NOTIONSBYNANCY LLC", and a keyword rule classifies all three as technology
companies.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services import employer_tech_classifier as etc


def company(**over):
    base = {"company_id": "c1", "name": "Acme Systems Inc"}
    base.update(over)
    return base


class TestEvidenceLadder:
    def test_a_software_sic_classifies_software(self):
        got = etc.classify_employer(company(
            name="MICROSOFT CORP", sic="7372",
            sic_description="Services-Prepackaged Software"))
        assert got.tech_classification == etc.TECH_SOFTWARE
        assert got.evidence_source == "sec_edgar_sic"
        assert "software" in got.tech_categories

    def test_a_semiconductor_sic_carries_the_category(self):
        got = etc.classify_employer(company(
            name="NVIDIA CORP", sic="3674",
            sic_description="Semiconductors & Related Devices"))
        assert got.tech_classification == etc.TECH_SOFTWARE
        assert "semiconductor" in got.tech_categories

    def test_a_nontech_sic_is_evidence_of_NOT_being_tech(self):
        """A filed SIC that is on neither list is the issuer's own statement."""
        got = etc.classify_employer(company(
            name="SAFEWAY INC.", sic="5411",
            sic_description="Retail-Grocery Stores"))
        assert got.tech_classification == etc.TECH_NON

    def test_posting_mix_classifies_a_private_company_edgar_cannot_see(self):
        got = etc.classify_employer(
            company(name="Databricks"),
            posting_mix={"core": 60, "adjacent": 10, "non_tech": 20, "unknown": 10})
        assert got.tech_classification == etc.TECH_SOFTWARE
        assert got.evidence_source == "posting_mix"

    def test_a_low_tech_posting_mix_reads_non_tech(self):
        got = etc.classify_employer(
            company(name="Some Retailer"),
            posting_mix={"core": 2, "adjacent": 1, "non_tech": 300, "unknown": 50})
        assert got.tech_classification == etc.TECH_NON

    def test_a_thin_posting_mix_is_not_evidence(self):
        """Three postings is not a hiring pattern."""
        got = etc.classify_employer(
            company(name="Tiny Co"),
            posting_mix={"core": 3, "adjacent": 0, "non_tech": 0, "unknown": 0})
        assert got.tech_classification == etc.TECH_UNKNOWN

    def test_unknown_scope_does_not_dilute_the_ratio(self):
        """A large board we cannot read is not thereby less technical."""
        got = etc.classify_employer(
            company(name="Big Tech"),
            posting_mix={"core": 40, "adjacent": 0, "non_tech": 5,
                         "unknown": 5000})
        assert got.tech_classification == etc.TECH_SOFTWARE

    def test_the_curated_seed_classifies(self):
        got = etc.classify_employer(company(name="Anthropic"),
                                    curated_category="ai-ml")
        assert got.tech_classification == etc.TECH_SOFTWARE
        assert "ai-ml" in got.tech_categories


class TestNameKeywordsAreNotEvidence:
    @pytest.mark.parametrize("name", [
        "NUROTOCO OF MASSACHUSETTS, INC.",
        "SLACK TIDE CRUISES LLC",
        "NOTIONSBYNANCY LLC",
        "RAMPART RANGE MOTORCYCLE MANAGEMENT COMMITTEE",
        "REDISTRIBUTION CENTER, INC.",
        "CHASE FAMILY LIMITED PARTNERSHIP NO. 5",
    ])
    def test_a_registry_shell_is_never_classified_from_its_name(self, name):
        got = etc.classify_employer(company(name=name))
        assert got.tech_classification != etc.TECH_SOFTWARE, name

    def test_a_tech_keyword_alone_reaches_unknown_at_most(self):
        got = etc.classify_employer(company(name="Quantum Computing Inc."))
        assert got.tech_classification == etc.TECH_UNKNOWN
        assert got.confidence <= 0.2
        assert got.evidence and got.evidence[0]["kind"] == "name_keyword"

    def test_a_false_friend_gets_no_signal_at_all(self):
        got = etc.classify_employer(company(name="Cloud Nine Cruises LLC"))
        assert got.evidence == []


class TestDisagreement:
    def test_a_bank_with_a_software_board_becomes_tech_adjacent(self):
        """Capital One and Goldman Sachs, exactly. Both signals are recorded."""
        got = etc.classify_employer(
            company(name="CAPITAL ONE FINANCIAL CORP", sic="6022",
                    sic_description="State Commercial Banks"),
            posting_mix={"core": 200, "adjacent": 30, "non_tech": 120,
                         "unknown": 400})
        assert got.tech_classification == etc.TECH_ADJACENT
        kinds = {e["kind"] for e in got.evidence}
        assert any(k.endswith("_dissent") for k in kinds)

    def test_agreeing_signals_raise_confidence(self):
        solo = etc.classify_employer(company(name="X", sic="7372"))
        agreed = etc.classify_employer(
            company(name="X", sic="7372"),
            posting_mix={"core": 80, "adjacent": 5, "non_tech": 5, "unknown": 0},
            curated_category="software")
        assert agreed.confidence > solo.confidence


class TestFacts:
    def test_an_edgar_cik_means_public(self):
        got = etc.classify_employer(company(cik="0001045810", sic="3674"))
        assert got.public_private_status == etc.STATUS_PUBLIC

    def test_absent_size_and_stage_stay_none_never_guessed(self):
        got = etc.classify_employer(company(sic="7372"))
        assert got.company_size is None and got.company_stage is None

    def test_validated_endpoint_count_is_counted_not_assumed(self):
        got = etc.classify_employer(company(sic="7372"), endpoints=[
            {"status": "validated", "vendor": "greenhouse",
             "validated_at": datetime(2026, 9, 1, tzinfo=timezone.utc)},
            {"status": "dead", "vendor": "lever"},
        ])
        assert got.validated_career_endpoint_count == 1
        assert got.last_endpoint_validation_at is not None

    def test_the_verdict_is_versioned(self):
        assert etc.classify_employer(company(sic="7372")).classifier_version \
            == etc.CLASSIFIER_VERSION


class TestCoverageState:
    def test_a_validated_endpoint_is_covered(self):
        state, detail = etc.coverage_state_for(company(), [
            {"status": "validated", "vendor": "greenhouse", "job_count_last": 42}])
        assert state == etc.COVERAGE_VALIDATED
        assert "42" in detail

    def test_a_successfully_empty_board_is_its_own_state(self):
        state, _ = etc.coverage_state_for(company(), [
            {"status": "empty", "vendor": "lever"}])
        assert state == etc.COVERAGE_NO_PUBLIC_JOBS

    def test_a_blocked_endpoint_reports_the_blocker(self):
        state, detail = etc.coverage_state_for(company(), [
            {"status": "blocked", "vendor": "icims",
             "last_check_detail": "405 Human Verification"}])
        assert state == etc.COVERAGE_BLOCKED
        assert "Human Verification" in detail

    def test_an_unsupported_vendor_is_distinguished_from_a_failure(self):
        state, detail = etc.coverage_state_for(company(), [
            {"status": "candidate", "vendor": "dayforce"}])
        assert state == etc.COVERAGE_UNSUPPORTED
        assert "dayforce" in detail

    def test_a_dead_endpoint_is_retrying_with_a_reason(self):
        state, detail = etc.coverage_state_for(company(), [
            {"status": "dead", "vendor": "icims",
             "last_check_detail": "migrated to https://careers.rivian.com/jobs"}])
        assert state == etc.COVERAGE_RETRYING
        assert "careers.rivian.com" in detail

    def test_no_portal_is_UNRESOLVED_not_no_jobs(self):
        """NVIDIA, Microsoft, Apple and Salesforce are all `no_portal` on live
        data while all four run a public ATS. That is a discovery failure, not
        an employer fact, and it must not be counted as coverage."""
        state, detail = etc.coverage_state_for(
            company(discovery_status="no_portal",
                    detail="read 5 page(s) on nvidia.com, no ATS link found"), [])
        assert state == etc.COVERAGE_UNRESOLVED
        assert "no ATS" in detail or "fingerprint" in detail

    def test_a_pending_company_is_retrying(self):
        state, _ = etc.coverage_state_for(company(discovery_status="deferred"), [])
        assert state == etc.COVERAGE_RETRYING

    def test_no_attempt_recorded_is_unresolved(self):
        state, detail = etc.coverage_state_for(company(), [])
        assert state == etc.COVERAGE_UNRESOLVED
        assert "no discovery attempt" in detail

    def test_a_resolving_domain_is_never_coverage(self):
        """ADR-004 sec. 3.2: `*.icims.com` is wildcard DNS, so every candidate
        slug resolves and DNS proves nothing."""
        state, _ = etc.coverage_state_for(
            company(domain="anything.com", discovery_status="no_portal"), [])
        assert state != etc.COVERAGE_VALIDATED
