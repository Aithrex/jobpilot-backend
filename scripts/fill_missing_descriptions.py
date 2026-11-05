#!/usr/bin/env python3
"""Fetch the job descriptions open postings are still owed. Dry run by default.

The crawler now does this on its own: every shard run keeps a share of its
detail budget for postings still owed a body (`job_ingest_worker._owed_backlog`,
oldest first) and retries a miss on `DETAIL_RETRY_HOURS` instead of abandoning
it. This clears an existing backlog faster, through the SAME code: each shard
is run with `run_shard(..., backlog_only=True)`, which skips the listing and
spends the whole budget on owed postings, takes the shard's lease (so it never
runs beside the crawler on the same boards), and writes through the same
fetch / write / parse path. A miss is recorded and scheduled, never written as
a live sighting.

    python scripts/fill_missing_descriptions.py                    # report only
    python scripts/fill_missing_descriptions.py --apply            # every source
    python scripts/fill_missing_descriptions.py --apply --source workday --per-shard 200

Requests go to EMPLOYER job boards, paced by the crawler's own host policy. Run
it on the box, by the owner, one copy at a time.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


def report(inv) -> dict:
    h = inv.description_health()
    print(f"open postings with a description: {h['with_body']:.1%} "
          f"({h['owed']:,} of {h['active']:,} still owed one)")
    print(f"new postings (2-26 h) with one:    {h['new_with_body']:.1%} "
          f"({h['new_owed']:,} of {h['new_total']:,} owed)")
    print(f"oldest owed: {h['oldest_owed_hours']} h")
    for src, v in sorted(h["by_source"].items(), key=lambda kv: -kv[1]["owed"]):
        if v["owed"]:
            print(f"  {src:14s} owed {v['owed']:7,} of {v['active']:8,}")
    if h["broken_boards"]:
        print("boards missing most descriptions (their detail route is likely broken):")
        for b in h["broken_boards"]:
            print(f"  {b['ats']}:{b['tenant']} ({b['employer']}): {b['owed']:,} of {b['active']:,}")
    return h


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="fetch (default: report only)")
    ap.add_argument("--source", default="", help="only this source (workday, career-pages, ...)")
    ap.add_argument("--per-shard", type=int, default=150,
                    help="owed postings one shard run may fetch (default 150)")
    ap.add_argument("--budget", type=float, default=120.0,
                    help="seconds per shard run (default 120)")
    ap.add_argument("--retry-now", action="store_true",
                    help="with --apply: clear the retry wait on every owed posting first. "
                         "For misses a since-fixed bug caused (2026-11-05: legacy Workday "
                         "was handed a full URL where its fetch appends a path).")
    args = ap.parse_args(argv)

    from services import job_ingest_worker as w
    from services import job_inventory_service as inv

    print("mode:", "APPLY" if args.apply else "dry-run")
    before = report(inv)
    if not args.apply:
        print("\ndry run - nothing fetched or written. Re-run with --apply.")
        return 0

    sources = [w.normalize_source(args.source)] if args.source else [
        src for src, v in before["by_source"].items() if v["owed"]]
    if args.retry_now:
        res = inv._postings().update_many(
            {"status": inv.STATUS_ACTIVE, "body_state": "teaser", "source": {"$in": sources},
             "detail_next_at": {"$ne": None}},
            {"$unset": {"detail_next_at": ""}})
        print(f"retry wait cleared on {res.modified_count:,} owed postings")
    totals: Counter = Counter()
    started = time.monotonic()
    for src in sources:
        for shard in w.all_shards(src):
            out = w.run_shard(src, shard, backlog_only=True, max_details=args.per_shard,
                              budget_seconds=args.budget)
            c = out.get("counts") or {}
            for k in ("backlog-attempted", "backlog-filled", "backlog-missed"):
                totals[k] += int(c.get(k) or 0)
            if c.get("backlog-attempted") or out.get("skipped") or out.get("error"):
                print(f"  {src}/{shard}: attempted {c.get('backlog-attempted', 0)} "
                      f"filled {c.get('backlog-filled', 0)} missed {c.get('backlog-missed', 0)}"
                      + (f" skipped={out['skipped']}" if out.get("skipped") else "")
                      + (f" error={out['error'][:80]}" if out.get("error") else ""), flush=True)
    print(f"\ndone in {time.monotonic() - started:.0f}s: {dict(totals)}")
    print("\nafter:")
    report(inv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
