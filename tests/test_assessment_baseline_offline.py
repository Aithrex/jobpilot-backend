"""Package 0 offline baseline: lock today's score/assessment payload shapes.

No live Bedrock. Stubs AI where the legacy path would call providers.
"""
from __future__ import annotations

import pytest

from services.resume_scorer import ResumeScorer
from services import tailoring_assessment as assessment
from fixtures.assessment.cases import SQL_NOSQL_JD, SQL_NOSQL_RESUME


@pytest.fixture
def offline_scorer(monkeypatch):
    """Disable paid / network AI sides of the hybrid scorer."""
    scorer = ResumeScorer()
    monkeypatch.setattr(scorer, "_ai_scores", lambda *a, **k: {})
    return scorer


def test_legacy_score_emits_numeric_overall(offline_scorer):
    """Non-evidence resume → hybrid path with numeric overall."""
    result = offline_scorer.score(SQL_NOSQL_RESUME, SQL_NOSQL_JD, pdf_bytes=None)
    assert isinstance(result.get("overall"), (int, float))
    assert result.get("schema_version") != 4
    assert "keyword_match" in result or "skills_alignment" in result


def test_evidence_score_payload_is_schema_3_null_overall(monkeypatch):
    """score_payload (evidence-v1) → schema 3, overall null — not assessment-v2."""
    monkeypatch.setattr(
        assessment, "assess",
        lambda resume, jd, data=None, error=None, run_reader=True: {
            "version": "evidence-v1",
            "content_hash": "c",
            "jd_hash": "j",
            "ready": False,
            "source_status": "ok",
            "analysis_completeness": "complete",
            "requirements": [],
            "artifact": {"format": "pdf", "status": "unchecked", "issues": [], "content_hash": "c"},
            "reader_review": {"status": "unavailable", "findings": []},
            "questions": [],
        },
    )
    payload = assessment.score_payload(SQL_NOSQL_RESUME, SQL_NOSQL_JD, None, None)
    assert payload["schema_version"] == 3
    assert payload["overall"] is None
    assert "document_assessment" in payload
    assert payload.get("assessment_version")  # evidence-v1 pin


def test_assessment_v2_discriminant_unused_by_current_scorer(offline_scorer):
    """Package 0 inventory: no current path emits schema_version 4."""
    result = offline_scorer.score(SQL_NOSQL_RESUME, SQL_NOSQL_JD, pdf_bytes=None)
    assert result.get("schema_version") != 4
    assert result.get("assessment_version") != "assessment-v2"
