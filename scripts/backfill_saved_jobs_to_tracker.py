"""Backfill: sync saved_jobs marked Applied onto each user's Apply → Tracker rows.

Before the Jobs apply-confirm fix (jobpilot-frontend 587d8fd), "Yes, I applied"
/ "Already applied" often updated saved_jobs without reliably calling
POST /jobs/tracker, so tailoring_records could stay on draft while the Jobs
Applied tab looked correct.

This sweep re-projects every saved_jobs row with status ``applied`` onto the
owner's tailoring_records via the same ``_sync_saved_job_to_tracker`` helper
the live API uses. Per-user isolation is absolute: each row is keyed by
``user_email`` + ``job_id``; one account never touches another's records.

Idempotent: re-running is safe. Locked tracker stages (interviewing, offer,
rejected, …) are never downgraded by ``_upsert_tracker_stub``. Rows with no
matching tracker card get a pipeline stub created.

Usage:
    python scripts/backfill_saved_jobs_to_tracker.py            # apply
    python scripts/backfill_saved_jobs_to_tracker.py --dry-run  # preview only
    python scripts/backfill_saved_jobs_to_tracker.py --user you@example.com
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

_APPLIED_STATUS = "applied"


def backfill_saved_jobs_to_tracker(db, *, dry_run: bool = False, user_email: str = ""):
    """Project saved_jobs.status=applied onto tailoring_records.

    Returns dict with scanned, synced, skipped, errors counts.
    """
    from services.job_service import (
        _find_tracker_records,
        _sync_saved_job_to_tracker,
        _TRACKER_RANK,
    )

    query = {"status": _APPLIED_STATUS}
    if user_email:
        query["user_email"] = user_email.strip().lower()

    scanned = synced = skipped = errors = 0
    col = db.tailoring_records

    cursor = db.saved_jobs.find(
        query,
        {
            "_id": 0,
            "user_email": 1,
            "job_id": 1,
            "status": 1,
            "job_data": 1,
        },
    )

    for row in cursor:
        scanned += 1
        email = str(row.get("user_email") or "").strip()
        job_id = str(row.get("job_id") or "").strip()
        status = str(row.get("status") or "").strip().lower()
        job_data = row.get("job_data") if isinstance(row.get("job_data"), dict) else {}

        if not email or not job_id or status != _APPLIED_STATUS:
            skipped += 1
            continue

        if dry_run:
            matched = _find_tracker_records(col, email, job_id, job_data)
            if not matched:
                synced += 1  # would create stub
                continue
            needs = False
            for rec in matched:
                current = ((rec.get("application") or {}).get("status") or "draft").lower()
                if current in {"interviewing", "interview", "offer", "rejected", "ghosted", "withdrawn"}:
                    continue
                if _TRACKER_RANK.get("applied", 0) > _TRACKER_RANK.get(current, 0):
                    needs = True
            synced += 1 if needs or not matched else 0
            skipped += 0 if needs or not matched else 1
            continue

        try:
            before = _find_tracker_records(col, email, job_id, job_data)
            before_statuses = [
                ((r.get("application") or {}).get("status") or "draft").lower()
                for r in before
            ]
            _sync_saved_job_to_tracker(email, job_id, status, job_data)
            after = _find_tracker_records(col, email, job_id, job_data)
            if not after and not before:
                synced += 1
                continue
            changed = False
            for rec in after:
                rid = rec.get("record_id")
                prior = next(
                    (r for r in before if r.get("record_id") == rid),
                    None,
                )
                new_status = ((rec.get("application") or {}).get("status") or "draft").lower()
                old_status = (
                    ((prior.get("application") or {}).get("status") or "draft").lower()
                    if prior else ""
                )
                if new_status != old_status or not prior:
                    changed = True
            if changed or (not before and after):
                synced += 1
            else:
                skipped += 1
        except Exception as e:  # noqa: BLE001 — one bad row never aborts the sweep
            errors += 1
            print(f"  [FAILED] {email} {job_id[:32]}: {e}")

    return {
        "scanned": scanned,
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
    }


def main():
    dry_run = "--dry-run" in sys.argv
    user = ""
    for i, arg in enumerate(sys.argv):
        if arg == "--user" and i + 1 < len(sys.argv):
            user = sys.argv[i + 1]
            break

    from utils.db_connect import DBConnect

    db = DBConnect().get_db()
    mode = "DRY RUN" if dry_run else "APPLY"
    scope = f"user={user}" if user else "all users"
    print(f"== backfill_saved_jobs_to_tracker ({mode}, {scope}) on db '{db.name}' ==")

    stats = backfill_saved_jobs_to_tracker(db, dry_run=dry_run, user_email=user)
    print(
        "  scanned={scanned} synced={synced} skipped={skipped} errors={errors}".format(
            **stats
        )
    )
    print("\nDone." + ("  (dry run — nothing written)" if dry_run else ""))


if __name__ == "__main__":
    main()
