"""Assessment accuracy regressions — matching, denominators, source, routing.

These fixtures are representative. The screenshot's exact saved pair was not
available in this workspace; numbers below are not that screenshot.
"""
from __future__ import annotations

import copy
import io

import pytest
from docx import Document

from fixtures.assessment.cases import (
    SCREENSHOT_LIKE_JD,
    SCREENSHOT_LIKE_RESUME,
)
from services.keyword_gap_engine import KeywordGapEngine
from services.match_gap_reporter import (
    _correct_already_demonstrated,
    _coerce_tiers,
    build_match_gaps,
)
from services.requirement_assessment import (
    evaluate_requirements,
    parse_expression_from_text,
)
from services.requirement_matcher import (
    build_requirement_diagnostics,
    coverage_hits,
    coverage_score,
    locate_term_in_resume,
    match_term_in_text,
    predicate_identity,
    scored_term_lists,
    slash_alternatives,
)
from services.resume_scorer import ResumeScorer
from schemas.resume_schemas import build_resume_text


def _docx(resume: dict) -> bytes:
    doc = Document()
    doc.add_paragraph(resume.get("summary") or "")
    for exp in resume.get("experience") or []:
        doc.add_paragraph(f"{exp.get('title')} at {exp.get('company')}")
        for b in exp.get("bullets") or []:
            doc.add_paragraph(b)
    for proj in resume.get("projects") or []:
        doc.add_paragraph(proj.get("name") or "")
        for b in proj.get("bullets") or []:
            doc.add_paragraph(b)
    for cert in resume.get("certifications") or []:
        doc.add_paragraph(str(cert))
    for skills in (resume.get("skills") or {}).values():
        doc.add_paragraph(", ".join(skills))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Reproduction diagnostic (representative fixtures)
# ---------------------------------------------------------------------------

def test_reproduce_screenshot_like_mismatch_is_corrected():
    """Old get_all_forms path missed 'REST API Development'; matcher finds it."""
    from utils.keyword_normalizer import get_all_forms
    from services.resume_scorer import ResumeScorer as Scorer

    text = build_resume_text(SCREENSHOT_LIKE_RESUME)
    old_found = False
    for form in get_all_forms("REST API Development"):
        if Scorer._text_contains(text.lower(), form):
            old_found = True
            break
    new = match_term_in_text(text, "REST API Development")
    assert old_found is False, "fixture no longer reproduces the literal-phrase miss"
    assert new.found is True
    assert new.method in {"recognized_alias", "semantic", "exact"}


def test_requirement_diagnostic_rows_for_screenshot_like():
    rows = build_requirement_diagnostics(
        jd_analysis=SCREENSHOT_LIKE_JD,
        document_text=build_resume_text(SCREENSHOT_LIKE_RESUME),
        resume=SCREENSHOT_LIKE_RESUME,
        source_text=build_resume_text(SCREENSHOT_LIKE_RESUME),
        source_available=True,
    )
    by_text = {r.jd_text: r for r in rows}
    rest = by_text["REST API Development"]
    assert rest.classification == "required"
    assert rest.in_denominator is True
    assert rest.match_method != "none"
    assert rest.matched_span
    assert "fintech" in {r.jd_text.lower() for r in rows if r.classification == "context"}
    context = [r for r in rows if r.classification == "context"]
    assert all(r.in_denominator is False for r in context)


# ---------------------------------------------------------------------------
# 2. Matching and denominators
# ---------------------------------------------------------------------------

def test_rest_api_development_matches_explicit_endpoint_work():
    hit = match_term_in_text(
        "Developed REST endpoints for billing.",
        "REST API Development",
    )
    assert hit.found
    assert "REST" in hit.span or "rest" in hit.span.lower()


def test_openapi_slash_swagger_parses_as_or():
    expr = parse_expression_from_text("OpenAPI/Swagger")
    assert expr.kind == "or"
    caps = [c.predicate.capability for c in expr.children if c.predicate]
    assert {c.lower() for c in caps} >= {"openapi", "swagger"}
    assert slash_alternatives("OpenAPI/Swagger") == ["OpenAPI", "Swagger"]
    assert match_term_in_text("Published OpenAPI specifications.", "OpenAPI/Swagger").found
    assert not match_term_in_text("Built generic HTTP APIs.", "OpenAPI/Swagger").found


