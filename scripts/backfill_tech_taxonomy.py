#!/usr/bin/env python3
"""Stamp `tech.*` and the canonical dedupe identity onto existing postings.

DRY RUN BY DEFAULT. `--apply` is the only thing that writes, and what it
writes is ADDITIVE: new fields on existing documents. It never deletes a
posting, never edits `status`, `posted_at`, `compensation` or any other field
the crawler owns, and it is reversible with a single `$unset`.

Why the backfill matters
------------------------
`normalize_posting` writes `tech` and `dedupe_group_id` from now on, but the
227,914 active postings already in `job_postings` predate them. Until they
carry the fields:

* every tech-feed search has to classify candidate TITLES at read time -
  measured at ~263 ms per search for 2,000 candidates, and title-only, so it
  cannot resolve the ambiguous titles ("Technology Analyst", "Member of
  Technical Staff") that the ingest-time classifier resolves from the body;
* the scope filter cannot run in Mongo, so retrieval has to over-fetch by
  ~4.4x (2,000 candidates to yield 454 core-tech rows) and the
  `active_tech_scope_posted_at` index cannot be used at all;
* `_dedupe` falls back to the display-string rule, which is the rule that
  missed cross-source duplicates in the first place.

What it CANNOT recover
----------------------
Job descriptions live in S3 under their content hash and are not re-fetched
here; classification therefore runs on TITLE plus whatever `requirements` the
ingest parser already extracted. That is strictly better than the read-time
path (which has only the title) and strictly worse than a fresh ingest (which
has the whole body). Rows classified here are marked
`tech.backfill_source: "title+requirements"` so a later pass can tell them
apart and re-run them with the body if that is ever worth doing.

Usage
-----
    PYTHONPATH=shared/python python scripts/backfill_tech_taxonomy.py            # dry run
    PYTHONPATH=shared/python python scripts/backfill_tech_taxonomy.py --sample 5000
    PYTHONPATH=shared/python python scripts/backfill_tech_taxonomy.py --apply --batch 1000
    PYTHONPATH=shared/python python scripts/backfill_tech_taxonomy.py --rollback --apply
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

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


_FIELDS = {"_id": 0, "job_id": 1, "title_raw": 1, "title_normalized": 1,
           "employer_id": 1, "ats": 1, "source": 1, "source_job_id": 1,
           "canonical_url": 1, "content_hash": 1, "requirements": 1,
           "role_family": 1, "tech": 1, "dedupe_group_id": 1}


def _classify(doc: Dict[str, Any]) -> Dict[str, Any]:
    from services import tech_taxonomy as tx
    from services import job_inventory_service as inv

    reqs = doc.get("requirements") if isinstance(doc.get("requirements"), dict) else None
    verdict = tx.classify(
        doc.get("title_raw"),
        description="",
        department="",
        skills=(reqs or {}).get("skills"),
        requirements=reqs,
    ).to_doc()
    verdict["backfill_source"] = "title+requirements"
    verdict["backfilled_at"] = datetime.now(timezone.utc)

    identity = inv.compute_dedupe_identity(
        employer_id=str(doc.get("employer_id") or ""),
        ats=str(doc.get("ats") or ""),
        source=str(doc.get("source") or ""),
        source_job_id=str(doc.get("source_job_id") or ""),
        canonical_url=str(doc.get("canonical_url") or ""),
        content_hash=str(doc.get("content_hash") or ""),
        title_normalized=str(doc.get("title_raw") or ""),
    )
    return {"tech": verdict, **identity}


def run(*, apply: bool, batch: int, sample: int, only_missing: bool,
        status: str) -> int:
    from pymongo import UpdateOne

    db = _db()
    query: Dict[str, Any] = {}
    if status != "all":
        query["status"] = status
    if only_missing:
        query["tech.taxonomy_version"] = {"$exists": False}

    total = db.job_postings.count_documents(query)
    print(f"matching documents: {total:,}   (apply={apply}, sample={sample or 'none'})")
    if not total:
        return 0

    cursor = db.job_postings.find(query, _FIELDS).batch_size(500)
    if sample:
        cursor = cursor.limit(sample)

    scopes = collections.Counter()
    families = collections.Counter()
    changed_family = collections.Counter()
    identity_kinds = collections.Counter()
    ops: List[Any] = []
    seen = written = 0
    started = time.perf_counter()

    for doc in cursor:
        seen += 1
        update = _classify(doc)
        scopes[update["tech"]["tech_scope"]] += 1
        fam = update["tech"].get("role_family")
        if fam:
            families[fam] += 1
        legacy = doc.get("role_family") or "other"
        changed_family[(legacy, update["tech"]["tech_scope"])] += 1
        identity_kinds["has_group" if update.get("dedupe_group_id")
                       else "no_group"] += 1
        if apply:
            ops.append(UpdateOne({"job_id": doc["job_id"]}, {"$set": update}))
            if len(ops) >= batch:
                res = db.job_postings.bulk_write(ops, ordered=False)
                written += res.modified_count
                ops = []
                print(f"  ... {seen:,}/{total:,} scanned, {written:,} written",
                      flush=True)
        elif seen % 25000 == 0:
            print(f"  ... {seen:,} scanned", flush=True)

    if apply and ops:
        res = db.job_postings.bulk_write(ops, ordered=False)
        written += res.modified_count

    took = time.perf_counter() - started
    print(f"\nscanned {seen:,} in {took:.1f}s ({seen / max(took, 0.001):,.0f}/s)")
    if apply:
        print(f"WROTE {written:,} documents")
    else:
        print("DRY RUN - nothing written")

    print("\n  tech_scope distribution:")
    for k, v in scopes.most_common():
        print(f"    {k:10s} {v:>8,}  {v / max(1, seen):6.1%}")
    print("\n  role_family (core + adjacent):")
    for k, v in families.most_common(20):
        print(f"    {k:24s} {v:>8,}")
    print("\n  canonical identity:")
    for k, v in identity_kinds.most_common():
        print(f"    {k:10s} {v:>8,}")
    print("\n  legacy role_family -> new tech_scope (top 20):")
    for (legacy, scope), v in changed_family.most_common(20):
        print(f"    {str(legacy):22s} -> {scope:9s} {v:>8,}")
    return 0


def rollback(*, apply: bool) -> int:
    db = _db()
    q = {"tech.backfill_source": "title+requirements"}
    n = db.job_postings.count_documents(q)
    print(f"documents carrying a backfilled `tech`: {n:,}")
    if not apply:
        print("DRY RUN - pass --apply to $unset tech/dedupe_group_id/"
              "variant_group_id/canonical_url_key on these documents")
        return 0
    res = db.job_postings.update_many(q, {"$unset": {
        "tech": "", "dedupe_group_id": "", "variant_group_id": "",
        "canonical_url_key": ""}})
    print(f"UNSET on {res.modified_count:,} documents")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually write")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--sample", type=int, default=0,
                    help="stop after N documents (dry-run sizing)")
    ap.add_argument("--all", action="store_true",
                    help="include documents that already carry a `tech` verdict")
    ap.add_argument("--status", default="active",
                    choices=["active", "closed", "expired", "all"])
    ap.add_argument("--rollback", action="store_true",
                    help="$unset what this script wrote")
    args = ap.parse_args()

    if args.rollback:
        return rollback(apply=args.apply)
    return run(apply=args.apply, batch=args.batch, sample=args.sample,
               only_missing=not args.all, status=args.status)


if __name__ == "__main__":
    raise SystemExit(main())
