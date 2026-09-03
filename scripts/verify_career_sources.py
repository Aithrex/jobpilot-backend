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

`--mode backlink`
    The cheap middle, and on live data the highest-yield pass there is. For
    sources holding exactly ONE confirmed independent evidence class, derive
    the employer's domain FROM THE BOARD and verify it names them. Three
    requests per SOURCE rather than ten per employer, and only for rows that
    are already one class short.

    Measured: 1,465 sources are `content_validated` — we have READ their
    postings — and only 18 of their employers carry a verified domain. 598
    hold the ATS provider's owner metadata and 144 hold a curated catalog row.
    Every one of those is one `employer_domain` signal from `validated`, and
    that signal was UNOBTAINABLE for iCIMS, BambooHR, Cornerstone, Taleo,
    Recruitee, Workday and Oracle until `AtsProvider.board_url` replaced a
    board URL built by `hosts[0].lstrip(".")` — which turned the SUFFIX
    `.icims.com` into `https://icims.com/<tenant>`, a page that is nobody's
    board.

`--mode browser`
    The fifth pass. For employers with a verified domain whose careers page
    is a JavaScript shell (the board link arrives over XHR and appears
    nowhere in the raw HTML), render the page with Playwright via
    `Fetcher.render` — same SSRF / robots / pace gates as every other
    fetch — and fingerprint the RENDERED DOM plus the network requests.
    Ownership still goes through `adjudicate_ownership`; this pass only
    supplies URLs the HTML walk could not see. A bot challenge stays
    `blocked`. Default concurrency is 1: one Chromium, no warm fleet.

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
from datetime import datetime, timedelta, timezone
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
from services.ats_providers.base import ChallengeError, Endpoint  # noqa: E402
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
                    *, existing_endpoints: Sequence[Endpoint] = (),
                    use_browser: bool = False,
                    fetcher: Optional[cso.Fetcher] = None) -> List[Dict[str, Any]]:
    hit = ocr.lookup(domain=employer.get("canonical_domain"),
                     name=employer.get("display_name"))
    return cso.verify_employer_source(
        employer=employer,
        fetcher=fetcher or cso.Fetcher(enable_browser=use_browser),
        ctx=ctx,
        candidate_endpoints=existing_endpoints,
        official_url=(hit or {}).get("career_landing_url") or "",
        use_browser=use_browser,
    )


# ---------------------------------------------------------------------------
# Pass 4 — the board's own backlink, for sources ONE signal short
# ---------------------------------------------------------------------------
def confirmed_classes(row: Dict[str, Any]) -> set:
    """The distinct, confirmed, NON-HEURISTIC evidence classes on a source."""
    out = set()
    for ev in (row.get("ownership_evidence") or ()):
        if not isinstance(ev, dict) or not ev.get("confirmed"):
            continue
        cls = str(ev.get("signal_class") or "")
        if cls and cls != css.CLASS_HEURISTIC:
            out.add(cls)
    return out


def hydrate_endpoint(row: Dict[str, Any], endpoints_by_member: Dict[str, Any]):
    """Build an `Endpoint` that knows its real HOST, not just its tenant.

    Workday and Oracle boards are not addressable from a tenant alone — a
    Workday board needs the pod (`wd108`) and an Oracle pod hostname
    (`eeho.fa.us2.oraclecloud.com`) is not derivable from a company name at
    all. `company_ats_endpoints` already stores both, keyed by the same
    `member_key` the career source carries, so the join costs one query for
    the whole pass and turns two of the four biggest vendors from
    unreachable into reachable.

    `company_ats_endpoints` is the first source and the career source's own
    `canonical_endpoint` is the second. The fallback matters: a source
    discovered by the ownership walk or by
    `verify_official_career_urls.py --register` has a canonical endpoint and
    no `company_ats_endpoints` row at all, because nothing has crawled it yet
    — which is precisely the population this pass exists to reach.
    """
    from services.ats_providers.base import Endpoint
    doc = endpoints_by_member.get(str(row.get("member_key") or "")) or {}
    if not doc:
        url = str(row.get("canonical_endpoint") or "").strip()
        if url:
            try:
                from services.ats_providers import registry as ats_registry
                for endpoint in ats_registry.endpoints_from_urls([url]):
                    if endpoint.vendor == (row.get("ats_vendor") or ""):
                        return endpoint
            except Exception:      # pragma: no cover - hydration is best-effort
                pass
    return Endpoint(
        vendor=row.get("ats_vendor") or doc.get("vendor") or "",
        tenant=row.get("tenant") or doc.get("tenant") or "",
        site=str(doc.get("site") or ""),
        host=str(doc.get("host") or ""),
        extra=dict(doc.get("extra") or {}),
    )


