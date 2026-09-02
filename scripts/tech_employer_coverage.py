#!/usr/bin/env python3
"""Which employers are technology employers, and are we actually crawling them?

READ-ONLY by default. `--apply` is gated and only ever re-arms discovery for
employers this script says are tech and uncovered; it never deletes, never
edits a posting, and never touches a schedule.

What it answers
---------------
1. **The denominator.** How many employers in `companies` are software /
   tech_adjacent / non_tech / unknown, classified by
   `employer_tech_classifier` - SEC SIC first, then the employer's own posting
   mix, then the curated seed. Never by a keyword in the name alone.

2. **The numerator, honestly.** For each tech employer, which of the six
   coverage states it is in (`employer_tech_classifier.coverage_state_for`).
   "Covered" requires content actually fetched from a board. A resolving
   domain is not coverage - `*.icims.com` is wildcard DNS and every candidate
   slug resolves.

3. **The join that was missing.** `companies` and the two Python catalogs
   (`workday_tenant_catalog`, `career_pages_catalog`) are DISJOINT registries.
   NVIDIA has 672 active postings crawled from the Workday catalog while its
   `companies` row says `discovery_status: no_portal, endpoints_found: 0`. Any
   coverage number computed from `companies` alone is therefore wrong in a
   direction that looks like failure. This script joins all three - registry,
   endpoints, catalogs and live postings - before it counts anything.

Usage
-----
    PYTHONPATH=shared/python python scripts/tech_employer_coverage.py
    PYTHONPATH=shared/python python scripts/tech_employer_coverage.py --json out.json
    PYTHONPATH=shared/python python scripts/tech_employer_coverage.py --gaps 200
    PYTHONPATH=shared/python python scripts/tech_employer_coverage.py --apply   # re-arm discovery
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

from services import employer_tech_classifier as etc   # noqa: E402
from services import tech_taxonomy as tx               # noqa: E402


def _db():
    # `utils.config` loads .env via python-dotenv when it is installed; a bare
    # venv has neither, and DBConnect then silently falls back to
    # localhost:27017 and times out after 30s. Read the file ourselves so the
    # script works in both.
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


def _norm_name(value: Any) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    s = re.sub(r"\b(inc|corp|corporation|co|llc|ltd|limited|plc|lp|llp|"
               r"holdings?|group|company|the|sa|nv|ag|se|gmbh|pte|pty)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Load the three registries and join them
# ---------------------------------------------------------------------------

def load_catalog_members() -> Dict[str, Dict[str, Any]]:
    """`{normalized display name: {...}}` for every Python-catalog employer.

    These are the employers the crawler reaches TODAY. They are invisible to
    `companies`, which is why the registry reports NVIDIA as having no portal.
    """
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
        for r in WORKDAY_TENANT_CATALOG:
            out[_norm_name(r["display_name"])] = {
                "display_name": r["display_name"], "vendor": "workday",
                "tenant": r["tenant"], "industry": r.get("industry"),
                "registry": "workday_tenant_catalog"}
    except Exception as exc:                       # pragma: no cover
        print(f"  ! workday catalog unavailable: {exc}", file=sys.stderr)
    try:
        from services.career_pages_catalog import CAREER_PAGES_CATALOG
        for r in CAREER_PAGES_CATALOG:
            out.setdefault(_norm_name(r["display_name"]), {
                "display_name": r["display_name"], "vendor": r.get("ats"),
                "tenant": r.get("slug"), "industry": r.get("industry"),
                "registry": "career_pages_catalog"})
    except Exception as exc:                       # pragma: no cover
        print(f"  ! career catalog unavailable: {exc}", file=sys.stderr)
    try:
        from services.mnc_catalog import MNC_CATALOG
        for r in MNC_CATALOG:
            out.setdefault(_norm_name(r["display_name"]), {
                "display_name": r["display_name"], "vendor": r.get("ats"),
                "tenant": r.get("slug"), "industry": r.get("sector"),
                "registry": "mnc_catalog"})
    except Exception:                              # pragma: no cover
        pass
    return out


def load_curated_seed() -> Dict[str, str]:
    """`{normalized name: category}` from the curated tech seed."""
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from data.tech_companies import TECH_COMPANIES        # type: ignore
    except Exception:
        try:
            from scripts.data.tech_companies import TECH_COMPANIES  # type: ignore
        except Exception as exc:
            print(f"  ! curated seed unavailable: {exc}", file=sys.stderr)
            return {}
    out: Dict[str, str] = {}
    for row in TECH_COMPANIES:
        if isinstance(row, dict):
            out[_norm_name(row.get("name"))] = str(row.get("category") or "software")
        elif isinstance(row, (tuple, list)) and row:
            out[_norm_name(row[0])] = str(row[2] if len(row) > 2 else "software")
    return out


def posting_mix_by_employer(db, *, days: int = 30) -> Dict[str, Dict[str, Any]]:
    """`{normalized employer name: {core, adjacent, non_tech, unknown, ...}}`.

    Classified from the STORED `tech.tech_scope` where the backfill has run,
    and from the title otherwise, so this works before and after the backfill.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    docs = list(db.job_postings.find(
        {"status": "active",
         "$or": [{"posted_at": {"$gte": cutoff}}, {"first_seen_at": {"$gte": cutoff}}]},
        {"_id": 0, "employer_name": 1, "title_raw": 1, "tech.tech_scope": 1,
         "source": 1, "ats": 1, "tenant": 1},
    ))
    # Resolve the employer name FIRST. 12.5% of active postings are published
    # under a name belonging to another company (see `employer_identity`), and
    # attributing them to that name puts "NUTMEG CONTAINER CORPORATION" in the
    # tech-employer list with 380 core-tech postings that are JPMorgan's.
    from services import employer_identity as ei
    rmap = ei.build_resolution_map(docs)
    for doc in docs:
        ei.apply_resolution(doc, rmap, name_field="employer_name")

    mix: Dict[str, Dict[str, Any]] = {}
    for doc in docs:
        key = _norm_name(doc.get("employer_name"))
        if not key:
            continue
        slot = mix.setdefault(key, {
            "display_name": doc.get("employer_name"), "core": 0, "adjacent": 0,
            "non_tech": 0, "unknown": 0, "total": 0,
            "sources": collections.Counter(), "tenants": set()})
        scope = ((doc.get("tech") or {}).get("tech_scope")
                 or tx.classify(doc.get("title_raw")).tech_scope)
        slot[scope if scope in ("core", "adjacent", "non_tech") else "unknown"] += 1
        slot["total"] += 1
        slot["sources"][f"{doc.get('source')}/{doc.get('ats')}"] += 1
        if doc.get("tenant"):
            slot["tenants"].add(doc["tenant"])
    return mix


