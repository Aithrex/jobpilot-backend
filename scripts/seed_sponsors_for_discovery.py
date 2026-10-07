#!/usr/bin/env python3
"""seed_sponsors_for_discovery.py — put every H-1B sponsor on the path to
being crawled.

DRY-RUN BY DEFAULT.

Why this exists
---------------
`sponsorship_signals` holds the USCIS H-1B Employer Data Hub (FY2026, the
"Employer Information.xlsx" export): 43,566 employers, 41,885 with at least
one approval. They are the employers our users can actually be hired by, and
on 2026-10-06 we held job postings for 1,090 of them - 2.6% of the employers,
27% of the approvals. 39,102 had no employer record of any kind, so nothing
was ever going to look for their careers page.

Discovery (`career_portal_discovery`) is the machinery that turns "a company
exists" into "a board we crawl": it finds the company's own site, reads its
careers page and fingerprints the ATS behind it. It works from `companies`,
which until now was fed by state registries and SEC filers only. This feeds
it the sponsors, through the registry's own `bulk_upsert_companies`, with the
signal the registry already ranks highest (`signals.h1b`, weight 50) - so a
proven sponsor is probed before the unscored registry tail (priority 1).

Within the sponsors, the ones that file more are probed first: priority is
raised with the approval count (50 + up to 40), never lowered.

What it does not do
-------------------
* It does not create job sources. A sponsor only becomes crawled when
  discovery finds and validates its board; a name alone proves nothing
  (see ADR-006 on why a guessed slug is not coverage).
* It does not touch an employer already discovered: an existing company keeps
  its `discovery_status`. A `deferred` one is retried by `refill_queue` on
  its normal cooldown.
* It does not seed a sponsor we already hold a board for. Which sponsors
  those are is taken from `sponsorship_gap_report` (evidence-only matching),
  because the first draft of this script matched on name + state and called
  Amazon, Microsoft, Google and NVIDIA "new" - 41,017 would-be duplicates.
* It does not duplicate a company the registry already knows under another
  state: that record is marked as a sponsor and re-prioritised instead.

    python scripts/seed_sponsors_for_discovery.py                    # dry run
    python scripts/seed_sponsors_for_discovery.py --apply
    python scripts/seed_sponsors_for_discovery.py --min-approvals 5  # the larger ones only
"""
from __future__ import annotations

import argparse
import collections
import math
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

SOURCE = "uscis-h1b"
_STATE_AT_END = re.compile(r",\s*([A-Za-z]{2})\s*$")


def state_of(location: Any) -> str:
    """Two-letter state from the signal's `location`, or "" when it has none."""
    if isinstance(location, dict):
        state = str(location.get("state") or "").strip().upper()
        if len(state) == 2 and state.isalpha():
            return state
        location = location.get("raw") or location.get("label") or ""
    m = _STATE_AT_END.search(str(location or ""))
    return m.group(1).upper() if m else ""


def priority_for(approvals: int) -> int:
    """50 (the registry's own H-1B weight) plus up to 40 for volume.
    1 approval -> 53, 10 -> 60, 100 -> 70, 1,000 -> 80, 10,000 -> 90."""
    return 50 + min(40, int(10 * math.log10(max(1, int(approvals)) + 1)))


def record_for(signal: Dict[str, Any]) -> Dict[str, Any]:
    naics = signal.get("naics")
    code = (naics.get("code") if isinstance(naics, dict) else naics) or ""
    rec: Dict[str, Any] = {
        "name": str(signal.get("employer_display") or "").strip(),
        "state": state_of(signal.get("location")),
        "source": SOURCE,
        "signals": {"h1b": True},
    }
    if code:
        rec["naics"] = str(code)[:10]
    return rec


#: Gap-report buckets that discovery can help. The others already have a
#: board on file (they need validation or a fetcher, not a search).
SEED_BUCKETS = ("no_employer_row", "no_career_source")


