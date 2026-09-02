#!/usr/bin/env python3
"""Re-arm discovery for the employers coverage says we are missing.

DRY-RUN BY DEFAULT. `--apply` only ever sets `discovery_status: pending` and
enqueues a discovery job. It never deletes, never edits a posting, never
touches a schedule and never writes a career source.

Ordering, and why it is not alphabetical
----------------------------------------
Priority is by MEASURED technology-job yield, not by raw provider board count.
A payroll ATS with 3,000 small customers is a bigger number and a smaller
product; one under-crawled enterprise board is often thousands of engineering
requisitions. So the queue is ordered:

1. **repair** - an employer we ALREADY yield core-tech postings for whose
   career source is `dead` or `moved`. Highest value per request: the board
   exists, we have crawled it, and something broke.
2. **deferred/unresolved with evidence** - an employer in the technology
   denominator whose source never got past `candidate`. This is the bulk.
3. **identity conflicts** - employers with an open row in
   `employer_merge_reviews`. Re-crawling before the identity is settled just
   files the postings under the wrong name again, so these are reported and
   NOT enqueued.
4. **no evidence** - not enqueued at all, and counted so the report can say
   how many employers we are deliberately not spending requests on.

Usage
-----
    PYTHONPATH=shared/python python scripts/rearm_uncovered_employers.py
    PYTHONPATH=shared/python python scripts/rearm_uncovered_employers.py \
        --apply --limit 2000
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))


def _db():
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


from services import career_source_service as css        # noqa: E402
from services import company_registry_service as reg     # noqa: E402
from services import employer_registry as er             # noqa: E402

import employer_coverage_report as report                # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


TIER_REPAIR = "1_repair_broken_source"
TIER_UNRESOLVED = "2_unresolved_with_evidence"
TIER_IDENTITY = "3_identity_conflict_hold"
TIER_NO_EVIDENCE = "4_no_evidence_not_queued"


def classify_tier(emp: Dict[str, Any], conflicted: set) -> Tuple[str, int]:
    """`(tier, priority)`. Priority feeds `discovery_queue`, higher first."""
    if emp["employer_id"] in conflicted:
        return TIER_IDENTITY, 0
    state = (emp.get("_coverage") or {}).get("state")
    core = int((emp.get("_mix") or {}).get("core", 0))
    if state in (css.STATE_DEAD, css.STATE_MOVED, css.STATE_BLOCKED):
        # The board existed and something broke. Cheapest real coverage
        # available, and the priority scales with what we would get back.
        return TIER_REPAIR, min(100, 80 + min(core // 50, 20))
    if state in (css.STATE_CANDIDATE, css.STATE_CONTENT_VALIDATED,
                 css.STATE_OWNERSHIP_VERIFIED):
        return TIER_UNRESOLVED, min(79, 40 + min(core // 10, 39))
    return TIER_NO_EVIDENCE, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--mix-days", type=int, default=30)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    db = _db()
    print(f"mode: {'APPLY' if args.apply else 'dry-run'}")
    built = report.build(db, mix_days=args.mix_days)
    rows = built["employers"]
    print(f"technology denominator: {len(rows):,}")

    conflicted = set()
    for review in db[er.MERGE_REVIEWS].find({"resolved": False},
                                            {"left": 1, "right": 1}):
        for side in ("left", "right"):
            eid = (review.get(side) or {}).get("employer_id")
            if eid:
                conflicted.add(eid)
    print(f"employers held for identity review: {len(conflicted):,}")

    tiers: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for emp in rows:
        if (emp.get("_coverage") or {}).get("state") in css.COVERED_STATES:
            continue
        tier, priority = classify_tier(emp, conflicted)
        tiers[tier].append({**emp, "_tier": tier, "_priority": priority})

    print("\nuncovered, by tier:")
    for tier in (TIER_REPAIR, TIER_UNRESOLVED, TIER_IDENTITY, TIER_NO_EVIDENCE):
        bucket = tiers.get(tier) or []
        core = sum(int((e.get("_mix") or {}).get("core", 0)) for e in bucket)
        print(f"  {tier:32s} {len(bucket):>6,} employers   "
              f"{core:>7,} core-tech postings today")

    queue = sorted(
        (tiers.get(TIER_REPAIR) or []) + (tiers.get(TIER_UNRESOLVED) or []),
        key=lambda e: -e["_priority"])[:args.limit]
    print(f"\nwould enqueue: {len(queue):,}")
    for emp in queue[:15]:
        print(f"  p{emp['_priority']:>3} {emp['_tier'][:1]} "
              f"{(emp.get('display_name') or '')[:44]:46s} "
              f"core={int((emp.get('_mix') or {}).get('core', 0)):>5,}  "
              f"{(emp.get('_coverage') or {}).get('state')}")

    enqueued = 0
    if args.apply:
        for emp in queue:
            for company_id in ((emp.get("registry_refs") or {}).get("company_ids") or []):
                db.companies.update_one(
                    {"company_id": company_id},
                    {"$set": {"discovery_status": reg.DISCOVERY_PENDING,
                              "priority": max(int(emp["_priority"]), 1),
                              "rearmed_reason": "employer_coverage_gap",
                              "updated_at": _now()}})
                # Through the registry's own helper, so the queue document has
                # the shape `claim_discovery_batch` sorts on. A direct upsert
                # omits `created_at` and `attempts`, and the claim query sorts
                # on `created_at`.
                reg.enqueue_discovery(company_id, priority=int(emp["_priority"]),
                                      reason="employer-coverage-gap", rearm=True)
                enqueued += 1
        print(f"\nenqueued {enqueued:,} discovery jobs")
    else:
        print("\ndry-run: nothing written (pass --apply)")

    summary = {
        "generated_at": _now().isoformat(),
        "source_revision": report.source_revision(),
        "denominator": len(rows),
        "by_tier": {t: len(v) for t, v in tiers.items()},
        "held_for_identity_review": len(conflicted),
        "queued": len(queue),
        "enqueued": enqueued,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str),
                                   encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