def verify_backlink_source(row: Dict[str, Any], employer: Dict[str, Any],
                           ctx: Any, fetcher: cso.Fetcher,
                           endpoint: Any = None) -> Dict[str, Any]:
    """Derive the employer's domain FROM the board, then verify it.

    Why this is a pass of its own
    -----------------------------
    Measured on live `career_sources`: 1,465 sources are `content_validated`
    — we have READ their postings — and only 18 of their employers carry a
    verified domain. 598 of them hold exactly one confirmed evidence class
    (the ATS naming the owner) and 144 hold exactly one (a curated catalog
    row). Every one of those is ONE independent class away from `validated`,
    and the class they are missing is `employer_domain`.

    Pass 1 cannot supply it: `backlink_signal` needs a verified domain and
    these employers have none. Pass 2 can, through
    `candidate_domains_from_board`, but pass 2 costs six to ten requests per
    EMPLOYER and is ordered by technology yield, so an employer outside the
    denominator waits behind 1,204 that are in it.

    This is the cheap middle: three requests per SOURCE, and only for sources
    that are already one class short.

    Why it is sound
    ---------------
    Two independent parties, which is rule 4b exactly:

      the BOARD links to `example.com`   (read off the board)
      `example.com` NAMES the employer   (read off their own site, in visible
                                          text — never in markup)

    A board link on its own is one page's assertion about itself, which is why
    the candidate domain is handed to `verify_domain` rather than believed.
    """
    from services.ats_providers.base import Endpoint

    name = employer.get("display_name") or ""
    ep = endpoint if endpoint is not None else Endpoint(
        vendor=row.get("ats_vendor") or "", tenant=row.get("tenant") or "")
    ep.display_name = name
    added: List[Dict[str, Any]] = []
    domain = ""
    for candidate in cso.candidate_domains_from_board(fetcher, ep, limit=3):
        ok, url, ev = cso.verify_domain(fetcher, display_name=name,
                                        domain=candidate)
        if ok:
            domain = er.normalize_domain(candidate)
            added.extend(ev)
            added.append(evidence(
                css.SIGNAL_BOARD_BACKLINK, source=url,
                value=f"the board links to {domain}, and that site names {name}",
                confirmed=True))
            break

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
            "owned": owned, "reason": why, "vendor": row.get("ats_vendor"),
            "verified_domain": domain,
            "employer_id": employer.get("employer_id")}


def _vendor_key(value: Any) -> str:
    """`oracle-orc` on a posting, `oracle_orc` in the registry."""
    return str(value or "").strip().lower().replace("-", "_")


def _boards_with_fresh_postings(db, *, hours: int = 48) -> Dict[Tuple[str, str], int]:
    """`(vendor, tenant) -> live posting count` for boards the crawler read
    recently. This is the ground truth a probe result has to argue with."""
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))
    out: Dict[Tuple[str, str], int] = {}
    try:
        cur = db.job_postings.aggregate([
            {"$match": {"status": "active", "last_seen_at": {"$gte": since}}},
            {"$group": {"_id": {"a": "$ats", "t": "$tenant"}, "n": {"$sum": 1}}},
        ])
        for r in cur:
            ten = str((r["_id"] or {}).get("t") or "").strip().lower()
            if ten:
                out[(_vendor_key((r["_id"] or {}).get("a")), ten)] = int(r["n"])
    except Exception as exc:      # pragma: no cover - a missing index must not
        print(f"  live-board check unavailable ({type(exc).__name__}); "
              f"probe results will be applied as-is")
    return out