def test_postgres_alias_matches_postgresql():
    assert match_term_in_text("Tuned Postgres queries.", "PostgreSQL").found
    assert match_term_in_text("Tuned PostgreSQL queries.", "Postgres").found


def test_sql_does_not_match_nosql_and_java_does_not_match_javascript():
    assert not match_term_in_text("Operated MongoDB NoSQL clusters.", "SQL").found
    assert not match_term_in_text("Wrote JavaScript front ends.", "Java").found
    assert match_term_in_text("Wrote Java services.", "Java").found
    assert match_term_in_text("Wrote SQL against the warehouse.", "SQL").found


def test_keyword_once_versus_repeated_same_coverage():
    terms = ["Python"]
    once, _, _ = coverage_hits("Used Python.", terms)
    many, _, _ = coverage_hits("Python Python Python engineer. Used Python.", terms)
    assert coverage_score(once, []) == coverage_score(many, [])
    scorer = ResumeScorer()
    a = scorer._keyword_match_score("Used Python.", ["Python"])
    b = scorer._keyword_match_score("Python Python Python. Used Python.", ["Python"])
    assert a[0] == b[0]
    assert a[1] == b[1]


def test_context_terms_not_mandatory_skills():
    lists = scored_term_lists(SCREENSHOT_LIKE_JD)
    req = {t.lower() for t in lists["required"]}
    assert "fintech" not in req
    assert "distributed systems" not in req
    det = ResumeScorer()._deterministic_scores(SCREENSHOT_LIKE_RESUME, SCREENSHOT_LIKE_JD)
    missing = {m.lower() for m in det["missing_keywords"]}
    assert "fintech" not in missing
    assert "distributed systems" not in missing


def test_projects_and_certifications_included():
    hit = locate_term_in_resume(SCREENSHOT_LIKE_RESUME, "AWS Certified Cloud Practitioner")
    assert hit.found
    assert hit.section.startswith("certifications")
    proj = locate_term_in_resume(
        {
            "summary": "", "skills": {}, "experience": [], "education": [],
            "certifications": [],
            "projects": [{"name": "GraphQL gateway", "bullets": ["Owned the GraphQL schema."]}],
        },
        "GraphQL",
    )
    assert proj.found
    assert proj.section.startswith("projects")


def test_same_capability_different_qualifiers_independent():
    a = predicate_identity("Kafka", ownership_required=True, evidence_expectation="ownership")
    b = predicate_identity("Kafka", duration_required=True, evidence_expectation="duration")
    assert a != b
    jd = {
        "requirements": [
            {
                "text": "Own Kafka", "priority": "required", "kind": "capability",
                "ownership_required": True, "evidence_expectation": "ownership",
                "action": "owned",
            },
            {
                "text": "Kafka for 3 years", "priority": "required", "kind": "capability",
                "duration_required": True, "evidence_expectation": "duration",
            },
        ]
    }
    out = evaluate_requirements(
        document_text="Owned the Kafka platform end-to-end. Used Kafka.",
        jd=jd,
    )
    assert len(out["evaluations"]) == 2
    states = {e["requirement_id"]: e["document_state"] for e in out["evaluations"]}
    assert len(states) == 2


# ---------------------------------------------------------------------------
# 3–4. Source vs document
# ---------------------------------------------------------------------------

def test_source_supported_but_absent_from_final_resume():
    source = {
        "summary": "", "skills": {}, "education": [], "projects": [],
        "certifications": [],
        "experience": [{
            "company": "Acme", "title": "Eng",
            "bullets": ["Built billing APIs in Python and Kafka."],
        }],
    }
    final = {
        "summary": "Engineer.", "skills": {"S": ["Python"]}, "education": [],
        "projects": [], "certifications": [],
        "experience": [{
            "company": "Acme", "title": "Eng",
            "bullets": ["Built billing APIs in Python."],
        }],
    }
    out = evaluate_requirements(
        document_text="Built billing APIs in Python.",
        jd={"required_skills": ["Kafka"], "requirements": []},
        resume=final,
        source_resume=source,
    )
    ev = out["evaluations"][0]
    assert ev["document_state"] == "not_demonstrated"
    assert ev["source_state"] == "supported"


