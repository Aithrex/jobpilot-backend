#!/usr/bin/env python3
"""register_ingested_boards.py — register the boards we are ALREADY crawling.

DRY-RUN BY DEFAULT.

The gap
-------
`career_sources` is the coverage denominator's other half: an employer is
covered when we hold a `validated` source for it. Measured on the live
database, 1,437 distinct `(ats, tenant)` boards — carrying **124,217 active
postings** — have no `career_sources` row at all. Wells Fargo, Northrop
Grumman, RTX, PwC, CVS Health, Amazon. We ingest their jobs every crawl and
the registry does not know their boards exist.

That is bookkeeping, not discovery, and it is why 6,514 of 7,110 `candidate`
sources have no endpoint to verify and 1,040 uncovered technology employers
have no domain: pass 2 walks from an employer's domain TO a board, and it had
no board to walk to.

The signal, and why it is not slug trust
----------------------------------------
For each board this counts the employer names its own postings carry. When
one name accounts for at least `--consistency` of them (default 0.90, minimum
`--min-postings` postings), that is the PROVIDER naming the owner — the same
`ats_provider` class as `board_owner` for Lever and Ashby, and a different
thing entirely from a slug that looks similar. A board whose postings name
many employers gets NO ownership signal and is flagged as a probable
marketplace: Jobgether's 6,533 postings name hundreds of companies, and that
is precisely the case the rule must refuse.

One signal class is never enough. These rows land as `content_validated`, not
`validated` — they still need a second, independent class, which is what
`verify_career_sources.py --mode employers` goes and gets.

Usage
-----
    PYTHONPATH=shared/python python3 scripts/register_ingested_boards.py
    PYTHONPATH=shared/python python3 scripts/register_ingested_boards.py --apply
    PYTHONPATH=shared/python python3 scripts/register_ingested_boards.py \\
        --apply --consistency 0.95 --min-postings 5
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))


def _db():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


from services import career_source_service as css      # noqa: E402
from services import employer_registry as er           # noqa: E402


def vendor_key(value: Any) -> str:
    """`oracle-orc` on a posting and `oracle_orc` in the registry are the same
    vendor. Comparing them raw reported 100 boards as unregistered that were
    already there."""
    return str(value or "").strip().lower().replace("-", "_")


def board_owner_signal(names: collections.Counter, *, consistency: float,
                       min_postings: int) -> Tuple[Optional[str], float, int]:
    """`(owner_name | None, share, total)` from the names the board printed."""
    total = sum(names.values())
    if not total:
        return None, 0.0, 0
    name, n = names.most_common(1)[0]
    share = n / total
    if total < min_postings or share < consistency or not str(name or "").strip():
        return None, share, total
    return str(name), share, total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the sources (default: report only)")
    ap.add_argument("--consistency", type=float, default=0.90,
                    help="share of a board's postings that must name one "
                         "employer before the provider counts as naming it")
    ap.add_argument("--min-postings", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    db = _db()
    print(f"mode: {'APPLY' if args.apply else 'dry-run'}")
    print(f"rule: one employer must name >= {args.consistency:.0%} of a board's "
          f"postings, minimum {args.min_postings}")

    known = set()
    for d in db[css.CAREER_SOURCES].find({}, {"ats_vendor": 1, "tenant": 1}):
        known.add((vendor_key(d.get("ats_vendor")),
                   str(d.get("tenant") or "").strip().lower()))
    print(f"career_sources already registered: {len(known):,}")

    boards: Dict[Tuple[str, str], collections.Counter] = collections.defaultdict(
        collections.Counter)
    urls: Dict[Tuple[str, str], str] = {}
    for doc in db.job_postings.find(
            {"active": True},
            {"ats": 1, "tenant": 1, "employer_name": 1, "canonical_url": 1,
             "employer_id": 1}):
        ten = str(doc.get("tenant") or "").strip().lower()
        if not ten:
            continue
        key = (vendor_key(doc.get("ats")), ten)
        boards[key][str(doc.get("employer_name") or "").strip()] += 1
        urls.setdefault(key, str(doc.get("canonical_url") or ""))
    print(f"boards we are actively ingesting: {len(boards):,}")

    missing = {k: v for k, v in boards.items() if k not in known}
    print(f"of those, unregistered: {len(missing):,} "
          f"({sum(sum(c.values()) for c in missing.values()):,} active postings)")

    stats = collections.Counter()
    records: List[Dict[str, Any]] = []
    multi: List[Tuple[int, str, str]] = []
    for (vendor, tenant), names in sorted(
            missing.items(), key=lambda kv: -sum(kv[1].values())):
        if args.limit and len(records) >= args.limit:
            break
        owner, share, total = board_owner_signal(
            names, consistency=args.consistency, min_postings=args.min_postings)
        if owner is None:
            # Two different reasons, and calling both "marketplace" would be a
            # false accusation: a board with one employer and two postings is
            # simply too small to prove anything yet.
            if len(names) > 1 and share < args.consistency:
                stats["many_employers_no_owner"] += 1
                multi.append((total, f"{vendor}:{tenant}",
                              f"{len(names)} distinct names, top {share:.0%}"))
            else:
                stats["too_few_postings_to_judge"] += 1
            # Registered anyway, with NO ownership signal: the board exists and
            # the denominator should say so. It simply cannot claim an owner.
            emp = (er.resolve_employer(tenant_key=f"{vendor}:{tenant}")
                   or er.resolve_employer(tenant_key=f":{tenant}"))
        else:
            stats["provider_names_owner"] += 1
            emp = er.resolve_employer(tenant_key=f"{vendor}:{tenant}", name=owner)
            if emp is None:
                # The registry stores bare `:tenant` for rows whose vendor was
                # not recorded; try that before falling back to the name.
                emp = er.resolve_employer(tenant_key=f":{tenant}")
            if emp is None:
                stats["owner_named_but_unresolved"] += 1

        employer_id = (emp or {}).get("employer_id") or ""
        if not employer_id:
            # No canonical employer to hang it on. Skipping keeps the registry
            # honest: a source with no employer cannot move anyone's coverage,
            # and inventing an employer here would be the name-only merge the
            # whole design refuses.
            stats["skipped_no_employer"] += 1
            continue

        own_ev = []
        if owner:
            own_ev.append(css.evidence(
                css.SIGNAL_PROVIDER_OWNER, source=f"{vendor}:postings",
                value=f"{owner} names {share:.0%} of {total} postings"))
        con_ev = [css.evidence(
            "content_fetch", source="job_inventory",
            value=f"{total} active postings ingested from this board")]
        records.append(css.build_source(
            employer_id=employer_id, ats_vendor=vendor, tenant=tenant,
            career_landing_url=urls.get((vendor, tenant), ""),
            discovery_method=css.METHOD_PROVIDER_METADATA if owner
            else css.METHOD_TENANT_ARCHIVE,
            ownership_evidence=own_ev, content_validation_evidence=con_ev,
            member_key=f"{vendor}-{tenant}", job_count_last=total))
        stats[f"state:{records[-1]['state']}"] += 1

    print(f"\nprepared {len(records):,} sources")
    for k, n in sorted(stats.items()):
        print(f"  {k:34} {n:>6,}")
    if multi:
        print(f"\nboards naming MANY employers (no ownership signal, likely "
              f"marketplaces or shared tenants) - top 10 of {len(multi):,}:")
        for total, key, detail in sorted(multi, reverse=True)[:10]:
            print(f"  {total:>7,}  {key:<34} {detail}")

    written: Any = 0
    if args.apply and records:
        written = css.bulk_upsert_sources(records)
        if isinstance(written, dict):
            print("\nwrote: " + ", ".join(f"{k}={v:,}" for k, v in
                                           sorted(written.items())))
        else:
            print(f"\nwrote {written:,} sources")
    elif not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "boards_ingesting": len(boards), "unregistered": len(missing),
            "prepared": len(records),
            "written": written if isinstance(written, dict) else int(written),
            "stats": dict(stats), "applied": bool(args.apply),
        }, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
