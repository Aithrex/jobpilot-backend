"""Package 5: absence findings, comparison, top actions."""
from __future__ import annotations

from schemas.assessment_contracts import RequirementEvaluation
from services.assessment_comparison import (
    absence_finding,
    compare_assessments,
    derive_top_actions,
    reader_pass_deterministic,
)


def test_absence_has_no_resume_span():
    f = absence_finding(requirement_id="r1", requirement_text="SQL")
    kinds = [r.kind for r in f.references]
    assert "resume_span" not in kinds
    assert "search_scope" in kinds
    assert "jd_requirement" in kinds


def test_reader_pass_absence_and_actions():
    evals = [RequirementEvaluation(
        requirement_id="r1",
        document_state="not_demonstrated",
        source_state="not_evidenced",
        claim_state="unverified",
        match_method="none",
    )]
    out = reader_pass_deterministic(
        "Nurse with bedside care experience.",
        {"job_title": "Accountant"},
        evals,
        [{"id": "r1", "display_text": "GAAP"}],
    )
    assert out["status"] == "completed"
    assert out["findings"]
    assert all(
        not any(r.get("kind") == "resume_span" for r in f.get("references") or [])
        for f in out["findings"] if f.get("kind") == "absence"
    )
    assert len(out["top_actions"]) <= 3


def test_compare_compatible():
    before = {
        "assessment_version": "assessment-v2",
        "snapshot": {"jd_hash": "j1"},
        "evaluations": [{
            "requirement_id": "r1",
            "document_state": "not_demonstrated",
            "source_state": "not_evidenced",
        }],
        "counts": {"required": {"supported_and_satisfied": 0}},
    }
    after = {
        "assessment_version": "assessment-v2",
        "snapshot": {"jd_hash": "j1"},
        "evaluations": [{
            "requirement_id": "r1",
            "document_state": "satisfied",
            "source_state": "supported",
        }],
        "counts": {"required": {"supported_and_satisfied": 1}},
    }
    cmp = compare_assessments(before, after)
    assert cmp["status"] == "comparable"
    assert any(c.get("kind") == "coverage" for c in cmp["changes"])


def test_compare_jd_mismatch():
    cmp = compare_assessments(
        {"assessment_version": "assessment-v2", "snapshot": {"jd_hash": "a"}},
        {"assessment_version": "assessment-v2", "snapshot": {"jd_hash": "b"}},
    )
    assert cmp["status"] == "not_comparable"


def test_derive_top_actions_limit():
    from schemas.assessment_contracts import AssessmentFinding
    findings = [
        AssessmentFinding(kind="absence", severity="material", explanation="a",
                          suggested_action="Add SQL"),
        AssessmentFinding(kind="absence", severity="material", explanation="b",
                          suggested_action="Add Kafka"),
        AssessmentFinding(kind="wording", severity="advisory", explanation="c",
                          suggested_action="Clarify ownership"),
        AssessmentFinding(kind="strength", severity="informational", explanation="d",
                          suggested_action="Keep this"),
    ]
    actions = derive_top_actions(findings, limit=3)
    assert len(actions) == 3
