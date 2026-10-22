#!/usr/bin/env python3
"""backfill_employer_business_model.py - stamp `employers.business_model`.

DRY-RUN BY DEFAULT. Prints what it would write; `--apply` writes it;
`--unstamp` removes exactly what this script wrote.

What it writes
--------------
`employers.business_model` (contract C1, `ebm-1.0.0`) and nothing else, via
`employer_business_model.classify_employer` - the same function the feed
runs at serve time when an employer has no stored model, so the backfill can
only make serve time FASTER, never different:

  owner_override > research_register (seed, by registrable domain) >
  registry_category > naics/sic (from `companies` joined BY DOMAIN, never by
  name) > name_rule (never `operating`) > unknown.

What it never touches
---------------------
`business_model_override` (the owner's field; read, never written),
`employer_id`, categories, identity, `job_postings`, `companies`. A row whose
stored model is already identical (same kind/basis/confidence/version) is
skipped, so a re-run writes nothing.

Rollback: `--unstamp --apply` `$unset`s `business_model` on rows whose
stored version is this script's version. Nothing else reads the field as
authoritative - serve time recomputes when it is absent.

Usage (owner-run; the repo `.env` IS production):
    PYTHONPATH=shared/python python3 scripts/backfill_employer_business_model.py
    PYTHONPATH=shared/python python3 scripts/backfill_employer_business_model.py --apply
    PYTHONPATH=shared/python python3 scripts/backfill_employer_business_model.py --unstamp [--apply]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

FIELDS = {"_id": 0, "employer_id": 1, "display_name": 1, "canonical_domain": 1,
          "verified_domains": 1, "employer_categories": 1, "category_evidence": 1,
          "business_model": 1, "business_model_override": 1}


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


def industry_codes_by_domain(db, domains: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """`{domain: [{system, code, source}]}` from `companies`, joined on DOMAIN.

    A name join would attach a stranger's NAICS code to an employer that
    merely shares a word with it; the domain is the identity."""
    out: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    if not domains:
        return out
    for doc in db.companies.find({"domain": {"$in": sorted(set(domains))}},
                                 {"_id": 0, "domain": 1, "naics": 1, "sic": 1}):
        from services.employer_registry import normalize_domain
        dom = normalize_domain(doc.get("domain"))
        if doc.get("naics"):
            out[dom].append({"system": "naics", "code": str(doc["naics"]),
                             "source": "companies.naics"})
        if doc.get("sic"):
            out[dom].append({"system": "sic", "code": str(doc["sic"]),
                             "source": "companies.sic"})
    return out


def _same(a: Optional[Dict[str, Any]], b: Dict[str, Any]) -> bool:
    if not isinstance(a, dict):
        return False
    return all(a.get(k) == b.get(k) for k in ("kind", "basis", "confidence", "version",
                                               "subtype"))


def run(db, *, apply: bool = False, unstamp: bool = False, limit: int = 0,
        batch: int = 500, verbose: bool = False) -> Dict[str, Any]:
    from services import employer_business_model as ebm
    summary: Dict[str, Any] = {"mode": ("unstamp" if unstamp else "stamp"),
                               "apply": bool(apply), "examined": 0,
                               "would_write": 0, "written": 0, "unchanged": 0,
                               "by_kind": collections.Counter(),
                               "by_basis": collections.Counter(), "samples": []}
    if unstamp:
        query = {"business_model.version": ebm.EBM_VERSION}
        n = db.employers.count_documents(query)
        summary["would_write"] = n
        if apply:
            summary["written"] = db.employers.update_many(
                query, {"$unset": {"business_model": ""}}).modified_count
        return summary

    cursor = db.employers.find({}, FIELDS).sort("employer_id", 1)
    if limit:
        cursor = cursor.limit(int(limit))
    pending: List[Dict[str, Any]] = []

    def flush(rows: List[Dict[str, Any]]) -> None:
        domains = [d for r in rows for d in [r.get("canonical_domain"),
                                             *(r.get("verified_domains") or [])] if d]
        codes = industry_codes_by_domain(db, domains)
        ops = []
        for emp in rows:
            summary["examined"] += 1
            emp_codes = [c for d in [emp.get("canonical_domain"),
                                     *(emp.get("verified_domains") or [])]
                         for c in codes.get(ebm.registrable_domain(d), [])]
            model = ebm.classify_employer(emp, industry_codes=emp_codes)
            summary["by_kind"][model["kind"]] += 1
            summary["by_basis"][model["basis"]] += 1
            if _same(emp.get("business_model"), model):
                summary["unchanged"] += 1
                continue
            summary["would_write"] += 1
            if len(summary["samples"]) < 25 and model["kind"] != "unknown":
                summary["samples"].append({"employer_id": emp.get("employer_id"),
                                           "name": emp.get("display_name"),
                                           "kind": model["kind"], "basis": model["basis"],
                                           "confidence": model["confidence"]})
            ops.append((emp["employer_id"], model))
        if apply:
            # One update per CHANGED row: a re-run writes nothing, and an
            # interrupted run resumes by recomputing, never by trusting state.
            for eid, model in ops:
                summary["written"] += db.employers.update_one(
                    {"employer_id": eid}, {"$set": {"business_model": model}}).modified_count

    for emp in cursor:
        if not emp.get("employer_id"):
            continue
        pending.append(emp)
        if len(pending) >= batch:
            flush(pending)
            pending = []
    if pending:
        flush(pending)
    summary["by_kind"] = dict(summary["by_kind"])
    summary["by_basis"] = dict(summary["by_basis"])
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--unstamp", action="store_true",
                    help="remove business_model written by this version (rollback)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=500)
    ap.add_argument("--json", default=None, help="write the summary to this path")
    args = ap.parse_args()
    summary = run(_db(), apply=args.apply, unstamp=args.unstamp, limit=args.limit,
                  batch=args.batch)
    print(json.dumps(summary, indent=2, default=str))
    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply to write.")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
