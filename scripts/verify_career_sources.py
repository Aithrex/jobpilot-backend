#!/usr/bin/env python3
"""Prove — live — whose board each career source is.

DRY-RUN BY DEFAULT. Discovering an endpoint and APPLYING it are two different
decisions, and the bug this whole subsystem exists to fix is a system that
conflated them: `probe_software_company_boards.py --apply` wrote
`status: validated` the moment a guessed slug returned five jobs. Nothing here
writes without `--apply`, and `--apply` promotes a source only when the
evidence in `career_source_service.adjudicate_ownership` is present.

Two passes, cheapest first
--------------------------
`--mode endpoints` (default first pass)
    For every career source we already have, ask the ATS who owns the board
    and check the curated catalogs. One or two requests per source. This
    reaches Greenhouse, Lever, Ashby and SmartRecruiters — which is exactly
    the cohort where a guessed slug could previously become `validated`,
    because those four are the vendors `slug_probe` targets.

`--mode employers`
    For every employer with no covered source, verify the domain and walk
    from it to the board. Six to ten requests per employer, and the only path
    that reaches Workday, iCIMS and Oracle, whose APIs name no owner. This is
    also the pass that finds employers we have NO endpoint for at all —
    Google, Apple, Meta, Atlassian and the other 37 the audit listed.

Politeness is not optional here: one global rate limit, a per-host pace, and
robots.txt on every origin. A verification sweep that gets us blocked costs
more coverage than it buys.

Usage
-----
    PYTHONPATH=shared/python python scripts/verify_career_sources.py
    PYTHONPATH=shared/python python scripts/verify_career_sources.py --apply
    PYTHONPATH=shared/python python scripts/verify_career_sources.py \
        --mode employers --limit 500 --workers 12 --apply
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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


from services import career_source_ownership as cso      # noqa: E402
from services import career_source_service as css        # noqa: E402
from services import employer_registry as er             # noqa: E402
from services import official_career_registry as ocr     # noqa: E402
from services.ats_providers import registry as ats_registry   # noqa: E402
from services.ats_providers.base import Endpoint         # noqa: E402
from services.ats_providers.http import simple_context   # noqa: E402
from services.career_source_service import evidence      # noqa: E402


def _ssrf_guard():
    """`jd_fetcher.url_is_safe` returns `(ok, reason)` — unpack it or the
    check silently passes every unsafe URL."""
    from services.jd_fetcher import url_is_safe

    def guard(_vendor: str, url: str) -> str:
        ok, reason = url_is_safe(url)
        if not ok:
            raise ValueError(f"blocked: {reason or 'unsafe_url'}")
        return url
    return guard


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pass 1 — cheap: ask the provider, check the catalog
# ---------------------------------------------------------------------------
def verify_endpoint_source(row: Dict[str, Any], employer: Dict[str, Any],
                           ctx: Any,
                           fetcher: Optional[cso.Fetcher] = None) -> Dict[str, Any]:
    """One or two requests. Returns `{source_id, state, added, reason}`.

    `fetcher` is shared across the sweep so robots.txt is fetched once per
    origin and the per-host pace actually paces. Constructing one per source
    re-fetched robots.txt for every check, which is both slower and ruder.
    """
    vendor = row.get("ats_vendor") or ""
    tenant = row.get("tenant") or ""
    name = employer.get("display_name") or ""
    ep = Endpoint(vendor=vendor, tenant=tenant, display_name=name)

    added: List[Dict[str, Any]] = []
    sig = cso.provider_owner_signal(ep, ctx, display_name=name)
    if sig:
        added.append(sig)
    cur = cso.curated_row_signal(ep, display_name=name)
    if cur:
        added.append(cur)

    # The employer's canonical domain, if the board declares one. This is an
    # `employer_domain`-class signal read off the board, which is what makes
    # it independent of what the ATS API said.
    dom = employer.get("canonical_domain") or ""
    if dom and not any(s["kind"] == css.SIGNAL_DOMAIN_LINK for s in added):
        back = cso.backlink_signal(fetcher or cso.Fetcher(), ep,
                                   verified_domain=dom)
        if back:
            added.append(back)

    existing = [e for e in (row.get("ownership_evidence") or [])
                if isinstance(e, dict)]
    owned, code, why = css.adjudicate_ownership(existing + added)
    content_ok = None
    if row.get("content_validation_evidence"):
        content_ok = any(e.get("confirmed")
                         for e in row["content_validation_evidence"])
    state = css.next_state(owned=owned, content_ok=content_ok,
                           job_count=int(row.get("job_count_last") or 0),
                           failure_code="" if owned else code)
    return {"source_id": row["source_id"], "state": state, "added": added,
            "owned": owned, "reason": why, "vendor": vendor,
            "employer_id": employer.get("employer_id")}


# ---------------------------------------------------------------------------
# Pass 2 — decisive: verify the domain and walk to the board
# ---------------------------------------------------------------------------
def verify_employer(employer: Dict[str, Any], ctx: Any,
                    *, existing_endpoints: Sequence[Endpoint] = ()) -> List[Dict[str, Any]]:
    hit = ocr.lookup(domain=employer.get("canonical_domain"),
                     name=employer.get("display_name"))
    return cso.verify_employer_source(
        employer=employer,
        fetcher=cso.Fetcher(),
        ctx=ctx,
        candidate_endpoints=existing_endpoints,
        official_url=(hit or {}).get("career_landing_url") or "",
    )


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the verified states (default: report only)")
    ap.add_argument("--mode",
                    choices=("endpoints", "employers", "content", "both"),
                    default="both")
    ap.add_argument("--limit", type=int, default=4000)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--rate", type=float, default=10.0,
                    help="global requests/second ceiling across ALL hosts")
    ap.add_argument("--budget", type=float, default=3600.0)
    ap.add_argument("--employer-id", action="append", default=None,
                    help="verify exactly these employers (repeatable)")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    db = _db()
    ctx = simple_context(budget_seconds=args.budget, global_rate=args.rate,
                         guard=_ssrf_guard())
    stats: collections.Counter = collections.Counter()
    lock = threading.Lock()
    t0 = time.time()
    print(f"mode: {'APPLY' if args.apply else 'dry-run'}   pass: {args.mode}")

    employers_col = db[er.EMPLOYERS]
    sources_col = db[css.CAREER_SOURCES]

    def _employer(eid: str) -> Optional[Dict[str, Any]]:
        return employers_col.find_one({"employer_id": eid})

    # ---------------- pass 1 ----------------
    if args.mode in ("endpoints", "both"):
        q: Dict[str, Any] = {"state": {"$in": [css.STATE_CANDIDATE,
                                               css.STATE_CONTENT_VALIDATED,
                                               css.STATE_OWNERSHIP_VERIFIED]}}
        if args.employer_id:
            q["employer_id"] = {"$in": args.employer_id}
        rows = list(sources_col.find(q).limit(args.limit))
        print(f"\npass 1 - provider owner metadata: {len(rows):,} sources")
        emp_cache: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            eid = r.get("employer_id")
            if eid and eid not in emp_cache:
                emp_cache[eid] = _employer(eid) or {}

        shared_fetcher = cso.Fetcher()

        def _one(row):
            emp = emp_cache.get(row.get("employer_id")) or {}
            if not emp:
                return None
            try:
                return verify_endpoint_source(row, emp, ctx, shared_fetcher)
            except Exception as exc:
                return {"source_id": row["source_id"], "error": type(exc).__name__}

        results: List[Dict[str, Any]] = []
        by_vendor: Dict[str, collections.Counter] = collections.defaultdict(
            collections.Counter)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for fut in as_completed([pool.submit(_one, r) for r in rows]):
                got = fut.result()
                if not got:
                    with lock:
                        stats["pass1_no_employer"] += 1
                    continue
                with lock:
                    if got.get("error"):
                        stats[f"pass1_error:{got['error']}"] += 1
                    else:
                        stats[f"pass1_{got['state']}"] += 1
                        vendor = got.get("vendor") or "?"
                        if got["owned"]:
                            stats["pass1_ownership_proven"] += 1
                            by_vendor[vendor]["proven"] += 1
                        by_vendor[vendor]["checked"] += 1
                        if not got["added"]:
                            # Nothing to adjudicate. Distinguishing "we asked
                            # and got nothing" from "we never asked" is the
                            # difference between an unprovable vendor and a
                            # sweep that quietly skipped it.
                            by_vendor[vendor]["no_signal"] += 1
                        results.append(got)
                        if len(results) % 200 == 0:
                            print(f"    {len(results):,} checked "
                                  f"({stats['pass1_ownership_proven']:,} proven) "
                                  f"{time.time() - t0:.0f}s")

        print(f"\n  {'vendor':<18}{'checked':>9}{'proven':>9}{'no signal':>11}")
        for vendor in sorted(by_vendor, key=lambda v: -by_vendor[v]["checked"]):
            c = by_vendor[vendor]
            print(f"  {vendor:<18}{c['checked']:>9,}{c['proven']:>9,}"
                  f"{c['no_signal']:>11,}")

        if args.apply:
            # One `update_one` per source is a round trip per source against a
            # hosted cluster; a 2,400-source sweep spent longer writing than
            # verifying. Same update document, one round trip per 500.
            from pymongo import UpdateOne
            ops = []
            for got in results:
                ops.append(UpdateOne({"source_id": got["source_id"]}, {
                    "$set": {"state": got["state"],
                             "ownership_verified": bool(got["owned"]),
                             "ownership_reason": got["reason"][:300],
                             "last_checked_at": _now(),
                             "last_verified_at": _now() if got["state"] in
                             css.COVERED_STATES else None,
                             "next_check_at": css.next_check_after(got["state"]),
                             "failure_code": css.FAIL_NONE if got["owned"]
                             else css.FAIL_OWNER_UNPROVEN,
                             "updated_at": _now()},
                    "$push": {"ownership_evidence": {"$each": got["added"],
                                                      "$slice": -20}},
                }))
            for i in range(0, len(ops), 500):
                sources_col.bulk_write(ops[i:i + 500], ordered=False)
            print(f"    wrote {len(results):,} sources")

    # ---------------- pass 2 ----------------
    if args.mode in ("employers", "both"):
        covered = {r["employer_id"] for r in sources_col.find(
            {"state": {"$in": list(css.COVERED_STATES)}}, {"employer_id": 1})}
        # Employers worth the expensive pass: those we hold curated evidence
        # for, and those we already have a board for. An employer with NO
        # domain is deliberately included when it has a career source — the
        # board can supply a candidate domain, which is the only route to the
        # decisive path for an employer that exists solely as an ATS display
        # name, and that is most of the uncovered denominator.
        with_sources = {r["employer_id"] for r in sources_col.find(
            {}, {"employer_id": 1})}
        eq: Dict[str, Any] = {
            "operating_status": er.STATUS_ACTIVE,
            "$or": [
                {"canonical_domain": {"$ne": None}},
                {"employer_id": {"$in": sorted(with_sources)}},
            ],
        }
        if covered:
            eq["employer_id"] = {"$nin": sorted(covered)}
        if args.employer_id:
            eq = {"employer_id": {"$in": args.employer_id}}
        emps = list(employers_col.find(eq).sort("identity_confidence", -1)
                    .limit(args.limit))
        print(f"\npass 2 - verify domain and walk to the board: {len(emps):,} employers")

        endpoints_by_emp: Dict[str, List[Endpoint]] = collections.defaultdict(list)
        for r in sources_col.find({"employer_id": {"$in": [e["employer_id"] for e in emps]}},
                                  {"employer_id": 1, "ats_vendor": 1, "tenant": 1}):
            if r.get("ats_vendor") and r.get("tenant"):
                endpoints_by_emp[r["employer_id"]].append(
                    Endpoint(vendor=r["ats_vendor"], tenant=r["tenant"]))

        produced: List[Dict[str, Any]] = []

        def _one_emp(emp):
            try:
                return verify_employer(
                    emp, ctx,
                    existing_endpoints=endpoints_by_emp.get(emp["employer_id"], []))
            except Exception as exc:
                return [{"__error__": type(exc).__name__,
                         "employer_id": emp.get("employer_id")}]

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for fut in as_completed([pool.submit(_one_emp, e) for e in emps]):
                recs = fut.result() or []
                with lock:
                    done += 1
                    for rec in recs:
                        if rec.get("__error__"):
                            stats[f"pass2_error:{rec['__error__']}"] += 1
                            continue
                        stats[f"pass2_{rec['state']}"] += 1
                        if rec.get("ownership_verified"):
                            stats["pass2_ownership_proven"] += 1
                        produced.append(rec)
                    if done % 100 == 0:
                        print(f"    {done:,}/{len(emps):,} employers "
                              f"({stats['pass2_ownership_proven']:,} proven) "
                              f"{time.time() - t0:.0f}s")

        if args.apply:
            # MERGE, do not overwrite. A pass-2 record carries ownership
            # evidence and no content evidence, and `state` is derived from
            # both — writing it blind would demote a source that pass 1 had
            # already taken to `validated` back down to `ownership_verified`,
            # i.e. lose coverage by proving MORE about it.
            existing = {r["source_id"]: r for r in sources_col.find(
                {"source_id": {"$in": [p["source_id"] for p in produced]}})}
            merged = []
            for rec in produced:
                prior = existing.get(rec["source_id"])
                if prior:
                    prior_content = [e for e in
                                     (prior.get("content_validation_evidence") or [])
                                     if isinstance(e, dict)]
                    if prior_content and not rec.get("content_validation_evidence"):
                        rec["content_validation_evidence"] = prior_content
                    content_ok = (any(e.get("confirmed") for e in prior_content)
                                  if prior_content else None)
                    rec["job_count_last"] = (rec.get("job_count_last")
                                             or int(prior.get("job_count_last") or 0))
                    rec["state"] = css.next_state(
                        owned=bool(rec.get("ownership_verified")),
                        content_ok=content_ok,
                        job_count=int(rec.get("job_count_last") or 0))
                    if rec["state"] in css.COVERED_STATES:
                        rec["last_verified_at"] = _now()
                    rec["next_check_at"] = css.next_check_after(rec["state"])
                merged.append(rec)
            stats_write = css.bulk_upsert_sources(merged)
            promoted = sum(1 for r in merged if r["state"] in css.COVERED_STATES)
            print(f"    wrote {stats_write['seen']:,} sources "
                  f"({stats_write['written']:,} inserted or changed; "
                  f"{promoted:,} now covered)")

    # ---------------- pass 3: content ----------------
    # Ownership proven, content never checked. Pass 2 walks to the endpoint but
    # does not read it, so a newly-proven source sits in `ownership_verified` -
    # which is NOT coverage - until something fetches postings from it. The
    # crawler gets there eventually; this closes the loop now, and it is the
    # step that turns proven ownership into a coverage number.
    if args.mode in ("content", "both"):
        q3: Dict[str, Any] = {"state": {"$in": [css.STATE_OWNERSHIP_VERIFIED,
                                                css.STATE_CONTENT_VALIDATED]},
                              "ats_vendor": {"$nin": ["", None]}}
        if args.employer_id:
            q3["employer_id"] = {"$in": args.employer_id}
        rows3 = list(sources_col.find(q3).limit(args.limit))
        print(f"\npass 3 - content validation: {len(rows3):,} sources")

        def _content(row):
            provider = ats_registry.get(row.get("ats_vendor") or "")
            if provider is None:
                return {"source_id": row["source_id"], "unsupported": True}
            ep = Endpoint(vendor=row["ats_vendor"], tenant=row.get("tenant") or "")
            try:
                result = provider.validate(ep, ctx)
            except Exception as exc:
                return {"source_id": row["source_id"], "error": type(exc).__name__}
            return {"source_id": row["source_id"], "status": result.status,
                    "count": int(result.job_count or 0),
                    "sample": str(result.sample_title or "")[:120],
                    "detail": str(result.detail or "")[:300],
                    "vendor": row.get("ats_vendor")}

        got3: List[Dict[str, Any]] = []
        done3 = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for fut in as_completed([pool.submit(_content, r) for r in rows3]):
                res = fut.result()
                with lock:
                    done3 += 1
                    if res.get("unsupported"):
                        stats["pass3_unsupported_provider"] += 1
                    elif res.get("error"):
                        stats[f"pass3_error:{res['error']}"] += 1
                    else:
                        stats[f"pass3_{res['status']}"] += 1
                        got3.append(res)
                    if done3 % 100 == 0:
                        print(f"    {done3:,}/{len(rows3):,} checked "
                              f"{time.time() - t0:.0f}s")

        if args.apply:
            promoted = 0
            for res in got3:
                if res["status"] == "validated":
                    state = css.record_success(res["source_id"],
                                               job_count=res["count"],
                                               sample_title=res["sample"])
                elif res["status"] == "empty":
                    state = css.record_success(res["source_id"], job_count=0)
                elif res["status"] == "blocked":
                    state = css.record_failure(res["source_id"],
                                               failure_code=css.FAIL_HTTP_403,
                                               detail=res["detail"])
                else:
                    state = css.record_failure(res["source_id"],
                                               failure_code=css.FAIL_PARSE,
                                               detail=res["detail"])
                if state in css.COVERED_STATES:
                    promoted += 1
            print(f"    wrote {len(got3):,} sources ({promoted:,} now covered)")

    print("\n--- results ---")
    for k, v in sorted(stats.items()):
        print(f"  {k:36s} {v:>7,}")

    if args.apply:
        print("\n--- career source coverage now ---")
        for k, v in sorted(css.counts()["by_state"].items()):
            print(f"  {k:22s} {v:>7,}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"generated_at": _now().isoformat(), "mode": args.mode,
             "applied": args.apply, "stats": dict(stats),
             "elapsed_s": round(time.time() - t0, 1)},
            indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