def test_missing_source_never_validates_generated_claims():
    generated = {
        "summary": "Kafka expert.",
        "skills": {"S": ["Kafka"]},
        "experience": [{"company": "X", "title": "Eng", "bullets": ["Owned Kafka."]}],
        "education": [], "projects": [], "certifications": [],
    }
    out = evaluate_requirements(
        document_text="Owned Kafka in production.",
        jd={"required_skills": ["Kafka"], "requirements": []},
        resume=generated,
        source_resume=None,
    )
    ev = out["evaluations"][0]
    assert ev["document_state"] in {"satisfied", "partially_supported"}
    assert ev["source_state"] == "unknown"
    # Empty source is explicit and still not the generated resume.
    empty = evaluate_requirements(
        document_text="Owned Kafka in production.",
        jd={"required_skills": ["Kafka"], "requirements": []},
        resume=generated,
        source_resume={"experience": [], "skills": {}, "summary": ""},
    )
    assert empty["evaluations"][0]["source_state"] == "not_evidenced"


def test_already_demonstrated_not_sent_to_gap_tiers():
    still, demonstrated = _correct_already_demonstrated(
        SCREENSHOT_LIKE_RESUME, ["REST API Development", "Kafka"],
    )
    assert any("REST" in d["item"] for d in demonstrated)
    assert "Kafka" in still
    tiers = _coerce_tiers({
        "defensible": [{
            "item": "REST API Development",
            "why": "The resume already shows explicit REST endpoint development.",
        }],
        "learnable": [],
        "stretch": [],
    }, ["REST API Development"])
    assert tiers["already_demonstrated"]
    assert not tiers["defensible"]


# ---------------------------------------------------------------------------
# 5. Scoring behavior
# ---------------------------------------------------------------------------

def test_legacy_score_has_no_frequency_bonus_or_domain_denominator(monkeypatch):
    scorer = ResumeScorer()
    monkeypatch.setattr(scorer, "_ai_scores", lambda *a, **k: {})
    once = dict(SCREENSHOT_LIKE_RESUME)
    many = copy.deepcopy(SCREENSHOT_LIKE_RESUME)
    many["summary"] = (many["summary"] + " REST REST REST PostgreSQL PostgreSQL")
    a = scorer.score(once, SCREENSHOT_LIKE_JD)
    b = scorer.score(many, SCREENSHOT_LIKE_JD)
    assert a["keyword_match"] == b["keyword_match"]
    assert a.get("coverage_kind") == "internal_document_coverage"
    assert a.get("vendor_scores_measured") is False
    assert "scanners" not in a or not a.get("vendor_scores_measured")
    assert a.get("format_score") in (None, 0)
    lists = scored_term_lists(SCREENSHOT_LIKE_JD)
    assert a["coverage_denominator"] == len(lists["required"])


def test_unavailable_ai_does_not_default_to_70(monkeypatch):
    scorer = ResumeScorer()

    def _boom(*a, **k):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(scorer, "_ai_scores", _boom)
    r = scorer.score(SCREENSHOT_LIKE_RESUME, SCREENSHOT_LIKE_JD)
    assert r.get("experience_relevance") in (None, 0) or r.get("experience_relevance") != 70
    # Combine stores None; schema coerce may drop it. Either way, not a success default.
    assert r.get("ai_screener") in (None, {},) or r.get("vendor_scores_measured") is False


# ---------------------------------------------------------------------------
# 6. Routing and revision binding
# ---------------------------------------------------------------------------

def test_v2_failure_never_falls_back_to_hybrid(monkeypatch):
    from services import assessment_service as assess

    monkeypatch.setenv("RESUME_ASSESSMENT_V2_ENABLED", "true")
    monkeypatch.setattr(assess, "enabled", lambda: True)

    def _boom(**kwargs):
        raise RuntimeError("v2 exploded")

    monkeypatch.setattr(assess, "score_payload_v2", _boom)
    result = ResumeScorer().score(SCREENSHOT_LIKE_RESUME, SCREENSHOT_LIKE_JD)
    assert result.get("schema_version") == 4
    assert result.get("assessment_version") == "assessment-v2"
    assert result.get("overall") is None
    assert result.get("stages", {}).get("processing") == "failed"
    assert "keyword_match" not in result or result.get("schema_version") == 4


