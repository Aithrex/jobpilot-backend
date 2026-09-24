#!/usr/bin/env python3
"""Pin every `job_postings` row a USER is attached to.

`pin_posting` is called on the live paths - apply (`application_ledger_service.
_pin_source_posting`) and save (`job_service.save_job`) - but both are
best-effort and neither raises: a pin that fails because object storage was
briefly unavailable is logged and dropped, and the posting then ages out on the
ordinary 7-day schedule like any other row. Tailoring never pinned at all.

The result is silent and only visible right before a purge. Measured
2026-09-15, before the retention backlog was drained: 398 postings were
referenced by `saved_jobs`, `tailoring_records` or `applications`, 214 of them
still in the inventory, and 65 of those carried no `pinned` flag - 32 already
past the 7-day window. A retention sweep would have deleted exactly the rows
the policy exists to protect, and the user's own history would have rendered as
a dead reference.

This backfills the flag from the durable user-side collections, which are the
real record of attachment. It is additive: it sets `pinned` / `pin_reasons` and
deletes nothing.

On archiving
------------
`pin_posting` also writes an S3 archive, and `purge_expired` verifies that
archive before deleting a pinned row. This script sets the FLAG only, which is
the half that prevents deletion - a pinned row is retained whether or not the
archive exists, so the flag alone is sufficient to make retention safe. Run
this where `RESUME_S3_BUCKET` is configured and the next sweep will archive the
rows properly; run it anywhere and they are at least no longer deletable.

    python scripts/backfill_pins.py            # dry run
    python scripts/backfill_pins.py --apply
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from utils.db_connect import DBConnect  # noqa: E402

HEXID = re.compile(r"^[0-9a-f]{32}$")

#: Where a durable user attachment is recorded, and the `pin_reasons` value it
#: earns. Deliberately EXCLUDES `resume_jobs`: those rows are async job results
#: whose payloads embed whole search-result pages (1,380 distinct postings on
#: live data). A posting that merely appeared in somebody's match run is not
#: attached to them, and pinning it would pin most of the inventory.
SOURCES = (
    ("saved_jobs", "job_id", "saved"),
    ("tailoring_records", "source_job_id", "tailored"),
    ("applications", "posting_job_id", "applied"),
)


def _collect(db) -> dict:
    reasons: dict[str, set] = {}

    def add(value, reason):
        if isinstance(value, str) and HEXID.match(value):
            reasons.setdefault(value, set()).add(reason)

    for coll, field, reason in SOURCES:
        for value in db[coll].distinct(field):
            add(value, reason)

    # Snapshots store the postings as embedded card dicts, not as a flat id
    # list, so they need a walk rather than a distinct().
    for snap in db.job_match_snapshots.find({}, {"jobs": 1}):
        for job in (snap.get("jobs") or []):
            if isinstance(job, dict):
                add(job.get("job_id"), "snapshot")
    return reasons


def pin_user_attached(db, *, apply: bool, now=None) -> dict:
    """Pin every posting a user is attached to, and make every pinned posting
    undeletable. Idempotent; returns counts.

    "Undeletable" is the second half and it is not optional: retention is a TTL
    index on `expire_at` (`job_inventory_service.retention_expiry`), which
    ignores a document without the field. So a pin that leaves `expire_at` in
    place protects nothing. Two populations need the field removed - rows this
    run pins, and rows pinned before the TTL existed (or by code that predates
    it) that still carry one.
    """
    now = now or datetime.now(timezone.utc)
    reasons = _collect(db)
    ids = list(reasons)

    present = {}
    for i in range(0, len(ids), 500):
        for doc in db.job_postings.find(
                {"job_id": {"$in": ids[i:i + 500]}},
                {"job_id": 1, "pinned": 1, "_id": 0}):
            present[doc["job_id"]] = bool(doc.get("pinned"))
    todo = [k for k, is_pinned in present.items() if not is_pinned]
    still_expiring = {"pinned": True, "expire_at": {"$exists": True}}

    report = {
        "attached": len(ids),
        "present": len(present),
        "already_pinned": sum(1 for p in present.values() if p),
        "to_pin": len(todo),
        # Not recoverable from Mongo, and not an error: `saved_jobs.job_data`
        # and `tailoring_records.jd_text` each store their own copy, so the
        # user-facing history still renders.
        "gone": len(ids) - len(present),
        "pinned_but_expiring": db.job_postings.count_documents(still_expiring),
        "pinned": 0,
        "expiry_removed": 0,
    }
    if not apply:
        return report

    # One write per posting. The set is small (hundreds: it is bounded by what
    # users saved, tailored and applied to), so a bulk_write buys nothing.
    for job_id in todo:
        res = db.job_postings.update_one(
            {"job_id": job_id},
            {"$set": {"pinned": True, "pinned_at": now, "updated_at": now},
             "$addToSet": {"pin_reasons": {"$each": sorted(reasons[job_id])}},
             "$unset": {"expire_at": ""}},
        )
        report["pinned"] += int(res.modified_count or 0)

    res = db.job_postings.update_many(still_expiring, {"$unset": {"expire_at": ""}})
    report["expiry_removed"] = int(res.modified_count or 0)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = DBConnect().get_db()
    r = pin_user_attached(db, apply=args.apply)
    print(f"user-attached posting ids: {r['attached']:,}")
    print(f"  present in inventory : {r['present']:,}")
    print(f"  already pinned       : {r['already_pinned']:,}")
    print(f"  NEED PINNING         : {r['to_pin']:,}")
    print(f"  pinned but expiring  : {r['pinned_but_expiring']:,}")
    print(f"  gone from inventory  : {r['gone']:,} (already purged or pre-inventory)")
    if not args.apply:
        print("\nDRY RUN - rerun with --apply to write.")
        return 0
    print(f"\npinned {r['pinned']:,}; removed expiry from {r['expiry_removed']:,} pinned rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
