#!/usr/bin/env python3
"""prioritize_reviews.py — order the human queues by consequence.

DRY-RUN BY DEFAULT.

Two queues, one rule
--------------------
`employer_merge_reviews` (1,443 open) and `employer_board_conflicts` are both
"a human must decide", and both were ordered by arrival. A queue of 1,443 rows
sorted by `created_at` is a queue nobody finishes, and the rows that matter —
an employer holding 839 core-tech postings, an employer someone put on the
strategic list — sit wherever they happened to land.

`employer_registry.score_review` ranks both on the same four inputs:

    active posting count      how much inventory the answer moves
    core-tech posting count   weighted 3x, because that is the product
    Tier-A membership         strategic, whatever the posting count
    evidence strength         a pair one signal from a decision first

and one penalty: a pair where BOTH sides are domain-keyed is two verified
domains, which `merge_decision` already answers as `distinct`. Those sink.

What it does not do
-------------------
Resolve anything. It writes `review_priority` and `priority_reasons` and
touches no verdict. A name-only match is quarantined by
`employer_registry.merge_decision` and stays quarantined; ordering the queue
is not the same as working it, and a script that started merging on a score
would be the exact failure the quarantine exists to prevent.

Usage
-----
    PYTHONPATH=shared/python python scripts/prioritize_reviews.py
    PYTHONPATH=shared/python python scripts/prioritize_reviews.py --apply
    PYTHONPATH=shared/python python scripts/prioritize_reviews.py --top 40
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

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


from services import canonical_employer as ce            # noqa: E402
from services import career_source_service as css        # noqa: E402
from services import employer_registry as er             # noqa: E402
from services import official_career_registry as ocr     # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def posting_counts(db) -> Dict[str, Dict[str, int]]:
    """`{canonical_employer_id: {active, core}}` for the whole live inventory."""
    out: Dict[str, Dict[str, int]] = {}
    for row in db.job_postings.aggregate([
            {"$match": {"status": "active",
                        "canonical_employer_id": {"$type": "string"}}},
            {"$group": {"_id": {"e": "$canonical_employer_id",
                                "s": "$tech.tech_scope"},
                        "n": {"$sum": 1}}}], allowDiskUse=True):
        rid = row["_id"]
        bucket = out.setdefault(str(rid.get("e")), {"active": 0, "core": 0})
        bucket["active"] += int(row["n"])
        if str(rid.get("s")) == "core":
            bucket["core"] += int(row["n"])
    return out


def tier_a_ids() -> Set[str]:
    return {er.employer_id_for(domain=r["domain"], name=r["display_name"])
            for r in ocr.OFFICIAL_CAREER_URLS}


def _evidence_classes(db, employer_ids: List[str]) -> int:
    classes: Set[str] = set()
    for src in db[css.CAREER_SOURCES].find(
            {"employer_id": {"$in": employer_ids}}, {"ownership_evidence": 1}):
        for ev in (src.get("ownership_evidence") or []):
            if not isinstance(ev, dict) or not ev.get("confirmed"):
                continue
            cls = str(ev.get("signal_class") or "")
            if cls and cls != css.CLASS_HEURISTIC:
                classes.add(cls)
    return len(classes)


def score_merge_reviews(db, counts, tier) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in db[er.MERGE_REVIEWS].find({"resolved": False}):
        left, right = row.get("left") or {}, row.get("right") or {}
        ids = [str(left.get("employer_id") or ""), str(right.get("employer_id") or "")]
        ids = [i for i in ids if i]
        active = sum(counts.get(i, {}).get("active", 0) for i in ids)
        core = sum(counts.get(i, {}).get("core", 0) for i in ids)
        is_tier = any(i in tier for i in ids)
        both_domain = (str(left.get("id_basis")) == "domain"
                       and str(right.get("id_basis")) == "domain")
        score, reasons = er.score_review(
            active_postings=active, core_tech_postings=core, tier_a=is_tier,
            evidence_classes=_evidence_classes(db, ids) if ids else 0,
            both_domain_keyed=both_domain)
        out.append({"queue": er.MERGE_REVIEWS, "key": row.get("review_key"),
                    "label": f"{left.get('display_name')} / {right.get('display_name')}",
                    "priority": score, "reasons": reasons,
                    "active_postings": active, "core_tech_postings": core,
                    "tier_a": is_tier})
    return out


def score_board_conflicts(db, counts, tier) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if ce.BOARD_CONFLICTS not in db.list_collection_names():
        return out
    for row in db[ce.BOARD_CONFLICTS].find({"resolved": False}):
        ids = [str(i) for i in (row.get("employer_ids") or [])]
        active = int(row.get("active_postings") or 0) or sum(
            counts.get(i, {}).get("active", 0) for i in ids)
        core = sum(counts.get(i, {}).get("core", 0) for i in ids)
        is_tier = any(i in tier for i in ids)
        score, reasons = er.score_review(
            active_postings=active, core_tech_postings=core, tier_a=is_tier,
            evidence_classes=_evidence_classes(db, ids) if ids else 0)
        # A conflict that is BLOCKING attribution outranks one a domain chain
        # already settled: the first is costing us cards today, the second is
        # a tidy-up.
        if row.get("attribution_blocked"):
            score += 1500.0
            reasons.insert(0, "blocking canonical attribution today")
        out.append({"queue": ce.BOARD_CONFLICTS, "key": row.get("review_key"),
                    "label": " / ".join((row.get("display_names") or ids)[:2]),
                    "priority": round(score, 2), "reasons": reasons,
                    "active_postings": active, "core_tech_postings": core,
                    "tier_a": is_tier})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    db = _db()
    counts = posting_counts(db)
    tier = tier_a_ids()
    rows = score_merge_reviews(db, counts, tier) + score_board_conflicts(db, counts, tier)
    rows.sort(key=lambda r: -r["priority"])

    by_queue = collections.Counter(r["queue"] for r in rows)
    print(f"open reviews: {len(rows):,}")
    for queue, n in by_queue.most_common():
        print(f"  {queue:28s} {n:>6,}")
    worth = sum(1 for r in rows if r["priority"] > 0)
    print(f"  reviews with any consequence behind them: {worth:,}")
    print(f"  Tier-A reviews: {sum(1 for r in rows if r['tier_a']):,}")

    print(f"\ntop {min(args.top, len(rows))}:")
    for row in rows[:args.top]:
        print(f"  {row['priority']:>10,.0f}  {str(row['label'])[:48]:<50}"
              f"{row['reasons'][0][:44]}")

    if args.apply:
        from pymongo import UpdateOne
        ops = collections.defaultdict(list)
        for row in rows:
            ops[row["queue"]].append(UpdateOne(
                {"review_key": row["key"]},
                {"$set": {"review_priority": row["priority"],
                          "priority_reasons": row["reasons"][:5],
                          "priority_active_postings": row["active_postings"],
                          "priority_core_tech_postings": row["core_tech_postings"],
                          "priority_tier_a": row["tier_a"],
                          "prioritized_at": _now()}}))
        for queue, batch in ops.items():
            for i in range(0, len(batch), 500):
                db[queue].bulk_write(batch[i:i + 500], ordered=False)
            db[queue].create_index([("resolved", 1), ("review_priority", -1)],
                                   name="review_priority_queue")
            print(f"\nwrote {len(batch):,} priorities to {queue}")
    else:
        print("\nDRY RUN - nothing written. Re-run with --apply.")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"generated_at": _now().isoformat(), "reviews": rows[:500]},
            indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