def gap_rows(db, min_approvals: int):
    """Every measurable sponsor with the coverage bucket the gap report gives
    it. Reused rather than re-derived: matching a sponsor to an employer we
    already crawl is evidence-only there (curated alias, registry at >= 0.80,
    canonical id), and a second, looser matcher here would disagree with it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sponsorship_gap_report", Path(__file__).resolve().parent / "sponsorship_gap_report.py")
    gap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gap)
    sponsors = gap.load_sponsors(db, min_approvals)
    by_id, by_name, by_first_token = gap.load_employers(db)
    alias_keys = gap.load_alias_keys(db)
    postings = gap.load_posting_counts(db)
    return gap.build_rows(sponsors, by_id, by_name, by_first_token, alias_keys,
                          gap.load_career_sources(db), gap.load_ats_endpoints(db), *postings)


def record_from_gap_row(row: Dict[str, Any]) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "name": str(row.get("employer_display") or "").strip(),
        "state": str(row.get("state") or "").strip().upper()[:2],
        "source": SOURCE,
        "signals": {"h1b": True},
    }
    if row.get("naics_code"):
        rec["naics"] = str(row["naics_code"])[:10]
    if row.get("domain"):
        rec["domain"] = str(row["domain"]).strip().lower()
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    ap.add_argument("--min-approvals", type=int, default=1)
    args = ap.parse_args()

    from pymongo import UpdateOne
    from services import company_registry_service as reg
    from utils.db_connect import DBConnect

    db = DBConnect().get_db()
    rows = gap_rows(db, args.min_approvals)
    buckets = collections.Counter(r["bucket"] for r in rows)
    todo = [r for r in rows if r["bucket"] in SEED_BUCKETS
            and str(r.get("employer_display") or "").strip()]
    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | sponsors with >= "
          f"{args.min_approvals} approval(s): {len(rows):,}")
    print(f"  by coverage bucket: {dict(buckets.most_common())}")
    print(f"  NOT seeded (we already hold a board for them): "
          f"{len(rows) - len(todo):,} | to seed: {len(todo):,}")

    # The registry may already know the company under another state: one
    # employer, registered where it incorporated and sponsoring where it hires.
    norm = {reg.normalize_company_name(r["employer_display"]): r for r in todo}
    known: Dict[str, list] = collections.defaultdict(list)
    names = list(norm)
    for i in range(0, len(names), 5000):
        for c in db.companies.find({"name_normalized": {"$in": names[i:i + 5000]}},
                                   {"company_id": 1, "name_normalized": 1, "discovery_status": 1}):
            known[c["name_normalized"]].append(c)
    new = [r for n, r in norm.items() if n not in known]
    seen = [r for n, r in norm.items() if n in known]
    status = collections.Counter(c.get("discovery_status") for n in known for c in known[n])
    print(f"  new to the registry (queued for discovery): {len(new):,} "
          f"carrying {sum(int(r['approvals']) for r in new):,} approvals")
    print(f"  already in the registry under that name: {len(seen):,} sponsors / "
          f"{sum(len(v) for v in known.values()):,} records, by status {dict(status)}")
    tiers = collections.Counter(priority_for(int(r["approvals"])) // 10 * 10 for r in todo)
    print(f"  priority tiers: {dict(sorted(tiers.items(), reverse=True))}")
    print("  largest sponsors that will be newly queued:")
    for r in sorted(new, key=lambda r: -int(r["approvals"]))[:15]:
        print(f"     {int(r['approvals']):>6,}  {str(r['employer_display'])[:52]:52} "
              f"{str(r.get('state') or ''):2}  {r.get('domain') or ''}")
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")
        return 0

    stats = reg.bulk_upsert_companies(record_from_gap_row(r) for r in new)
    ops = []
    for r in new:
        rec = record_from_gap_row(r)
        ops.append(UpdateOne(
            {"company_id": reg.company_id_for(rec["name"], rec["state"])},
            {"$max": {"priority": priority_for(int(r["approvals"]))},
             "$set": {"signals.h1b": True, "signals.h1b_approvals": int(r["approvals"])}}))
    for n, r in norm.items():
        for c in known.get(n, ()):
            ops.append(UpdateOne(
                {"company_id": c["company_id"]},
                {"$max": {"priority": priority_for(int(r["approvals"]))},
                 "$set": {"signals.h1b": True, "signals.h1b_approvals": int(r["approvals"])},
                 "$addToSet": {"sources": SOURCE}}))
    for i in range(0, len(ops), 1000):
        db.companies.bulk_write(ops[i:i + 1000], ordered=False)
    print(f"\nregistry: {stats} | priorities set on {len(ops):,} records")
    print("discovery picks these up on its next refill (every 15 minutes), highest priority first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
