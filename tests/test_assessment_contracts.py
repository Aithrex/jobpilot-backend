"""Package 1: assessment-v2 contract validation and display counting."""
from schemas.assessment_contracts import (
    ASSESSMENT_VERSION,
    SCHEMA_VERSION,
    AssessmentFinding,
    AssessmentResult,
    AssessmentSnapshot,
    CanonicalRequirement,
    FindingReference,
    RequirementCounts,
    RequirementEvaluation,
    StageStatus,
    atomic,
    count_evaluations,
    display_bucket,
    group_and,
    group_or,
    unresolved,
    validate_assessment_result,
)


def test_schema_version_is_four():
    assert SCHEMA_VERSION == 4
    assert ASSESSMENT_VERSION == "assessment-v2"


def test_result_overall_always_null():
    result = AssessmentResult(assessment_id="a1")
    payload = result.as_dict()
    assert payload["overall"] is None
    assert payload["schema_version"] == 4
    assert payload["assessment_version"] == "assessment-v2"
    assert "stages" in payload


def test_validate_rejects_numeric_overall():
    errs = validate_assessment_result({
        "assessment_id": "a1",
        "schema_version": 4,
        "assessment_version": "assessment-v2",
        "overall": 72,
        "stages": {
            "processing": "complete",
            "artifact": "passed",
            "source_check": "complete",
            "reader": "completed",
            "comparison": "comparable",
        },
    })
    assert any("overall" in e for e in errs)


def test_validate_accepts_null_overall():
    errs = validate_assessment_result({
        "assessment_id": "a1",
        "schema_version": 4,
        "assessment_version": "assessment-v2",
        "overall": None,
        "stages": {
            "processing": "complete",
            "artifact": "passed",
            "source_check": "complete",
            "reader": "completed",
            "comparison": "unavailable",
        },
    })
    assert errs == []


def test_validate_rejects_wrong_schema():
    errs = validate_assessment_result({
        "assessment_id": "a1",
        "schema_version": 3,
        "assessment_version": "evidence-v1",
        "overall": None,
        "stages": {},
    })
    assert any("schema_version" in e for e in errs)


def test_expression_helpers_serialize():
    expr = group_and(
        group_or(atomic("Python"), atomic("Java")),
        atomic("SQL", ownership_required=True),
    )
    d = expr.as_dict()
    assert d["kind"] == "and"
    assert d["children"][0]["kind"] == "or"
    assert d["children"][1]["predicate"]["capability"] == "SQL"
    assert d["children"][1]["predicate"]["ownership_required"] is True
    amb = unresolved("ambiguous and/or wording").as_dict()
    assert amb["kind"] == "unresolved"


def test_display_bucket_full_credit():
    assert display_bucket("satisfied", "supported", "supported_explicit") == (
        "supported_and_satisfied"
    )


def test_display_bucket_partial_listed():
    assert display_bucket("satisfied", "listed_only", "supported_explicit") == (
        "partial_or_related"
    )


def test_display_bucket_conflict():
    assert display_bucket("satisfied", "contradicted", "supported_explicit") == (
        "conflict"
    )


def test_display_bucket_unknown():
    assert display_bucket("unknown", "supported", "unverified") == "unknown"


def test_display_bucket_not_demonstrated():
    assert display_bucket("not_demonstrated", "not_evidenced", "unverified") == (
        "not_demonstrated"
    )


def test_count_each_top_level_once():
    reqs = [
        CanonicalRequirement(
            id="r1", display_text="Python", priority="required",
            kind="capability", expression=atomic("Python"),
        ),
        CanonicalRequirement(
            id="r2", display_text="Kafka preferred", priority="preferred",
            kind="capability", expression=atomic("Kafka"),
        ),
        CanonicalRequirement(
            id="r3", display_text="Domain", priority="unspecified",
            kind="domain", expression=atomic("payments"),
        ),
    ]
    evals = [
        RequirementEvaluation(
            requirement_id="r1", document_state="satisfied",
            source_state="supported", claim_state="supported_explicit",
            match_method="exact",
        ),
        RequirementEvaluation(
            requirement_id="r2", document_state="not_demonstrated",
            source_state="not_evidenced", claim_state="unverified",
            match_method="none",
        ),
        RequirementEvaluation(
            requirement_id="r3", document_state="partially_supported",
            source_state="related", claim_state="supported_semantic",
            match_method="semantic",
        ),
    ]
    counts = count_evaluations(reqs, evals)
    assert counts.required["supported_and_satisfied"] == 1
    assert counts.preferred["not_demonstrated"] == 1
    assert counts.unspecified["partial_or_related"] == 1
    # Mutually exclusive: each priority totals to number of reqs in that bucket
    assert sum(counts.required.values()) == 1
    assert sum(counts.preferred.values()) == 1
    assert sum(counts.unspecified.values()) == 1


def test_absence_finding_uses_search_scope_not_fake_quote():
    finding = AssessmentFinding(
        kind="absence",
        severity="material",
        explanation="NoSQL not found after searching skills and experience.",
        requirement_id="r1",
        references=[
            FindingReference(kind="search_scope", value="skills+experience"),
            FindingReference(kind="jd_requirement", value="NoSQL"),
        ],
    )
    d = finding.as_dict()
    assert all(r["kind"] != "resume_span" for r in d["references"])
    assert any(r["kind"] == "search_scope" for r in d["references"])


def test_stage_status_label():
    assert StageStatus(processing="complete").user_status_label() == "Review complete"
    assert StageStatus(artifact="failed").user_status_label() == "File needs attention"
    assert StageStatus(processing="partial").user_status_label() == "Review incomplete"


def test_snapshot_roundtrip():
    snap = AssessmentSnapshot(
        owner_scope="user:abc",
        record_id="rec1",
        version_id="v1",
        format="pdf",
        jd_hash="jd1",
        document_content_hash="doc1",
    )
    d = snap.as_dict()
    assert d["owner_scope"] == "user:abc"
    assert d["format"] == "pdf"
    assert d["policy_version"] == ASSESSMENT_VERSION


def test_counts_as_dict_empty_ok():
    assert isinstance(RequirementCounts().as_dict(), dict)
