#!/usr/bin/env python3
"""Probe the shared job-board APIs for every company classified `software`.

Why this exists alongside discovery
-----------------------------------
`career_portal_discovery` fingerprints an employer's own website, which is the
better strategy in general and a poor one for exactly this cohort: a modern
software company's careers page is a React shell that loads its board by XHR,
so the ATS URL is nowhere in the served HTML. Measured on the live run of
2026-08-27, the crawl read four to six pages of salesforce.com, apple.com,
microsoft.com, shopify.com, atlassian.com, dell.com, broadcom.com and
synopsys.com and found no ATS link on any of them.

For the four vendors that serve every tenant from ONE shared API host, a slug
guess is a single definitive request - `boards-api.greenhouse.io/v1/boards/<slug>`
either returns a board or 404s. That makes a direct sweep cheap and, for
software companies specifically, high yield: Greenhouse, Lever and Ashby are
where this cohort actually hosts its jobs.

`career_portal_discovery._slug_probe` already does this per company, but only
after a full site crawl has failed and inside a six-request budget shared
across all vendors. This sweep skips the crawl entirely for a cohort we have
already classified, so it costs ~4 requests per company instead of ~20 seconds.

Everything below the guess is the SAME machinery the scheduled worker uses:
`provider.slug_candidates` for the naming convention, `provider.validate` to
confirm a board actually serves postings, and `reg.upsert_endpoint` /
`reg.record_validation` to record it. Nothing here re-implements a protocol.

Usage:
    PYTHONPATH=shared/python python scripts/probe_software_company_boards.py
    PYTHONPATH=shared/python python scripts/probe_software_company_boards.py --apply
    PYTHONPATH=shared/python python scripts/probe_software_company_boards.py \
        --apply --signal software_name --limit 500
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from services import company_registry_service as reg          # noqa: E402
from services.ats_providers import registry as ats_registry    # noqa: E402
from services.ats_providers.base import STATUS_VALIDATED, Endpoint  # noqa: E402
from services.ats_providers.http import simple_context         # noqa: E402

#: Only vendors with ONE shared API host, where a wrong guess is a cheap 404
#: rather than a DNS lookup or a hit against a shared anti-bot budget. Workday,
#: iCIMS and Oracle are deliberately absent - `AtsProvider.slug_probe`
#: documents why for each.
VENDORS = ("greenhouse", "lever", "ashby", "smartrecruiters")

_LEGAL = re.compile(r"\b(inc|corp|corporation|co|ltd|limited|plc|holdings|"
                    r"group|sa|se|nv|ag|llc|lp|the)\b", re.IGNORECASE)


def _slugs(provider, name: str, domain: str) -> list:
    """The provider's own naming convention, deduped and bounded."""
    base = _LEGAL.sub(" ", name or "").strip()
    out, seen = [], set()
    for cand in list(provider.slug_candidates(base, domain or ""))[:3]:
        c = str(cand or "").strip()
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--signal", default="software",
                    help="companies.signals key to sweep (default: software)")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--rate", type=float, default=8.0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    companies = list(reg._db()[reg.COMPANIES].find(
        {f"signals.{args.signal}": True},
        {"company_id": 1, "name": 1, "domain": 1, "priority": 1},
    ).sort("priority", -1).limit(args.limit))
    print(f"companies with signals.{args.signal}: {len(companies):,}   "
          f"mode: {'APPLY' if args.apply else 'dry-run'}")

    providers = [p for p in (ats_registry.get(v) for v in VENDORS) if p]
    ctx = simple_context(budget_seconds=60 * 60, global_rate=args.rate)

    lock = threading.Lock()
    hits, stats = [], collections.Counter()
    t0 = time.time()

    def probe(row):
        name, domain = row.get("name") or "", row.get("domain") or ""
        for provider in providers:
            for slug in _slugs(provider, name, domain):
                endpoint = Endpoint(vendor=provider.vendor, tenant=slug,
                                    site="", host="")
                try:
                    result = provider.validate(endpoint, ctx)
                except Exception:
                    continue
                if result.status != STATUS_VALIDATED or not result.job_count:
                    continue
                with lock:
                    stats[provider.vendor] += 1
                    hits.append((name, provider.vendor, slug,
                                 result.job_count, result.sample_title))
                    print(f"  HIT {provider.vendor:15s} {slug:28s} "
                          f"{result.job_count:>3} jobs  {name[:34]}", flush=True)
                if args.apply:
                    key = reg.upsert_endpoint(
                        endpoint, company_id=row.get("company_id", ""),
                        display_name=name, source="slug-probe")
                    reg.record_validation(key, result)
                    reg.complete_discovery(row["company_id"],
                                           state=reg.DISCOVERY_DONE,
                                           endpoints_found=1,
                                           detail=f"slug probe: "
                                                  f"{provider.vendor}/{slug}")
                return
        with lock:
            stats["miss"] += 1

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(probe, companies))

    print(f"\nprobed {len(companies):,} companies in {time.time()-t0:.0f}s")
    print(f"boards found: {len(hits):,}  "
          f"({100 * len(hits) / max(len(companies), 1):.1f}%)")
    for vendor, n in stats.most_common():
        if vendor != "miss":
            print(f"   {vendor:16s} {n}")
    if not args.apply:
        print("\ndry-run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
