#!/usr/bin/env python3
"""rederive_posting_levels.py — re-extract every active posting's requirements
and level from its FULL stored description, through the crawler's own write.

DRY-RUN BY DEFAULT.

Why this exists
---------------
fit-v2.3 / EXTRACTOR_VERSION 5 fixed how a posting's level is read
(`fit_score.level_from_signals`, `_required_years`). Measured on live
inventory 2026-10-02, before the fix:

  * 41,845 active postings were filed `entry`, including "Software Engineer
    II" (3+ years), "Program Manager 2" (6-8 years), "Private Client Advisor
    II" (12+ years) and "Business Intelligence Developer" (5 years).
  * `required_years` was the SMALLEST number in the body, so the scorer gave
    a two-year candidate 98 against a "6+ years" posting.

The crawler would apply the fix itself as it re-parses each shard, over days.
This applies it now, from bodies already in OUR S3 bucket - no employer's
servers are touched - and through `job_ingest_worker._persist_requirements`,
the exact function the crawl uses, so the two paths cannot disagree.

Run it only AFTER the v5 code is deployed to the ingest box: the old crawler
reads `extractor_version != 4` as "needs parsing" and would re-parse every row
this writes back to the old semantics.

What it never does
------------------
* It never reads a truncated body when a full one exists. Postings whose body
  was offloaded keep only a prefix inline, and the requirements section is
  usually at the END - which is how "6+ years" went missing in the first place.
* It never blanks a level: an extraction that finds none leaves `seniority`
  exactly as it was (`mark_requirements_parsed`).
* A posting with no readable body is re-levelled from its TITLE
  (`job_inventory_service.relevel_from_title`, v8) - the same reading insert
  gives it - and its requirements are left for the crawler to parse.

    python scripts/rederive_posting_levels.py                 # dry run
    python scripts/rederive_posting_levels.py --apply --workers 8
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

PROJECTION = {"_id": 0, "source": 1, "source_job_id": 1, "content_hash": 1,
              "title_raw": 1, "seniority": 1, "description_ref": 1,
              "description_text": 1, "employer_name": 1}


def full_body(doc: Dict[str, Any], get_jd: Optional[Callable[[str], Any]]) -> str:
    """The FULL description: the S3 copy when the posting was offloaded, else
    the inline text (which is the whole body when nothing was offloaded)."""
    inline = str(doc.get("description_text") or "")
    ref = doc.get("description_ref")
    if ref and get_jd is not None:
        try:
            stored = get_jd(ref)
        except Exception:
            stored = None
        if stored and len(stored) >= len(inline):
            return str(stored)
    return inline


def rederive_one(doc: Dict[str, Any], body: str, *, apply: bool) -> Tuple[str, Optional[str]]:
    """`(outcome, new_level)` for one posting. Writes only when `apply`."""
    from services import fit_score
    from services import job_ingest_worker as worker

    if not body.strip():
        # No body to re-parse: re-read the level from the title, which is
        # what insert did - under the title rules of its day.
        from services import job_inventory_service as inv
        if apply:
            moved = inv.relevel_from_title(str(doc.get("source") or ""),
                                           str(doc.get("source_job_id") or ""))
            return ("no_body_relevelled", moved[1]) if moved else ("no_body", None)
        level = inv.seniority_of(str(doc.get("title_raw") or ""),
                                 employer=doc.get("employer_name"))
        if level != inv.SENIORITY_UNKNOWN and level != (doc.get("seniority") or "unknown"):
            return "no_body_would_relevel", level
        return "no_body", None
    title = str(doc.get("title_raw") or "")
    level = fit_score.extract_requirements(body, title).get("job_level")
    if not apply:
        return "would_write", level
    ok = worker._persist_requirements(
        str(doc.get("source") or ""), str(doc.get("source_job_id") or ""),
        str(doc.get("content_hash") or ""), body, title)
    return ("written" if ok else "hash_moved"), level


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N postings (0 = all)")
    ap.add_argument("--workers", type=int, default=8,
                    help="parallel body reads + writes (keep modest on a shared cluster)")
    ap.add_argument("--json", default=None, help="write the report here too")
    args = ap.parse_args()

    from utils.db_connect import DBConnect
    from services import job_content_store, job_inventory_service as inv

    jp = DBConnect().get_db().job_postings
    get_jd = job_content_store.get_jd if job_content_store.available() else None
    if get_jd is None:
        print("WARNING: JD store unavailable - offloaded postings will be skipped "
              "rather than re-read from a truncated prefix")

    q = {"status": "active",
         "requirements.extractor_version": {"$ne": inv.EXTRACTOR_VERSION}}
    total = jp.count_documents(q)
    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | target extractor_version "
          f"{inv.EXTRACTOR_VERSION} | active postings below it: {total:,}")

    outcomes: collections.Counter = collections.Counter()
    moves: collections.Counter = collections.Counter()
    started = time.perf_counter()

    def work(doc: Dict[str, Any]) -> Tuple[str, str, Optional[str]]:
        if doc.get("description_ref") and get_jd is None:
            return "skipped_offloaded", str(doc.get("seniority") or "unknown"), None
        outcome, level = rederive_one(doc, full_body(doc, get_jd), apply=args.apply)
        return outcome, str(doc.get("seniority") or "unknown"), level

    cursor = jp.find(q, PROJECTION).batch_size(500)
    if args.limit:
        cursor = cursor.limit(args.limit)
    seen = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for outcome, old, new in pool.map(work, cursor):
            seen += 1
            outcomes[outcome] += 1
            if new and new != old:
                moves[(old, new)] += 1
            if seen % 10000 == 0:
                print(f"  {seen:,}/{total:,} ({time.perf_counter() - started:.0f}s)", flush=True)

    print(f"\nscanned {seen:,} in {time.perf_counter() - started:.0f}s")
    for k, n in outcomes.most_common():
        print(f"  {k:20} {n:>9,}")
    print("\nlevel changes (old -> new), top 25:")
    for (old, new), n in moves.most_common(25):
        print(f"  {old:>10} -> {new:<10} {n:>8,}")
    from_entry = sum(n for (old, _), n in moves.items() if old == "entry")
    print(f"\npostings leaving `entry`: {from_entry:,}")
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")
    if args.json:
        Path(args.json).write_text(json.dumps({
            "applied": args.apply, "scanned": seen, "outcomes": dict(outcomes),
            "moves": {f"{o}->{n}": c for (o, n), c in moves.items()},
        }, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
