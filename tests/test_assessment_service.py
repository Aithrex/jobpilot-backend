"""Package 2: ownership, idempotency, empty-stage assessment persistence."""
from __future__ import annotations

import pytest

from services import assessment_service as svc


@pytest.fixture
def assessment_db(mock_db, monkeypatch):
    monkeypatch.setenv("RESUME_ASSESSMENT_V2_ENABLED", "true")
    svc.ensure_indexes(db=mock_db)
    mock_db.tailoring_records.delete_many({})
    mock_db.resume_assessments.delete_many({})
    return mock_db


def _seed_record(db, *, email="owner@example.com", record_id="rec-1",
                 version_id="ver-1", content_hash="hash-a"):
    db.tailoring_records.insert_one({
        "record_id": record_id,
        "user_email": email,
        "current_version_id": version_id,
        "jd_analysis": {"job_title": "Engineer", "required_skills": ["Python"]},
        "source_hash": "src-1",
        "versions": [{
            "version_id": version_id,
            "version_number": 1,
            "content_hash": content_hash,
            "tailored_resume": {
                "contact": {"name": "A", "email": email},
                "summary": "Engineer",
                "skills": {"Skills": ["Python"]},
                "experience": [],
                "education": [],
                "projects": [],
                "certifications": [],
            },
        }],
    })


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("RESUME_ASSESSMENT_V2_ENABLED", raising=False)
    assert not svc.enabled()


def test_flag_on(monkeypatch):
    monkeypatch.setenv("RESUME_ASSESSMENT_V2_ENABLED", "true")
    assert svc.enabled()


def test_cross_user_rejected(assessment_db):
    _seed_record(assessment_db)
    with pytest.raises(svc.AssessmentError) as ei:
        svc.create_or_get_assessment(
            user_email="other@example.com",
            record_id="rec-1",
            idempotency_key="k1",
        )
    assert ei.value.code == svc.ERR_NOT_FOUND


def test_forged_missing_auth(assessment_db):
    _seed_record(assessment_db)
    with pytest.raises(svc.AssessmentError) as ei:
        svc.create_or_get_assessment(record_id="rec-1")
    assert ei.value.code == svc.ERR_FORBIDDEN


def test_stale_version_rejected(assessment_db):
    _seed_record(assessment_db)
    with pytest.raises(svc.AssessmentError) as ei:
        svc.create_or_get_assessment(
            user_email="owner@example.com",
            record_id="rec-1",
            version_id="does-not-exist",
        )
    assert ei.value.code == svc.ERR_STALE


def test_idempotent_same_inputs(assessment_db):
    _seed_record(assessment_db)
    a = svc.create_or_get_assessment(
        user_email="owner@example.com",
        record_id="rec-1",
        idempotency_key="idem-1",
    )
    b = svc.create_or_get_assessment(
        user_email="owner@example.com",
        record_id="rec-1",
        idempotency_key="idem-1",
    )
    assert a["assessment_id"] == b["assessment_id"]


def test_idempotency_conflict_different_inputs(assessment_db):
    _seed_record(assessment_db, content_hash="hash-a")
    svc.create_or_get_assessment(
        user_email="owner@example.com",
        record_id="rec-1",
        idempotency_key="idem-2",
    )
    # Change document hash on the version so fingerprint differs.
    assessment_db.tailoring_records.update_one(
        {"record_id": "rec-1"},
        {"$set": {"versions.0.content_hash": "hash-b"}},
    )
    with pytest.raises(svc.AssessmentError) as ei:
        svc.create_or_get_assessment(
            user_email="owner@example.com",
            record_id="rec-1",
            idempotency_key="idem-2",
        )
    assert ei.value.code == svc.ERR_IDEMPOTENCY_CONFLICT


def test_empty_stages_no_overall(assessment_db):
    _seed_record(assessment_db)
    doc = svc.create_or_get_assessment(
        user_email="owner@example.com",
        record_id="rec-1",
    )
    finished = svc.run_empty_stages(doc["assessment_id"])
    result = finished["result"]
    assert result["overall"] is None
    assert result["schema_version"] == 4
    assert result["assessment_version"] == "assessment-v2"
    assert result["stages"]["processing"] == "partial"
    assert result["stages"]["artifact"] == "unchecked"
    assert finished["status"] == "partial"


def test_attach_and_stale_attach(assessment_db):
    _seed_record(assessment_db, content_hash="hash-a")
    doc = svc.create_or_get_assessment(
        user_email="owner@example.com",
        record_id="rec-1",
        version_id="ver-1",
    )
    ok = svc.attach_assessment_to_version(
        record_id="rec-1",
        version_id="ver-1",
        assessment_id=doc["assessment_id"],
        user_email="owner@example.com",
        expected_content_hash="hash-a",
    )
    assert ok is True
    rec = assessment_db.tailoring_records.find_one({"record_id": "rec-1"})
    assert rec.get("current_assessment_id") == doc["assessment_id"]

    stale = svc.attach_assessment_to_version(
        record_id="rec-1",
        version_id="ver-1",
        assessment_id=doc["assessment_id"],
        user_email="owner@example.com",
        expected_content_hash="wrong-hash",
    )
    assert stale is False


def test_get_assessment_owner_scoped(assessment_db):
    _seed_record(assessment_db)
    doc = svc.create_or_get_assessment(
        user_email="owner@example.com",
        record_id="rec-1",
    )
    got = svc.get_assessment(doc["assessment_id"], user_email="owner@example.com")
    assert got["assessment_id"] == doc["assessment_id"]
    with pytest.raises(svc.AssessmentError):
        svc.get_assessment(doc["assessment_id"], user_email="other@example.com")


def test_disabled_without_pin(monkeypatch, assessment_db):
    monkeypatch.setenv("RESUME_ASSESSMENT_V2_ENABLED", "false")
    _seed_record(assessment_db)
    with pytest.raises(svc.AssessmentError) as ei:
        svc.create_or_get_assessment(
            user_email="owner@example.com",
            record_id="rec-1",
            pin_policy=False,
        )
    assert ei.value.code == svc.ERR_DISABLED
