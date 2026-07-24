"""Backfill: project legacy application state onto the canonical `applications`
ledger (defect E, E2/E3).

Repeatable + idempotent. Two projections, each joined to a canonical row via
`tailoring_record_id` (primary) — the strongest, stable link E1 already writes:

  1. tailoring_records.application.{status,updated_at} -> the canonical OUTCOME
     axis, via application_ledger_service.set_outcome (find-or-create). Only
     statuses that carry a real employer-response signal are projected; draft /
     interested (no application yet) are SKIPPED (they add no funnel signal — the
     E3 dual-read falls back to the tailoring_records mirror for them anyway).
  2. live auto_submit_approvals (pending/approved) -> the canonical APPROVAL fold:
     if the approval maps to a canonical row still in `ready_for_review` (or the
     legacy `ready` peer), drive it through ledger.approve (F snapshot frozen).
     Best-effort; an approval that maps to no canonical row is left in place — the
     legacy doc is NEVER deleted (the mirror/legacy stores are kept until a later
     cleanup).

Idempotency: set_outcome upserts on (user_email, tailoring_record_id), and
ledger.approve is a status-guarded no-op on an already-approved row, so re-running
never creates a duplicate canonical row nor double-approves. On a clean / new
Mongo it is a pure no-op (nothing to iterate).

Usage:
    python scripts/backfill_applications_canonical.py            # apply
    python scripts/backfill_applications_canonical.py --dry-run  # preview only
"""
import sys
from pathlib import Path

# Mirror local.py path wiring so `utils.*` / `services.*` resolve from the shared
# layer (kept identical to scripts/migrate_indexes.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


# The coarse tracker/funnel status vocabulary -> the canonical OUTCOME axis. Only
# statuses that mean "there was an application + an employer-response signal" are
# projected; draft / interested carry no funnel signal and are intentionally
# absent (so they are skipped).
_STATUS_TO_OUTCOME = {
    "applied": "applied",
    "viewed": "applied",
    "interviewing": "interviewing",
    "offer": "offer",
    "rejected": "rejected",
    "ghosted": "ghosted",
    "withdrawn": "withdrawn",
}


def backfill_outcomes(db, als, dry_run: bool):
    """Project tailoring_records.application.status onto the canonical outcome
    axis. Returns (projected, skipped)."""
    projected = skipped = 0
    cursor = db.tailoring_records.find(
        {"deleted": {"$ne": True}},
        {"_id": 0, "record_id": 1, "user_email": 1, "application": 1},
    )
    for rec in cursor:
        record_id = rec.get("record_id")
        user_email = rec.get("user_email")
        app = rec.get("application") or {}
        status = str(app.get("status") or "").strip().lower()
        outcome = _STATUS_TO_OUTCOME.get(status)
        if not record_id or not user_email or not outcome:
            skipped += 1
            continue
        if dry_run:
            projected += 1
            continue
        try:
            als.set_outcome(user_email, record_id=record_id, outcome=outcome,
                            source="backfill")
            projected += 1
        except Exception as e:  # one bad row never aborts the sweep
            print(f"  [outcome-FAILED] {str(record_id)[:24]}: {e}")
            skipped += 1
    return projected, skipped


def backfill_approvals(db, als, dry_run: bool):
    """Fold live auto_submit_approvals into the canonical approval state. Returns
    (folded, skipped)."""
    folded = skipped = 0
    approvable = (als.STATUS_READY_FOR_REVIEW, als.STATUS_READY)
    cursor = db.auto_submit_approvals.find(
        {"status": {"$in": ["approved", "pending"]}},
        {"_id": 0, "application_id": 1, "user_email": 1},
    )
    for ap in cursor:
        user_email = ap.get("user_email")
        application_id = ap.get("application_id")
        if not user_email or not application_id:
            skipped += 1
            continue
        try:
            row = als.get_application(user_email, application_id)
        except Exception:
            row = None
        # Only fold an approval that maps to a canonical row awaiting approval; a
        # legacy approval for a non-ledger (extension-only) application id maps to
        # nothing and is left in place.
        if not row or row.get("status") not in approvable:
            skipped += 1
            continue
        if dry_run:
            folded += 1
            continue
        try:
            moved = als.approve(user_email, [application_id], approved_by="backfill")
            if moved:
                folded += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  [approval-FAILED] {str(application_id)[:24]}: {e}")
            skipped += 1
    return folded, skipped


def main():
    dry_run = "--dry-run" in sys.argv
    from utils.db_connect import DBConnect
    from services import application_ledger_service as als

    db = DBConnect().get_db()
    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"== backfill_applications_canonical ({mode}) on db '{db.name}' ==")

    o_projected, o_skipped = backfill_outcomes(db, als, dry_run)
    print(f"  outcomes:  projected={o_projected} skipped={o_skipped}")
    a_folded, a_skipped = backfill_approvals(db, als, dry_run)
    print(f"  approvals: folded={a_folded} skipped={a_skipped}")

    print("\nDone." + ("  (dry run — nothing written)" if dry_run else ""))


if __name__ == "__main__":
    main()
