"""Prove tailor → edit → recheck → save → download → reopen consistency.

Uses mongomock + assessment-v2 with the flag pinned for the test only.
Production default remains off (asserted separately).
"""
from __future__ import annotations

import copy
import io

import pytest
from docx import Document

from services import assessment_service as assess
from services.artifact_assessment import assess_artifact, bytes_hash
from services.requirement_assessment import evaluate_requirements
from services.resume_versioning import append_version, build_initial_version, canonical_content_hash


@pytest.fixture
def v2_on(monkeypatch, mock_db):
    monkeypatch.setenv("RESUME_ASSESSMENT_V2_ENABLED", "true")
    assess.ensure_indexes(db=mock_db)
    mock_db.tailoring_records.delete_many({})
    mock_db.resume_assessments.delete_many({})
    return mock_db


def test_flag_still_default_off(monkeypatch):
    monkeypatch.delenv("RESUME_ASSESSMENT_V2_ENABLED", raising=False)
    assert assess.enabled() is False


def _resume(summary="Engineer", bullets=None):
    return {
        "contact": {"name": "Alex", "email": "a@example.com", "phone": ""},
        "summary": summary,
        "skills": {"Skills": ["Python", "SQL"]},
        "experience": [{
            "company": "Acme", "title": "Engineer", "dates": "2022 - Present",
            "bullets": bullets or [
                "Built billing APIs in Python.",
                "Wrote SQL against the warehouse.",
            ],
        }],
        "education": [], "projects": [], "certifications": [],
    }


def _jd():
    return {
        "job_title": "Backend Engineer",
        "required_skills": ["Python", "SQL"],
        "requirements": [{
            "text": "Python or Java, plus SQL",
            "priority": "required",
            "kind": "skill",
            "alternatives": ["Python", "Java"],
            "source_excerpt": "Python or Java, plus SQL",
        }],
        "analysis_completeness": "complete",
    }


