#!/usr/bin/env python3
"""backfill_canonical_employer_id.py — stamp the canonical employer on postings
that have owner-verified evidence, and quarantine every posting that does not.

DRY-RUN BY DEFAULT.

What it writes
--------------
`job_postings.canonical_employer_id` (plus `canonical_employer_basis` and
`canonical_employer_version`) and nothing else. `employer_id` is not read,
not rewritten and not deleted: it is the key three production indexes and
every existing client use, and the entire value of this field is that it can
be wrong without anything breaking.

What it refuses to write
------------------------
Everything `canonical_employer.BoardOwnership` refuses — a board two employers
own-verify and neither is the other's parent, a marketplace board, a board
with no accepted ownership evidence, a posting with no `(ats, tenant)`. A
conflicting board is written to `employer_board_conflicts` for a human,
ordered by how many active postings hang on the answer.

There is no name-matching path in this script, deliberately. A name-only match
is exactly what `employer_registry.merge_decision` quarantines, and re-deriving
one here under a different name would reintroduce it through the back door.

`--unstamp` reverses the whole thing (`$unset` on the three fields), which is
what makes this safe to run: the migration is a field, and the rollback is a
field.

Usage
-----
    PYTHONPATH=shared/python python scripts/backfill_canonical_employer_id.py
    PYTHONPATH=shared/python python scripts/backfill_canonical_employer_id.py --apply
    PYTHONPATH=shared/python python scripts/backfill_canonical_employer_id.py \
        --apply --all-postings --batch 2000
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))


def _db():
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


from services import canonical_employer as ce        # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def plan(db, *, active_only: bool = True) -> Dict[str, Any]:
    """What each board WOULD get, without touching a posting.

    Grouped by board rather than iterated per posting: the decision is a
    property of the board, so 238,000 postings ask 3,147 questions.
    """
    own = ce.build_board_ownership(db)
    match: Dict[str, Any] = {"status": "active"} if active_only else {}
    rows = db.job_postings.aggregate([
        {"$match": match},
        {"$group": {"_id": {"a": "$ats", "t": "$tenant", "k": "$board_kind"},
                    "n": {"$sum": 1},
                    "names": {"$addToSet": "$employer_name"}}},
    ], allowDiskUse=True)

    assign: Dict[tuple, Dict[str, Any]] = {}
    refuse: Dict[str, int] = collections.defaultdict(int)
    by_basis: Dict[str, int] = collections.defaultdict(int)
    contested_postings: Dict[tuple, int] = collections.defaultdict(int)
    contested_names: Dict[tuple, set] = collections.defaultdict(set)
    considered = owner_verified = 0
    for row in rows:
        rid, n = row["_id"], int(row["n"])
        considered += n
        key = ce.board_key(rid.get("a"), rid.get("t"))
        contested = key in own.conflicts
        if contested:
            contested_postings[key] += n
            contested_names[key].update(
                str(x) for x in (row.get("names") or []) if x)
        d = own.resolve_board(ats=rid.get("a"), tenant=rid.get("t"),
                              board_kind=rid.get("k"))
        if d.assigned or contested:
            owner_verified += n
        if d.assigned:
            bucket = assign.setdefault(key, {"employer_id": d.employer_id,
                                             "basis": d.basis,
                                             "postings": 0, "raw": []})
            bucket["postings"] += n
            bucket["raw"].append((rid.get("a"), rid.get("t"), rid.get("k")))
            by_basis[d.basis] += n
            continue
        refuse[d.reason] += n
    return {
        "ownership": own,
        "assign": assign,
        "refused_by_reason": dict(refuse),
        "attributed_by_basis": dict(by_basis),
        "contested_postings": dict(contested_postings),
        "contested_names": {k: sorted(v)[:8] for k, v in contested_names.items()},
        "postings_considered": considered,
        "owner_verified_postings": owner_verified,
        "attributable_postings": sum(b["postings"] for b in assign.values()),
    }


def apply_plan(db, built: Dict[str, Any], *, active_only: bool,
               batch: int, limit: int = 0, verbose: bool = False) -> Dict[str, Any]:
    from pymongo import UpdateMany

    ops: List[Any] = []
    written = 0
    touched_boards = 0
    for (vendor, tenant), bucket in sorted(
            built["assign"].items(), key=lambda kv: -kv[1]["postings"]):
        if limit and written >= limit:
            break
        # Re-express the board key in the spellings actually STORED on
        # postings, so the update filter is an equality the index can serve.
        # Normalising in the filter would need `$expr` and a collection scan.
        variants = {(a, t) for a, t, _k in bucket["raw"]}
        for ats, ten in variants:
            q: Dict[str, Any] = {"ats": ats, "tenant": ten,
                                 "board_kind": {"$nin": list(
                                     ce.NON_ATTRIBUTABLE_BOARD_KINDS)},
                                 "$or": [{ce.FIELD: {"$exists": False}},
                                         {ce.FIELD: {"$ne": bucket["employer_id"]}}]}
            if active_only:
                q["status"] = "active"
            ops.append(UpdateMany(q, {"$set": {
                ce.FIELD: bucket["employer_id"],
                ce.BASIS_FIELD: bucket["basis"],
                ce.VERSION_FIELD: ce.CANONICAL_EMPLOYER_VERSION,
            }}))
        touched_boards += 1
        if len(ops) >= batch:
            written += db.job_postings.bulk_write(ops, ordered=False).modified_count
            ops = []
            if verbose:
                print(f"  ... {written:,} postings stamped", flush=True)
    if ops:
        written += db.job_postings.bulk_write(ops, ordered=False).modified_count

    own = built["ownership"]
    precedence = own.precedence_boards
    conflicts = blocked = 0
    for key, n in built["contested_postings"].items():
        owners = own.conflicts.get(key) or []
        winner = precedence.get(key, "")
        ce.record_board_conflict(
            db, key, owners, postings=n,
            display_names=built["contested_names"].get(key, ()),
            attributed_to=winner,
            resolved_by=ce.BASIS_DOMAIN_CHAIN_PRECEDENCE if winner else "")
        conflicts += 1
        if not winner:
            blocked += 1
    return {"postings_stamped": written, "boards": touched_boards,
            "conflicts_recorded": conflicts,
            "conflicts_blocking_attribution": blocked}


def unstamp(db, *, active_only: bool) -> int:
    q: Dict[str, Any] = {ce.FIELD: {"$exists": True}}
    if active_only:
        q["status"] = "active"
    return db.job_postings.update_many(
        q, {"$unset": {ce.FIELD: "", ce.BASIS_FIELD: "",
                       ce.VERSION_FIELD: ""}}).modified_count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write. Without it nothing is written.")
    ap.add_argument("--unstamp", action="store_true",
                    help="remove the three canonical fields and exit (needs --apply)")
    ap.add_argument("--batch", type=int, default=400)
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after roughly this many postings (0 = no limit)")
    ap.add_argument("--active-only", action="store_true", default=True)
    ap.add_argument("--all-postings", dest="active_only", action="store_false",
                    help="include closed postings (history keeps its attribution)")
    ap.add_argument("--indexes", action="store_true",
                    help="create the canonical-company indexes (needs --apply)")
    ap.add_argument("--json", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    db = _db()

    if args.unstamp:
        if not args.apply:
            print("--unstamp needs --apply. Nothing written.")
            return 0
        n = unstamp(db, active_only=args.active_only)
        print(f"unstamped {n:,} postings")
        return 0

    if args.indexes:
        if not args.apply:
            print("--indexes needs --apply. Nothing created.")
        else:
            print("indexes:", ", ".join(ce.ensure_indexes()) or "(none)")

    built = plan(db, active_only=args.active_only)
    own = built["ownership"]
    ov = built["owner_verified_postings"]
    att = built["attributable_postings"]
    print(f"career_sources read         {own.sources_read:,}")
    print(f"attributing boards          {own.boards:,} "
          f"({own.unambiguous_boards:,} unambiguous, "
          f"{len(own.precedence_boards):,} settled by domain chain)")
    print(f"contested boards            {len(own.conflicts):,} "
          f"({len(own.unresolved_conflicts):,} still blocking attribution)")
    print(f"postings considered         {built['postings_considered']:,}")
    print(f"on owner-verified boards    {ov:,}")
    print(f"attributable postings       {att:,}"
          + (f"  ({att / ov:.1%} of owner-verified)" if ov else ""))
    print("\nattributed, by basis:")
    for basis, n in sorted(built["attributed_by_basis"].items(), key=lambda kv: -kv[1]):
        print(f"  {basis:38s} {n:>9,}")
    print("\nquarantined, by reason:")
    for reason, n in sorted(built["refused_by_reason"].items(), key=lambda kv: -kv[1]):
        print(f"  {reason:38s} {n:>9,}")

    result: Dict[str, Any] = {
        "generated_at": _now().isoformat(),
        "canonical_employer_version": ce.CANONICAL_EMPLOYER_VERSION,
        "attributing_boards": own.boards,
        "unambiguous_boards": own.unambiguous_boards,
        "contested_boards": len(own.conflicts),
        "contested_boards_settled_by_precedence": len(own.precedence_boards),
        "contested_boards_unresolved": len(own.unresolved_conflicts),
        "postings_considered": built["postings_considered"],
        "owner_verified_postings": ov,
        "attributable_postings": att,
        "attributed_by_basis": built["attributed_by_basis"],
        "quarantined_by_reason": built["refused_by_reason"],
        "applied": bool(args.apply),
    }

    if args.apply:
        print("\napplying ...")
        out = apply_plan(db, built, active_only=args.active_only,
                         batch=args.batch, limit=args.limit, verbose=args.verbose)
        result.update(out)
        print(f"postings stamped            {out['postings_stamped']:,}")
        print(f"boards written              {out['boards']:,}")
        print(f"conflicts recorded          {out['conflicts_recorded']:,} "
              f"({out['conflicts_blocking_attribution']:,} blocking)")
        stats = ce.attribution_stats(db, only_active=args.active_only)
        result["attribution"] = stats
        print(f"\nattributed / attributable   {stats['attributed_postings']:,} / "
              f"{stats['attributable_postings']:,} "
              f"({stats['attribution_pct']:.1%})")
        print(f"attributed / owner-verified {stats['attributed_postings']:,} / "
              f"{stats['owner_verified_postings']:,} "
              f"({stats['owner_verified_attribution_pct']:.1%})"
              "   <- the acceptance criterion")
    else:
        print("\nDRY RUN — nothing written. Re-run with --apply.")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, default=str),
                                   encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
