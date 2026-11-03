#!/usr/bin/env python3
"""reread_stale_requirements.py - bring every stored JD up to the current reader.

DRY-RUN BY DEFAULT.

Why this exists
---------------
`requirements` (years, degree, work mode, sponsorship, skills, the `jd`
block) is written by the reader at crawl time and stamped with
`job_inventory_service.EXTRACTOR_VERSION`. A posting whose listing never
changes was never re-read, so a reader upgrade reached the inventory only as
fast as postings happened to change. Measured 2026-11-03: 96k of 282k active
postings carried an old reading (74,660 Workday at v8, before `jd_profile`
existed) and 84k of them already had their full body in our own store.

The crawler now re-reads those itself (`job_ingest_worker._reread_stored`,
`JOB_INGEST_REREAD_PER_SHARD` per tick). This script does the backlog at once,
through the SAME function, so the two can never disagree. It reads bodies from
our own object store only: no request reaches an employer.

    python scripts/reread_stale_requirements.py                 # dry run: counts + a sampled preview
    python scripts/reread_stale_requirements.py --apply
    python scripts/reread_stale_requirements.py --apply --source workday --limit 20000
"""
from __future__ import annotations

import argparse
import collections
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

PAGE = 500


def _stale_query(inv, source):
    q = {"status": "active",
         "requirements.extractor_version": {"$ne": inv.EXTRACTOR_VERSION},
         "$or": [{"description_ref": {"$nin": [None, ""]}},
                 {"description_chars": {"$gt": 200}}]}
    if source:
        q["source"] = source
    return q


def _preview(inv, fit_score, coll, q, n):
    """What the current reader would write for a sample, without writing it."""
    changes = collections.Counter()
    t0, done = time.monotonic(), 0
    for doc in coll.find(q, {"source": 1, "source_job_id": 1, "title_raw": 1, "content_hash": 1,
                             "description_ref": 1, "description_chars": 1, "requirements": 1}).limit(n):
        body = inv.stored_description(doc.get("source"), doc)
        if not body.strip():
            changes["no_body"] += 1
            continue
        new = fit_score.extract_requirements(body, doc.get("title_raw") or "")
        old = doc.get("requirements") or {}
        done += 1
        changes["gains_jd_block"] += bool(new.get("jd")) and not old.get("jd")
        for f in ("required_years", "required_degree_level", "work_mode", "job_level"):
            changes[f"changes_{f}"] += (new.get(f) or None) != (old.get(f) or None)
        changes["skills_added"] += len(set(new.get("required_skills") or []) - set(old.get("required_skills") or []))
        changes["skills_dropped"] += len(set(old.get("required_skills") or []) - set(new.get("required_skills") or []))
    per = (time.monotonic() - t0) / max(1, done)
    return changes, done, per


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    ap.add_argument("--source", default="", help="only this ingest source (workday, oracle, ...)")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many postings")
    ap.add_argument("--sample", type=int, default=300, help="dry run: postings to preview")
    args = ap.parse_args()

    from collections import defaultdict
    from services import fit_score, job_ingest_worker as w, job_inventory_service as inv
    from utils.db_connect import DBConnect

    coll = DBConnect().get_db().job_postings
    q = _stale_query(inv, args.source.strip() or None)
    by_ver = collections.Counter()
    by_src = collections.Counter()
    for d in coll.aggregate([{"$match": q}, {"$group": {
            "_id": {"v": "$requirements.extractor_version", "s": "$source"}, "n": {"$sum": 1}}}]):
        by_ver[d["_id"].get("v")] += d["n"]
        by_src[d["_id"].get("s")] += d["n"]
    total = sum(by_src.values())
    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | reader v{inv.EXTRACTOR_VERSION} | "
          f"stale active postings with a stored body: {total:,}")
    print(f"  by reader version: {dict(by_ver.most_common())}")
    print(f"  by source: {dict(by_src.most_common())}")

    if not args.apply:
        changes, done, per = _preview(inv, fit_score, coll, q, max(1, args.sample))
        print(f"\npreview of {done} (nothing written):")
        for k, v in sorted(changes.items()):
            print(f"  {k:28s} {v:>6}  ({100 * v / max(1, done):.0f}% of sample)")
        print(f"  ~{per * 1000:.0f} ms per posting -> ~{per * total / 60:.0f} min for all {total:,}")
        print("\ndry run - nothing was written. Re-run with --apply.")
        return 0

    counts: dict = defaultdict(int)
    now = datetime.now(timezone.utc)
    last_id, seen, t0 = None, 0, time.monotonic()
    while True:
        page_q = dict(q, **({"_id": {"$gt": last_id}} if last_id is not None else {}))
        page = list(coll.find(page_q, {"_id": 1, "source": 1, "source_job_id": 1, "title_raw": 1,
                                       "content_hash": 1, "description_ref": 1,
                                       "description_chars": 1}).sort("_id", 1).limit(PAGE))
        if not page:
            break
        last_id = page[-1]["_id"]
        by_source = collections.defaultdict(list)
        for d in page:
            by_source[d.get("source")].append((d, d.get("title_raw") or ""))
        for src, pending in by_source.items():
            w.REREAD_PER_SHARD = len(pending)
            w._reread_stored(src, pending, deadline=float("inf"), stop=None, counts=counts, now=now)
        seen += len(page)
        rate = seen / max(1e-6, time.monotonic() - t0)
        print(f"  {seen:,}/{total:,}  parsed={counts['reread_parsed']:,} reused={counts['reread_reused']:,} "
              f"no_body={counts['reread_no_body']:,} failed={counts['reread_failed']:,}  "
              f"({rate:.0f}/s)", flush=True)
        if args.limit and seen >= args.limit:
            break
    print(f"done: {dict(counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
