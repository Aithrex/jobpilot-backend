#!/usr/bin/env python3
"""export_company_status.py — dump every harvested company and what we know.

One tab-separated row per company in `companies`, so the registry can be read,
diffed and sorted without a Mongo client. Written for the question "which
employers do we actually have, and did we find their careers page?", which is
otherwise only answerable by an aggregation nobody remembers.

    PYTHONPATH=shared/python python3 scripts/export_company_status.py
    PYTHONPATH=shared/python python3 scripts/export_company_status.py --status done
    PYTHONPATH=shared/python python3 scripts/export_company_status.py -o /tmp/x.tsv

The `status` column is the load-bearing one, and its four values do not mean
equally strong things:

    done       a careers page was found and at least one ATS endpoint recorded
    no_portal  we FETCHED the employer's site and it had no ATS we recognise
    deferred   we did not get a fair look — the domain would not resolve, the
               site rate-limited us, or robots disallowed it. Retryable, and
               `refill_queue` picks these up again
    pending    not probed yet

`deferred` exists because `no_portal` used to absorb it, which made the file a
liar: 4,420 companies were recorded as having no careers page when we had never
fetched a page at all.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Optional

sys.path.insert(0, "shared/python")

#: Ordered so the rows worth reading come first: employers we can crawl, then
#: the ones still owed an answer, then the ones we have answered negatively.
_STATUS_ORDER = {"done": 0, "deferred": 1, "pending": 2, "no_portal": 3}

_COLUMNS = ("name", "state", "domain", "status", "endpoints", "priority",
            "ticker", "sources", "last_discovered_at", "company_id")


def _clean(value: Any) -> str:
    """TSV-safe scalar. Tabs and newlines in a company name would shift columns."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, (list, tuple)):
        return ",".join(_clean(v) for v in value)
    text = str(value)
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--out",
                    default="docs/coverage/company-discovery-status.tsv",
                    help="output path (default: %(default)s)")
    ap.add_argument("--status", action="append", default=[],
                    help="only this status (repeatable)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap rows, for a quick look")
    args = ap.parse_args(argv)

    from utils.db_connect import DBConnect

    db = DBConnect().get_db()
    query: Dict[str, Any] = {}
    if args.status:
        query["discovery_status"] = {"$in": args.status}

    projection = {"_id": 0, "name": 1, "state": 1, "domain": 1,
                  "discovery_status": 1, "endpoints_found": 1, "priority": 1,
                  "ticker": 1, "sources": 1, "last_discovered_at": 1,
                  "company_id": 1}

    rows = []
    cursor = db["companies"].find(query, projection)
    if args.limit:
        cursor = cursor.limit(int(args.limit))
    for doc in cursor:
        rows.append(doc)

    # Sorted in Python rather than Mongo: the collection has no index on this
    # composite order, and a server-side sort of 80k+ documents without one is
    # a memory-limit error rather than a slow query.
    rows.sort(key=lambda d: (
        _STATUS_ORDER.get(str(d.get("discovery_status") or ""), 9),
        -int(d.get("priority") or 0),
        str(d.get("name") or "").upper()))

    counts = Counter(str(d.get("discovery_status") or "?") for d in rows)
    with_domain = sum(1 for d in rows if d.get("domain"))
    endpoints = sum(int(d.get("endpoints_found") or 0) for d in rows)

    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# JobPilot company registry — {len(rows):,} companies\n")
        fh.write(f"# generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC "
                 f"by scripts/export_company_status.py\n#\n")
        fh.write("# status:\n")
        fh.write("#   done       careers page found, >=1 ATS endpoint recorded\n")
        fh.write("#   no_portal  site was FETCHED and had no ATS we recognise\n")
        fh.write("#   deferred   no fair look yet (domain unresolved, rate-limited,\n")
        fh.write("#              robots-disallowed) — retryable\n")
        fh.write("#   pending    not probed yet\n#\n")
        for status, _rank in sorted(_STATUS_ORDER.items(), key=lambda kv: kv[1]):
            if counts.get(status):
                fh.write(f"#   {status:<10} {counts[status]:>7,}\n")
        fh.write(f"#\n# with a resolved domain: {with_domain:,}\n")
        fh.write(f"# ATS endpoints recorded:  {endpoints:,}\n#\n")
        fh.write("\t".join(_COLUMNS) + "\n")
        for doc in rows:
            fh.write("\t".join((
                _clean(doc.get("name")),
                _clean(doc.get("state")),
                _clean(doc.get("domain")),
                _clean(doc.get("discovery_status")),
                _clean(doc.get("endpoints_found") or 0),
                _clean(doc.get("priority") or 0),
                _clean(doc.get("ticker")),
                _clean(doc.get("sources")),
                _clean(doc.get("last_discovered_at")),
                _clean(doc.get("company_id")),
            )) + "\n")

    print(f"wrote {args.out}: {len(rows):,} companies")
    for status, _rank in sorted(_STATUS_ORDER.items(), key=lambda kv: kv[1]):
        if counts.get(status):
            print(f"  {status:<10} {counts[status]:>7,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