def endpoints_by_company(db) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for e in db.company_ats_endpoints.find({}, {"_id": 0}):
        cid = str(e.get("company_id") or "")
        if cid:
            out[cid].append(e)
    return out


# ---------------------------------------------------------------------------

def build(db, *, mix_days: int) -> Dict[str, Any]:
    print("  loading catalogs ...", flush=True)
    catalogs = load_catalog_members()
    curated = load_curated_seed()
    print(f"    {len(catalogs)} catalog employers, {len(curated)} curated seeds",
          flush=True)

    print(f"  classifying live postings ({mix_days}d) ...", flush=True)
    mix = posting_mix_by_employer(db, days=mix_days)
    print(f"    {len(mix)} employers with active postings", flush=True)

    print("  loading endpoints ...", flush=True)
    eps = endpoints_by_company(db)

    print("  classifying employers ...", flush=True)
    rows: List[Dict[str, Any]] = []
    for company in db.companies.find({}, {
            "_id": 0, "company_id": 1, "name": 1, "name_normalized": 1,
            "domain": 1, "sic": 1, "sic_description": 1, "signals": 1,
            "cik": 1, "ticker": 1, "discovery_status": 1, "priority": 1,
            "endpoints_found": 1, "last_discovered_at": 1, "state": 1}):
        key = _norm_name(company.get("name"))
        m = mix.get(key)
        cls = etc.classify_employer(
            company,
            posting_mix=m,
            endpoints=eps.get(company.get("company_id") or "", []),
            curated_category=curated.get(key),
        )
        cat = catalogs.get(key)
        state, detail = etc.coverage_state_for(
            company, eps.get(company.get("company_id") or "", []))
        # The join. An employer crawled through a Python catalog IS covered,
        # whatever `companies.discovery_status` says about it.
        if state != etc.COVERAGE_VALIDATED and cat:
            state = etc.COVERAGE_VALIDATED
            detail = (f"crawled via {cat['registry']} "
                      f"({cat['vendor']}/{cat['tenant']}) - not linked to this "
                      "companies row")
        active = int(m["total"]) if m else 0
        if state != etc.COVERAGE_VALIDATED and active > 0:
            state = etc.COVERAGE_VALIDATED
            detail = f"{active} active postings ingested under this employer name"

        rows.append({
            "company_id": company.get("company_id"),
            "name": company.get("name"),
            "name_key": key,
            "domain": company.get("domain"),
            "ticker": company.get("ticker"),
            "sic": company.get("sic"),
            "sic_description": company.get("sic_description"),
            "discovery_status": company.get("discovery_status"),
            "priority": company.get("priority"),
            "tech_classification": cls.tech_classification,
            "tech_categories": cls.tech_categories,
            "confidence": cls.confidence,
            "evidence_source": cls.evidence_source,
            "evidence": cls.evidence,
            "public_private_status": cls.public_private_status,
            "validated_career_endpoint_count": cls.validated_career_endpoint_count,
            "coverage_state": state,
            "coverage_detail": detail,
            "catalog": cat,
            "active_postings": active,
            "core_tech_postings": int(m["core"]) if m else 0,
        })
    return {"rows": rows, "mix": mix, "catalogs": catalogs}


