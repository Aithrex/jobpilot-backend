#!/usr/bin/env python3
"""Repair `career_sources.canonical_endpoint` values that name a bare vendor
suffix instead of a board.

`https://icims.com/us-careers-rivian` is nobody's board. It is the residue of
`_endpoint_url` treating `portal_hosts` entries as HOSTNAMES when they are
SUFFIXES — fixed in the provider layer by `AtsProvider.board_url`, but the
wrong value was already frozen onto 631 career sources, and everything
downstream reads the stored field:

  * `verify_career_sources.py --mode content` hydrates from it, so the fetch
    goes to the wrong address and returns `unparseable_response` — which reads
    as "their board is broken" and means "we asked the wrong URL". Adobe,
    Accenture and Snap were each reported that way and each returned hundreds
    of live postings the moment the address was right;
  * a source demoted to `dead` or `blocked` on those fetches was demoted on
    evidence about a URL nobody uses.

**No network requests.** The real host is already recorded three times over,
and this reads them in order of authority:

  1. `company_ats_endpoints` joined on `member_key` — what the crawler uses;
  2. the source's own `redirect_chain`, which is where the ownership walk
     actually landed;
  3. an active posting's `apply_url` from the same `(ats, tenant)` — the
     crawler has been reading these boards all along.

A row whose host cannot be established from any of the three is LEFT ALONE.
Guessing a hostname is what produced the bad values in the first place.

    python scripts/repair_canonical_endpoints.py            # dry run
    python scripts/repair_canonical_endpoints.py --apply
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:      # pragma: no cover - dotenv is a dev convenience
    pass

from services import career_source_service as css            # noqa: E402
from services.ats_providers import registry as ats_registry  # noqa: E402
from utils.db_connect import DBConnect                       # noqa: E402

#: A `canonical_endpoint` whose HOST is one of these is the bug's signature:
#: the suffix was used as though it were the hostname.
BARE_SUFFIXES = frozenset({
    "icims.com", "oraclecloud.com", "myworkdayjobs.com", "myworkdaysite.com",
    "bamboohr.com", "csod.com", "taleo.net", "recruitee.com",
    "phenompeople.com", "successfactors.com", "jobvite.com", "workable.com",
    "ashbyhq.com", "greenhouse.io", "lever.co", "smartrecruiters.com",
})


def _is_bare_suffix(url: str) -> bool:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    return host in BARE_SUFFIXES


def _from_url(url: str, vendor: str):
    """Re-fingerprint a real board URL back into an Endpoint."""
    if not url:
        return None
    try:
        for endpoint in ats_registry.endpoints_from_urls([url]):
            if endpoint.vendor == vendor:
                return endpoint
    except Exception:      # pragma: no cover - best effort
        return None
    return None


def main() -> int:
    apply = "--apply" in sys.argv
    db = DBConnect().get_db()
    sources = db[css.CAREER_SOURCES]

    rows = [r for r in sources.find(
        {"canonical_endpoint": {"$regex": "^https?://"}},
        {"_id": 0, "source_id": 1, "ats_vendor": 1, "tenant": 1,
         "member_key": 1, "canonical_endpoint": 1, "redirect_chain": 1,
         "state": 1})
        if _is_bare_suffix(r.get("canonical_endpoint"))]

    print(f"== canonical_endpoint repair ({'APPLY' if apply else 'DRY RUN'}) ==\n")
    print(f"  {len(rows):,} source(s) carry a bare-suffix endpoint\n")
    if not rows:
        return 0

    member_keys = sorted({str(r.get("member_key") or "") for r in rows
                          if r.get("member_key")})
    catalog = {
        str(d.get("member_key")): d
        for d in db.company_ats_endpoints.find(
            {"member_key": {"$in": member_keys}},
            {"_id": 0, "member_key": 1, "vendor": 1, "tenant": 1,
             "host": 1, "site": 1, "extra": 1})} if member_keys else {}

    fixed, by_source, unfixable = [], Counter(), Counter()
    for row in rows:
        vendor = str(row.get("ats_vendor") or "")
        provider = None
        try:
            provider = ats_registry.get(vendor)
        except Exception:
            provider = None
        if provider is None:
            unfixable[f"{vendor}: no provider"] += 1
            continue

        endpoint = None
        origin = ""

        doc = catalog.get(str(row.get("member_key") or "")) or {}
        if doc.get("host"):
            from services.ats_providers.base import Endpoint
            endpoint = Endpoint(vendor=vendor,
                                tenant=str(row.get("tenant") or doc.get("tenant") or ""),
                                site=str(doc.get("site") or ""),
                                host=str(doc.get("host") or ""),
                                extra=dict(doc.get("extra") or {}))
            origin = "company_ats_endpoints"

        if endpoint is None:
            for candidate in reversed(list(row.get("redirect_chain") or [])):
                endpoint = _from_url(str(candidate), vendor)
                if endpoint is not None and endpoint.host:
                    origin = "redirect_chain"
                    break
                endpoint = None

        if endpoint is None:
            posting = db.job_postings.find_one(
                {"status": "active", "ats": vendor, "tenant": row.get("tenant")},
                {"_id": 0, "apply_url": 1, "url": 1})
            for candidate in ((posting or {}).get("apply_url"),
                              (posting or {}).get("url")):
                endpoint = _from_url(str(candidate or ""), vendor)
                if endpoint is not None and endpoint.host:
                    origin = "live posting"
                    break
                endpoint = None

        if endpoint is None:
            unfixable[f"{vendor}: no host on record"] += 1
            continue

        url = ""
        try:
            url = provider.board_url(endpoint)
        except Exception:      # pragma: no cover
            url = ""
        if not url or _is_bare_suffix(url) or url == row.get("canonical_endpoint"):
            unfixable[f"{vendor}: no better url"] += 1
            continue

        fixed.append((row["source_id"], url))
        by_source[f"{vendor} <- {origin}"] += 1

    print(f"  repairable {len(fixed):,}")
    for k, n in by_source.most_common():
        print(f"    {k:<40} {n:>5,}")
    if unfixable:
        print(f"\n  left alone {sum(unfixable.values()):,} "
              f"(guessing a hostname is what caused this)")
        for k, n in unfixable.most_common(10):
            print(f"    {k:<40} {n:>5,}")

    if not apply:
        print("\n(dry run - nothing written; re-run with --apply)")
        return 0

    from pymongo import UpdateOne
    ops = [UpdateOne({"source_id": sid}, {"$set": {"canonical_endpoint": url}})
           for sid, url in fixed]
    written = 0
    for i in range(0, len(ops), 500):
        written += sources.bulk_write(ops[i:i + 500], ordered=False).modified_count
    print(f"\nrepaired {written:,} canonical_endpoint value(s)")
    print("Re-run `verify_career_sources.py --apply --mode content` — the "
          "sources demoted on the wrong URL are now reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
