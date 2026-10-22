"""Recompute stored posting identity under identity-1.0.0. DRY-RUN by default.

Why: `canonical_url_key` used to strip `gh_jid` (the Greenhouse job id on an
employer-hosted board) and kept job-less portal URLs, so every job on
`acme.com/careers?gh_jid=N` shared ONE `dedupe_group_id` and the served
dedupe showed one of them. New writes are correct; stored rows are not
rewritten by the crawler while their listing is unchanged, and bumping
NORMALIZATION_VERSION would re-fetch the whole inventory to fix one field.

What it does, per active posting (keyset-paged by `_id`):
  * recomputes `dedupe_group_id`, `canonical_url_key`,
    `possible_duplicate_group_id` (from fields already on the document) and
    `apply_url_key`;
  * changes a row only when one of them differs, and keeps the previous
    values in `identity_prev` so `--rollback` restores them exactly.

Usage (owner-run):
    python scripts/recompute_dedupe_identity.py                # dry run, prints the plan
    python scripts/recompute_dedupe_identity.py --apply        # writes
    python scripts/recompute_dedupe_identity.py --rollback --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from pymongo import UpdateOne

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))

FIELDS = ("dedupe_group_id", "canonical_url_key", "possible_duplicate_group_id",
          "apply_url_key")
PROJECTION = {"_id": 1, "employer_id": 1, "ats": 1, "source": 1, "source_job_id": 1,
              "canonical_url": 1, "canonical_apply_url": 1, "original_job_url": 1,
              "content_hash": 1, "title_normalized": 1, "title_raw": 1,
              "identity_prev": 1, **{f: 1 for f in FIELDS}}


def plan_for(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from services import job_inventory_service as inv
    ident = inv.compute_dedupe_identity(
        employer_id=str(doc.get("employer_id") or ""), ats=str(doc.get("ats") or ""),
        source=str(doc.get("source") or ""),
        source_job_id=str(doc.get("source_job_id") or ""),
        canonical_url=str(doc.get("canonical_url") or ""),
        content_hash=str(doc.get("content_hash") or ""),
        title_normalized=str(doc.get("title_raw") or doc.get("title_normalized") or ""))
    ident = {k: ident.get(k, "") for k in FIELDS[:3]}
    ident["apply_url_key"] = inv.apply_url_key(
        doc.get("canonical_apply_url") or doc.get("original_job_url")
        or doc.get("canonical_url"))
    # An empty `dedupe_group_id` is served as the row's own job_id (a stored
    # content-derived group is exactly what identity-1.0.0 stops merging on).
    changed = {k: v for k, v in ident.items() if (doc.get(k) or "") != (v or "")}
    if not changed:
        return None
    return {"set": changed, "prev": {k: doc.get(k) for k in changed}}


def run(db: Any, *, apply: bool = False, rollback: bool = False, batch: int = 2000,
        limit: Optional[int] = None, progress: bool = False) -> Dict[str, Any]:
    coll = db.job_postings
    stats: Counter = Counter()
    examples: List[Dict[str, Any]] = []
    last = None
    seen = 0
    while True:
        q: Dict[str, Any] = {"status": "active"}
        if rollback:
            q["identity_prev"] = {"$exists": True}
        if last is not None:
            q["_id"] = {"$gt": last}
        page = list(coll.find(q, PROJECTION).sort("_id", 1).limit(batch))
        if not page:
            break
        # One round trip per PAGE, not per row. One `update_one` per posting
        # ran at ~13 rows/s against Atlas from a laptop - 311,650 rows was a
        # six-hour run on 2026-10-22. Rows already rewritten plan as unchanged,
        # so a stopped run resumes where it left off.
        ops: List[Any] = []
        for doc in page:
            last = doc["_id"]
            seen += 1
            if rollback:
                prev = doc.get("identity_prev") or {}
                stats["rollback_rows"] += 1
                if apply:
                    unset = {k: "" for k, v in prev.items() if v is None}
                    unset["identity_prev"] = ""
                    update: Dict[str, Any] = {"$unset": unset}
                    restore = {k: v for k, v in prev.items() if v is not None}
                    if restore:
                        update["$set"] = restore
                    ops.append(UpdateOne({"_id": doc["_id"]}, update))
                continue
            plan = plan_for(doc)
            if plan is None:
                stats["unchanged"] += 1
                continue
            stats["changed"] += 1
            for k in plan["set"]:
                stats[f"changed.{k}"] += 1
            if len(examples) < 10:
                examples.append({"_id": str(doc["_id"]), **plan})
            if apply:
                ops.append(UpdateOne({"_id": doc["_id"]},
                                     {"$set": {**plan["set"],
                                               "identity_prev": plan["prev"]}}))
            if limit and seen >= limit:
                break
        if ops:
            try:
                coll.bulk_write(ops, ordered=False)
            except (TypeError, NotImplementedError):
                # mongomock does not take every kwarg the driver's UpdateOne
                # carries - same fallback as employer_registry.bulk_upsert_employers.
                for op in ops:
                    coll.update_one(op._filter, op._doc)
            stats["written"] += len(ops)
            if progress:
                print(f"  examined {seen:,} written {stats['written']:,}", flush=True)
        if limit and seen >= limit:
            break
    return {"mode": ("rollback" if rollback else "recompute") + (" APPLY" if apply else
                                                                 " dry-run"),
            "rows_examined": seen, "stats": dict(stats), "examples": examples}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--rollback", action="store_true",
                    help="restore `identity_prev` (with --apply)")
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:      # pragma: no cover
        pass
    from utils.db_connect import DBConnect
    out = run(DBConnect().get_db(), apply=args.apply, rollback=args.rollback,
              batch=max(1, args.batch), limit=args.limit, progress=True)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
