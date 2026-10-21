#!/usr/bin/env python3
"""backfill_posting_sightings.py — remember every live posting's identity, so
a posting the retention TTL deletes comes back as the SAME posting.

DRY-RUN BY DEFAULT. INSERT-ONLY: an existing sighting is never changed.

Why this exists
---------------
`upsert_posting` used to insert each posting with a random `job_id`. An
undated posting (Rippling, JazzHR, iCIMS and Atlassian list no date) expires
`max_age_days_for` after it is first seen; the TTL deleted it while its board
still listed it, and the next crawl inserted it again with a NEW id and
`first_seen_at = now`. Users saw old jobs under "Today", and hidden/dismissed
cards came back (reported 2026-10-21).

New inserts now derive `job_id` from `(source, source_job_id)` and record a
`posting_sightings` row (see `job_inventory_service._resume_sighting`). Rows
inserted BEFORE that deploy have a random id and no sighting; this script
records one for each, so their eventual re-insert restores the id users
already hold and the original first sighting.

    python scripts/backfill_posting_sightings.py            # dry run
    python scripts/backfill_posting_sightings.py --apply

Run AFTER the deploy that ships `_resume_sighting`, then let
`retention_watchdog.py --apply` create the sightings TTL index (or run
`job_inventory_service.ensure_sightings_index()` once).

Rollback: `db.posting_sightings.drop()`. Nothing else reads the collection;
the only effect of dropping it is that re-inserts behave as they did before.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    ap.add_argument("--page", type=int, default=5000)
    args = ap.parse_args()

    from pymongo import UpdateOne
    from services import job_inventory_service as inv
    from utils.db_connect import DBConnect

    db = DBConnect().get_db()
    jp, sightings = db.job_postings, db.posting_sightings
    now = datetime.now(timezone.utc)
    expire = now + timedelta(days=inv.SIGHTING_RETENTION_DAYS)
    proj = {"_id": 1, "source": 1, "source_job_id": 1, "job_id": 1, "first_seen_at": 1}

    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | postings: "
          f"{jp.estimated_document_count():,} | sightings already: "
          f"{sightings.estimated_document_count():,}")
    scanned = eligible = written = 0
    last_id = None
    while True:
        q = {"_id": {"$gt": last_id}} if last_id is not None else {}
        page = list(jp.find(q, proj).sort("_id", 1).limit(args.page))
        if not page:
            break
        last_id = page[-1]["_id"]
        ops = []
        for doc in page:
            scanned += 1
            source, sjid = doc.get("source"), doc.get("source_job_id")
            if not source or not sjid or not doc.get("job_id"):
                continue
            eligible += 1
            ops.append(UpdateOne(
                {"_id": f"{source}\x1f{sjid}"},
                {"$setOnInsert": {"job_id": doc["job_id"],
                                  "first_seen_at": doc.get("first_seen_at") or now,
                                  "expire_at": expire,
                                  "backfilled_at": now}},
                upsert=True))
        if args.apply and ops:
            res = sightings.bulk_write(ops, ordered=False)
            written += res.upserted_count
    print(f"scanned {scanned:,} | eligible {eligible:,} | "
          f"{'inserted ' + format(written, ',') if args.apply else 'would upsert up to ' + format(eligible, ',')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
