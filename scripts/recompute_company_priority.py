#!/usr/bin/env python3
"""Recompute `companies.signals` + `companies.priority` across the registry.

Why this exists
---------------
`score_priority` is applied by `upsert_company` at write time, so a NEW signal
(or a new weight) only reaches rows that happen to be written again. The
software/tech signals added in `company_registry_service.classify_tech` are
exactly that case: 82,492 rows were already in the collection, and without a
sweep the discovery queue would keep serving them in the old order forever.

Discovery ordering is the whole point. `refill_queue` selects by
`priority DESC`, so raising a software employer from 30 to 70 is what puts it
in front of the 74,000 state-registry shells rather than behind them.

Usage
-----
    PYTHONPATH=shared/python python scripts/recompute_company_priority.py --dry-run
    PYTHONPATH=shared/python python scripts/recompute_company_priority.py --apply

`--dry-run` is the default and writes nothing. It reports exactly what would
change, which is the only honest way to look at a sweep over a live collection.
"""
from __future__ import annotations

import argparse
import collections
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from pymongo import UpdateOne  # noqa: E402

from services import company_registry_service as reg  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the recomputed signals and priority")
    ap.add_argument("--batch", type=int, default=1000)
    args = ap.parse_args()

    col = reg._db()[reg.COMPANIES]
    total = col.estimated_document_count()
    print(f"companies: {total:,}   mode: {'APPLY' if args.apply else 'dry-run'}")

    cur = col.find({}, {"company_id": 1, "name": 1, "sic": 1,
                        "sic_description": 1, "signals": 1, "priority": 1,
                        "domain": 1, "jurisdictions": 1}).batch_size(2000)

    ops = []
    seen = changed = 0
    before = collections.Counter()
    after = collections.Counter()
    kinds = collections.Counter()
    t0 = time.time()
    for doc in cur:
        seen += 1
        merged = dict(doc.get("signals") or {})
        derived = reg.signals_for_company(doc)
        kinds.update(derived.keys() or ["none"])
        merged.update(derived)
        if doc.get("domain"):
            merged["known_domain"] = True
        if len(doc.get("jurisdictions") or []) > 1:
            merged["multi_state"] = True
        new_priority = reg.score_priority(merged)
        old_priority = int(doc.get("priority") or 0)
        before[old_priority] += 1
        after[new_priority] += 1
        if new_priority == old_priority and derived.items() <= (doc.get("signals") or {}).items():
            continue
        changed += 1
        if args.apply:
            update = {"priority": new_priority}
            for key, val in derived.items():
                update[f"signals.{key}"] = val
            ops.append(UpdateOne({"company_id": doc["company_id"]},
                                 {"$set": update}))
            if len(ops) >= args.batch:
                col.bulk_write(ops, ordered=False)
                ops = []
        if seen % 20000 == 0:
            print(f"  {seen:,}/{total:,}  changed={changed:,}  {time.time()-t0:.0f}s")
    if ops:
        col.bulk_write(ops, ordered=False)

    print(f"\nscanned {seen:,}, would change {changed:,}  ({time.time()-t0:.0f}s)")
    print("\nclassification:")
    for key, n in kinds.most_common():
        print(f"  {key:16s} {n:>7,}")
    print("\npriority distribution (top bands):")
    bands = sorted(set(list(before) + list(after)), reverse=True)[:12]
    print(f"  {'priority':>8}  {'before':>8}  {'after':>8}")
    for p in bands:
        print(f"  {p:>8}  {before.get(p,0):>8,}  {after.get(p,0):>8,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
