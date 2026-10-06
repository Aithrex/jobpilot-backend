#!/usr/bin/env python3
"""repair_title_role_families.py — give stored postings the role family their
TITLE states.

DRY-RUN BY DEFAULT.

Why this exists
---------------
`role_family` is the key the For-you board retrieves on. It is stamped from
the title at insert, and - when the title names none - upgraded from the body.
Cross-checking every real user's board against employers' own postings
(2026-10-06) found 39 of 128 in-inventory roles missing because that key was
wrong:

  * "Software Engineering Intern, Android" -> `hr`, "Software Engineering
    Winter Internship" -> `teaching`, "PhD Intern, Data Science" -> `design`:
    the title rules knew "software engineer" and not "software engineering",
    so the title named no family and the body's boilerplate supplied one.
  * "Software Engineer, Test Frameworks & Tooling" and "Data Engineer,
    Hardware & Consumer Devices" -> `hardware_engineering`: a team name read
    as a physical-domain qualifier.

`fit_score.classify_role` is fixed. The rule this applies is the module's own:
**a family derived from the title is never overwritten by one derived from
the body.** So wherever the title now states a family and the stored one
differs, the title wins. A title that states none (`other`) changes nothing.

    python scripts/repair_title_role_families.py           # dry run
    python scripts/repair_title_role_families.py --apply
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))


def new_family(doc) -> str:
    """The family to write, or "" to leave the posting alone."""
    from services import job_inventory_service as inv
    stated = inv.role_family_of(str(doc.get("title_raw") or ""))
    if stated in ("", "other") or stated == (doc.get("role_family") or "other"):
        return ""
    return stated


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    args = ap.parse_args()

    from pymongo import UpdateOne
    from utils.db_connect import DBConnect

    jp = DBConnect().get_db().job_postings
    q = {"status": "active"}
    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | active postings: {jp.count_documents(q):,}")
    moves: collections.Counter = collections.Counter()
    samples: dict = collections.defaultdict(list)
    ops, changed = [], 0
    for doc in jp.find(q, {"_id": 1, "title_raw": 1, "role_family": 1}).batch_size(2000):
        fam = new_family(doc)
        if not fam:
            continue
        changed += 1
        key = (doc.get("role_family") or "other", fam)
        moves[key] += 1
        if len(samples[key]) < 3:
            samples[key].append(str(doc.get("title_raw") or "")[:52])
        if args.apply:
            ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"role_family": fam}}))
            if len(ops) >= 500:
                jp.bulk_write(ops, ordered=False)
                ops = []
    if ops:
        jp.bulk_write(ops, ordered=False)
    print(f"postings whose family changes: {changed:,}")
    for (old, new), n in moves.most_common(40):
        print(f"  {old:>22} -> {new:<22} {n:>6,}   e.g. {samples[(old, new)]}")
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
