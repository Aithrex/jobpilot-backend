#!/usr/bin/env python3
"""repair_vetoed_software_titles.py — re-file stored postings two title rules
got wrong.

DRY-RUN BY DEFAULT.

1. `tech.tech_scope == "non_tech"` on a title that says software outright.
   A physical qualifier ("tooling", "manufacturing") vetoed the software
   reading even of "Software Engineer, Test Frameworks & Tooling", and the
   feed hides an engineering-family posting the taxonomy calls non-technical.
   Only `tech.*` is rewritten, from the title and the requirements already
   extracted, and only when the new verdict is no longer non_tech.

2. No level on a graduate intake named by class year ("Software Engineer Grad
   2027"). Re-read through `relevel_from_title`, which never blanks a level.

Deliberately NOT a taxonomy version bump: `backfill_tech_taxonomy` re-stamps
from the title alone, and running it over the inventory would replace verdicts
that were made with the whole description.

    python scripts/repair_vetoed_software_titles.py           # dry run
    python scripts/repair_vetoed_software_titles.py --apply
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

_GRAD_YEAR = re.compile(r"grad\w*\s+20\d\d|20\d\d\s+grad", re.IGNORECASE)
_UNSTATED = (None, "", "unknown")


def new_tech(doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The `tech` verdict to write, or None to leave the posting alone."""
    from services import tech_taxonomy as tx

    tech = doc.get("tech") if isinstance(doc.get("tech"), dict) else {}
    title = str(doc.get("title_raw") or "")
    if tech.get("tech_scope") != "non_tech":
        return None
    if not tx._EXPLICIT_SOFTWARE_TITLE_RE.search(tx._norm(title)):
        return None
    reqs = doc.get("requirements") if isinstance(doc.get("requirements"), dict) else None
    verdict = tx.classify(title, skills=(reqs or {}).get("skills"),
                          requirements=reqs).to_doc()
    if verdict.get("tech_scope") == "non_tech":
        return None
    # The level is not this script's to change here.
    if tech.get("seniority"):
        verdict["seniority"] = tech["seniority"]
    verdict["backfill_source"] = "title+requirements"
    return verdict


def owes_a_level(doc: Dict[str, Any]) -> bool:
    return doc.get("seniority") in _UNSTATED \
        and bool(_GRAD_YEAR.search(str(doc.get("title_raw") or "")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    args = ap.parse_args()

    from services import job_inventory_service as inv
    from utils.db_connect import DBConnect

    jp = DBConnect().get_db().job_postings
    fields = {"_id": 0, "job_id": 1, "title_raw": 1, "tech": 1, "requirements": 1,
              "seniority": 1, "source": 1, "source_job_id": 1, "role_family": 1}

    scope_moves: collections.Counter = collections.Counter()
    examples: Dict[str, list] = collections.defaultdict(list)
    scoped = 0
    for doc in jp.find({"status": "active", "tech.tech_scope": "non_tech",
                        "role_family": {"$in": sorted(inv.ENGINEERING_FAMILIES)}}, fields):
        verdict = new_tech(doc)
        if not verdict:
            continue
        scoped += 1
        key = f"non_tech -> {verdict['tech_scope']}"
        scope_moves[key] += 1
        if len(examples[key]) < 6:
            examples[key].append(str(doc.get("title_raw"))[:70])
        if args.apply:
            jp.update_one({"job_id": doc["job_id"]}, {"$set": {"tech": verdict}})

    levelled = 0
    level_moves: collections.Counter = collections.Counter()
    for doc in jp.find({"status": "active", "seniority": {"$in": list(_UNSTATED)},
                        "title_raw": {"$regex": "grad", "$options": "i"}}, fields):
        if not owes_a_level(doc):
            continue
        new = inv.seniority_of(doc.get("title_raw") or "")
        if new in _UNSTATED:
            continue
        levelled += 1
        level_moves[f"unknown -> {new}"] += 1
        if len(examples["level"]) < 6:
            examples["level"].append(str(doc.get("title_raw"))[:70])
        if args.apply:
            inv.relevel_from_title(doc["source"], doc["source_job_id"])

    print(f"mode: {'APPLY' if args.apply else 'dry-run'}")
    print(f"software titles no longer non_tech: {scoped}")
    for key, n in scope_moves.most_common():
        print(f"    {key:24} {n:>5}   e.g. {examples[key]}")
    print(f"graduate intakes given a level: {levelled}  {dict(level_moves)}")
    print(f"    e.g. {examples['level']}")
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
