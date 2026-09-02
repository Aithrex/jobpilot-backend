"""The coverage number, and the claims it is allowed to make.

The report is the accountability artifact: it is what the acceptance criteria
are checked against, so its own arithmetic needs checking. In particular it
must never (a) count an endpoint that merely answered, (b) let an employer
leave the denominator because a provider failed, or (c) place an employer in a
cohort on the strength of its name.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import employer_coverage_report as rep  # noqa: E402

from services import career_source_service as css  # noqa: E402
from services import employer_registry as er       # noqa: E402


def _emp(**over):
    row = er.build_employer(
        display_name=over.pop("display_name", "Acme"),
        canonical_domain=over.pop("canonical_domain", None),
        evidence=over.pop("evidence", ()),
        cohorts=over.pop("cohorts", ()),
        employer_categories=over.pop("employer_categories", ()),
    )
    row.update(over)
    return row


class TestDenominator:
    def test_a_curated_seed_row_is_evidence(self):
        ok, why = rep.is_evidence_backed_tech(
            _emp(evidence=[{"kind": "curated_seed", "source": "seed", "value": "saas"}]),
            {})
        assert ok and "curated" in why

    def test_an_official_career_url_is_evidence(self):
        ok, _ = rep.is_evidence_backed_tech(
            _emp(evidence=[{"kind": "official_career_url", "source": "registry",
                            "value": "https://x/careers"}]), {})
        assert ok

    def test_a_technology_sounding_name_alone_is_not_evidence(self):
        """The live registry contains "NUROTOCO OF MASSACHUSETTS", "SLACK TIDE
        CRUISES LLC" and "NOTIONSBYNANCY LLC". A name rule promotes all
        three."""
        for name in ("SLACK TIDE CRUISES LLC", "NOTIONSBYNANCY LLC",
                     "NUROTOCO OF MASSACHUSETTS", "SUPERIOR TECHNOLOGIES LLC"):
            ok, why = rep.is_evidence_backed_tech(_emp(display_name=name), {})
            assert not ok, (name, why)

    def test_an_sec_sic_code_is_evidence(self):
        ok, why = rep.is_evidence_backed_tech(
            _emp(display_name="Some Filer"), {},
            {"sic": "7372", "sic_description": "Services-Prepackaged Software",
             "cik": "0000123"})
        assert ok and "classifier" in why

    def test_a_technology_posting_mix_is_evidence(self):
        """The only signal that reaches a private company: EDGAR knows public
        filers and nothing else."""
        ok, why = rep.is_evidence_backed_tech(
            _emp(display_name="Private Co"),
            {"core": 40, "adjacent": 5, "non_tech": 10, "unknown": 3, "active": 58})
        assert ok, why

    def test_a_retail_posting_mix_is_not_evidence(self):
        ok, _ = rep.is_evidence_backed_tech(
            _emp(display_name="Shops Inc"),
            {"core": 0, "adjacent": 0, "non_tech": 200, "unknown": 40, "active": 240})
        assert not ok


class TestCohorts:
    def test_a_declared_cohort_wins(self):
        got = rep.cohorts_for(_emp(cohorts=["mnc"]), None, {})
        assert got == ["mnc"]

    def test_an_sec_filer_is_public(self):
        got = rep.cohorts_for(_emp(), {"cik": "0000123"}, {})
        assert got == [er.COHORT_PUBLIC_TECH]

    @pytest.mark.parametrize("field", ["catalog_keys", "catalog"])
    def test_an_mnc_catalog_row_is_documentary(self, field):
        """Both spellings. `catalog_keys` is canonical; `catalog` is what rows
        written before the field names were unified carry, and a report that
        reads only the new one shows a cohort emptying out mid-migration."""
        emp = _emp()
        emp["registry_refs"] = {field: ["mnc_catalog"]}
        assert er.COHORT_MNC in rep.cohorts_for(emp, {"cik": "1"}, {})

    def test_a_curated_private_company_splits_on_observed_hiring(self):
        curated = _emp(evidence=[{"kind": "curated_seed", "source": "s", "value": "ai-ml"}])
        assert rep.cohorts_for(curated, None, {"active": 120}) \
            == [er.COHORT_PRIVATE_GROWTH]
        assert rep.cohorts_for(curated, None, {"active": 3}) \
            == [er.COHORT_NICHE_STARTUP]

    def test_no_evidence_is_reported_as_unknown_not_spread_around(self):
        """`unknown` is a real answer. Filling the cohort table by guessing
        would make it look complete and mean nothing."""
        assert rep.cohorts_for(_emp(), None, {}) == [er.COHORT_UNKNOWN]


@pytest.fixture()
def db(mock_db):
    for c in ("employers", "career_sources", "employer_merge_reviews",
              "companies", "job_postings", "company_ats_endpoints"):
        mock_db[c].delete_many({})
    return mock_db


class TestReportArithmetic:
    def _seed(self, db, *, covered: int, uncovered: int):
        for i in range(covered + uncovered):
            emp = er.build_employer(
                display_name=f"Tech Co {i}",
                canonical_domain=f"techco{i}.example",
                evidence=[{"kind": "curated_seed", "source": "seed", "value": "saas"}])
            db.employers.insert_one(dict(emp))
            state = (css.STATE_VALIDATED if i < covered else css.STATE_CANDIDATE)
            db.career_sources.insert_one({
                "source_id": f"cs_{i}", "employer_id": emp["employer_id"],
                "ats_vendor": "greenhouse", "tenant": f"t{i}", "state": state,
                "ownership_verified": state == css.STATE_VALIDATED})

    def test_only_ownership_verified_sources_count(self, db):
        self._seed(db, covered=3, uncovered=7)
        built = rep.build(db, mix_days=30)
        summary = rep.report(db, built, mix_days=30)
        assert summary["registry"]["evidence_backed_technology_employers"] == 10
        assert summary["coverage"]["validated_career_sources"] == 3
        assert summary["coverage"]["validated_pct"] == 0.3

    def test_an_endpoint_that_merely_answered_is_reported_separately(self, db):
        """`company_ats_endpoints.status == "validated"` means the endpoint
        answered. It was reported AS coverage and is not coverage."""
        self._seed(db, covered=1, uncovered=1)
        db.company_ats_endpoints.insert_many(
            [{"member_key": f"m{i}", "status": "validated"} for i in range(50)])
        summary = rep.report(db, rep.build(db, mix_days=30), mix_days=30)
        assert summary["coverage"]["validated_career_sources"] == 1
        assert summary["endpoint_status_for_comparison"]["validated"] == 50

    def test_a_provider_failure_cannot_remove_an_employer_from_the_denominator(
            self, db):
        self._seed(db, covered=0, uncovered=4)
        db.career_sources.update_many({}, {"$set": {"state": css.STATE_DEAD}})
        summary = rep.report(db, rep.build(db, mix_days=30), mix_days=30)
        assert summary["registry"]["evidence_backed_technology_employers"] == 4
        assert sum(summary["coverage"]["by_state"].values()) == 4

    def test_an_employer_with_no_career_source_is_still_counted(self, db):
        emp = er.build_employer(
            display_name="Never Probed",
            canonical_domain="neverprobed.example",
            evidence=[{"kind": "curated_seed", "source": "seed", "value": "saas"}])
        db.employers.insert_one(dict(emp))
        summary = rep.report(db, rep.build(db, mix_days=30), mix_days=30)
        assert summary["registry"]["evidence_backed_technology_employers"] == 1
        assert summary["coverage"]["by_state"][css.STATE_CANDIDATE] == 1

    def test_a_merged_employer_is_not_counted_twice(self, db):
        parent = er.build_employer(
            display_name="Broadcom", canonical_domain="broadcom.example",
            evidence=[{"kind": "curated_seed", "source": "s", "value": "semis"}])
        child = er.build_employer(
            display_name="VMware", canonical_domain="vmware.example",
            operating_status=er.STATUS_ACQUIRED,
            parent_employer_id=parent["employer_id"],
            evidence=[{"kind": "curated_seed", "source": "s", "value": "cloud"}])
        db.employers.insert_many([dict(parent), dict(child)])
        summary = rep.report(db, rep.build(db, mix_days=30), mix_days=30)
        assert summary["registry"]["evidence_backed_technology_employers"] == 1

    def test_the_measurement_carries_its_provenance(self, db):
        self._seed(db, covered=1, uncovered=1)
        summary = rep.report(db, rep.build(db, mix_days=30), mix_days=30)
        assert summary["source_revision"]
        assert summary["generated_at"]
        assert summary["versions"]["taxonomy"]
        assert summary["versions"]["career_source"]

    def test_every_tier_a_employer_is_accounted_for(self, db):
        """The acceptance criterion: none silently disappears."""
        self._seed(db, covered=1, uncovered=1)
        summary = rep.report(db, rep.build(db, mix_days=30), mix_days=30)
        tier = summary["tier_a"]
        assert tier["total"] > 50
        assert len(tier["rows"]) == tier["total"]
        for row in tier["rows"]:
            assert row["state"], row
            assert row["display_name"] and row["domain"]

    def test_the_markdown_is_generated_and_says_so(self, db):
        self._seed(db, covered=2, uncovered=3)
        text = rep.render_markdown(
            rep.report(db, rep.build(db, mix_days=30), mix_days=30))
        assert "GENERATED FILE" in text
        assert "employer_coverage_report.py" in text
        assert "40.0%" in text
        # It must not promise what no denominator can support.
        assert "every technology company" in text
        assert "not a claim to cover every technology company" in text.lower()
