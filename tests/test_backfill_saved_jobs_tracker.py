"""Backfill: saved_jobs applied -> tailoring_records applied."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from services.job_service import JobService

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "backfill_saved_jobs_to_tracker",
    _ROOT / "scripts" / "backfill_saved_jobs_to_tracker.py",
)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)
backfill_saved_jobs_to_tracker = _mod.backfill_saved_jobs_to_tracker


USER_A = "alice@example.com"
USER_B = "bob@example.com"
JOB_A = "job-a"
JOB_B = "job-b"


def _posting(job_id: str, title: str, company: str):
    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "apply_link": f"https://example.com/{job_id}",
    }


def test_backfill_upgrades_draft_tracker_for_applied_saved_job(mock_db):
    mock_db["saved_jobs"].delete_many({})
    mock_db["tailoring_records"].delete_many({})
    svc = JobService()
    posting = _posting(JOB_A, "Engineer", "Acme")
    svc.save_job(posting, user_email=USER_A)
    mock_db["saved_jobs"].update_one(
        {"job_id": JOB_A, "user_email": USER_A},
        {"$set": {"status": "applied"}},
    )
    mock_db["tailoring_records"].insert_one({
        "record_id": "rec-draft",
        "user_email": USER_A,
        "jd_analysis": {"job_title": "Engineer", "company": "Acme"},
        "application": {"status": "draft"},
        "created_at": datetime.now(timezone.utc),
    })

    stats = backfill_saved_jobs_to_tracker(mock_db, dry_run=False)
    assert stats["synced"] == 1

    rec = mock_db["tailoring_records"].find_one({"record_id": "rec-draft"})
    assert rec["application"]["status"] == "applied"
    assert rec["source_job_id"] == JOB_A


def test_backfill_does_not_touch_other_users(mock_db):
    mock_db["saved_jobs"].delete_many({})
    mock_db["tailoring_records"].delete_many({})
    svc = JobService()
    svc.save_job(_posting(JOB_A, "Engineer", "Acme"), user_email=USER_A)
    svc.save_job(_posting(JOB_B, "Designer", "Beta"), user_email=USER_B)
    mock_db["saved_jobs"].update_many({}, {"$set": {"status": "applied"}})
    mock_db["tailoring_records"].insert_one({
        "record_id": "rec-b",
        "user_email": USER_B,
        "source_job_id": JOB_B,
        "application": {"status": "draft"},
        "created_at": datetime.now(timezone.utc),
    })

    stats = backfill_saved_jobs_to_tracker(
        mock_db, dry_run=False, user_email=USER_A,
    )
    assert stats["scanned"] == 1
    assert stats["synced"] >= 0

    rec_b = mock_db["tailoring_records"].find_one({"record_id": "rec-b"})
    assert rec_b["application"]["status"] == "draft"


def test_backfill_creates_stub_when_no_tracker_row(mock_db):
    mock_db["saved_jobs"].delete_many({})
    mock_db["tailoring_records"].delete_many({})
    mock_db["saved_jobs"].insert_one({
        "job_id": JOB_A,
        "user_email": USER_A,
        "status": "applied",
        "job_data": _posting(JOB_A, "Engineer", "Acme"),
        "saved_at": datetime.now(timezone.utc),
    })

    stats = backfill_saved_jobs_to_tracker(mock_db, dry_run=False)
    assert stats["synced"] == 1
    rec = mock_db["tailoring_records"].find_one({"source_job_id": JOB_A, "user_email": USER_A})
    assert rec is not None
    assert rec["application"]["status"] == "applied"


def test_backfill_dry_run_writes_nothing(mock_db):
    mock_db["saved_jobs"].delete_many({})
    mock_db["tailoring_records"].delete_many({})
    mock_db["saved_jobs"].insert_one({
        "job_id": JOB_A,
        "user_email": USER_A,
        "status": "applied",
        "job_data": _posting(JOB_A, "Engineer", "Acme"),
        "saved_at": datetime.now(timezone.utc),
    })

    stats = backfill_saved_jobs_to_tracker(mock_db, dry_run=True)
    assert stats["scanned"] == 1
    assert mock_db["tailoring_records"].count_documents({}) == 0
