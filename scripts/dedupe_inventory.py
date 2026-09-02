#!/usr/bin/env python3
"""Close inventory rows that are the SAME requisition seen twice.

Two mechanisms, both measured on live `aspirely_db` (2026-09-02, 229,965
active postings):

Rule A — identical `canonical_url` (747 rows)
---------------------------------------------
Two ACTIVE rows pointing at one byte-identical apply URL are one job by
definition. Almost all are iCIMS (744), where the same requisition is reachable
from two tenant registrations.

Rule B — Oracle ORC multi-site (9,545 rows)
-------------------------------------------
`member_key` is `oracle_orc-<tenant>-<site>` and `source_job_id` is
`<member_key>:<raw_id>`, so ONE requisition served through two candidate-
experience sites on one tenant gets two `source_job_id`s and
`upsert_posting`'s idempotency on `(source, source_job_id)` structurally cannot
see them as the same row. Measured: tenant `eofd` serves requisition 749191 as
both `oracle_orc-eofd-cx_1` and `oracle_orc-eofd-cx_1001`, same title, URLs
differing only in the `/sites/CX_1` vs `/sites/CX_1001` segment. 135 Oracle
member_keys resolve to 111 distinct pods.

Why this is a cleanup and NOT a change to the identity contract
--------------------------------------------------------------
The tempting fix is to drop `site` from the Oracle `member_key`, or to make
`_identity_of` ignore it. Both are wrong here:

* Two CE sites on one tenant are not ALWAYS redundant — a "Students" site and a
  "Professionals" site can carry disjoint requisitions. Merging blindly would
  silently drop jobs, which is worse than showing one twice.
* `member_key` is the prefix a shard uses to decide which rows it owns
  (ADR-003). Two member_keys hash to two different shards, so letting both
  write one `source_job_id` would make two shards contend over closing it.

So this operates on EVIDENCE instead: it only collapses rows we have actually
observed carrying the same requisition id under one tenant, keeps the copy on
the site with the widest coverage, and closes the rest through the normal
`close_posting` path (which is the only thing that stamps `purge_at`).

Usage:
    PYTHONPATH=shared/python python scripts/dedupe_inventory.py
    PYTHONPATH=shared/python python scripts/dedupe_inventory.py --apply
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from services import job_inventory_service as inv               # noqa: E402

#: Trailing CE-site discriminator on an ORC member_key (`..._1`, `..._1001`).
_SITE_SUFFIX = re.compile(r"_\d+$")


def _keep_first(rows, key):
    """Order rows so the row we KEEP sorts first."""
    return sorted(rows, key=key)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    postings = inv._postings()
    stats = collections.Counter()

    # ---- load once; both rules need the same projection --------------------
    by_url = collections.defaultdict(list)
    orc = collections.defaultdict(list)
    site_size = collections.Counter()
    scanned = 0
    for d in postings.find(
        {"status": "active"},
        {"_id": 0, "source": 1, "source_job_id": 1, "canonical_url": 1,
         "tenant": 1, "first_seen_at": 1, "title_raw": 1},
    ).batch_size(4000):
        scanned += 1
        url = d.get("canonical_url")
        if url:
            by_url[url].append(d)
        if d.get("source") == "oracle":
            sid = str(d.get("source_job_id") or "")
            if ":" in sid:
                member_key, raw_id = sid.split(":", 1)
                site_size[member_key] += 1
                orc[(str(d.get("tenant") or ""), raw_id)].append((member_key, d))
    print(f"scanned {scanned:,} active postings\n")

    closed_ids = set()

    def close(row, reason):
        sid = str(row.get("source_job_id") or "")
        if sid in closed_ids:
            return
        closed_ids.add(sid)
        stats[reason] += 1
        if args.apply:
            inv.close_posting(row.get("source") or "", sid, reason=reason)

    # ---- Rule A: identical canonical_url -----------------------------------
    a_groups = 0
    for url, rows in by_url.items():
        if len(rows) < 2:
            continue
        a_groups += 1
        # Keep the EARLIEST first_seen_at: `first_seen_days_ago` is user-facing
        # and the honest answer is when we first saw the job, not this row.
        ordered = _keep_first(rows, lambda r: (str(r.get("first_seen_at") or ""),
                                               str(r.get("source_job_id") or "")))
        for row in ordered[1:]:
            close(row, "duplicate_canonical_url")
    print(f"Rule A  identical canonical_url : {a_groups:,} groups, "
          f"{stats['duplicate_canonical_url']:,} rows to close")

    # ---- Rule B: Oracle ORC, one requisition on several CE sites -----------
    b_groups = 0
    for (tenant, raw_id), entries in orc.items():
        if len(entries) < 2 or not tenant:
            continue
        pods = {_SITE_SUFFIX.sub("", mk) for mk, _ in entries}
        if len(pods) != 1:
            # Different ORC pods are different employers that happen to share a
            # requisition number. Not a duplicate.
            stats["orc_cross_pod_skipped"] += 1
            continue
        b_groups += 1
        # Keep the copy on the CE site with the widest coverage for this
        # tenant: that is the site a candidate is most likely to be sent to,
        # and it keeps one shard consistently owning the tenant's rows.
        ordered = _keep_first(entries, lambda e: (-site_size[e[0]], e[0]))
        for _member_key, row in ordered[1:]:
            close(row, "duplicate_orc_site")
    print(f"Rule B  ORC same tenant+req id  : {b_groups:,} groups, "
          f"{stats['duplicate_orc_site']:,} rows to close")

    print(f"\ntotal rows to close: {len(closed_ids):,} "
          f"({100 * len(closed_ids) / max(scanned, 1):.1f}% of active)")
    if stats["orc_cross_pod_skipped"]:
        print(f"skipped {stats['orc_cross_pod_skipped']:,} ORC groups spanning "
              f"different pods (distinct employers, not duplicates)")
    if not args.apply:
        print("\ndry-run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
