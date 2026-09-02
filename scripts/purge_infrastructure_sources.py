#!/usr/bin/env python3
"""Delete `career_sources` and `company_ats_endpoints` rows that name a
vendor's own infrastructure.

Fingerprints ran over every url a careers page mentioned, so every Phenom
customer's page contributed the same four `*.phenompeople.com` asset hosts.
`verify_official_career_urls.py --register` then wrote them as OWNER-VERIFIED
career sources for Adobe, Cisco and Splunk: an endpoint we would go and fetch,
attributed to an employer that does not own it.

`career_source_ownership.endpoints_from_page` now drops asset urls before
fingerprinting and `AtsProvider.is_reserved_tenant` refuses the handful of
tokens no company is named after, so nothing new arrives. This removes what
was already written.

**It never deletes a source that carries live postings.** A board the crawler
is reading is a board, whatever its name looks like: `greenhouse:css` is
CloudKitchens (City Storage Systems) and `greenhouse:manifest` is Manifest
Cyber. Those rows are printed and kept.

    python scripts/purge_infrastructure_sources.py            # dry run
    python scripts/purge_infrastructure_sources.py --apply
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:      # pragma: no cover - dotenv is a dev convenience
    pass

from services import career_source_service as css          # noqa: E402
from services.ats_providers import registry as ats_registry  # noqa: E402
from utils.db_connect import DBConnect                     # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    db = DBConnect().get_db()
    sources = db[css.CAREER_SOURCES]

    doomed, kept = [], []
    for row in sources.find({}, {"_id": 0, "source_id": 1, "ats_vendor": 1,
                                 "tenant": 1, "state": 1, "employer_id": 1}):
        vendor = str(row.get("ats_vendor") or "")
        tenant = str(row.get("tenant") or "")
        try:
            provider = ats_registry.get(vendor)
        except Exception:
            continue
        if provider is None or not provider.is_reserved_tenant(tenant):
            continue
        live = db.job_postings.count_documents(
            {"status": "active", "ats": vendor, "tenant": tenant}, limit=1)
        (kept if live else doomed).append(row)

    print(f"== infrastructure career sources "
          f"({'APPLY' if apply else 'DRY RUN'}) ==\n")
    print(f"  matched {len(doomed) + len(kept):,}   "
          f"deletable {len(doomed):,}   kept for live postings {len(kept):,}\n")

    by = Counter((r["ats_vendor"], r["state"]) for r in doomed)
    for (vendor, state), n in by.most_common():
        print(f"  {vendor:<14} {state:<20} {n:>5,}")

    if kept:
        print("\n  KEPT - the crawler is reading these, so they are boards:")
        for row in kept:
            print(f"    {row['ats_vendor']}:{row['tenant']} ({row['state']})")

    # The crawler's own endpoint catalog, which is where a fetch actually
    # comes from. Fewer rows land here because most never returned a job, but
    # each one is a request we make on every cycle.
    endpoints = db.company_ats_endpoints
    ep_doomed, ep_kept = [], []
    for row in endpoints.find({}, {"_id": 1, "ats": 1, "vendor": 1,
                                   "tenant": 1, "status": 1}):
        vendor = str(row.get("ats") or row.get("vendor") or "")
        tenant = str(row.get("tenant") or "")
        try:
            provider = ats_registry.get(vendor)
        except Exception:
            continue
        if provider is None or not tenant or not provider.is_reserved_tenant(tenant):
            continue
        live = db.job_postings.count_documents(
            {"status": "active", "ats": vendor, "tenant": tenant}, limit=1)
        (ep_kept if live else ep_doomed).append(row)

    print(f"\n  company_ats_endpoints: deletable {len(ep_doomed):,}   "
          f"kept for live postings {len(ep_kept):,}")
    for row in ep_doomed:
        print(f"    {row.get('ats') or row.get('vendor')}:{row.get('tenant')} "
              f"({row.get('status')})")

    if not apply:
        print("\n(dry run - nothing deleted; re-run with --apply)")
        return 0
    if not doomed and not ep_doomed:
        print("\nNothing to delete.")
        return 0

    if doomed:
        res = sources.delete_many(
            {"source_id": {"$in": [r["source_id"] for r in doomed]}})
        print(f"\ndeleted {res.deleted_count:,} career source(s)")
    if ep_doomed:
        res = endpoints.delete_many({"_id": {"$in": [r["_id"] for r in ep_doomed]}})
        print(f"deleted {res.deleted_count:,} ats endpoint(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
