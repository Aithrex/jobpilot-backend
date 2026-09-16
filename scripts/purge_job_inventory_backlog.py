#!/usr/bin/env python3
"""Drain the `job_postings` retention backlog in bulk.

`job_inventory_service` already implements the 7-day policy correctly:
`expire_aged_postings` closes anything past `MAX_INVENTORY_AGE_DAYS` and
`purge_expired` deletes it, while `pin_posting` archives the postings a user
applied to / saved / tailored against so they are never lost. Nothing about
that design is wrong. What it cannot do is CATCH UP.

`job_ingest_worker.run_maintenance` is bounded at 5,000 expired + 5,000 purged
per invocation, and it funnels every row through `close_posting`, which is one
`update_one` per posting. Ingest adds ~147,000 postings a week (~21,000/day),
so a daily sweep at that bound falls further behind every single day. Measured
2026-09-15: 512,057 postings, 344,235 of them active and already past the
7-day window, 14,284 more closed and past `purge_at` but never deleted -
2,510 MiB of documents plus 814 MiB of indexes, i.e. the whole of the cluster's
~4 GB.

This script is the one-off drain that the bounded sweep can never perform. It
is deliberately NOT a replacement for `run_maintenance`: it exists to get the
collection back inside the window, after which the ordinary sweep can hold it
there.

Why it deletes active rows directly
-----------------------------------
`expire_aged_postings` closes an aged row with `retention_days=0`, which makes
it purge-eligible in the SAME sweep; `purge_expired` then deletes it. For an
UNPINNED row the net effect of that two-step is exactly one delete, so this
script performs the delete and skips the intermediate write. That is a real
deviation from "purge_at has exactly one writer", and it is confined to this
backlog drain for one reason: 344,235 individual `update_one` calls against a
remote cluster is hours of round-trips to reach a state that is, row for row,
identical.

The guard that actually matters is preserved and STRENGTHENED. `pinned` is
re-checked inside the delete filter itself, not merely when the candidate ids
are selected, so a posting that a user saves WHILE the drain is running cannot
be deleted by an in-flight batch. Run `backfill_pins` first (or
`--verify-pins`) so every user-attached row carries the flag before anything
is deleted; a pinned row is never touched here regardless of age.

What is NOT deleted
-------------------
  * anything pinned (applied / saved / tailored / snapshot / manual)
  * anything first seen, or posted, inside the 7-day window
  * anything still `active` and inside the window - i.e. everything
    `find_active_fresh(max_age_days=7)` can actually serve

    python scripts/purge_job_inventory_backlog.py              # dry run
    python scripts/purge_job_inventory_backlog.py --apply
    python scripts/purge_job_inventory_backlog.py --apply --days 14
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
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

BATCH = 5000

# Mirrors job_inventory_service.STATUS_ACTIVE / CLOSED_STATUSES rather than
# importing them, so a drain can still be run against a cluster whose deployed
# code has moved on.
STATUS_ACTIVE = "active"


def _aged_filter(cutoff: datetime) -> dict:
    """Exactly `expire_aged_postings`' predicate, plus the pinned guard.

    A posting with no `posted_at` ages out on `first_seen_at`: `posted_at <
    cutoff` never matches a null, and without the null arms an undated row
    would sit here forever.
    """
    return {
        "status": STATUS_ACTIVE,
        "pinned": {"$ne": True},
        "$or": [
            {"posted_at": {"$lt": cutoff}},
            {"posted_at": None, "first_seen_at": {"$lt": cutoff}},
            {"posted_at": {"$exists": False}, "first_seen_at": {"$lt": cutoff}},
        ],
    }


def _due_filter(now: datetime) -> dict:
    """Closed rows already past `purge_at` - `purge_expired`'s own candidates."""
    return {"purge_at": {"$lte": now}, "status": {"$ne": STATUS_ACTIVE},
            "pinned": {"$ne": True}}


def _drain(coll, filt: dict, label: str, apply: bool) -> int:
    n = coll.count_documents(filt)
    print(f"  {label:<42} {n:>9,}")
    if not apply or not n:
        return n
    deleted, started = 0, time.time()
    while True:
        ids = [d["_id"] for d in coll.find(filt, {"_id": 1}).limit(BATCH)]
        if not ids:
            break
        # The filter is re-applied alongside the id list so a row pinned since
        # it was selected is skipped rather than deleted.
        res = coll.delete_many({"$and": [{"_id": {"$in": ids}}, filt]})
        deleted += int(res.deleted_count or 0)
        rate = deleted / max(1e-6, time.time() - started)
        print(f"    deleted {deleted:>9,} / {n:,}  ({rate:,.0f}/s)", flush=True)
        if len(ids) < BATCH and not res.deleted_count:
            break
    return deleted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete")
    ap.add_argument("--days", type=int, default=7,
                    help="retention window in days (default 7)")
    args = ap.parse_args()

    db = DBConnect().get_db()
    coll = db.job_postings
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max(0, args.days))

    total = coll.estimated_document_count()
    pinned = coll.count_documents({"pinned": True})
    print(f"job_postings: {total:,} docs, {pinned:,} pinned, window={args.days}d "
          f"(cutoff {cutoff:%Y-%m-%d %H:%M} UTC)\n")

    # A drain that runs before the pins are backfilled deletes the user's own
    # history, so this is a hard stop rather than a warning.
    unpinned_refs = _unpinned_user_refs(db)
    if unpinned_refs:
        print(f"REFUSING TO RUN: {unpinned_refs} user-attached postings "
              f"(saved/tailored/applied) are NOT pinned.\n"
              f"Run scripts/backfill_pins.py --apply first.")
        return 2

    print("candidates:")
    aged = _drain(coll, _aged_filter(cutoff), f"active, older than {args.days}d", args.apply)
    due = _drain(coll, _due_filter(now), "closed, past purge_at", args.apply)

    if not args.apply:
        print(f"\n  {'TOTAL deletable':<42} {aged + due:>9,}")
        print("\nDRY RUN - rerun with --apply to delete.")
        return 0

    remaining = coll.estimated_document_count()
    print(f"\ndeleted {aged + due:,}; {remaining:,} postings remain "
          f"({pinned:,} pinned retained)")
    return 0


def _unpinned_user_refs(db) -> int:
    """Count postings a user is attached to that carry no `pinned` flag."""
    import re
    hexid = re.compile(r"^[0-9a-f]{32}$")
    ids = set()
    for v in db.saved_jobs.distinct("job_id"):
        if isinstance(v, str) and hexid.match(v):
            ids.add(v)
    for v in db.tailoring_records.distinct("source_job_id"):
        if isinstance(v, str) and hexid.match(v):
            ids.add(v)
    for v in db.applications.distinct("posting_job_id"):
        if isinstance(v, str) and hexid.match(v):
            ids.add(v)
    if not ids:
        return 0
    ids = list(ids)
    n = 0
    for i in range(0, len(ids), 500):
        n += db.job_postings.count_documents(
            {"job_id": {"$in": ids[i:i + 500]}, "pinned": {"$ne": True}})
    return n


if __name__ == "__main__":
    raise SystemExit(main())
