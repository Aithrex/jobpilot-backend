"""Run job-inventory shards by hand, against real boards and the real DB.

The scheduled path (EventBridge -> resume Lambda -> `job_ingest_dispatch` ->
`job_ingest_shard`) is fire-and-forget, so there is no good way to watch a first
crawl. This is that way: it runs the SAME `run_shard` the Lambda runs, in the
foreground, and prints the counts.

Use it to
  * smoke-test a code change against live ATS boards before enabling schedules;
  * warm the inventory so the guest read path has supply on day one;
  * reproduce a shard that shows failures in `job_ingestion_runs`.

    python scripts/ingest_smoke.py --source career-pages --shards 1
    python scripts/ingest_smoke.py --source career-pages --shards 8 --from 0
    python scripts/ingest_smoke.py --source workday --shards 4
    python scripts/ingest_smoke.py --stats            # inventory summary only

Writes to whatever MONGODB_URI/RESUME_S3_BUCKET the environment points at, so
check those before running it against production.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "shared" / "python"))

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except Exception:
    pass


def _fmt(value):
    return json.dumps(value, default=str, sort_keys=True)


def stats() -> None:
    from services import job_inventory_service as inv
    from utils.db_connect import DBConnect

    db = DBConnect().get_db()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=inv.MAX_INVENTORY_AGE_DAYS)

    total = db.job_postings.estimated_document_count()
    active = db.job_postings.count_documents({"status": "active"})
    servable = db.job_postings.count_documents({
        "status": "active", "posted_at": {"$gte": cutoff},
        "posted_at_confidence": {"$ne": "unknown"},
        "cost_class": "free",
    })
    parsed = db.job_postings.count_documents({
        "status": "active", "requirements.score_version": {"$exists": True}})
    pinned = db.job_postings.count_documents({"pinned": True})

    print(f"job_postings      : {total} total / {active} active")
    print(f"  guest-servable  : {servable}  (free, <= {inv.MAX_INVENTORY_AGE_DAYS}d, dated)")
    print(f"  requirement-ok  : {parsed}")
    print(f"  pinned          : {pinned}")
    print(f"shard state       : {db.job_source_state.estimated_document_count()}")
    print(f"ingestion runs    : {db.job_ingestion_runs.estimated_document_count()}")

    families = list(db.job_postings.aggregate([
        {"$match": {"status": "active", "posted_at": {"$gte": cutoff}}},
        {"$group": {"_id": "$role_family", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 15},
    ]))
    if families:
        print("top role families :")
        for f in families:
            print(f"    {f['_id'] or '(none)':<24} {f['n']}")

    recent = list(db.job_ingestion_runs.find(
        {}, {"_id": 0, "source": 1, "shard": 1, "status": 1, "counts": 1}
    ).sort("started_at", -1).limit(5))
    for r in recent:
        print("  run:", _fmt(r))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="career-pages",
                    choices=["career-pages", "workday"])
    ap.add_argument("--shards", type=int, default=1, help="how many shards to run")
    ap.add_argument("--from", dest="start", type=int, default=0,
                    help="index of the first shard to run")
    ap.add_argument("--stats", action="store_true", help="print inventory stats and exit")
    args = ap.parse_args()

    print(f"mongo  : {(os.getenv('MONGODB_URI') or '').split('@')[-1].split('/')[0] or '(unset)'}")
    print(f"bucket : {os.getenv('RESUME_S3_BUCKET') or '(unset)'}")
    print()

    if args.stats:
        stats()
        return 0

    from services import job_ingest_worker as w

    w.ensure_shard_state(args.source)
    shards = w.all_shards(args.source)[args.start:args.start + max(1, args.shards)]
    print(f"running {len(shards)} shard(s) of {args.source}: {', '.join(shards)}\n")

    totals: dict = {}
    for shard in shards:
        started = time.monotonic()
        out = w.run_shard(args.source, shard)
        elapsed = time.monotonic() - started
        counts = out.get("counts") or {}
        for k, v in counts.items():
            if isinstance(v, int):
                totals[k] = totals.get(k, 0) + v
        status = out.get("error") or out.get("skipped") or "ok"
        print(f"  {shard}  {status:<20} {elapsed:6.1f}s  {_fmt(counts)}")
        for failure in (out.get("failures") or [])[:3]:
            print(f"      ! {_fmt(failure)}")

    print(f"\ntotals: {_fmt(totals)}\n")
    stats()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
