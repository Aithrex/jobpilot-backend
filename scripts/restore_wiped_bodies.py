#!/usr/bin/env python3
"""restore_wiped_bodies.py — put back descriptions a listing-only write erased.

DRY-RUN BY DEFAULT.

Until 3c60c1d, `upsert_posting` wrote a deferred listing-only row's EMPTY body
over the stored one. Measured 2026-10-02: 59,454 of 74,703 body-less active
postings had had a description.

The description is recoverable without touching any employer: the posting's
`requirements` were never overwritten (they are `$setOnInsert`-only on
upsert), `requirements.source_content_hash` is the hash of the body they were
extracted from, and `job_content_store` keeps bodies content-addressed under
exactly that hash. So the original text is in OUR bucket whenever it was
offloaded - it is not when it was stored inline (S3 writes were refused
2026-09-20 -> 10-02); those are left for the crawler, which now keeps a body
once it fetches one.

A restore sets `description_ref`, `description_chars` and `content_hash` back
together, so the requirements guard (`source_content_hash == content_hash`)
holds again, and only on a row that is STILL body-less at write time.

    python scripts/restore_wiped_bodies.py            # dry run
    python scripts/restore_wiped_bodies.py --apply
"""
from __future__ import annotations

import argparse
import collections
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

BODYLESS = {"$or": [{"description_text": {"$in": [None, ""]}},
                    {"description_text": {"$exists": False}}],
            "description_ref": {"$in": [None, ""]}}


def candidates_query() -> Dict[str, Any]:
    return {"status": "active", **BODYLESS,
            "requirements.text_length": {"$gt": 0},
            "requirements.source_content_hash": {"$type": "string"}}


def restore_one(jp, doc: Dict[str, Any], get_jd, *, apply: bool) -> Tuple[str, int]:
    """`(outcome, chars)`. Writes only when `apply` and the row is still body-less."""
    h = str(((doc.get("requirements") or {}).get("source_content_hash")) or "")
    if not h:
        return "no_hash", 0
    try:
        text = get_jd(h)
    except Exception:
        text = None
    if not text:
        return "not_in_store", 0
    if not apply:
        return "restorable", len(text)
    res = jp.update_one(
        {"_id": doc["_id"], **BODYLESS},
        {"$set": {"description_ref": h, "description_text": "",
                  "description_chars": len(text), "content_hash": h}})
    return ("restored" if res.modified_count else "changed_meanwhile"), len(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    from utils.db_connect import DBConnect
    from services import job_content_store

    if not job_content_store.available():
        print("JD store unavailable (RESUME_S3_BUCKET unset) - nothing can be restored")
        return 1
    jp = DBConnect().get_db().job_postings
    q = candidates_query()
    total = jp.count_documents(q)
    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | wiped candidates: {total:,}")
    cursor = jp.find(q, {"_id": 1, "source": 1, "requirements.source_content_hash": 1}).batch_size(500)
    if args.limit:
        cursor = cursor.limit(args.limit)

    outcomes: collections.Counter = collections.Counter()
    by_source: collections.Counter = collections.Counter()
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        docs = list(cursor)
        for doc, (outcome, _n) in zip(docs, pool.map(
                lambda d: restore_one(jp, d, job_content_store.get_jd, apply=args.apply), docs)):
            outcomes[outcome] += 1
            if outcome in ("restorable", "restored"):
                by_source[doc.get("source") or "?"] += 1
    print(f"scanned {sum(outcomes.values()):,} in {time.perf_counter() - started:.0f}s")
    for k, n in outcomes.most_common():
        print(f"  {k:18} {n:>8,}")
    print("  by source:", dict(by_source))
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
