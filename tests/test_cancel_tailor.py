"""Cancel-in-flight tailoring: refund, no tracker draft, cooperative stop."""
from datetime import datetime, timezone

import pytest

from services import quota_ledger_service as ql
from services.resume_finalizer import JobCancelled, check_cancelled
from utils import db_connect

EMAIL = "cancel.user@example.com"

COLLECTIONS = (
    "users",
    "resume_jobs",
    "tailoring_records",
    "saved_jobs",
    "quota_counters",
    "quota_ledger",
)


@pytest.fixture()
def db():
    database = db_connect.DBConnect().get_db()
    for c in COLLECTIONS:
        database[c].delete_many({})
    yield database


def _counter_count(db):
    row = db.quota_counters.find_one({"_id": ql.counter_id_for(EMAIL)})
    return int((row or {}).get("count") or 0)


def test_cancel_refunds_exactly_one_credit(db):
    db.users.insert_one({"email": EMAIL, "plan": "free"})
    _, entries, _ = ql.reserve_tailor(EMAIL)
    eid = entries[0]
    assert _counter_count(db) == 1

    import services.resume_service as rs
    svc = rs.get_resume_service()
    job_id = svc.create_job(
        "extract_and_tailor",
        {"user_email": EMAIL, "quota_entry_id": eid, "job_description": "x"},
        user_email=EMAIL,
    )
    ql.attach_job(eid, job_id)

    job = svc.cancel_job(job_id, user_email=EMAIL)
    assert job is not None
    assert job["status"] == "cancelled"
    assert job.get("cancel_requested") is True
    assert _counter_count(db) == 0
    entry = db.quota_ledger.find_one({"entry_id": eid})
    assert entry["state"] == "refunded"

    # Idempotent: second cancel / refund is a no-op.
    again = svc.cancel_job(job_id, user_email=EMAIL)
    assert again["status"] == "cancelled"
    assert ql.refund_entry(eid, user_email=EMAIL) is False
    assert _counter_count(db) == 0


def test_cancel_does_not_advance_saved_job_to_draft(db):
    db.saved_jobs.insert_one({
        "job_id": "job-saved-1",
        "user_email": EMAIL,
        "status": "saved",
        "job_data": {"title": "Nurse", "company": "Clinic"},
        "saved_at": datetime.now(timezone.utc),
    })
    db.users.insert_one({"email": EMAIL, "plan": "free"})
    _, entries, _ = ql.reserve_tailor(EMAIL)
    import services.resume_service as rs
    svc = rs.get_resume_service()
    job_id = svc.create_job(
        "extract_and_tailor",
        {
            "user_email": EMAIL,
            "quota_entry_id": entries[0],
            "source_job_id": "job-saved-1",
        },
        user_email=EMAIL,
    )
    svc.cancel_job(job_id, user_email=EMAIL)

    assert db.tailoring_records.count_documents({"user_email": EMAIL}) == 0
    saved = db.saved_jobs.find_one({"job_id": "job-saved-1", "user_email": EMAIL})
    assert saved["status"] == "saved"


def test_cancel_does_not_demote_applied_stub(db):
    db.tailoring_records.insert_one({
        "record_id": "rec-applied",
        "user_email": EMAIL,
        "source_job_id": "job-a",
        "application": {"status": "applied"},
        "created_at": datetime.now(timezone.utc),
    })
    db.users.insert_one({"email": EMAIL, "plan": "free"})
    _, entries, _ = ql.reserve_tailor(EMAIL)
    import services.resume_service as rs
    svc = rs.get_resume_service()
    job_id = svc.create_job(
        "extract_and_tailor",
        {"user_email": EMAIL, "quota_entry_id": entries[0]},
        user_email=EMAIL,
    )
    svc.cancel_job(job_id, user_email=EMAIL)
    rec = db.tailoring_records.find_one({"record_id": "rec-applied"})
    assert rec["application"]["status"] == "applied"


def test_cancel_racing_complete_settles_once(db):
    db.users.insert_one({"email": EMAIL, "plan": "free"})
    _, entries, _ = ql.reserve_tailor(EMAIL)
    eid = entries[0]
    import services.resume_service as rs
    svc = rs.get_resume_service()
    job_id = svc.create_job(
        "tailor",
        {"user_email": EMAIL, "quota_entry_id": eid, "jd_analysis": {}},
        user_email=EMAIL,
    )
    # Cancel wins first.
    svc.cancel_job(job_id, user_email=EMAIL)
    svc.complete_job(job_id, {"record_id": "should-not-settle"})
    job = svc.get_job(job_id, user_email=EMAIL)
    assert job["status"] == "cancelled"
    entry = db.quota_ledger.find_one({"entry_id": eid})
    assert entry["state"] == "refunded"
    assert _counter_count(db) == 0


def test_check_cancelled_raises_after_cancel(db):
    db.users.insert_one({"email": EMAIL, "plan": "free"})
    import services.resume_service as rs
    svc = rs.get_resume_service()
    job_id = svc.create_job("tailor", {"user_email": EMAIL}, user_email=EMAIL)
    check_cancelled(job_id)  # still processing — no raise
    svc.cancel_job(job_id, user_email=EMAIL)
    with pytest.raises(JobCancelled):
        check_cancelled(job_id)


def test_cancel_refuses_foreign_owner(db):
    db.users.insert_one({"email": EMAIL, "plan": "free"})
    db.users.insert_one({"email": "other@example.com", "plan": "free"})
    _, entries, _ = ql.reserve_tailor(EMAIL)
    import services.resume_service as rs
    svc = rs.get_resume_service()
    job_id = svc.create_job(
        "tailor",
        {"user_email": EMAIL, "quota_entry_id": entries[0]},
        user_email=EMAIL,
    )
    missing = svc.cancel_job(job_id, user_email="other@example.com")
    assert missing is None
    still = svc.get_job(job_id, user_email=EMAIL)
    assert still["status"] == "processing"
    assert _counter_count(db) == 1