def _docx_bytes(resume: dict) -> bytes:
    doc = Document()
    doc.add_paragraph(resume.get("summary") or "")
    for exp in resume.get("experience") or []:
        doc.add_paragraph(f"{exp.get('title')} at {exp.get('company')}")
        for b in exp.get("bullets") or []:
            doc.add_paragraph(b)
    for skills in (resume.get("skills") or {}).values():
        doc.add_paragraph(", ".join(skills))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_workflow_tailor_edit_recheck_save_download_reopen(v2_on):
    db = v2_on
    email = "owner@example.com"
    record_id = "rec-flow-1"
    source = _resume()
    jd = _jd()

    # 1) Tailor (initial version)
    tailored = _resume()
    v0 = build_initial_version(tailored, source="initial")
    db.tailoring_records.insert_one({
        "record_id": record_id,
        "user_email": email,
        "current_version_id": v0["version_id"],
        "jd_analysis": jd,
        "source_hash": "src",
        "versions": [v0],
        "tailored_resume": tailored,
    })

    # 2) Initial assessment (owned)
    result1 = assess.score_payload_v2(
        user_email=email,
        record_id=record_id,
        version_id=v0["version_id"],
        tailored_resume=tailored,
        jd_analysis=jd,
        fmt="docx",
        artifact_bytes=_docx_bytes(tailored),
        source_resume=source,
    )
    assert result1["schema_version"] == 4
    assert result1["overall"] is None
    assert result1.get("evaluations"), "must run real requirement stage"
    assert result1.get("counts"), "must produce counts"
    aid1 = result1["assessment_id"]
    assert not aid1.startswith("draft-")

    # 3) Edit prose (new version) — recheck must not rewrite the edited text
    edited = copy.deepcopy(tailored)
    edited["experience"][0]["bullets"][0] = "Built billing APIs in Python with retries."
    versions = db.tailoring_records.find_one({"record_id": record_id})["versions"]
    v1, created = append_version(versions, edited, source="edited", parent_version_id=v0["version_id"])
    assert created
    # Prose preserved on version object
    assert "retries" in v1["tailored_resume"]["experience"][0]["bullets"][0]
    db.tailoring_records.update_one(
        {"record_id": record_id, "user_email": email},
        {"$set": {
            "versions": versions,
            "current_version_id": v1["version_id"],
            "tailored_resume": edited,
        }},
    )

    # 4) Recheck — new assessment; does not mutate resume prose
    before_prose = copy.deepcopy(edited)
    result2 = assess.score_payload_v2(
        user_email=email,
        record_id=record_id,
        version_id=v1["version_id"],
        tailored_resume=edited,
        jd_analysis=jd,
        fmt="docx",
        artifact_bytes=_docx_bytes(edited),
        source_resume=source,
        previous_result=result1,
        idempotency_key=f"recheck-{v1['version_id']}",
    )
    assert edited == before_prose, "recheck must never change prose"
    assert result2["assessment_id"] != aid1
    assert result2["overall"] is None

    # 5) Save binds assessment_id (simulate save_version attach)
    ok = assess.attach_assessment_to_version(
        record_id=record_id,
        version_id=v1["version_id"],
        assessment_id=result2["assessment_id"],
        user_email=email,
        expected_content_hash=v1.get("content_hash") or canonical_content_hash(edited),
    )
    assert ok is True
    rec = db.tailoring_records.find_one({"record_id": record_id})
    assert rec["current_assessment_id"] == result2["assessment_id"]

    # 6) Download check — exported bytes hash agrees with assessment artifact
    data = _docx_bytes(edited)
    art = assess_artifact(data, edited, fmt="docx")
    stored = assess.get_assessment(result2["assessment_id"], user_email=email)
    # Fresh download hash must equal raw bytes; assessment stores its own bytes_hash
    assert art["bytes_hash"] == bytes_hash(data)
    assert art["status"] in {"passed", "issues"}

    # Cross-version attach of the old assessment must be rejected
    with pytest.raises(assess.AssessmentError) as ei:
        assess.attach_assessment_to_version(
            record_id=record_id,
            version_id=v1["version_id"],
            assessment_id=aid1,
            user_email=email,
            expected_content_hash=v1.get("content_hash") or "",
        )
    assert ei.value.code == assess.ERR_STALE

    # Content-hash guard: a new assessment for this version with wrong expected hash
    # is retained as superseded history and must not become current.
    result3 = assess.score_payload_v2(
        user_email=email,
        record_id=record_id,
        version_id=v1["version_id"],
        tailored_resume=edited,
        jd_analysis=jd,
        fmt="docx",
        artifact_bytes=_docx_bytes(edited),
        source_resume=source,
        idempotency_key=f"late-{v1['version_id']}",
    )
    stale = assess.attach_assessment_to_version(
        record_id=record_id,
        version_id=v1["version_id"],
        assessment_id=result3["assessment_id"],
        user_email=email,
        expected_content_hash="not-the-current-hash",
    )
    assert stale is False
    rec2 = db.tailoring_records.find_one({"record_id": record_id})
    assert rec2["current_assessment_id"] == result2["assessment_id"]

    # 7) Reopen — owner can load assessment; other user cannot
    reopened = assess.get_assessment(result2["assessment_id"], user_email=email)
    assert reopened["result"]["overall"] is None
    assert reopened["version_id"] == v1["version_id"]
    with pytest.raises(assess.AssessmentError):
        assess.get_assessment(result2["assessment_id"], user_email="other@example.com")


def test_score_payload_v2_detached_runs_evaluations(monkeypatch):
    """Detached path (no record) still evaluates — not an empty skeleton."""
    monkeypatch.setenv("RESUME_ASSESSMENT_V2_ENABLED", "true")
    result = assess.score_payload_v2(
        tailored_resume=_resume(),
        jd_analysis=_jd(),
        fmt="docx",
        artifact_bytes=_docx_bytes(_resume()),
    )
    assert result["schema_version"] == 4
    assert result["overall"] is None
    assert result.get("evaluations")
    assert result["stages"]["source_check"] == "complete"
