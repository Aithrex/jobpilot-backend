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
    ap.add_argument("--no-portal", action="store_true",
                    help="instead: sponsors filed `no_portal` (a website, no board found). "
                         "Worth re-reading when a new board reader ships - ADP alone was "
                         "on 10 of 420 such sponsors sampled 2026-10-07.")
    args = ap.parse_args()
    query = dict(QUERY)
    reason = REASON
    if args.no_portal:
        reason = "sponsor_new_readers"
        query = {"signals.h1b": True, "discovery_status": "no_portal",
                 "rearmed_reason": {"$ne": reason}}

    from services import company_registry_service as reg
    from utils.db_connect import DBConnect

    companies = DBConnect().get_db().companies
    total = companies.count_documents(query)
    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | "
          f"{'no_portal sponsors' if args.no_portal else 'deferred sponsors with no domain'}: {total:,}")
    for doc in companies.find(query, {"name": 1, "signals.h1b_approvals": 1}) \
            .sort("signals.h1b_approvals", -1).limit(8):
        print(f"    {int((doc.get('signals') or {}).get('h1b_approvals') or 0):>6}  {doc.get('name')}")
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")
        return 0
    res = companies.update_many(query, {"$set": {
        "discovery_status": reg.DISCOVERY_PENDING, "rearmed_reason": reason}})
    print(f"re-armed: {res.modified_count:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
