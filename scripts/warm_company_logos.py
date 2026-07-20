#!/usr/bin/env python3
"""Warm the company_logos cache across every company catalog.

Iterates the MNC, enterprise, career-pages (board ATS) and Workday tenant
catalogs, deduplicates by normalized company name, and runs
``company_logo_service.get_or_fetch`` for each with polite pacing (0.3s
between companies). Fetches are Google-favicon-first, SSRF-guarded, capped
at 512KB, and cached in S3 + the ``company_logos`` Mongo collection;
failures are negative-cached as "missing" (refetch-eligible after 30 days).

Run from the repo root (needs MongoDB + AWS creds for S3):

    python scripts/warm_company_logos.py                # warm everything
    python scripts/warm_company_logos.py --limit 25     # first 25 companies
    python scripts/warm_company_logos.py --dry-run      # print keys/domains only

Prints ok/missing counts at the end. --dry-run touches neither the network
nor the database.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared" / "python"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PACING_SECONDS = 0.3


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Warm the company logo cache.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print key/domain per company; no network, no DB")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="stop after N companies (0 = all)")
    args = parser.parse_args()

    from services.company_logo_service import get_or_fetch, logo_key, resolve_domain

    ok = missing = 0
    processed = 0
    for name in iter_catalog_companies():
        if args.limit and processed >= args.limit:
            break
        processed += 1
        key = logo_key(name)
        domain = resolve_domain(name)
        if args.dry_run:
            print(f"[dry-run] {name} -> key={key} domain={domain or '(none)'}")
            continue
        result = None
        try:
            result = get_or_fetch(name)
        except Exception as e:  # keep warming; one company must not stop the run
            print(f"[error] {name}: {type(e).__name__}")
        if result and result.get("status") == "ok":
            ok += 1
            print(f"[ok]      {name} -> {result['s3_key']}")
        else:
            missing += 1
            print(f"[missing] {name} (domain={domain or '(none)'})")
        time.sleep(PACING_SECONDS)

    print(f"\nprocessed={processed} ok={ok} missing={missing}"
          + (" (dry run - nothing fetched)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
