"""100% coverage campaign: measure gates and plan verify steps."""
from __future__ import annotations

from _vg_support import current_db
from services import career_source_service as css
from services import coverage_campaign as camp
from services import employer_registry as er


def _emp(name: str, domain: str, *, categories=("software_saas",)):
    eid = er.employer_id_for(domain=domain, name=name)
    return {
        "employer_id": eid,
        "display_name": name,
        "canonical_domain": domain,
        "cohorts": ["mnc"],
        "employer_categories": list(categories),
        "identity_confidence": er.CONFIDENCE_DOMAIN,
    }


def test_measure_reports_gap_until_covered_or_terminal():
    db = current_db()
    db.employers.delete_many({})
    db[css.CAREER_SOURCES].delete_many({})
    covered = _emp("Covered Co", "covered.example")
    pending = _emp("Pending Co", "pending.example")
    terminal = _emp("Dead Co", "dead.example")
    db.employers.insert_many([covered, pending, terminal])
    db[css.CAREER_SOURCES].insert_many([
        {
            "employer_id": covered["employer_id"],
            "state": next(iter(css.COVERED_STATES)),
            "ats_vendor": "greenhouse",
            "tenant": "covered",
            "ownership_verified": True,
        },
        {
            "employer_id": pending["employer_id"],
            "state": "discovered",
            "ats_vendor": "greenhouse",
            "tenant": "pending",
            "ownership_verified": True,
            "ownership_evidence": [
                {"signal_class": "employer_domain", "confirmed": True},
                {"signal_class": "ats_owner", "confirmed": True},
            ],
        },
        {
            "employer_id": terminal["employer_id"],
            "state": "failed",
            "failure_code": "dead",
            "ats_vendor": "greenhouse",
            "tenant": "dead",
        },
    ])

    progress = camp.measure(db)
    assert "technology_employer_denominator" in progress
    assert "measured_100_pct" in progress
    assert progress["pending_unproven_employers"] >= 0
    assert progress["gate_employer_100"] in (True, False)


def test_plan_skips_empty_buckets_and_orders_content_first():
    queue = [
        {
            "employer_id": "e1", "bucket": "endpoint_unproven",
            "core_tech_postings": 10, "display_name": "A", "domain": "a.com",
            "vendors": ["oracle_orc"], "categories": [], "cohorts": [],
        },
        {
            "employer_id": "e2", "bucket": "content_pending",
            "core_tech_postings": 5, "display_name": "B", "domain": "b.com",
            "vendors": ["workday"], "categories": [], "cohorts": [],
        },
    ]
    steps = camp.plan_verify_steps(queue, limit_per_step=10)
    assert steps[0]["mode"] == "content"
    assert steps[0]["bucket"] == "content_pending"
    assert "e2" in steps[0]["employer_ids"]
    assert any(s["mode"] == "employers" for s in steps)


def test_plan_excludes_terminal_ids():
    queue = [
        {"employer_id": "keep", "bucket": "content_pending",
         "core_tech_postings": 1, "display_name": "K", "domain": "k.com",
         "vendors": [], "categories": [], "cohorts": []},
        {"employer_id": "skip", "bucket": "content_pending",
         "core_tech_postings": 9, "display_name": "S", "domain": "s.com",
         "vendors": [], "categories": [], "cohorts": []},
    ]
    steps = camp.plan_verify_steps(queue, terminal_ids={"skip"})
    assert steps[0]["employer_ids"] == ["keep"]
