#!/usr/bin/env python3
"""rearm_sponsors_without_domain.py — give H-1B sponsors discovery could not
find a website for another attempt, now that it can ask for a suggestion.

DRY-RUN BY DEFAULT.

Discovery used to derive a company's domain from its name alone, and a company
whose name does not spell its domain (American Airlines -> aa.com) was
`deferred` with no domain - 71% of the sponsors deferred by 2026-10-07. A
deferred company is only retried after `DISCOVERY_RETRY_AFTER_DAYS` (7); this
puts those back on the queue now. `--apply` only ever sets
`discovery_status: pending` (and why) on a company that is `deferred`, is an
H-1B sponsor and has no domain recorded. Nothing is deleted.

    python scripts/rearm_sponsors_without_domain.py           # dry run
    python scripts/rearm_sponsors_without_domain.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

REASON = "sponsor_domain_lookup"
QUERY = {"signals.h1b": True, "discovery_status": "deferred",
         "domain": {"$in": [None, ""]}, "rearmed_reason": {"$ne": REASON}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    args = ap.parse_args()

    from services import company_registry_service as reg
    from utils.db_connect import DBConnect

    companies = DBConnect().get_db().companies
    total = companies.count_documents(QUERY)
    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | "
          f"deferred sponsors with no domain: {total:,}")
    for doc in companies.find(QUERY, {"name": 1, "signals.h1b_approvals": 1}) \
            .sort("signals.h1b_approvals", -1).limit(8):
        print(f"    {int((doc.get('signals') or {}).get('h1b_approvals') or 0):>6}  {doc.get('name')}")
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")
        return 0
    res = companies.update_many(QUERY, {"$set": {
        "discovery_status": reg.DISCOVERY_PENDING, "rearmed_reason": REASON}})
    print(f"re-armed: {res.modified_count:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
