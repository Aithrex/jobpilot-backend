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

from services.ats_providers.util import (names_match,          # noqa: E402
                                         slug_is_trustworthy)


def _ssrf_guard():
    """Block private/link-local space and non-HTTP schemes on every fetch.

    `jd_fetcher.url_is_safe` returns `(ok, reason)`; the tuple must be
    unpacked or the check silently passes everything.
    """
    from services.jd_fetcher import url_is_safe

    def guard(_vendor: str, url: str) -> str:
        ok, reason = url_is_safe(url)
        if not ok:
            raise ValueError(f"blocked: {reason or 'unsafe_url'}")
        return url
    return guard


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
    ap.add_argument("--company-ids", default=None,
                    help="newline-delimited company_id file. Probes EXACTLY "
                         "these rows instead of a signals sweep - lets the "
                         "richer `employer_tech_classifier` cohort be probed "
                         "without first persisting a signal onto `companies`.")
    ap.add_argument("--skip-covered", action="store_true",
                    help="skip companies that already have a validated endpoint")
    args = ap.parse_args()

    if args.company_ids:
        ids = [line.strip() for line in Path(args.company_ids).read_text().splitlines()
               if line.strip()]
        companies = list(reg._db()[reg.COMPANIES].find(
            {"company_id": {"$in": ids}},
            {"company_id": 1, "name": 1, "domain": 1, "priority": 1},
        ).sort("priority", -1).limit(args.limit))
        print(f"companies from {args.company_ids}: {len(companies):,} of "
              f"{len(ids):,} ids   mode: {'APPLY' if args.apply else 'dry-run'}")
    else:
        companies = list(reg._db()[reg.COMPANIES].find(
            {f"signals.{args.signal}": True},
            {"company_id": 1, "name": 1, "domain": 1, "priority": 1},
        ).sort("priority", -1).limit(args.limit))
        print(f"companies with signals.{args.signal}: {len(companies):,}   "
              f"mode: {'APPLY' if args.apply else 'dry-run'}")

    if args.skip_covered:
        covered = {e.get("company_id") for e in
                   reg._db()[reg.ENDPOINTS].find({"status": "validated"},
                                                 {"company_id": 1})}
        before = len(companies)
        companies = [c for c in companies if c.get("company_id") not in covered]
        print(f"  skipping {before - len(companies):,} already-covered; "
              f"{len(companies):,} to probe")

    providers = [p for p in (ats_registry.get(v) for v in VENDORS) if p]
    # This script reaches arbitrary employer-guessed hosts, so it needs the
    # same SSRF guard the discovery worker uses. Without `guard=` the
    # fetchers fall back to `_noop_guard` and nothing is checked at all.
    ctx = simple_context(budget_seconds=60 * 60, global_rate=args.rate,
                         guard=_ssrf_guard())

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
                # A board that validates is a real board. Proving it is THIS
                # company's board is a separate question, and skipping it is
                # how `greenhouse/super` (Super Technologies) got recorded for
                # Super Micro Computer - a perfectly live board, filed under
                # the wrong employer, which reads as coverage rather than as an
                # error. See `AtsProvider.board_owner`.
                try:
                    owner = provider.board_owner(endpoint, ctx)
                except Exception:
                    owner = None
                if owner is not None:
                    if not names_match(name, owner):
                        with lock:
                            stats["wrong_owner"] += 1
                            print(f"  skip {provider.vendor}/{slug}: board is "
                                  f"{owner!r}, not {name!r}", flush=True)
                        continue
                elif not slug_is_trustworthy(name, slug):
                    with lock:
                        stats["weak_slug"] += 1
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
    print(f"rejected: {stats['wrong_owner']} named another company, "
          f"{stats['weak_slug']} too weak a slug to claim")
    if not args.apply:
        print("\ndry-run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
