#!/usr/bin/env python3
"""Check that each board endpoint belongs to the company it is filed under.

A slug that validates proves a board EXISTS. It does not prove whose board it
is, and a wrong guess that validates is the worst shape of wrong: it serves
real jobs, so it reads as coverage rather than as an error, and those jobs are
then shown to users under another employer's name.

Two paths in this repo guess slugs - `career_portal_discovery._slug_probe` and
`scripts/probe_software_company_boards.py` - and both now corroborate the guess
against `AtsProvider.board_owner` before claiming it. This script applies the
same check to the rows already in `company_ats_endpoints`, which were written
before that existed.

Only Greenhouse and SmartRecruiters can answer; Lever and Ashby publish no
organization name, so their rows are reported as UNVERIFIABLE rather than
judged. A row whose owner cannot be read is left alone - "cannot say" is not
"does not match".

Usage:
    PYTHONPATH=shared/python python scripts/audit_board_endpoint_owners.py
    PYTHONPATH=shared/python python scripts/audit_board_endpoint_owners.py --apply
"""
from __future__ import annotations

import argparse
import collections
import sys
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

from services import company_registry_service as reg           # noqa: E402
from services.ats_providers import registry as ats_registry     # noqa: E402
from services.ats_providers.base import Endpoint                # noqa: E402
from services.ats_providers.http import simple_context          # noqa: E402
from services.ats_providers.util import names_match             # noqa: E402

#: The vendors a slug can be guessed for. Others are reached only by
#: fingerprinting an employer's own site, which cannot name the wrong company.
VENDORS = ("greenhouse", "lever", "ashby", "smartrecruiters")


def main() -> int:
    ap = argparse.ArgumentParser()
    # No blanket --apply. The residual mismatches are a judgement call this
    # script cannot make: "Precoat Metals" under AZZ INC and "Penn Interactive"
    # under PENN Entertainment are subsidiaries and correct, while "Garage Door
    # Specialist" under GDS Holdings and "Space Programs" under SpaceX are
    # wrong. Retiring the set wholesale would delete real coverage to remove
    # bad coverage, which is the trade this whole change exists to avoid.
    ap.add_argument("--retire", action="append", default=[], metavar="MEMBER_KEY",
                    help="retire ONE endpoint by member_key; repeatable")
    ap.add_argument("--rate", type=float, default=8.0)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    col = reg._db()[reg.ENDPOINTS]
    rows = list(col.find({"vendor": {"$in": list(VENDORS)}},
                         {"member_key": 1, "vendor": 1, "tenant": 1,
                          "display_name": 1, "status": 1, "source": 1}))
    print(f"board endpoints to check: {len(rows):,}")

    ctx = simple_context(budget_seconds=60 * 60, global_rate=args.rate)
    stats = collections.Counter()
    bad = []
    t0 = time.time()

    def check(row):
        provider = ats_registry.get(row["vendor"])
        name = row.get("display_name") or ""
        if provider is None or not name:
            stats["skipped"] += 1
            return
        try:
            owner = provider.board_owner(
                Endpoint(vendor=row["vendor"], tenant=row.get("tenant") or "",
                         site="", host=""), ctx)
        except Exception:
            owner = None
        if owner is None:
            stats[f"unverifiable:{row['vendor']}"] += 1
            return
        if names_match(name, owner):
            stats["ok"] += 1
            return
        stats["MISMATCH"] += 1
        bad.append((row, owner))
        print(f"  MISMATCH {row['vendor']}/{row.get('tenant')}: board is "
              f"{owner!r}, filed as {name!r}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(check, rows))

    print(f"\nchecked {len(rows):,} in {time.time()-t0:.0f}s")
    for key, n in sorted(stats.items()):
        print(f"   {key:26s} {n}")

    if args.retire:
        wanted = set(args.retire)
        for row, owner in [(r, o) for r, o in bad if r["member_key"] in wanted]:
            col.update_one(
                {"member_key": row["member_key"]},
                {"$set": {"status": reg.STATUS_DEAD,
                          "detail": f"board belongs to {owner!r}, not "
                                    f"{row.get('display_name')!r}",
                          "retired_at": reg._now()},
                 # Detach it from the company so nothing reads it as that
                 # employer's board; the slug itself may be someone else's
                 # legitimate endpoint, which discovery can re-file properly.
                 "$unset": {"company_id": ""}})
            print(f"retired {row['member_key']} (board is {owner!r})")
    elif bad:
        print(f"\n{len(bad)} need a human call. Retire one with:\n"
              f"   --retire <member_key>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