def test_bullet_only_edit_changes_content_hash():
    from services.assessment_service import document_content_hash
    a = document_content_hash(SCREENSHOT_LIKE_RESUME)
    edited = copy.deepcopy(SCREENSHOT_LIKE_RESUME)
    edited["experience"][0]["bullets"][0] = "Developed REST endpoints with retries."
    b = document_content_hash(edited)
    assert a != b
    same_summary = copy.deepcopy(SCREENSHOT_LIKE_RESUME)
    same_summary["summary"] = SCREENSHOT_LIKE_RESUME["summary"]
    assert document_content_hash(same_summary) == a


def test_pdf_docx_and_saved_reopened_use_same_stages(monkeypatch, mock_db):
    from services import assessment_service as assess

    monkeypatch.setenv("RESUME_ASSESSMENT_V2_ENABLED", "true")
    assess.ensure_indexes(db=mock_db)
    email = "owner@example.com"
    record_id = "rec-acc-1"
    from services.resume_versioning import build_initial_version
    v0 = build_initial_version(SCREENSHOT_LIKE_RESUME, source="initial")
    mock_db.tailoring_records.insert_one({
        "record_id": record_id,
        "user_email": email,
        "current_version_id": v0["version_id"],
        "jd_analysis": SCREENSHOT_LIKE_JD,
        "source_hash": "src",
        "versions": [v0],
        "tailored_resume": SCREENSHOT_LIKE_RESUME,
    })
    docx = assess.score_payload_v2(
        user_email=email,
        record_id=record_id,
        version_id=v0["version_id"],
        tailored_resume=SCREENSHOT_LIKE_RESUME,
        jd_analysis=SCREENSHOT_LIKE_JD,
        fmt="docx",
        artifact_bytes=_docx(SCREENSHOT_LIKE_RESUME),
        source_resume=SCREENSHOT_LIKE_RESUME,
        idempotency_key="docx-1",
    )
    reopened = assess.get_assessment(docx["assessment_id"], user_email=email)
    assert reopened["result"]["evaluations"] == docx["evaluations"]
    assert reopened["result"]["counts"] == docx["counts"]
    assert docx["schema_version"] == 4
    assert docx["overall"] is None
    detached = assess.score_payload_v2(
        tailored_resume=SCREENSHOT_LIKE_RESUME,
        jd_analysis=SCREENSHOT_LIKE_JD,
        fmt="docx",
        artifact_bytes=_docx(SCREENSHOT_LIKE_RESUME),
        source_resume=SCREENSHOT_LIKE_RESUME,
    )
    assert detached["schema_version"] == 4
    assert {e["document_state"] for e in detached["evaluations"]} == {
        e["document_state"] for e in docx["evaluations"]
    }


def test_gap_engine_uses_same_matcher_as_scorer():
    gap = KeywordGapEngine().analyze(SCREENSHOT_LIKE_RESUME, SCREENSHOT_LIKE_JD)
    required_missing = {m.lower() for m in gap["required_missing"]}
    assert "rest api development" not in required_missing
    assert "postgresql" not in required_missing


def test_build_match_gaps_without_model_marks_demonstrated(monkeypatch):
    class FakeEngine:
        def analyze(self, *a, **k):
            return {
                "required_missing": ["REST API Development", "Kafka"],
                "required_coverage": 50,
                "coverage_percentage": 50,
            }

    import services.keyword_gap_engine as kge
    monkeypatch.setattr(kge, "KeywordGapEngine", FakeEngine)
    report = build_match_gaps(
        SCREENSHOT_LIKE_RESUME, SCREENSHOT_LIKE_JD, gap=FakeEngine().analyze(),
    )
    items = {d["item"] for d in report.get("already_demonstrated") or []}
    assert any("REST" in i for i in items)
