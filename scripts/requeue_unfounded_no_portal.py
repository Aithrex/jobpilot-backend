#!/usr/bin/env python3
"""Re-open `no_portal` discovery verdicts that carry no evidence.

`no_portal` is a CLAIM about an employer - "this company has no careers page" -
and it is terminal: `refill_queue` never looks at it again. So a `no_portal`
written by a run that never actually read a page is a permanent false negative,
and unlike a `deferred` row nothing corrects it.

`career_portal_discovery._inconclusive` now prevents new ones, and `_persist`
now records a reason on every terminal state. Neither helps the rows already in
the collection. Measured on live `aspirely_db` (2026-08-27): 2,059 `no_portal`
queue rows, of which 2,058 recorded an EMPTY detail - so the record cannot show
whether a page was ever fetched. Among the companies filed that way: NVIDIA,
Apple, Microsoft, Salesforce, Broadcom, SAP, Dell, Palantir and Shopify. All
nine plainly have a careers page.

The rule here is deliberately narrow. A row is re-opened only when the record
does not support the claim:

  * the queue row's `detail` is empty (no evidence either way), AND
  * the company found no endpoints.

A `no_portal` that DID record what it read is left alone - that one is a
finding, not a gap. Re-opened rows go back to `pending`, so the fixed pipeline
re-derives the right answer rather than this script guessing it.

Usage:
    PYTHONPATH=shared/python python scripts/requeue_unfounded_no_portal.py
    PYTHONPATH=shared/python python scripts/requeue_unfounded_no_portal.py --apply
"""
from __future__ import annotations

import argparse
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

from services import company_registry_service as reg  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = reg._db()
    queue, companies = db[reg.QUEUE], db[reg.COMPANIES]

    unfounded = {"state": reg.DISCOVERY_NO_PORTAL,
                 "$or": [{"detail": ""}, {"detail": None},
                         {"detail": {"$exists": False}}],
                 "endpoints_found": {"$in": [0, None]}}
    total = queue.count_documents({"state": reg.DISCOVERY_NO_PORTAL})
    n = queue.count_documents(unfounded)
    print(f"no_portal rows: {total:,}   with no supporting evidence: {n:,}")

    sample = list(queue.find(unfounded, {"company_id": 1}).limit(15))
    names = {c["company_id"]: c.get("name") for c in companies.find(
        {"company_id": {"$in": [s["company_id"] for s in sample]}},
        {"company_id": 1, "name": 1})}
    print("\nsample:")
    for s in sample:
        print(f"   {names.get(s['company_id'], s['company_id'])}")

    if not args.apply:
        print(f"\ndry-run: would re-open {n:,} rows as pending "
              f"(pass --apply to write)")
        return 0

    t0 = time.time()
    ids = [d["company_id"] for d in queue.find(unfounded, {"company_id": 1})]
    q = queue.update_many(unfounded,
                          {"$set": {"state": reg.DISCOVERY_PENDING,
                                    "lease_owner": None, "lease_expiry": None,
                                    "detail": "re-opened: no_portal recorded "
                                              "without evidence"}})
    c = companies.update_many(
        {"company_id": {"$in": ids},
         "discovery_status": reg.DISCOVERY_NO_PORTAL},
        {"$set": {"discovery_status": reg.DISCOVERY_PENDING}})
    print(f"\nre-opened {q.modified_count:,} queue rows and "
          f"{c.modified_count:,} company rows in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
