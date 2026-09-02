#!/usr/bin/env python3
"""Seed the curated tech-employer list into `companies` and flag it software.

Why this is needed before any probing
-------------------------------------
`probe_software_company_boards.py` sweeps `companies` where
`signals.software` is true. That set was 796 rows on 2026-09-02, because
`companies` was built from SEC EDGAR and EDGAR only knows PUBLIC filers. Every
private tech employer - Databricks, Stripe, Anthropic, OpenAI, Notion, Discord,
Plaid, Ramp, Vercel, Instacart - was absent from the registry entirely, so the
prober could not reach them and their boards were never found.

Identity, and why domain wins
-----------------------------
`upsert_company` keys on `company_id_for(normalize_company_name(name))`, which
strips legal suffixes, so "MongoDB" merges onto "MONGODB, INC." correctly. It
does NOT merge "Cisco" onto "CISCO SYSTEMS INC" - "Systems" is part of the
name, not a suffix - which would insert a second row for one employer. So this
script resolves by DOMAIN first (the strongest identity we hold) and only
falls through to `upsert_company` when no existing row claims that domain.

Nothing here asserts an ATS. It only says "this employer exists and hires in
tech, go look at it". `probe_software_company_boards.py` does the looking and
rejects any board whose owner names a different company.

Usage:
    PYTHONPATH=shared/python python scripts/seed_tech_companies.py
    PYTHONPATH=shared/python python scripts/seed_tech_companies.py --apply
"""
from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from data.tech_companies import TECH_COMPANIES                 # noqa: E402
from services import company_registry_service as reg           # noqa: E402
from services.ats_providers.util import names_match            # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    col = reg._db()[reg.COMPANIES]
    stats = collections.Counter()
    print(f"curated tech employers: {len(TECH_COMPANIES)}   "
          f"mode: {'APPLY' if args.apply else 'dry-run'}\n")

    for name, domain, category in TECH_COMPANIES:
        existing = col.find_one({"domain": domain},
                                {"company_id": 1, "name": 1, "signals": 1,
                                 "discovery_status": 1, "priority": 1})
        # A shared domain is NOT proof of a shared employer. EDGAR domains are
        # sloppy: `wasabi.com` is filed against WASABI GV LLC, not Wasabi
        # Technologies. Merging on domain alone would flag the wrong row as
        # software and leave the employer we actually wanted unprobed — a
        # silent coverage miss, which is the exact failure this seed exists to
        # fix. So the name must agree too; when it does not, the curated row is
        # inserted on its own and the prober owner-verifies whatever it finds.
        if existing and not names_match(name, existing.get("name") or ""):
            stats["domain_name_mismatch"] += 1
            print(f"  split  {name:34s} domain {domain} is filed against "
                  f"{existing.get('name')!r} — inserting separately")
            existing = None
        if existing:
            already = bool((existing.get("signals") or {}).get("software"))
            stats["matched_by_domain"] += 1
            stats["already_software" if already else "flagged_software"] += 1
            if args.apply and not already:
                col.update_one(
                    {"company_id": existing["company_id"]},
                    {"$set": {"signals.software": True,
                              "tech_category": category,
                              "priority": max(90, int(existing.get("priority") or 0))}})
            if not already:
                print(f"  flag   {name:34s} -> existing {existing.get('name')!r} "
                      f"({existing.get('discovery_status')})")
            continue

        stats["new"] += 1
        print(f"  NEW    {name:34s} {domain}")
        if args.apply:
            cid = reg.upsert_company({
                "name": name,
                "domain": domain,
                "signals": {"software": True},
                "sources": ["curated-tech-seed"],
                "tech_category": category,
            })
            # A brand-new row is `pending`; make it CLAIMABLE so the scheduled
            # discovery sweep can reach it too, not only this script's prober.
            reg.enqueue_discovery(cid, priority=90,
                                  reason="curated tech seed", rearm=True)

    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    if not args.apply:
        print("\ndry-run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