def report(built: Dict[str, Any], *, gaps: int) -> Dict[str, Any]:
    rows = built["rows"]
    by_class = collections.Counter(r["tech_classification"] for r in rows)
    tech = [r for r in rows
            if r["tech_classification"] in (etc.TECH_SOFTWARE, etc.TECH_ADJACENT)]
    software = [r for r in tech if r["tech_classification"] == etc.TECH_SOFTWARE]
    by_state = collections.Counter(r["coverage_state"] for r in tech)
    by_state_sw = collections.Counter(r["coverage_state"] for r in software)
    by_evidence = collections.Counter(r["evidence_source"] for r in tech)
    by_status = collections.Counter(r["public_private_status"] for r in tech)
    by_category = collections.Counter(
        c for r in tech for c in (r["tech_categories"] or ["(uncategorised)"]))

    n = max(1, len(tech))
    covered = by_state.get(etc.COVERAGE_VALIDATED, 0)

    print("\n" + "=" * 78)
    print("EMPLOYER UNIVERSE")
    print("=" * 78)
    print(f"  companies rows                {len(rows):,}")
    for k in (etc.TECH_SOFTWARE, etc.TECH_ADJACENT, etc.TECH_NON, etc.TECH_UNKNOWN):
        print(f"    {k:16s} {by_class.get(k, 0):>8,}")
    print(f"\n  TECH DENOMINATOR (software + tech_adjacent)  {len(tech):,}")
    print(f"    of which software                          {len(software):,}")

    print("\n  classification evidence source:")
    for k, v in by_evidence.most_common():
        print(f"    {k or '(none)':22s} {v:>8,}")
    print("\n  public/private (tech employers):")
    for k, v in by_status.most_common():
        print(f"    {k:22s} {v:>8,}")

    print("\n" + "=" * 78)
    print("COVERAGE OF THE TECH DENOMINATOR")
    print("=" * 78)
    for st in etc.COVERAGE_STATES:
        v = by_state.get(st, 0)
        print(f"  {st:24s} {v:>8,}  {v / n:6.1%}   (software only: "
              f"{by_state_sw.get(st, 0):,})")
    print(f"\n  VALIDATED COVERAGE  {covered:,}/{len(tech):,} = {covered / n:.1%}")

    print("\n  top tech categories:")
    for k, v in by_category.most_common(18):
        print(f"    {k:24s} {v:>7,}")

    gaps_rows = sorted(
        (r for r in tech if r["coverage_state"] != etc.COVERAGE_VALIDATED),
        key=lambda r: (-(r.get("priority") or 0), r["name"] or ""))
    print("\n" + "=" * 78)
    print(f"UNCOVERED TECH EMPLOYERS - {len(gaps_rows):,} total, top {gaps} by priority")
    print("=" * 78)
    print(f"  {'name':38s} {'domain':26s} {'state':14s} {'why'}")
    for r in gaps_rows[:gaps]:
        print(f"  {str(r['name'])[:37]:38s} {str(r['domain'] or '-')[:25]:26s} "
              f"{r['coverage_state']:14s} {str(r['coverage_detail'])[:60]}")

    yielding = sorted((r for r in tech if r["active_postings"] > 0),
                      key=lambda r: -r["core_tech_postings"])
    print("\n" + "=" * 78)
    print(f"TECH EMPLOYERS ALREADY YIELDING POSTINGS - {len(yielding):,}")
    print("=" * 78)
    print(f"  {'name':38s} {'core':>7s} {'active':>8s}  registry")
    for r in yielding[:60]:
        cat = r["catalog"]
        print(f"  {str(r['name'])[:37]:38s} {r['core_tech_postings']:>7,} "
              f"{r['active_postings']:>8,}  "
              f"{(cat or {}).get('registry', 'company_ats_endpoints')}")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "companies_total": len(rows),
        "by_classification": dict(by_class),
        "tech_denominator": len(tech),
        "software_denominator": len(software),
        "coverage_by_state": dict(by_state),
        "coverage_by_state_software": dict(by_state_sw),
        "validated_coverage_pct": round(covered / n, 4),
        "by_evidence_source": dict(by_evidence),
        "by_public_private": dict(by_status),
        "by_category": dict(by_category),
        "uncovered_total": len(gaps_rows),
        "uncovered": gaps_rows[:2000],
        "yielding": yielding[:2000],
        "tech_rows": tech,
    }


