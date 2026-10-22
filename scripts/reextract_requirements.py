#!/usr/bin/env python3
"""reextract_requirements.py — re-read stored postings' requirements with the
current skill lexicon, for the role families a lexicon change affects.

DRY-RUN BY DEFAULT.

Why this exists
---------------
fit-v2.4 taught `fit_score.SKILL_LEXICON` the vocabulary power and electrical
engineers write (ETAP, PSCAD, PSS/E, power flow, short circuit, arc flash,
protection and control, substations, PLC programming, AutoCAD...). A posting's
`requirements` were extracted when it was crawled, with the lexicon of the
day, so an "Electrical Engineer I" requisition asking for exactly those skills
still stores none of them - and the scorer can only credit a skill the
POSTING names. Measured on a synthetic power-systems résumé: Electrical
Engineer I, Power Systems Engineer and Field Service Technician all scored 40
before; 82, 63 and 52 after, but only once both sides are read with the new
lexicon.

The crawler would re-extract each row the next time its content hash moves,
which for a stable requisition is never. This re-reads bodies already in OUR
S3 bucket (no employer is contacted) and writes through
`job_ingest_worker._persist_requirements`, the exact function the crawl uses.

Scope is the legacy families whose postings use the new vocabulary
(`--families`, default `hardware_engineering`), so a run costs a few thousand
body reads, not the whole inventory.

    python scripts/reextract_requirements.py                       # dry run
    python scripts/reextract_requirements.py --apply --workers 4

Run it only AFTER fit-v2.4 is deployed to the ingest box and the API Lambdas.
Rollback: none needed - the write is the same requirements shape every crawl
writes, and re-running the previous build's crawler would simply re-extract
the old way when a body changes.
"""
from __future__ import annotations

import argparse
import collections
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PROJECTION = {"_id": 0, "source": 1, "source_job_id": 1, "content_hash": 1,
              "title_raw": 1, "description_ref": 1, "description_text": 1,
              "requirements.required_skills": 1,
              "requirements.preferred_skills": 1}


def reextract_one(doc: Dict[str, Any], body: str, *, apply: bool) -> Tuple[str, int]:
    """`(outcome, skills gained)` for one posting. Writes only when `apply`."""
    from services import fit_score
    from services import job_ingest_worker as worker

    if not body.strip():
        return "no_body", 0
    title = str(doc.get("title_raw") or "")
    fresh = fit_score.extract_requirements(body, title)
    old = doc.get("requirements") or {}
    before = set(old.get("required_skills") or []) | set(old.get("preferred_skills") or [])
    after = set(fresh.get("required_skills") or []) | set(fresh.get("preferred_skills") or [])
    gained = len(after - before)
    if not gained:
        return "unchanged", 0
    if not apply:
        return "would_write", gained
    ok = worker._persist_requirements(
        str(doc.get("source") or ""), str(doc.get("source_job_id") or ""),
        str(doc.get("content_hash") or ""), body, title)
    return ("written" if ok else "hash_moved"), gained


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    ap.add_argument("--families", default="hardware_engineering",
                    help="comma-separated legacy role families to re-read")
    ap.add_argument("--limit", type=int, default=0, help="stop after N postings (0 = all)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    from rederive_posting_levels import full_body
    from services import job_content_store
    from utils.db_connect import DBConnect

    families = [f.strip() for f in args.families.split(",") if f.strip()]
    jp = DBConnect().get_db().job_postings
    get_jd = job_content_store.get_jd if job_content_store.available() else None
    if get_jd is None:
        print("WARNING: JD store unavailable - offloaded postings are skipped "
              "rather than re-read from a truncated prefix")

    q = {"status": "active", "role_family": {"$in": families}}
    total = jp.count_documents(q)
    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | families {families} | "
          f"active postings: {total:,}")

    outcomes: collections.Counter = collections.Counter()
    gained_total = 0
    started = time.perf_counter()

    def work(doc: Dict[str, Any]) -> Tuple[str, int]:
        if doc.get("description_ref") and get_jd is None:
            return "skipped_offloaded", 0
        return reextract_one(doc, full_body(doc, get_jd), apply=args.apply)

    cursor = jp.find(q, PROJECTION).batch_size(500)
    if args.limit:
        cursor = cursor.limit(args.limit)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for outcome, gained in pool.map(work, cursor):
            outcomes[outcome] += 1
            gained_total += gained

    print(f"\nscanned {sum(outcomes.values()):,} in {time.perf_counter() - started:.0f}s")
    for k, n in outcomes.most_common():
        print(f"  {k:20} {n:>9,}")
    print(f"skills gained across postings: {gained_total:,}")
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
