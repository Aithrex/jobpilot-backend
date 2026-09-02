#!/usr/bin/env python3
"""backfill_posting_normalization.py — bring stored postings up to
`NORMALIZATION_VERSION` using ONLY what is already on the document.

DRY-RUN BY DEFAULT.

Why this exists
---------------
`normalize_posting` writes the current normalisation, but it only runs when a
posting is ingested. On the live database every one of 224,149 active postings
still carries `normalization_version: 3`, so three normalisations that shipped
in v4 exist in code and on no row:

  * **seniority from the posting's own stated experience requirement.**
    10,721 active postings carry a `requirements.required_years` — the
    employer's own "minimum N years" line, already extracted — and STILL read
    `seniority: unknown`. Retrieval filters on the stored band, so a candidate
    filtering for `senior` never sees them. The number is sitting on the
    document; declining to read it is not caution.
  * **`locations[].country_code`** (ISO 3166-1 alpha-2), derived from the
    stored country spelling. Without it the country filter can only match on
    exact spelling, and "USA" / "United States" / "us" are three rows apart.
  * **`board_kind`** — first-party employer board or third-party marketplace.
    The catalogs have carried it since Jobgether was added and the feed's
    projection has always asked for it, but no write path stored it: it was
    absent on all 225,095 active postings, so `apply_mode: direct_apply`
    matched every one of them, marketplace reposts included. Jobgether alone
    carries 1,806 core-tech postings belonging to other companies.
  * **`employment_type_normalized`**, derived from the stored provider
    spelling. `employment_type` itself is deliberately NOT rewritten: it is
    digested by `compute_content_hash`, and changing it would re-hash and
    re-parse the entire inventory for a rename that changes no meaning.

`--with-bodies` and why re-reading OUR OWN storage is the polite option
----------------------------------------------------------------------
Two v4 normalisations need the JD text: the work-mode read and the
description-derived seniority signals. `--with-bodies` fetches those from S3
under `description_ref`, which is our own bucket — no employer is touched.

That matters because of what `job_ingest_worker._listing_implies_same_content`
does with the version stamp: a posting whose `normalization_version` is not
current fails the fetch-once check, so the crawler re-fetches its body from the
employer's board. With every row at v3 and the code at v4, the next full crawl
would pull ~224,000 detail pages off employers' own sites to learn nothing new.
Reading the same bodies out of S3 instead costs cents and reaches nobody
else's servers.

The version stamp is therefore tied to whether the bodies were actually read.
Stamping v4 tells the crawler "this row is current", so a row stamped without
its body would keep its v3 work mode forever. Without `--with-bodies` (or when
the JD store turns out to be unreachable) this pass improves the three
body-free fields and LEAVES the version alone, so the crawler still does the
complete job on its next pass. `--stamp-version` overrides that for an
operator who has decided the body-derived reads are not worth a re-crawl;
`--no-stamp-version` refuses it even with bodies.

What it never does
------------------
It never writes a seniority it had to invent. A posting with no title signal,
no band and no stated years stays `unknown`, because `unknown` is the true
answer and retrieval already ORs it in rather than excluding it. And it never
rewrites `employment_type` itself, only the normalized twin: the raw spelling
is digested by `compute_content_hash`, and renaming it would re-hash and
re-parse the whole inventory for a change that alters no meaning.

Usage
-----
    PYTHONPATH=shared/python python3 scripts/backfill_posting_normalization.py
    PYTHONPATH=shared/python python3 scripts/backfill_posting_normalization.py --apply
    PYTHONPATH=shared/python python3 scripts/backfill_posting_normalization.py \
        --apply --limit 50000 --batch 1000
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
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


from services import job_inventory_service as inv    # noqa: E402

TARGET = inv.NORMALIZATION_VERSION


def plan(doc: Dict[str, Any], body: str = "") -> Tuple[Dict[str, Any], List[str]]:
    """`($set fields, reasons)` for one posting. Empty when nothing improves.

    `body` is the JD text when the caller fetched it, "" otherwise. Every
    improvement it unlocks is additive: with no body this still does the three
    field-derived ones.
    """
    sets: Dict[str, Any] = {}
    why: List[str] = []

    # --- seniority ------------------------------------------------------
    stored = (doc.get("seniority") or "").strip().lower()
    if stored in ("", "unknown"):
        req = doc.get("requirements") if isinstance(doc.get("requirements"), dict) else {}
        years = req.get("required_years")
        band = inv.seniority_of(doc.get("title_raw") or "", description=body,
                                required_years=years)
        if band and band != "unknown" and band != stored:
            sets["seniority"] = band
            why.append(f"seniority {stored or 'missing'} -> {band} (years={years})")

    # --- work_mode, only ever from unknown ------------------------------
    # A stated work mode is the employer's answer and is never overwritten
    # here; this only fills a gap. Without a body there is no new evidence, so
    # the field is left exactly as it is.
    if body and (doc.get("work_mode") or "unknown") == "unknown":
        locs = doc.get("locations") if isinstance(doc.get("locations"), list) else []
        mode = inv.normalize_work_mode(
            None, title=doc.get("title_raw") or "",
            locations=" ".join(str((l or {}).get("raw") or "") for l in locs),
            description=body)
        if mode and mode != "unknown":
            sets["work_mode"] = mode
            why.append(f"work_mode -> {mode}")

    # --- locations[].country_code --------------------------------------
    locs = doc.get("locations")
    if isinstance(locs, list) and locs:
        rebuilt, changed = [], False
        for loc in locs:
            if not isinstance(loc, dict):
                rebuilt.append(loc)
                continue
            entry = dict(loc)
            if not entry.get("country_code"):
                code = inv.normalize_country(entry.get("country"))
                if code:
                    entry["country_code"] = code
                    changed = True
            rebuilt.append(entry)
        if changed:
            sets["locations"] = rebuilt
            why.append("locations[].country_code")

    # --- dedupe identity ------------------------------------------------
    # `variant_group_id` is what the location rollup collapses on. Missing on
    # 7,043 of 232,032 active postings overall but on 2,624 of the NEWEST
    # 3,000 — and newest-first is the order the feed retrieves in, so a role
    # open in six countries showed as six cards on page one.
    if not (doc.get("variant_group_id") or "").strip():
        ident = inv.compute_dedupe_identity(
            employer_id=str(doc.get("employer_id") or ""),
            ats=str(doc.get("ats") or ""),
            source=str(doc.get("source") or ""),
            source_job_id=str(doc.get("source_job_id") or ""),
            canonical_url=str(doc.get("canonical_url") or ""),
            content_hash=str(doc.get("content_hash") or ""),
            title_normalized=str(doc.get("title_normalized") or ""),
        )
        for k, v in ident.items():
            if v and not (doc.get(k) or "").strip():
                sets[k] = v
        if sets.get("variant_group_id"):
            why.append("variant_group_id")

    # --- board_kind -----------------------------------------------------
    # Never written by the ingest path until now, so it is absent on every
    # stored posting. A marketplace board lists many employers' roles under one
    # board, so leaving it absent means a repost reads as the employer's own
    # posting - an attribution error, not a cosmetic one.
    if not (doc.get("board_kind") or "").strip():
        kind = inv._board_kind_of({"tenant": doc.get("tenant"),
                                   "company": doc.get("employer_name")})
        sets["board_kind"] = kind
        why.append(f"board_kind -> {kind}")

    # --- employment_type_normalized ------------------------------------
    if not (doc.get("employment_type_normalized") or "").strip():
        et = inv.normalize_employment_type(doc.get("employment_type"),
                                           title=doc.get("title_raw"))
        if et and et != "unknown":
            sets["employment_type_normalized"] = et
            why.append(f"employment_type_normalized -> {et}")

    return sets, why


def _body_reader(enabled: bool):
    """`(doc -> jd text, bodies_are_live)`.

    Returns "" for every doc when bodies are off or the store is unreachable,
    and never raises: a body we cannot read is a missing improvement, not a
    failed backfill. The second value is what decides the version stamp.
    """
    if not enabled:
        return (lambda _doc: ""), False
    try:
        from services import job_content_store
        if not job_content_store.available():
            print("  --with-bodies requested but the JD store is unavailable; "
                  "continuing with the body-free improvements only")
            return (lambda _doc: ""), False
    except Exception as exc:
        print(f"  JD store unusable ({type(exc).__name__}); "
              f"continuing with the body-free improvements only")
        return (lambda _doc: ""), False

    def _read(doc: Dict[str, Any]) -> str:
        inline = doc.get("description_text") or ""
        if len(inline) > 400:
            return inline
        ch = doc.get("description_ref") or doc.get("content_hash") or ""
        if not ch:
            return inline
        try:
            return job_content_store.get_jd(ch) or inline
        except Exception:
            return inline

    return _read, True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the updates (default: report only)")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many postings (0 = all)")
    ap.add_argument("--batch", type=int, default=1000)
    ap.add_argument("--with-bodies", action="store_true",
                    help=("read JD text back from OUR S3 bucket so the "
                          "work-mode and description-derived seniority reads "
                          "can run. Touches no employer's servers, and is the "
                          "cheap alternative to letting the crawler re-fetch "
                          "224,000 detail pages to learn the same thing."))
    ap.add_argument("--body-workers", type=int, default=16)
    ap.add_argument("--stamp-version", dest="stamp", action="store_true",
                    default=None,
                    help="force the version stamp even without JD bodies")
    ap.add_argument("--no-stamp-version", dest="stamp", action="store_false",
                    help="never stamp, even when bodies were read")
    ap.add_argument("--active-only", action="store_true", default=True)
    ap.add_argument("--all-postings", dest="active_only", action="store_false",
                    help="include closed postings too")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    db = _db()
    jp = db.job_postings
    from pymongo import UpdateOne

    # Rows below the target version, PLUS any row missing the dedupe identity
    # whatever its version. A row can be stamped current and still be missing
    # something this pass can supply, and a selector that only looks at the
    # version stamp would never revisit it.
    q: Dict[str, Any] = {"$or": [
        {"normalization_version": {"$ne": TARGET}},
        {"variant_group_id": {"$in": [None, ""]}},
    ]}
    if args.active_only:
        q["active"] = True
    total = jp.count_documents(q)
    print(f"mode: {'APPLY' if args.apply else 'dry-run'}")
    print(f"target normalization_version: {TARGET}")
    print(f"postings below it: {total:,}"
          f"{' (active only)' if args.active_only else ''}")

    projection = {"job_id": 1, "title_raw": 1, "seniority": 1, "locations": 1,
                  "employment_type": 1, "employment_type_normalized": 1,
                  "requirements": 1, "work_mode": 1, "description_ref": 1,
                  "description_text": 1, "content_hash": 1,
                  "board_kind": 1, "tenant": 1, "employer_name": 1,
                  "variant_group_id": 1, "dedupe_group_id": 1,
                  "canonical_url_key": 1, "canonical_url": 1, "ats": 1,
                  "source": 1, "source_job_id": 1, "title_normalized": 1,
                  "employer_id": 1}

    fetch_body, bodies_live = _body_reader(args.with_bodies)
    stamp = bodies_live if args.stamp is None else bool(args.stamp)
    print(f"version stamp: {'YES' if stamp else 'no'}"
          f" ({'bodies read' if bodies_live else 'no JD bodies'}"
          f"{'; forced by flag' if args.stamp is not None else ''})")
    if not stamp:
        print("  rows stay below the current version, so the crawler will "
              "re-normalise them in full on its next pass")
    counts: collections.Counter = collections.Counter()
    seniority_moves: collections.Counter = collections.Counter()
    ops: List[Any] = []
    seen = written = touched = 0
    started = time.perf_counter()

    cursor = jp.find(q, projection).batch_size(500)
    if args.limit:
        cursor = cursor.limit(args.limit)

    for doc in cursor:
        seen += 1
        sets, why = plan(doc, fetch_body(doc))
        for w in why:
            counts[w.split(" ->")[0].split(" (")[0]] += 1
        if "seniority" in sets:
            seniority_moves[sets["seniority"]] += 1
        if stamp:
            # Only when this pass could actually do the WHOLE v4 job. The stamp
            # is what `_listing_implies_same_content` reads to decide the row
            # needs no re-fetch, so stamping a row we could not fully normalise
            # would freeze it in a half-upgraded state.
            sets["normalization_version"] = TARGET
        elif not sets:
            continue
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": sets}))
        touched += 1
        if len(ops) >= args.batch:
            if args.apply:
                written += jp.bulk_write(ops, ordered=False).modified_count
            ops = []
            if seen % 20000 == 0:
                print(f"    {seen:,}/{total:,} scanned "
                      f"({time.perf_counter() - started:.0f}s)")
    if ops and args.apply:
        written += jp.bulk_write(ops, ordered=False).modified_count

    print(f"\nscanned {seen:,} in {time.perf_counter() - started:.0f}s")
    print("improvements found:")
    for k, n in counts.most_common():
        print(f"  {k:34} {n:>8,}")
    if seniority_moves:
        print("\nseniority bands recovered:")
        for k, n in seniority_moves.most_common():
            print(f"  {k:14} {n:>8,}")
    print(f"\n{'written' if args.apply else 'would write'}: "
          f"{written if args.apply else touched:,} documents")
    if not args.apply:
        print("\ndry run - nothing was written. Re-run with --apply.")

    if args.json:
        Path(args.json).write_text(json.dumps({
            "target_version": TARGET, "scanned": seen, "written": written,
            "touched": touched, "stamped_version": stamp,
            "bodies_read": bodies_live,
            "improvements": dict(counts), "seniority": dict(seniority_moves),
            "applied": bool(args.apply),
        }, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
