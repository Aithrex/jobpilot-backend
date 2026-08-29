"""Backfill: label every existing application with the civil day it happened on.

``tailoring_records.application.applied_at`` is a UTC instant. The tracker's
applied-date filter needs a *civil day* — "which day did I apply?" — which only
means something relative to a timezone. Deriving it from the UTC instant at read
time makes an 8pm-in-Phoenix application show up as the next day, so the day is
stored once as a plain ``YYYY-MM-DD`` string and frozen.

New writes get their day from the browser's zone. Historical rows have no
browser to ask, so the owner's zone is resolved from stored state via
``services.user_time.resolve_user_timezone``:

    notification_prefs.quiet_hours.tz -> parse_streaks.tz -> America/New_York

Idempotent and repeatable: only rows that have an ``applied_at`` and are missing
``applied_date`` are touched, so re-running writes nothing. On a clean / new
Mongo it is a pure no-op. Records with no ``applied_at`` are skipped — they were
never applied to.

Usage:
    python scripts/backfill_applied_date.py            # apply
    python scripts/backfill_applied_date.py --dry-run  # preview only
"""
import sys
from pathlib import Path

# Mirror local.py path wiring so `utils.*` / `services.*` resolve from the shared
# layer (kept identical to scripts/backfill_applications_canonical.py).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def backfill_applied_dates(db, user_time, dry_run: bool):
    """Stamp application.applied_date / applied_tz on rows that lack them.

    Returns (labelled, skipped, users_seen).
    """
    labelled = skipped = 0
    # One zone lookup per user, not per record — a heavy user has hundreds of
    # rows and the zone is the same for all of them.
    tz_cache = {}

    cursor = db.tailoring_records.find(
        {
            "application.applied_at": {"$ne": None, "$exists": True},
            "application.applied_date": {"$in": [None, ""]},
        },
        {"_id": 0, "record_id": 1, "user_email": 1, "application.applied_at": 1},
    )

    for rec in cursor:
        record_id = rec.get("record_id")
        user_email = rec.get("user_email")
        applied_at = (rec.get("application") or {}).get("applied_at")
        if not record_id or not user_email or applied_at is None:
            skipped += 1
            continue
        if not hasattr(applied_at, "astimezone"):
            # A pre-existing string timestamp — leave it for the read-time
            # fallback rather than guessing at its format here.
            skipped += 1
            continue

        if user_email not in tz_cache:
            tz_cache[user_email] = user_time.resolve_user_timezone(user_email)
        tz_name = tz_cache[user_email]

        if dry_run:
            labelled += 1
            continue

        try:
            db.tailoring_records.update_one(
                {"record_id": record_id, "user_email": user_email},
                {"$set": {
                    "application.applied_date": user_time.civil_day(applied_at, tz_name),
                    "application.applied_tz": tz_name,
                }},
            )
            labelled += 1
        except Exception as e:  # one bad row never aborts the sweep
            print(f"  [FAILED] {str(record_id)[:24]}: {e}")
            skipped += 1

    return labelled, skipped, len(tz_cache)


def main():
    dry_run = "--dry-run" in sys.argv
    from utils.db_connect import DBConnect
    from services import user_time

    db = DBConnect().get_db()
    mode = "DRY RUN" if dry_run else "APPLY"
    print(f"== backfill_applied_date ({mode}) on db '{db.name}' ==")

    labelled, skipped, users = backfill_applied_dates(db, user_time, dry_run)
    print(f"  applied_date: labelled={labelled} skipped={skipped} users={users}")

    print("\nDone." + ("  (dry run — nothing written)" if dry_run else ""))


if __name__ == "__main__":
    main()
