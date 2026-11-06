#!/usr/bin/env python3
"""Find and fix missing company logos. DRY RUN unless --apply.

The scheduler's ``logo_refresh`` job (every 15 minutes, see
``company_logo_service.refresh_due``) does this on its own; this script is
for measuring it and for clearing a backlog in one go.

What counts as "due" (in this order): companies stamped on a board but never
fetched, misses written by an older resolver (``RESOLVER_VERSION``), misses
past the 30-day negative cache, and working logos an older resolver chose
(rechecked against registry/catalog evidence only, never replaced by a guess).

Run from the repo root:

    python scripts/warm_company_logos.py                     # dry run: what is due, which domains each would try
    python scripts/warm_company_logos.py --source catalogs   # dry run over the catalogs only: no DB, no network
    python scripts/warm_company_logos.py --probe --limit 80  # dry run that ALSO asks the icon services; writes nothing
    python scripts/warm_company_logos.py --apply             # fetch + store everything due

The dry run READS the ``company_logos`` and ``employers`` collections (except
with ``--source catalogs``). ``--probe`` adds outbound requests to Google's
and DuckDuckGo's icon services and the companies' own sites. ``--apply``
writes ``company_logos`` and the icon bytes to the app's S3 store, so it must
run where that store is reachable (the Oracle box); anywhere else the fetch
succeeds, the store fails, and the doc is left ``pending`` for the scheduler.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared" / "python"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def iter_catalog_companies():
    """Yield display names across all catalogs, deduped by normalized name."""
    from services.company_logo_service import normalize_company

    catalogs = []
    from services.mnc_catalog import MNC_CATALOG
    catalogs.append(MNC_CATALOG)
    from services.career_pages_service import ENTERPRISE_CATALOG
    catalogs.append(ENTERPRISE_CATALOG)
    from services.career_pages_catalog import CAREER_PAGES_CATALOG
    catalogs.append(CAREER_PAGES_CATALOG)
    from services.workday_tenant_catalog import WORKDAY_TENANT_CATALOG
    catalogs.append(WORKDAY_TENANT_CATALOG)

    seen = set()
    for catalog in catalogs:
        for row in catalog:
            name = str(row.get("display_name") or "").strip()
            normalized = normalize_company(name)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            yield name


def _fmt_candidates(candidates):
    return ", ".join(f"{d} [{b}]" for d, b in candidates) or "(none)"


def dry_run_catalogs(limit: int) -> int:
    from services import company_logo_service as svc
    n = 0
    for name in iter_catalog_companies():
        if limit and n >= limit:
            break
        n += 1
        cands = svc.candidate_domains(name, use_registry=False)
        print(f"[dry-run] {name} -> key={svc.logo_key(name)} try: {_fmt_candidates(cands)}")
    print(f"\ncatalog companies listed={n} (dry run - no database, no network)")
    return 0


def dry_run_cache(limit: int, probe: bool) -> int:
    from services import company_logo_service as svc
    svc._index_ensured = True   # a dry run issues no create_index either: reads only
    coll = svc._coll()
    print("company_logos by status:")
    for row in coll.aggregate([{"$group": {"_id": {"status": "$status",
                                                    "resolver": "$resolver_version"},
                                           "n": {"$sum": 1}}},
                               {"$sort": {"n": -1}}]):
        print(f"  {row['_id']}: {row['n']}")

    stuck = coll.count_documents({"status": "pending", "fetched_at": {"$ne": None}})
    print(f"pending after a FAILED STORE (icon fetched, S3 write failed): {stuck}")

    picked = svc.due_docs(limit or 1_000_000)
    print(f"\ndue now: {len(picked)}  " + str(dict(Counter(label for label, _ in picked))))
    outcome = Counter()
    for label, doc in picked[: (limit or 60)]:
        cands = svc.candidate_domains(doc["company"], doc.get("canonical_employer_id"))
        line = (f"[{label}] {doc['company']!r} had={doc.get('domain') or '-'}"
                f" -> try: {_fmt_candidates(cands)}")
        if probe:
            found = (svc.fetch_for_candidates(cands, svc.BACKGROUND_BUDGET_S, company=doc["company"])
                     if cands else None)
            if found:
                domain, basis, got = found
                width = svc._image_width(got["data"])
                line += f"\n      FOUND {domain} [{basis}] via {got['source']} ({width or '?'}px)"
                outcome[f"{label}:found:{basis}"] += 1
            else:
                line += "\n      none (stays a monogram)"
                outcome[f"{label}:none"] += 1
        print(line)
    if probe:
        print("\nprobe outcomes (nothing written):")
        for k, v in sorted(outcome.items()):
            print(f"  {k}: {v}")
    print("\n(dry run - nothing written; pass --apply to fetch and store)")
    return 0


def apply(source: str, limit: int, batch: int) -> int:
    from services import company_logo_service as svc
    if source in ("catalogs", "all"):
        names = {svc.normalize_company(n): svc.logo_key(n) for n in iter_catalog_companies()}
        svc._ensure_docs(names)
        print(f"catalog companies ensured: {len(names)}")

    def report(label, doc, outcome):
        print(f"[{outcome:>12}] ({label}) {doc['company']}")

    totals = Counter()
    done = 0
    while not limit or done < limit:
        size = min(batch, limit - done) if limit else batch
        res = svc.refresh_due(budget_seconds=3600, limit=size, workers=4, on_result=report)
        if not res["picked"]:
            break
        done += res["picked"]
        for k, v in res["outcomes"].items():
            totals[k] += v
        progressed = sum(v for k, v in res["outcomes"].items()
                         if k.rsplit(":", 1)[-1] in ("ok", "missing", "kept", "replaced"))
        if not progressed:  # only store failures / errors left: stop, don't spin
            print("no progress this round (store unreachable?) - stopping")
            break
    print("\ntotals:")
    for k, v in sorted(totals.items()):
        print(f"  {k}: {v}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Find and fix missing company logos.")
    parser.add_argument("--apply", action="store_true",
                        help="fetch and store (default: dry run, nothing written)")
    parser.add_argument("--probe", action="store_true",
                        help="dry run that also asks the icon services (no writes)")
    parser.add_argument("--source", choices=("all", "cache", "catalogs"), default="all",
                        help="catalogs = the shipped catalogs; cache = companies already "
                             "stamped on boards (company_logos); all = both")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="stop after N companies (0 = all)")
    parser.add_argument("--batch", type=int, default=500, metavar="N",
                        help="companies per refresh round with --apply")
    args = parser.parse_args()

    if args.apply:
        return apply(args.source, args.limit, args.batch)
    if args.source == "catalogs":
        return dry_run_catalogs(args.limit)
    return dry_run_cache(args.limit, args.probe)


if __name__ == "__main__":
    sys.exit(main())
