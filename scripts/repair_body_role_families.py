#!/usr/bin/env python3
"""repair_body_role_families.py — take back ENGINEERING role families that
non-technical postings borrowed from their job description.

DRY-RUN BY DEFAULT.

Why this exists
---------------
`mark_requirements_parsed` let a posting whose TITLE named no role family
adopt the family its BODY implied. A body describes the company as much as
the job, so GitLab's "Social Impact Manager" became `devops`, Oscar Health's
"Provider Network Associate" `software_engineering`, and Applied Materials'
"Field Service Engineer" a software role - and all three were retrieved for a
backend engineer's board (live, 2026-10-03). The upgrade now requires the
title itself to be technical (`job_inventory_service.title_admits_body_family`);
this applies the same rule to postings stored before it.

A posting is reset to `other` only when ALL hold:
  * its stored family is an engineering one (`ENGINEERING_FAMILIES`);
  * its title alone names no family (`role_family_of(title) == "other"`), so
    the stored family can only have come from the body;
  * the title is not technical (`title_admits_body_family` is False) - it
    names no technical domain, does not classify as tech on its own, and does
    not pair an engineering role noun with a core-tech classification.
`other` is what insert writes for these titles, so nothing is invented.

    python scripts/repair_body_role_families.py           # dry run
    python scripts/repair_body_role_families.py --apply
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))


def should_reset(doc) -> bool:
    from services import job_inventory_service as inv
    title = str(doc.get("title_raw") or "")
    return doc.get("role_family") in inv.ENGINEERING_FAMILIES \
        and inv.role_family_of(title) == "other" \
        and not inv.title_admits_body_family(
            title, (doc.get("tech") or {}).get("tech_scope") if isinstance(doc.get("tech"), dict) else None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    args = ap.parse_args()

    from pymongo import UpdateOne
    from utils.db_connect import DBConnect
    from services import job_inventory_service as inv

    jp = DBConnect().get_db().job_postings
    q = {"status": "active", "role_family": {"$in": sorted(inv.ENGINEERING_FAMILIES)}}
    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | engineering-family postings: "
          f"{jp.count_documents(q):,}")
    by_family: collections.Counter = collections.Counter()
    titles: collections.Counter = collections.Counter()
    ops = []
    for doc in jp.find(q, {"_id": 1, "title_raw": 1, "role_family": 1, "tech.tech_scope": 1}).batch_size(2000):
        if not should_reset(doc):
            continue
        by_family[doc["role_family"]] += 1
        titles[str(doc.get("title_raw") or "")[:60]] += 1
        if args.apply:
            ops.append(UpdateOne({"_id": doc["_id"], "role_family": doc["role_family"]},
                                 {"$set": {"role_family": "other"}}))
            if len(ops) >= 500:
                jp.bulk_write(ops, ordered=False)
                ops = []
    if ops:
        jp.bulk_write(ops, ordered=False)
    print(f"reset to `other`: {sum(by_family.values()):,}  {dict(by_family)}")
    print("most common titles:")
    for t, n in titles.most_common(40):
        print(f"  {n:>5}  {t}")
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
