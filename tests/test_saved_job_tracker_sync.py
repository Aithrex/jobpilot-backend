"""Marking a saved job Applied must move the Apply → Tracker card too.

Listing Apply used to write only saved_jobs. Tracker reads tailoring_records
(application.status). Single-job tailor never stored source_job_id, so the
kanban stayed on Draft (or Interested, which the UI also buckets as Draft).
"""
from datetime import datetime, timezone

from services.job_service import JobService, _norm_job_label


USER = "tracker-sync@example.com"
JOB_ID = "career-pages:req-42"


def _posting():
    return {
        "job_id": JOB_ID,
        "title": "Platform Engineer",
        "company": "Acme Corp",
        "apply_link": "https://acme.example/apply/42",
    }


def test_norm_job_label_ignores_punctuation():
    assert _norm_job_label("Acme Corp") == _norm_job_label("acme-corp")


def test_applied_saved_job_moves_unlinked_tailoring_record(mock_db):
    mock_db["saved_jobs"].delete_many({})
    mock_db["tailoring_records"].delete_many({})
    svc = JobService()
    svc.save_job(_posting(), user_email=USER)
    mock_db["tailoring_records"].insert_one({
        "record_id": "rec-1",
        "user_email": USER,
        "jd_analysis": {"job_title": "Platform Engineer", "company": "Acme Corp"},
        "application": {"status": "draft"},
        "created_at": datetime.now(timezone.utc),
    })

    svc.update_saved_job(JOB_ID, status="applied", user_email=USER)

    rec = mock_db["tailoring_records"].find_one({"record_id": "rec-1"})
    assert rec["application"]["status"] == "applied"
    assert rec["source_job_id"] == JOB_ID


def test_does_not_downgrade_interviewing_tracker_card(mock_db):
    mock_db["saved_jobs"].delete_many({})
    mock_db["tailoring_records"].delete_many({})
    svc = JobService()
    svc.save_job(_posting(), user_email=USER)
    mock_db["tailoring_records"].insert_one({
        "record_id": "rec-2",
        "user_email": USER,
        "source_job_id": JOB_ID,
        "jd_analysis": {"job_title": "Platform Engineer", "company": "Acme Corp"},
        "application": {"status": "interviewing"},
        "created_at": datetime.now(timezone.utc),
    })

    svc.update_saved_job(JOB_ID, status="applied", user_email=USER)

    rec = mock_db["tailoring_records"].find_one({"record_id": "rec-2"})
    assert rec["application"]["status"] == "interviewing"
