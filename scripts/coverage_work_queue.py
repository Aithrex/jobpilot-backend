#!/usr/bin/env python3
"""coverage_work_queue.py — what is uncovered, why, and what fixing it buys.

READ-ONLY.

The coverage report says 33.4% and names the denominator. It does not say what
to DO next, and "1,749 employers are uncovered" is not a work queue — 1,365 of
them hold 219 postings between them, and one of them holds 839.

This ranks the uncovered technology denominator by what each employer is
BLOCKED ON, and weights every bucket by the postings behind it, so a sweep can
be pointed at the work that moves the number rather than at the longest list.

The buckets, in the order they are worth doing
-----------------------------------------------
1. `content_pending`   ownership already proven, content never confirmed. One
                       fetch each. This is the cheapest coverage in the system
                       and it is pure bookkeeping.
2. `endpoint_unproven` we know the board; we cannot yet show whose it is.
                       Needs a domain walk. Almost the entire posting-weighted
                       gap lives here.
3. `tier_a`            strategically named, whatever the posting count.
4. `source_no_endpoint` a `career_sources` row with no `(vendor, tenant)`. The
                       employer's site has to be fingerprinted first.
5. `no_source`         no row at all. Real discovery, and the thinnest yield.

Usage
-----
    PYTHONPATH=shared/python python scripts/coverage_work_queue.py
    PYTHONPATH=shared/python python scripts/coverage_work_queue.py \
        --bucket endpoint_unproven --ids > /tmp/queue.txt
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))


def _db():
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


import employer_coverage_report as rep                   # noqa: E402
from services import career_source_service as css        # noqa: E402
from services import employer_registry as er             # noqa: E402
from services import official_career_registry as ocr     # noqa: E402

BUCKET_CONTENT_PENDING = "content_pending"
BUCKET_ENDPOINT_UNPROVEN = "endpoint_unproven"
BUCKET_SOURCE_NO_ENDPOINT = "source_no_endpoint"
BUCKET_NO_SOURCE = "no_source"

#: Order is the order the work is worth doing, and the report prints it this
#: way so a reader cannot mistake the longest bucket for the most valuable.
BUCKET_ORDER: Tuple[str, ...] = (
    BUCKET_CONTENT_PENDING, BUCKET_ENDPOINT_UNPROVEN,
    BUCKET_SOURCE_NO_ENDPOINT, BUCKET_NO_SOURCE,
)


def build(db, *, mix_days: int = 30) -> Dict[str, Any]:
    built = rep.build(db, mix_days=mix_days)
    rows = built["employers"]

    sources_by_emp: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for src in db[css.CAREER_SOURCES].find(
            {}, {"_id": 0, "employer_id": 1, "state": 1, "ats_vendor": 1,
                 "tenant": 1, "ownership_verified": 1, "failure_code": 1,
                 "ownership_evidence": 1}):
        sources_by_emp[str(src.get("employer_id"))].append(src)

    tier_a_ids = {er.employer_id_for(domain=d, name=n)
                  for n, d in rep.strategic_tier_a()}

    out: List[Dict[str, Any]] = []
    for emp in rows:
        cov = emp.get("_coverage") or {}
        if cov.get("state") in css.COVERED_STATES:
            continue
        eid = emp["employer_id"]
        srcs = sources_by_emp.get(eid, [])
        with_endpoint = [s for s in srcs if s.get("tenant")]
        owned = [s for s in with_endpoint if s.get("ownership_verified")]
        if owned:
            bucket = BUCKET_CONTENT_PENDING
        elif with_endpoint:
            bucket = BUCKET_ENDPOINT_UNPROVEN
        elif srcs:
            bucket = BUCKET_SOURCE_NO_ENDPOINT
        else:
            bucket = BUCKET_NO_SOURCE
        mix = emp.get("_mix") or {}
        # How close the ownership rule is to being satisfied. `signals` counts
        # DISTINCT confirmed evidence classes, and the rule needs two — so a
        # source sitting at 1 is one signal from covered and a source at 0 has
        # never been probed. Those are very different pieces of work and the
        # queue has to tell them apart.
        classes: set = set()
        for s in with_endpoint:
            for ev in (s.get("ownership_evidence") or []):
                if not isinstance(ev, dict) or not ev.get("confirmed"):
                    continue
                cls = str(ev.get("signal_class") or "")
                if cls and cls != css.CLASS_HEURISTIC:
                    classes.add(cls)
        out.append({
            "employer_id": eid,
            "display_name": emp.get("display_name"),
            "domain": emp.get("canonical_domain"),
            "bucket": bucket,
            "tier_a": eid in tier_a_ids,
            "cohorts": emp.get("cohorts") or [],
            "categories": emp.get("employer_categories") or [],
            "sources": len(srcs),
            "endpoints": len(with_endpoint),
            "vendors": sorted({str(s.get("ats_vendor") or "") for s in with_endpoint
                               if s.get("ats_vendor")}),
            "evidence_classes": sorted(classes),
            "signals": len(classes),
            "active_postings": int(mix.get("active", 0)),
            "core_tech_postings": int(mix.get("core", 0)),
            "state": cov.get("state"),
        })

    # Priority: Tier-A first, then core-tech postings, then active postings,
    # then how close the evidence already is. A tie broken on evidence means a
    # sweep finishes the nearly-done before starting the never-started.
    out.sort(key=lambda r: (0 if r["tier_a"] else 1,
                            -r["core_tech_postings"], -r["active_postings"],
                            -r["signals"], r["display_name"] or ""))
    return {"queue": out, "denominator": len(rows),
            "covered": len(rows) - len(out)}


def summarize(queue: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_bucket: Dict[str, Dict[str, int]] = {
        b: {"employers": 0, "active_postings": 0, "core_tech_postings": 0,
            "tier_a": 0, "one_signal_away": 0} for b in BUCKET_ORDER}
    for row in queue:
        b = by_bucket[row["bucket"]]
        b["employers"] += 1
        b["active_postings"] += row["active_postings"]
        b["core_tech_postings"] += row["core_tech_postings"]
        b["tier_a"] += 1 if row["tier_a"] else 0
        b["one_signal_away"] += 1 if row["signals"] == 1 else 0
    return by_bucket


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default="", choices=("",) + BUCKET_ORDER)
    ap.add_argument("--tier-a-only", action="store_true")
    ap.add_argument("--min-core", type=int, default=0)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--ids", action="store_true",
                    help="print ONLY employer ids, one per line, for piping")
    ap.add_argument("--mix-days", type=int, default=30)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    db = _db()
    built = build(db, mix_days=args.mix_days)
    queue = built["queue"]
    if args.bucket:
        queue = [r for r in queue if r["bucket"] == args.bucket]
    if args.tier_a_only:
        queue = [r for r in queue if r["tier_a"]]
    if args.min_core:
        queue = [r for r in queue if r["core_tech_postings"] >= args.min_core]

    if args.ids:
        for row in queue[:args.limit or len(queue)]:
            print(row["employer_id"])
        return 0

    buckets = summarize(built["queue"])
    print(f"technology denominator {built['denominator']:,}   "
          f"covered {built['covered']:,}   "
          f"uncovered {len(built['queue']):,}")
    print(f"\n{'bucket':<22}{'employers':>10}{'core-tech':>11}{'active':>10}"
          f"{'tier-A':>8}{'1 signal':>10}")
    for name in BUCKET_ORDER:
        b = buckets[name]
        print(f"  {name:<20}{b['employers']:>10,}{b['core_tech_postings']:>11,}"
              f"{b['active_postings']:>10,}{b['tier_a']:>8,}"
              f"{b['one_signal_away']:>10,}")

    print(f"\ntop {min(args.limit, len(queue))} by priority:")
    print(f"  {'employer':<34}{'bucket':<20}{'core':>7}{'sig':>5}  vendors")
    for row in queue[:args.limit]:
        star = "*" if row["tier_a"] else " "
        print(f"{star} {str(row['display_name'])[:32]:<34}{row['bucket']:<20}"
              f"{row['core_tech_postings']:>7,}{row['signals']:>5}  "
              f"{','.join(row['vendors'][:2])}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"buckets": buckets, "queue": built["queue"],
             "denominator": built["denominator"], "covered": built["covered"]},
            indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