def _tech_yield_order(db, employers_col, eq: Dict[str, Any],
                      limit: int) -> List[Dict[str, Any]]:
    """Uncovered TECHNOLOGY employers, most core-tech postings first.

    `eq` already excludes covered employers and the ones with neither a domain
    nor a source, so this only reorders and narrows — it never verifies an
    employer the confidence order would have refused to.

    If the report cannot be built (it reads `job_postings`, which a thin local
    database may not have), this falls back to confidence order rather than
    verifying nothing. A slower sweep beats a stalled one.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import employer_coverage_report as report_mod
        built = report_mod.build(db, mix_days=30)
    except Exception as exc:
        print(f"  tech-yield order unavailable ({type(exc).__name__}); "
              f"falling back to confidence order")
        return list(employers_col.find(eq)
                    .sort("identity_confidence", -1).limit(limit))

    yield_by_id = {r["employer_id"]: int((r.get("_mix") or {}).get("core", 0))
                   for r in built["employers"]}
    eligible = {d["employer_id"]: d for d in employers_col.find(eq)}

    # Employers a HUMAN put in the official careers registry come first, before
    # any yield ordering. Their measured yield is 0 precisely BECAUSE we cannot
    # reach their board — Google, Apple, Meta, Atlassian, Bloomberg, Riot,
    # 65 of the registry's 94 rows produce no live postings — so ordering
    # purely by yield sorted the ones we most need to discover to the very
    # bottom and they were never swept.
    wanted_domains, wanted_names = set(), set()
    try:
        for row in ocr.OFFICIAL_CAREER_URLS:
            dom = er.normalize_domain(row.get("domain"))
            if dom:
                wanted_domains.add(dom)
            nk = er.name_key(row.get("display_name"))
            if nk:
                wanted_names.add(nk)
    except Exception:
        pass

    def _is_wanted(d: Dict[str, Any]) -> bool:
        if er.normalize_domain(d.get("canonical_domain")) in wanted_domains:
            return True
        if any(er.normalize_domain(v) in wanted_domains
               for v in (d.get("verified_domains") or [])):
            return True
        return er.name_key(d.get("display_name")) in wanted_names

    ranked = sorted(
        (d for eid, d in eligible.items()
         if eid in yield_by_id or _is_wanted(d)),
        key=lambda d: (0 if _is_wanted(d) else 1,
                       -yield_by_id.get(d["employer_id"], 0),
                       -float(d.get("identity_confidence") or 0.0)),
    )
    n_wanted = sum(1 for d in ranked[:limit] if _is_wanted(d))
    picked = ranked[:limit]
    if len(picked) < limit:
        # The technology denominator is smaller than the budget: spend the
        # remainder on everything else, still confidence-ordered, rather than
        # returning early with the sweep half used.
        chosen = {d["employer_id"] for d in picked}
        rest = sorted((d for eid, d in eligible.items() if eid not in chosen),
                      key=lambda d: -float(d.get("identity_confidence") or 0.0))
        picked += rest[:limit - len(picked)]
    top = sum(yield_by_id.get(d["employer_id"], 0) for d in picked)
    print(f"  tech-yield order: {len(ranked):,} uncovered technology employers, "
          f"{top:,} core-tech postings in this sweep "
          f"({n_wanted:,} from the official careers registry, taken first)")
    return picked


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the verified states (default: report only)")
    ap.add_argument("--mode",
                    choices=("endpoints", "employers", "content", "backlink",
                             "browser", "both"),
                    default="both")
    ap.add_argument("--limit", type=int, default=4000)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--rate", type=float, default=10.0,
                    help="global requests/second ceiling across ALL hosts")
    ap.add_argument("--budget", type=float, default=3600.0)
    ap.add_argument("--employer-id", action="append", default=None,
                    help="verify exactly these employers (repeatable)")
    ap.add_argument("--order", choices=("tech-yield", "confidence"),
                    default="tech-yield",
                    help=("pass-2 ordering. `tech-yield` (default) walks the "
                          "TECHNOLOGY denominator first, most core-tech "
                          "postings first, because that is the number the "
                          "coverage report is scored on and a sweep ordered "
                          "by identity confidence spends most of its budget "
                          "on employers outside it. `confidence` is the old "
                          "behaviour and is how everything else eventually "
                          "gets swept."))
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
        if args.order == "tech-yield" and not args.employer_id:
            # The report already computes the technology denominator AND each
            # employer's live core-tech posting count, so ordering by it costs
            # one build rather than a second definition of "technology
            # employer" that could drift from the one we are scored on.
            emps = _tech_yield_order(db, employers_col, eq, args.limit)
        else:
            emps = list(employers_col.find(eq).sort("identity_confidence", -1)
                        .limit(args.limit))
        print(f"\npass 2 - verify domain and walk to the board: {len(emps):,} employers"
              f" ({args.order} order)")

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

    # ---------------- pass 4: backlink ----------------
    # Sources ONE independent evidence class short, where the missing class is
    # `employer_domain` and the board itself can supply the candidate. Three
    # requests each, and only for sources that are already close.
    if args.mode == "backlink":
        # `$elemMatch` on the evidence array does the "already has ONE
        # confirmed, non-heuristic class" half in Mongo. Pulling a fixed
        # multiple of `--limit` and filtering in Python instead returned an
        # EMPTY queue against a collection that holds 742 qualifying rows —
        # the qualifying ones are a small fraction and are not at the front.
        q4: Dict[str, Any] = {
            "ats_vendor": {"$nin": ["", None]},
            "tenant": {"$nin": ["", None]},
            "ownership_verified": {"$ne": True},
            "state": {"$in": [css.STATE_CONTENT_VALIDATED, css.STATE_CANDIDATE]},
            "ownership_evidence": {"$elemMatch": {
                "confirmed": True,
                "signal_class": {"$in": [css.CLASS_ATS_PROVIDER,
                                         css.CLASS_CURATED,
                                         css.CLASS_EMPLOYER_DOMAIN]}}},
        }
        if args.employer_id:
            q4["employer_id"] = {"$in": args.employer_id}
        # `content_validated` first: those already have the content half, so
        # proving ownership takes them straight to `validated` rather than to
        # `ownership_verified`, which is not coverage.
        rows4 = [r for r in sources_col.find(q4).sort("state", 1)
                 if len(confirmed_classes(r)) == 1][:args.limit]
        print(f"\npass 4 - board backlink: {len(rows4):,} sources one class short")
        emp_cache4: Dict[str, Dict[str, Any]] = {}
        for r in rows4:
            eid = r.get("employer_id")
            if eid and eid not in emp_cache4:
                emp_cache4[eid] = _employer(eid) or {}

        # ONE query for every endpoint row these sources point at, so a
        # Workday pod and an Oracle pod hostname are available without a
        # lookup per source.
        member_keys = sorted({str(r.get("member_key") or "") for r in rows4
                              if r.get("member_key")})
        endpoints_by_member = {
            str(d.get("member_key")): d
            for d in db.company_ats_endpoints.find(
                {"member_key": {"$in": member_keys}},
                {"_id": 0, "member_key": 1, "vendor": 1, "tenant": 1,
                 "host": 1, "site": 1, "extra": 1})} if member_keys else {}
        print(f"    hydrated {len(endpoints_by_member):,} endpoint host(s) "
              f"from company_ats_endpoints")

        shared4 = cso.Fetcher(min_interval=0.5)

        def _one4(row):
            emp = emp_cache4.get(row.get("employer_id")) or {}
            if not emp:
                return None
            if ctx is not None and getattr(ctx, "should_stop", None) \
                    and ctx.should_stop():
                return None
            try:
                return verify_backlink_source(
                    row, emp, ctx, shared4,
                    endpoint=hydrate_endpoint(row, endpoints_by_member))
            except Exception as exc:
                return {"source_id": row["source_id"], "error": type(exc).__name__}

        got4: List[Dict[str, Any]] = []
        done4 = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for fut in as_completed([pool.submit(_one4, r) for r in rows4]):
                res = fut.result()
                with lock:
                    done4 += 1
                    if not res:
                        stats["pass4_skipped"] += 1
                    elif res.get("error"):
                        stats[f"pass4_error:{res['error']}"] += 1
                    else:
                        stats[f"pass4_{res['state']}"] += 1
                        if res["owned"]:
                            stats["pass4_ownership_proven"] += 1
                        got4.append(res)
                    if done4 % 100 == 0:
                        print(f"    {done4:,}/{len(rows4):,} checked "
                              f"({stats['pass4_ownership_proven']:,} proven) "
                              f"{time.time() - t0:.0f}s")

        if args.apply and got4:
            from pymongo import UpdateOne
            ops = []
            for res in got4:
                update: Dict[str, Any] = {
                    "$set": {"state": res["state"],
                             "ownership_verified": bool(res["owned"]),
                             "ownership_reason": res["reason"][:300],
                             "last_checked_at": _now(),
                             "last_verified_at": _now() if res["state"] in
                             css.COVERED_STATES else None,
                             "next_check_at": css.next_check_after(res["state"]),
                             "failure_code": css.FAIL_NONE if res["owned"]
                             else css.FAIL_OWNER_UNPROVEN,
                             "updated_at": _now()},
                }
                if res.get("verified_domain"):
                    update["$set"]["verified_domain"] = res["verified_domain"]
                if res["added"]:
                    update["$push"] = {"ownership_evidence":
                                       {"$each": res["added"], "$slice": -20}}
                ops.append(UpdateOne({"source_id": res["source_id"]}, update))
            for i in range(0, len(ops), 500):
                sources_col.bulk_write(ops[i:i + 500], ordered=False)
            print(f"    wrote {len(ops):,} sources "
                  f"({sum(1 for r in got4 if r['state'] in css.COVERED_STATES):,} "
                  f"now covered)")

            # The domain we just VERIFIED belongs on the employer too. Added
            # to `verified_domains` (additive) and NEVER used to re-key the
            # employer: `employer_id` is derived from the domain, so a rekey
            # would change the id and orphan every `canonical_employer_id`
            # already written onto a posting. What this buys is that
            # `resolve_employer(domain=...)` — which reads `verified_domains`
            # FIRST — can now find these employers, and the next pass 2 starts
            # from a verified domain instead of from nothing.
            #
            # Promotion to a domain-derived id is `employer_registry.
            # rekey_to_domain`, which keeps the old row as `merged` with a
            # parent pointer. That is a deliberate, separate decision and not
            # something a sweep should take.
            domain_ops = []
            for res in got4:
                dom = res.get("verified_domain")
                if not dom or not res.get("employer_id"):
                    continue
                domain_ops.append(UpdateOne(
                    {"employer_id": res["employer_id"]},
                    {"$addToSet": {"verified_domains": dom},
                     "$set": {"last_verified_at": _now(), "updated_at": _now()}}))
            if domain_ops:
                for i in range(0, len(domain_ops), 500):
                    employers_col.bulk_write(domain_ops[i:i + 500], ordered=False)
                print(f"    recorded {len(domain_ops):,} verified employer "
                      f"domain(s) (no re-key - see the comment)")

    # ---------------- pass 5: browser-rendered discovery ----------------
    # JS-shell careers pages: the board link arrives over XHR and is absent
    # from the raw HTML pass 2 reads. Render via Fetcher.render (same SSRF /
    # robots / pace gates), fingerprint the DOM + network, feed the SAME
    # adjudicate_ownership path. A bot challenge stays `blocked`.
    if args.mode == "browser":
        # Playwright sync + one shared Chromium is not thread-safe. Cap hard.
        browser_workers = max(1, min(int(args.workers), 2))
        if browser_workers != args.workers:
            print(f"  browser mode: workers clamped {args.workers} -> "
                  f"{browser_workers} (one Chromium per worker)")

        covered = {r["employer_id"] for r in sources_col.find(
            {"state": {"$in": list(css.COVERED_STATES)}}, {"employer_id": 1})}
        # Prefer the work-queue buckets the numbers point at: Tier-A in
        # source_no_endpoint first, then endpoint_unproven (ownership proof
        # via a rendered domain link), then the rest of source_no_endpoint.
        try:
            import coverage_work_queue as cwq
            built_q = cwq.build(db)
            queue_rows = [r for r in built_q["queue"]
                          if r["bucket"] in (cwq.BUCKET_SOURCE_NO_ENDPOINT,
                                             cwq.BUCKET_ENDPOINT_UNPROVEN)]
        except Exception as exc:
            print(f"  work queue unavailable ({type(exc).__name__}); "
                  f"falling back to uncovered employers with a domain")
            queue_rows = []

        if args.employer_id:
            emps = list(employers_col.find(
                {"employer_id": {"$in": args.employer_id}}).limit(args.limit))
        elif queue_rows:
            ids = [r["employer_id"] for r in queue_rows][:args.limit]
            by_id = {e["employer_id"]: e for e in employers_col.find(
                {"employer_id": {"$in": ids}})}
            emps = [by_id[i] for i in ids if i in by_id]
        else:
            eq_b: Dict[str, Any] = {
                "operating_status": er.STATUS_ACTIVE,
                "canonical_domain": {"$nin": [None, ""]},
            }
            if covered:
                eq_b["employer_id"] = {"$nin": sorted(covered)}
            emps = list(employers_col.find(eq_b).sort("identity_confidence", -1)
                        .limit(args.limit))

        print(f"\npass 5 - browser-rendered discovery: {len(emps):,} employers "
              f"(workers={browser_workers})")

        endpoints_by_emp_b: Dict[str, List[Endpoint]] = collections.defaultdict(list)
        for r in sources_col.find(
                {"employer_id": {"$in": [e["employer_id"] for e in emps]}},
                {"employer_id": 1, "ats_vendor": 1, "tenant": 1}):
            if r.get("ats_vendor") and r.get("tenant"):
                endpoints_by_emp_b[r["employer_id"]].append(
                    Endpoint(vendor=r["ats_vendor"], tenant=r["tenant"]))

        # One Fetcher (and therefore one Chromium) per worker. Sharing a
        # sync Playwright browser across threads is undefined behaviour.
        thread_local = threading.local()
        fetchers_lock = threading.Lock()
        all_fetchers: List[cso.Fetcher] = []

        def _browser_fetcher() -> cso.Fetcher:
            f = getattr(thread_local, "fetcher", None)
            if f is None:
                f = cso.Fetcher(enable_browser=True, min_interval=0.6)
                thread_local.fetcher = f
                with fetchers_lock:
                    all_fetchers.append(f)
            return f

        produced_b: List[Dict[str, Any]] = []

        def _one_browser(emp):
            try:
                return verify_employer(
                    emp, ctx,
                    existing_endpoints=endpoints_by_emp_b.get(
                        emp["employer_id"], []),
                    use_browser=True,
                    fetcher=_browser_fetcher())
            except Exception as exc:
                return [{"__error__": type(exc).__name__,
                         "employer_id": emp.get("employer_id")}]

        done_b = 0
        try:
            with ThreadPoolExecutor(max_workers=browser_workers) as pool:
                for fut in as_completed(
                        [pool.submit(_one_browser, e) for e in emps]):
                    recs = fut.result() or []
                    with lock:
                        done_b += 1
                        for rec in recs:
                            if rec.get("__error__"):
                                stats[f"pass5_error:{rec['__error__']}"] += 1
                                continue
                            stats[f"pass5_{rec['state']}"] += 1
                            if rec.get("ownership_verified"):
                                stats["pass5_ownership_proven"] += 1
                            if rec.get("ats_vendor") and rec.get("tenant"):
                                stats["pass5_endpoint_found"] += 1
                            if rec.get("state") == css.STATE_BLOCKED:
                                stats["pass5_blocked"] += 1
                            produced_b.append(rec)
                        if done_b % 10 == 0 or done_b == len(emps):
                            print(f"    {done_b:,}/{len(emps):,} employers "
                                  f"({stats['pass5_ownership_proven']:,} proven, "
                                  f"{stats['pass5_endpoint_found']:,} endpoints, "
                                  f"{stats['pass5_blocked']:,} blocked) "
                                  f"{time.time() - t0:.0f}s")
        finally:
            # Do not leave Chromium warm after the sweep.
            for f in all_fetchers:
                try:
                    f.close()
                except Exception:
                    pass

        if args.apply and produced_b:
            existing_b = {r["source_id"]: r for r in sources_col.find(
                {"source_id": {"$in": [p["source_id"] for p in produced_b]}})}
            merged_b = []
            for rec in produced_b:
                prior = existing_b.get(rec["source_id"])
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
                    # Never overwrite a content verdict with a discovery state.
                    if prior.get("state") in (css.STATE_VALIDATED, css.STATE_EMPTY,
                                              css.STATE_CONTENT_VALIDATED):
                        if not (prior.get("state") == css.STATE_CONTENT_VALIDATED
                                and rec.get("ownership_verified")):
                            rec["state"] = prior["state"]
                        else:
                            rec["state"] = css.next_state(
                                owned=True, content_ok=True,
                                job_count=int(rec.get("job_count_last") or 0))
                    else:
                        rec["state"] = css.next_state(
                            owned=bool(rec.get("ownership_verified")),
                            content_ok=content_ok,
                            job_count=int(rec.get("job_count_last") or 0),
                            failure_code=rec.get("failure_code") or "")
                    if rec["state"] in css.COVERED_STATES:
                        rec["last_verified_at"] = _now()
                    rec["next_check_at"] = css.next_check_after(rec["state"])
                merged_b.append(rec)
            stats_write = css.bulk_upsert_sources(merged_b)
            promoted = sum(1 for r in merged_b if r["state"] in css.COVERED_STATES)
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
        # `ownership_verified` FIRST, and not by alphabetical luck.
        #
        # Both states need a content check, but only one of them can change
        # the coverage number: `ownership_verified` + content = `validated`,
        # while `content_validated` + content = `content_validated` again,
        # because what it lacks is ownership and this pass cannot supply it.
        # Unordered, a 400-row budget spent 396 of its requests on the second
        # kind — there are 1,197 of those and 93 of the first — and moved
        # coverage by zero.
        # TWO queries, not one sorted afterwards: a `.limit()` truncates
        # before any ordering this side could apply, which is how a 400-row
        # budget drew 396 rows of the kind that cannot move.
        rows3 = list(sources_col.find(
            {**q3, "state": css.STATE_OWNERSHIP_VERIFIED}).limit(args.limit))
        movable = len(rows3)
        if movable < args.limit:
            rows3 += list(sources_col.find(
                {**q3, "state": css.STATE_CONTENT_VALIDATED}
            ).limit(args.limit - movable))
        print(f"\npass 3 - content validation: {len(rows3):,} sources "
              f"({movable:,} can reach `validated` on this pass; the rest are "
              f"`content_validated` and need OWNERSHIP, which this pass "
              f"cannot supply)")

        # The SAME hydration pass 4 does, for the same reason. Rebuilding the
        # endpoint as `(vendor, tenant)` throws away the pod and the site
        # path, and a Workday board is not addressable without them: Adobe is
        # `adobe.wd5.myworkdayjobs.com/external_experienced`, Accenture is
        # `wd103`, Snap is `wd1.myworkdaysite.com/recruiting/snapchat/snap`.
        # Every one of them came back `unparseable_response` — which reads as
        # "their board is broken" and meant "we asked the wrong URL". The
        # failure was concentrated in exactly the three vendors that need a
        # host (Workday, iCIMS, Oracle) and invisible in the ones that do not.
        member_keys3 = sorted({str(r.get("member_key") or "") for r in rows3
                               if r.get("member_key")})
        endpoints3 = {
            str(d.get("member_key")): d
            for d in db.company_ats_endpoints.find(
                {"member_key": {"$in": member_keys3}},
                {"_id": 0, "member_key": 1, "vendor": 1, "tenant": 1,
                 "host": 1, "site": 1, "extra": 1})} if member_keys3 else {}
        print(f"    hydrated {len(endpoints3):,} endpoint host(s) "
              f"from company_ats_endpoints")

        def _content(row):
            provider = ats_registry.get(row.get("ats_vendor") or "")
            if provider is None:
                return {"source_id": row["source_id"], "unsupported": True}
            ep = hydrate_endpoint(row, endpoints3)
            try:
                result = provider.validate(ep, ctx)
            except ChallengeError as exc:
                # A bot challenge IS the `blocked` state, with a retry. Counting
                # it as an unclassified error left 410 sources sitting in
                # whatever state they were already in, so a wall read as "no
                # news" instead of "come back in three days".
                return {"source_id": row["source_id"], "status": "blocked",
                        "count": 0, "sample": "",
                        "detail": f"challenge: {str(exc)[:200]}",
                        "vendor": row.get("ats_vendor")}
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
            # Boards we are DEMONSTRABLY ingesting from right now. A 403 or a
            # bot challenge is a fact about our request, not about the board:
            # when the live inventory holds fresh postings from this exact
            # (vendor, tenant), the endpoint is plainly serving jobs and
            # demoting the source would erase proven coverage because our own
            # prober got rate-limited. Measured: one content pass run beside
            # two others turned 830 sources `blocked` in a single sweep, all
            # of them boards the crawler had read that morning.
            live_boards = _boards_with_fresh_postings(db, hours=48)
            protected = 0
            promoted = 0
            for res in got3:
                if res["status"] in ("blocked", "empty"):
                    row = sources_col.find_one({"source_id": res["source_id"]},
                                               {"ats_vendor": 1, "tenant": 1})
                    key = (_vendor_key((row or {}).get("ats_vendor")),
                           str((row or {}).get("tenant") or "").strip().lower())
                    if key in live_boards:
                        css.record_note(
                            res["source_id"],
                            f"probe returned {res['status']} but the inventory "
                            f"holds {live_boards[key]:,} fresh postings from "
                            f"this board; state unchanged")
                        protected += 1
                        continue
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
            print(f"    wrote {len(got3) - protected:,} sources "
                  f"({promoted:,} now covered, {protected:,} kept because the "
                  f"inventory shows the board is live)")

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
