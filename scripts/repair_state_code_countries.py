#!/usr/bin/env python3
"""repair_state_code_countries.py — re-file "City, ST" postings that the
location parser put in the wrong COUNTRY because their US state code is also
a country code.

DRY-RUN BY DEFAULT.

Why this exists
---------------
`job_inventory_service.normalize_location` promoted the second part of a
two-part location to the country whenever it parsed as one. Six US state
codes are also ISO country codes, so on live inventory (2026-10-03):

    "San Francisco, CA"   -> Canada     2,963 active postings
    "Downers Grove, IL"   -> Israel     1,387
    "Littleton, CO"       -> Colombia     946
    "Wilmington, DE"      -> Germany      168
    "Idaho Falls, ID"     -> Indonesia    105
    "Fayetteville, AR"    -> Argentina     84
    "Whitestown, IN"      -> India        658 (Indiana)

Every one was dropped by the US-only feeds and by the tech feed's country
filter. The parser no longer promotes a state code; this re-runs the FIXED
parser over the stored raw string of each affected entry, so the stored value
is exactly what a fresh crawl would now write.

It touches only entries carrying the bug's signature - a two-part raw string
whose second part became `country` with `region` left empty - and writes only
when the re-derived `country_code` differs. `key`, the indexed form, is
unchanged by construction ("san-francisco-ca" either way).

    python scripts/repair_state_code_countries.py           # dry run
    python scripts/repair_state_code_countries.py --apply
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

#: US state codes that are also ISO country codes, plus "IN".
COLLIDING = ("AR", "CA", "CO", "DE", "ID", "IL", "IN")
_TWO_PART = re.compile(r"^\s*[^,]+,\s*([A-Za-z]{2})\s*$")
_SEVERAL_PLACES = re.compile(r";|\||\u2022|\s/\s")


def has_signature(loc) -> bool:
    """A "City, ST" entry the parser filed under the state's namesake country
    (region empty, country = ST), or one already re-filed as a region that
    the resolver could not place then ("Whitestown, IN") or placed abroad
    ("Peru, IN") - both resolve since location-country-1.1.0."""
    if not isinstance(loc, dict):
        return False
    raw = str(loc.get("raw") or "")
    if _SEVERAL_PLACES.search(raw) or raw.count(",") >= 2:
        # "San Francisco, CA; Mountain View, CA" / "Building 5, Santa Clara,
        # CA": the trailing state code was read as the country.
        return str(loc.get("country_code") or "").upper() in COLLIDING
    m = _TWO_PART.match(raw)
    if not m or m.group(1).upper() not in COLLIDING:
        return False
    st = m.group(1).upper()
    region = str(loc.get("region") or "").upper()
    country = str(loc.get("country") or "").upper()
    return (not region and country == st) or (region == st and not country)


def repaired(locations):
    """`(new_locations, changes)`; `changes` lists `(old, new, raw)`."""
    from services import job_inventory_service as inv
    out, changes = [], []
    for loc in locations or []:
        if has_signature(loc):
            new = inv.normalize_location(str(loc.get("raw") or ""))
            multi = bool(_SEVERAL_PLACES.search(str(loc.get("raw") or ""))) \
                or str(loc.get("raw") or "").count(",") >= 2
            # A multi-place or three-part string is only ever moved TO the US:
            # that is the one direction the trailing-state bug went wrong in.
            if multi and new["country_code"] != "US":
                out.append(loc)
                continue
            if new["country_code"] != loc.get("country_code"):
                changes.append((loc.get("country_code"), new["country_code"], loc.get("raw")))
                loc = new
        out.append(loc)
    return out, changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    ap.add_argument("--samples", type=int, default=3, help="example raw strings per move")
    args = ap.parse_args()

    from pymongo import UpdateOne
    from utils.db_connect import DBConnect

    jp = DBConnect().get_db().job_postings
    codes = list(COLLIDING) + [c.lower() for c in COLLIDING]
    q = {"status": "active", "$or": [
        {"locations": {"$elemMatch": {"country_code": {"$in": list(COLLIDING)},
                                      "raw": {"$regex": r";|\||\u2022| / |,[^,]*,"}}}},
        {"locations": {"$elemMatch": {"country": {"$in": codes}, "region": {"$in": ["", None]}}}},
        {"locations": {"$elemMatch": {"region": {"$in": codes}, "country": {"$in": ["", None]}}}},
    ]}
    print(f"mode: {'APPLY' if args.apply else 'dry-run'} | candidates: {jp.count_documents(q):,}")

    moves: collections.Counter = collections.Counter()
    samples: dict = collections.defaultdict(list)
    ops, docs_changed = [], 0
    for doc in jp.find(q, {"_id": 1, "locations": 1}).batch_size(1000):
        new_locs, changes = repaired(doc.get("locations"))
        if not changes:
            continue
        docs_changed += 1
        for old, new, raw in changes:
            moves[(old, new or "unresolved")] += 1
            if len(samples[(old, new)]) < args.samples:
                samples[(old, new)].append(raw)
        if args.apply:
            ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"locations": new_locs}}))
            if len(ops) >= 500:
                jp.bulk_write(ops, ordered=False)
                ops = []
    if ops:
        jp.bulk_write(ops, ordered=False)

    print(f"postings re-filed: {docs_changed:,}")
    for (old, new), n in moves.most_common():
        ex = samples[(old, "" if new == "unresolved" else new)]
        print(f"  {old} -> {new:<10} {n:>7,}   e.g. {ex}")
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