def persist_classification(db, summary: Dict[str, Any]) -> int:
    """Write the employer verdict onto `companies`. WRITES (gated).

    Additive: sets `tech.*` and `signals.tech`, and never removes anything.

    Why it matters beyond reporting: `probe_software_company_boards.py` and
    the discovery prioritiser both select on `signals.software`, which is set
    for 1,554 rows. This classifier finds 2,454 tech employers using SIC codes
    and posting mix that `signals.software` never saw - so 900 real technology
    employers are currently invisible to the two tools whose job is to find
    their boards.
    """
    now = datetime.now(timezone.utc)
    n = 0
    for r in summary["tech_rows"]:
        cid = r.get("company_id")
        if not cid:
            continue
        db.companies.update_one({"company_id": cid}, {"$set": {
            "tech": {
                "tech_classification": r["tech_classification"],
                "tech_categories": r["tech_categories"],
                "confidence": r["confidence"],
                "evidence_source": r["evidence_source"],
                "evidence": r["evidence"],
                "evidence_updated_at": now,
                "public_private_status": r["public_private_status"],
                "coverage_state": r["coverage_state"],
                "coverage_detail": r["coverage_detail"],
                "classifier_version": etc.CLASSIFIER_VERSION,
            },
            "signals.tech": True,
            "updated_at": now,
        }})
        n += 1
    return n


def rearm(db, summary: Dict[str, Any], *, limit: int) -> int:
    """Re-arm discovery for uncovered tech employers. WRITES (gated)."""
    targets = [r for r in summary["uncovered"]
               if r["coverage_state"] in (etc.COVERAGE_UNRESOLVED,)][:limit]
    now = datetime.now(timezone.utc)
    n = 0
    for r in targets:
        cid = r.get("company_id")
        if not cid:
            continue
        db.companies.update_one(
            {"company_id": cid},
            {"$set": {"discovery_status": "pending",
                      "priority": max(int(r.get("priority") or 0), 90),
                      "rearmed_reason": "tech_employer_coverage",
                      "updated_at": now}})
        db.discovery_queue.update_one(
            {"company_id": cid},
            {"$set": {"state": "pending", "priority": 90,
                      "lease_owner": None, "lease_expiry": None,
                      "rearmed_at": now, "reason": "tech-coverage-gap"}},
            upsert=True)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", help="write the full report to this path")
    ap.add_argument("--gaps", type=int, default=80,
                    help="how many uncovered employers to print")
    ap.add_argument("--mix-days", type=int, default=30,
                    help="window for the posting-mix signal")
    ap.add_argument("--apply", action="store_true",
                    help="RE-ARM discovery for uncovered tech employers (writes)")
    ap.add_argument("--apply-limit", type=int, default=500)
    ap.add_argument("--persist-classification", action="store_true",
                    help="write the employer tech verdict onto `companies` "
                         "(writes; additive). Needs --apply.")
    args = ap.parse_args()

    db = _db()
    built = build(db, mix_days=args.mix_days)
    summary = report(built, gaps=args.gaps)

    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, default=str))
        print(f"\nwrote {args.json}")

    if args.apply:
        if args.persist_classification:
            n = persist_classification(db, summary)
            print(f"\nPERSISTED the tech verdict onto {n} companies rows "
                  "(companies.tech + signals.tech)")
        n = rearm(db, summary, limit=args.apply_limit)
        print(f"\nRE-ARMED {n} companies for discovery "
              f"(discovery_status=pending, priority>=90)")
    else:
        print("\n(read-only; pass --apply to re-arm discovery for the gaps, "
              "and --apply --persist-classification to store the verdict)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
