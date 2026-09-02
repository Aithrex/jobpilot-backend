#!/usr/bin/env python3
"""repair_employer_metadata.py — categories, provenance and geography.

DRY-RUN BY DEFAULT.

Four repairs, each one a defect that was measured on the live registry:

1. **`other` beside a real category.** `other` is what an UNMAPPED vocabulary
   term becomes, and `employer_categories` was merged with `$addToSet` — so
   one source contributing an unrecognised string permanently added `other`
   next to whatever another source had established. Measured: 1,177 employers
   carry `other` alongside a specific category, and `other` appears on 2,475
   of the 2,624 technology employers, which makes the category facet a list
   whose largest row means "unknown".

2. **Categories with no provenance.** A category with no recorded origin
   cannot be argued with. `category_evidence` records what each source
   actually declared, including the terms that resolved to `other`, so the
   next vocabulary gap is visible instead of silently widening the `other`
   bucket.

3. **No categories at all.** 22,256 employers carry only `other`, and for the
   SEC filers among them the answer is already in the database: the
   registrant's own SIC code. `employer_tech_classifier` already maps SIC to
   categories; this writes that mapping down with `sic:<code>` as its source.
   Nothing is inferred from a company NAME — "SLACK TIDE CRUISES LLC" is in
   this registry.

4. **One geography field doing four jobs.** `countries` was written from
   whatever the caller held. Where an employer IS, where they have been
   observed HIRING, where a posting is, and whether a role may be worked
   remotely are four different questions:

     `headquarters_country`  DOCUMENTARY only — an SEC registrant state means
                             a US registrant, and nothing else places one.
                             Absent stays absent.
     `hiring_countries`      OBSERVED, from this employer's own live postings.
                             An observation, and named as one.
     `remote_eligible`       True only when a posting SAYS so.

   An employer hiring in Poland is not a Polish company. The headquarters
   field is never filled from a posting location, and that refusal is the
   point of splitting the fields at all.

Usage
-----
    PYTHONPATH=shared/python python scripts/repair_employer_metadata.py
    PYTHONPATH=shared/python python scripts/repair_employer_metadata.py --apply
    PYTHONPATH=shared/python python scripts/repair_employer_metadata.py \
        --apply --only-categories
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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


from services import canonical_employer as ce            # noqa: E402
from services import employer_registry as er             # noqa: E402
from services import employer_tech_classifier as etc     # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def observed_geography(db) -> Dict[str, Dict[str, Any]]:
    """`{canonical_employer_id: {hiring_countries, remote_eligible}}`.

    Keyed on the CANONICAL id, so an employer publishing under three of its
    own legal-entity names has one set of hiring countries rather than three.
    Postings with no canonical id contribute nothing: attributing an
    observation to an employer we cannot identify is how a geography gets
    guessed.
    """
    out: Dict[str, Dict[str, Any]] = {}
    rows = db.job_postings.aggregate([
        {"$match": {"status": "active",
                    "canonical_employer_id": {"$type": "string"}}},
        {"$unwind": {"path": "$locations", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": {"e": "$canonical_employer_id",
                            "c": "$locations.country_code"},
                    "n": {"$sum": 1}}},
    ], allowDiskUse=True)
    for row in rows:
        rid = row["_id"]
        eid = str(rid.get("e") or "")
        if not eid:
            continue
        bucket = out.setdefault(eid, {"hiring_countries": set(), "postings": 0})
        bucket["postings"] += int(row["n"])
        code = str(rid.get("c") or "").strip().upper()
        if len(code) == 2:
            bucket["hiring_countries"].add(code)

    remote = db.job_postings.aggregate([
        {"$match": {"status": "active",
                    "canonical_employer_id": {"$type": "string"},
                    "work_mode": "remote"}},
        {"$group": {"_id": "$canonical_employer_id", "n": {"$sum": 1}}},
    ], allowDiskUse=True)
    for row in remote:
        eid = str(row["_id"] or "")
        if eid in out:
            out[eid]["remote_eligible"] = True
    for bucket in out.values():
        bucket["hiring_countries"] = sorted(bucket["hiring_countries"])[:40]
    return out


def plan(db, *, only_categories: bool = False) -> Dict[str, Any]:
    from pymongo import UpdateOne

    companies: Dict[str, Dict[str, Any]] = {}
    for c in db.companies.find(
            {}, {"_id": 0, "company_id": 1, "sic": 1, "sic_description": 1,
                 "signals": 1, "cik": 1, "ticker": 1, "domain": 1, "state": 1}):
        companies[str(c.get("company_id"))] = c

    geo = {} if only_categories else observed_geography(db)

    ops: List[Any] = []
    counts: Dict[str, int] = collections.Counter()
    samples: List[Dict[str, Any]] = []

    for emp in db[er.EMPLOYERS].find({}, {
            "_id": 0, "employer_id": 1, "display_name": 1, "canonical_domain": 1,
            "employer_categories": 1, "category_evidence": 1, "registry_refs": 1,
            "countries": 1, "hiring_countries": 1, "headquarters_country": 1,
            "remote_eligible": 1, "evidence": 1}):
        eid = emp["employer_id"]
        sets: Dict[str, Any] = {}
        stored = [str(c) for c in (emp.get("employer_categories") or [])]

        # --- 1 + 2: resolve categories, and record where each came from ----
        evidence_rows = [e for e in (emp.get("category_evidence") or [])
                         if isinstance(e, dict)]
        if not evidence_rows and stored:
            evidence_rows = [{"category": er.canonical_category(c),
                              "declared_as": c,
                              "source": "pre-provenance registry row"}
                             for c in stored]
            counts["category_provenance_seeded"] += 1

        # --- 3: the registrant's own SIC, for rows with nothing specific ---
        refs = emp.get("registry_refs") or {}
        company = None
        for cid in (list(refs.get("company_ids") or [])
                    + list(refs.get("company") or [])):
            company = companies.get(str(cid))
            if company:
                break
        resolved = er.resolve_categories(
            [e.get("declared_as") or e.get("category") for e in evidence_rows])
        if resolved in ([], [er.CATEGORY_OTHER]) and company:
            verdict = etc.classify_employer({
                "name": emp.get("display_name"),
                "domain": emp.get("canonical_domain") or company.get("domain"),
                "sic": company.get("sic"),
                "sic_description": company.get("sic_description"),
                "signals": company.get("signals") or {},
                "cik": company.get("cik"), "ticker": company.get("ticker")})
            # ONLY when the verdict rests on the FILED SIC CODE. A verdict
            # derived from an ATS vendor or a name keyword is explicitly a
            # discovery signal in `employer_tech_classifier` and must not
            # become a stored category — "Technologies" in a name is how
            # "SLACK TIDE CRUISES LLC" would get one.
            #
            # The source string is `sec_edgar_sic`, and matching it on a
            # `sic` PREFIX silently matched nothing at all: the first run of
            # this pass reported `categories_from_sic: 0` against 5,363
            # employers that carry a filed SIC.
            if (verdict.tech_categories
                    and "sic" in str(verdict.evidence_source or "")):
                for cat in verdict.tech_categories:
                    evidence_rows.append({
                        "category": er.canonical_category(cat),
                        "declared_as": str(cat),
                        "source": f"sic:{company.get('sic')}"})
                counts["categories_from_sic"] += 1
                resolved = er.resolve_categories(
                    [e.get("declared_as") or e.get("category")
                     for e in evidence_rows])

        if resolved and resolved != stored:
            sets["employer_categories"] = resolved
            if er.CATEGORY_OTHER in stored and len(stored) > 1:
                counts["other_removed_beside_specific"] += 1
            else:
                counts["categories_changed"] += 1
            if len(samples) < 12:
                samples.append({"employer": emp.get("display_name"),
                                "was": stored, "now": resolved})
        if evidence_rows and evidence_rows != emp.get("category_evidence"):
            sets["category_evidence"] = evidence_rows[:20]

        # --- 4: geography, four fields ------------------------------------
        if not only_categories:
            observed = geo.get(eid) or {}
            hiring = observed.get("hiring_countries") or []
            if hiring and hiring != (emp.get("hiring_countries") or []):
                sets["hiring_countries"] = hiring
                counts["hiring_countries_written"] += 1
            if observed.get("remote_eligible") and emp.get("remote_eligible") is not True:
                sets["remote_eligible"] = True
                counts["remote_eligible_written"] += 1
            # Headquarters: DOCUMENTARY ONLY. An SEC registrant with a filed
            # state of incorporation is a US registrant — that is a filing the
            # company made, not an inference from where we saw a job.
            if emp.get("headquarters_country") is None and company:
                if str(company.get("state") or "").strip() or company.get("cik"):
                    sets["headquarters_country"] = "US"
                    sets["headquarters_evidence"] = (
                        f"sec_registrant"
                        + (f":{company.get('state')}" if company.get("state") else ""))
                    counts["headquarters_from_filing"] += 1

        if sets:
            sets["updated_at"] = _now()
            ops.append(UpdateOne({"employer_id": eid}, {"$set": sets}))
    return {"ops": ops, "counts": dict(counts), "samples": samples}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only-categories", action="store_true",
                    help="skip the geography pass (it reads job_postings)")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    db = _db()
    built = plan(db, only_categories=args.only_categories)
    ops, counts = built["ops"], built["counts"]

    print(f"employers needing a repair: {len(ops):,}")
    for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {key:34s} {n:>8,}")
    if built["samples"]:
        print("\ncategory changes (sample):")
        for row in built["samples"]:
            print(f"  {str(row['employer'])[:34]:36s} {row['was']} -> {row['now']}")

    written = 0
    if args.apply and ops:
        for i in range(0, len(ops), args.batch):
            written += db[er.EMPLOYERS].bulk_write(
                ops[i:i + args.batch], ordered=False).modified_count
        print(f"\nwrote {written:,} employers")
        left = db[er.EMPLOYERS].count_documents(
            {"employer_categories": er.CATEGORY_OTHER,
             "employer_categories.1": {"$exists": True}})
        print(f"employers still carrying `other` beside a specific category: {left:,}")
    elif not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply.")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"generated_at": _now().isoformat(), "counts": counts,
             "samples": built["samples"], "written": written},
            indent=2, default=str), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
