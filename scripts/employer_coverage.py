#!/usr/bin/env python3
"""employer_coverage.py — operate the ADR-004 employer-coverage pipeline.

One entry point for the whole loop, so an operator does not have to remember
which module does what:

    # 1. create indexes and import the existing Python catalogs as endpoints
    PYTHONPATH=shared/python python scripts/employer_coverage.py seed

    # 2. pull companies from a state registry into `companies`
    PYTHONPATH=shared/python python scripts/employer_coverage.py ingest-state --state NY --limit 50000

    # 3. fingerprint careers pages and validate the boards found
    PYTHONPATH=shared/python python scripts/employer_coverage.py discover --limit 200

    # 4. re-check endpoints that have not been seen recently
    PYTHONPATH=shared/python python scripts/employer_coverage.py revalidate --limit 300

    # 5. federal employment signals (EDGAR tickers; optional DOL H-1B LCA CSV)
    PYTHONPATH=shared/python python scripts/employer_coverage.py ingest-federal --lca path/to/lca.csv

    # 6. measure → attack the worst ADR-004 goal → repeat
    PYTHONPATH=shared/python python scripts/employer_coverage.py loop --rounds 5

Every write-mode subcommand defaults to `--dry-run` OFF but prints exactly what
it touched, and `seed`/`ingest-state` are idempotent (upsert on a stable id), so
a re-run costs time rather than duplicates.

Measurement lives in `scripts/coverage_report.py` — deliberately a separate tool,
because the thing that reports progress should not be the thing that makes it.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("employer_coverage")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _guard_for_discovery():
    """SSRF guard for outbound fetches made by this script.

    Discovery deliberately fetches ARBITRARY employer websites, so a host
    allowlist is not available to it — that is the whole point of the feature.
    The public-IP check still applies, and it is the load-bearing control here:
    without it a planted link could make this a request forwarder into private
    address space.
    """
    from services.jd_fetcher import url_is_safe

    def guard(vendor: str, url: str) -> str:
        ok, reason = url_is_safe(url)
        if not ok:
            raise ValueError(f"blocked: {reason}")
        return url

    return guard


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------

def cmd_seed(args: argparse.Namespace) -> int:
    from services import company_registry_service as reg

    if not args.skip_indexes:
        made = reg.ensure_indexes()
        logger.info("indexes ensured: %s", ", ".join(made))
    stats = reg.seed_from_catalogs(dry_run=args.dry_run)
    logger.info("seed %s: %s", "(dry run)" if args.dry_run else "written", stats)
    if not args.dry_run:
        logger.info("endpoint counts: %s",
                    json.dumps(reg.endpoint_counts(), default=str))
    return 0


# ---------------------------------------------------------------------------
# ingest-state
# ---------------------------------------------------------------------------

def cmd_ingest_state(args: argparse.Namespace) -> int:
    from services import company_registry_service as reg
    try:
        from services.state_registries import base as sr_base
    except Exception as exc:
        logger.error("state_registries unavailable: %s", exc)
        return 2

    states = ([args.state.upper()] if args.state
              else [a.state for a in sr_base.all_adapters()
                    if getattr(a, "enabled", False)])
    total = {"seen": 0, "written": 0, "skipped": 0}
    for code in states:
        adapter = sr_base.get(code)
        if adapter is None or not getattr(adapter, "enabled", False):
            logger.warning("%s: no usable adapter, skipping", code)
            continue
        logger.info("%s: pulling via %s (%s)", code, adapter.method,
                    adapter.source_url[:70])
        started = time.monotonic()
        try:
            records = adapter.fetch(limit=args.limit, cursor=args.cursor)
            if args.dry_run:
                shown = 0
                for rec in records:
                    total["seen"] += 1
                    if shown < 5:
                        print(f"  {rec.state} {rec.name[:56]:<58} {rec.entity_type}")
                        shown += 1
                    if total["seen"] >= args.limit:
                        break
                continue
            stats = reg.bulk_upsert_companies(
                (r.as_upsert() if hasattr(r, "as_upsert") else r)
                for r in records)
            for k in total:
                total[k] += stats.get(k, 0)
            logger.info("%s: %s in %.1fs", code, stats,
                        time.monotonic() - started)
        except Exception as exc:
            logger.error("%s: pull failed: %s: %s", code, type(exc).__name__, exc)
    logger.info("TOTAL %s", total)
    return 0


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------

def cmd_discover(args: argparse.Namespace) -> int:
    from services import company_registry_service as reg
    from services.ats_providers.http import simple_context
    from services.career_portal_discovery import discover_batch

    refilled = reg.refill_queue(limit=max(args.limit * 4, 200))
    logger.info("queue refilled with %d companies (depth=%s)", refilled,
                reg.queue_depth())

    owner = f"cli-{int(time.time())}"
    batch = reg.claim_discovery_batch(owner, size=args.limit)
    if not batch:
        logger.info("nothing to discover — queue empty")
        return 0

    companies: List[Dict[str, Any]] = []
    for item in batch:
        doc = reg.get_company(item["company_id"]) or {}
        companies.append({"company_id": item["company_id"],
                          "name": doc.get("name") or "",
                          "domain": doc.get("domain") or ""})

    ctx = simple_context(budget_seconds=args.budget,
                         guard=_guard_for_discovery())
    stats = discover_batch(companies, ctx, persist=not args.dry_run,
                           validate=not args.no_validate)
    unknown = stats.pop("unknown_hosts", {})
    logger.info("discovery: %s", stats)
    if unknown:
        top = sorted(unknown.items(), key=lambda kv: -kv[1])[:15]
        # These are hosts that look like an ATS but matched no provider. They
        # are the evidence-backed shortlist for which vendor to implement next.
        logger.info("unmatched ATS-looking hosts (candidates for a new provider):")
        for host, n in top:
            logger.info("    %-52s x%d", host, n)
    return 0


# ---------------------------------------------------------------------------
# discover-tenants  (vendor-first)
# ---------------------------------------------------------------------------

def _vendor_cooling(ctx, host: str, vendor: str) -> bool:
    """True when the shared pacing key for this vendor is in cooldown."""
    try:
        from services.ats_providers.http import PacedSession
        sess = getattr(ctx, "_session", None)
        if sess is None:
            return False
        return sess.in_cooldown(sess.key_for(f"https://{host}/", vendor))
    except Exception:
        return False


def cmd_discover_tenants(args: argparse.Namespace) -> int:
    """Enumerate a vendor's tenants from public archives, then validate them.

    `discover` is COMPANY-first: it needs a company that we already know exists
    and whose domain we already have, then asks that company's own site which
    ATS it uses. That is the right primary strategy (ADR-004 §2.1) and it is
    also the reason the long tail is unreachable — state registries publish
    names, not domains, and the smallest employers are on nobody's list.

    This is the other direction. Vendors that give every customer their own
    hostname have already published their customer list, in Common Crawl's URL
    index and in Certificate Transparency. Read the tenants off the vendor and
    work backwards to the employer.

    Nothing here decides a tenant is real: an archived hostname is a CANDIDATE
    (wildcard DNS resolves anything, crawled URLs go 404, certificates outlive
    customers). `provider.validate()` promotes it, exactly as it does for a
    fingerprinted endpoint.
    """
    from services import company_registry_service as reg
    from services.ats_providers import registry as ats_registry
    from services.ats_providers.base import Endpoint
    from services.ats_providers.http import simple_context
    from services import tenant_discovery

    vendors = args.vendor or [v for v, _d, _p in
                              tenant_discovery.SUBDOMAIN_VENDORS]
    domains = {v: (d, p) for v, d, p in tenant_discovery.SUBDOMAIN_VENDORS}

    totals: Dict[str, int] = {"candidates": 0, "validated": 0, "empty": 0,
                              "dead": 0, "blocked": 0, "skipped": 0,
                              "not_attempted": 0}
    for vendor in vendors:
        domain, path_pattern = domains.get(vendor, ("", ""))
        if not domain:
            logger.warning("no tenant domain known for %s — skipping", vendor)
            continue
        provider = ats_registry.get(vendor)
        if provider is None:
            logger.warning("no provider registered for %s — skipping", vendor)
            continue

        logger.info("enumerating %s tenants under *.%s", vendor, domain)
        candidates = tenant_discovery.discover_vendor_tenants(
            vendor, domain, cc_pages=args.cc_pages,
            cc_snapshots=args.cc_snapshots,
            path_pattern=path_pattern, use_crtsh=args.use_crtsh)
        logger.info("%s: %d candidate hostnames", vendor, len(candidates))
        totals["candidates"] += len(candidates)
        if args.dry_run:
            for cand in candidates[:args.limit]:
                logger.info("    %-52s %s", cand.host, sorted(cand.sources))
            continue

        # Validation is the expensive half and some vendors are deliberately
        # slow (iCIMS paces at 2.5s with one connection), so it gets its own
        # budget and the highest-confidence candidates go first.
        ctx = simple_context(budget_seconds=args.budget,
                             guard=_guard_for_discovery())
        checked = 0
        for cand in candidates:
            if checked >= args.limit or ctx.should_stop():
                break
            endpoint = Endpoint(vendor=vendor, tenant=cand.label,
                                host=cand.host)
            checked += 1
            try:
                result = provider.validate(endpoint, ctx)
            except Exception as exc:
                logger.debug("%s: %s validate failed (%s)", vendor, cand.host,
                             type(exc).__name__)
                totals["skipped"] += 1
                continue
            status = getattr(result, "status", "") or ""
            totals[status] = totals.get(status, 0) + 1

            # A vendor-wide cooldown means every LATER candidate would be
            # reported "blocked" without a request ever being made. That is not
            # a measurement, it is the absence of one, and it reads in the
            # summary as "90 employers are blocking us" when the truth is "we
            # tried one". Stop, and say which it was.
            if status == "blocked" and _vendor_cooling(ctx, cand.host, vendor):
                remaining = max(0, min(args.limit, len(candidates)) - checked)
                totals["not_attempted"] += remaining
                logger.warning(
                    "%s: vendor-wide cooldown after %d checked; %d candidates "
                    "NOT attempted (this is not evidence about them)",
                    vendor, checked, remaining)
                break
            # Only a board that actually served rows is worth a row of its own.
            # Persisting every candidate would fill `company_ats_endpoints` with
            # hostnames that merely resolve, which is precisely the mistake
            # wildcard DNS invites.
            if status not in ("validated", "empty"):
                continue
            key = reg.upsert_endpoint(endpoint,
                                      display_name=cand.label,
                                      source=f"tenant-archive:{vendor}")
            reg.record_validation(key, result)
            logger.info("    %-46s %-10s %5d  %s", cand.host, status,
                        getattr(result, "job_count", 0),
                        (getattr(result, "sample_title", "") or "")[:40])

    logger.info("discover-tenants: %s", totals)
    return 0


# ---------------------------------------------------------------------------
# revalidate
# ---------------------------------------------------------------------------

def cmd_revalidate(args: argparse.Namespace) -> int:
    from services import company_registry_service as reg
    from services.ats_providers import registry as ats_registry
    from services.ats_providers.base import Endpoint
    from services.ats_providers.http import simple_context

    due = reg.endpoints_needing_revalidation(limit=args.limit)
    if not due:
        logger.info("nothing due for revalidation")
        return 0
    logger.info("revalidating %d endpoints", len(due))

    ctx = simple_context(budget_seconds=args.budget,
                         guard=_guard_for_discovery())
    tally: Dict[str, int] = {}
    for doc in due:
        if ctx.should_stop():
            logger.info("budget exhausted, stopping early")
            break
        provider = ats_registry.get(str(doc.get("vendor") or ""))
        if provider is None:
            continue
        endpoint = Endpoint.from_doc(doc)
        try:
            result = provider.validate(endpoint, ctx)
        except Exception as exc:
            logger.debug("%s: %s", endpoint.member_key, type(exc).__name__)
            continue
        tally[result.status] = tally.get(result.status, 0) + 1
        if not args.dry_run:
            reg.record_validation(doc["member_key"], result)
    logger.info("revalidation: %s", tally)
    return 0


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def cmd_probe(args: argparse.Namespace) -> int:
    """Fingerprint one company. No writes — the fast feedback loop when adding
    a provider or debugging why an employer is not being found."""
    from services.ats_providers.http import simple_context
    from services.career_portal_discovery import PortalDiscovery

    ctx = simple_context(budget_seconds=args.budget,
                         guard=_guard_for_discovery())
    engine = PortalDiscovery(ctx, validate=not args.no_validate)
    result = engine.discover(args.name, domain=args.domain or "")

    print(f"\n  company : {args.name}")
    print(f"  domain  : {result.domain or '(unresolved)'}")
    print(f"  pages   : {result.pages_fetched}")
    if result.detail:
        print(f"  note    : {result.detail}")
    print(f"  found   : {result.found} endpoint(s)\n")
    for ep in result.endpoints:
        line = f"    {ep.vendor:<16} tenant={ep.tenant:<28}"
        if ep.site:
            line += f" site={ep.site}"
        if ep.host:
            line += f" host={ep.host}"
        print(line)
    for ep, res in result.validated:
        print(f"      -> {ep.member_key:<44} {res.status:<10} "
              f"jobs={res.job_count} {res.detail}")
    if result.unknown_hosts:
        print("\n  unmatched ATS-looking hosts:")
        for host, n in sorted(result.unknown_hosts.items(), key=lambda kv: -kv[1]):
            print(f"    {host}  x{n}")
    print()
    return 0 if result.endpoints else 1


def cmd_ingest_federal(args: argparse.Namespace) -> int:
    from services import national_signals

    persist = not args.dry_run
    stats: Dict[str, Any] = {}
    if args.lca:
        stats["lca"] = national_signals.ingest_lca(args.lca, limit=args.limit,
                                                   persist=persist)
        logger.info("lca: %s", stats["lca"])
    if args.skip_edgar:
        logger.info("skipping EDGAR")
    else:
        try:
            stats["edgar"] = national_signals.ingest_edgar(persist=persist)
            logger.info("edgar: %s", stats["edgar"])
        except Exception as exc:
            logger.error("edgar failed: %s: %s", type(exc).__name__, exc)
            if not args.lca:
                return 2
    print(json.dumps(stats, default=str))
    return 0


#: Binding operator for each ADR-004 goal. Code-only goals (G3, G5) re-measure
#: after a no-op so the loop still has a heartbeat; G8 is operational (the
#: ingest worker must be running).
def _action_for(key: str):
    class _NS:
        pass

    def seed():
        ns = _NS(); ns.dry_run = False; ns.skip_indexes = False
        return cmd_seed(ns)

    def ingest_all():
        ns = _NS(); ns.state = None; ns.limit = 5000; ns.cursor = None
        ns.dry_run = False
        return cmd_ingest_state(ns)

    def discover():
        ns = _NS(); ns.limit = 50; ns.budget = 180.0; ns.no_validate = False
        ns.dry_run = False
        return cmd_discover(ns)

    def revalidate():
        ns = _NS(); ns.limit = 100; ns.budget = 180.0; ns.dry_run = False
        return cmd_revalidate(ns)

    return {
        "G1": seed,
        "G2": discover,
        "G3": lambda: 0,
        "G4": ingest_all,
        "G5": ingest_all,
        "G6": discover,
        "G7": revalidate,
        "G8": lambda: 0,
    }.get(key, lambda: 0)


def cmd_loop(args: argparse.Namespace) -> int:
    """Measure → pick the worst goal → run its operator → repeat."""
    import coverage_report as report

    last_code = 1
    for round_n in range(1, args.rounds + 1):
        goals = report.run(args.goal)
        met = [g for g in goals if g.state == "MET"]
        miss = [g for g in goals if g.state != "MET"]
        print(f"\n--- loop round {round_n}/{args.rounds}  "
              f"{len(met)} met / {len(miss)} open ---")
        for g in goals:
            print(g.render())
        if not miss:
            print("all selected goals MET")
            return 0
        worst = miss[0]
        print(f"\nnext: {worst.key} — {worst.title}")
        print(f"      {worst.error or worst.detail}")
        if args.dry_run:
            continue
        try:
            _action_for(worst.key)()
        except Exception as exc:
            logger.error("action %s failed: %s: %s", worst.key,
                         type(exc).__name__, exc)
        last_code = 1
    return last_code


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("seed", help="indexes + import Python catalogs")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-indexes", action="store_true")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("ingest-state", help="pull companies from state registries")
    p.add_argument("--state", help="2-letter code; omit for every enabled state")
    p.add_argument("--limit", type=int, default=10000)
    p.add_argument("--cursor", help="resume token from a previous pull")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ingest_state)

    p = sub.add_parser("discover", help="fingerprint careers pages")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--budget", type=float, default=600.0)
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("discover-tenants",
                       help="enumerate a vendor's tenants from public archives")
    p.add_argument("--vendor", action="append", default=[],
                   help="vendor key (repeatable); default = every enumerable one")
    p.add_argument("--limit", type=int, default=200,
                   help="candidates to VALIDATE per vendor")
    p.add_argument("--budget", type=float, default=900.0,
                   help="seconds of validation per vendor")
    p.add_argument("--cc-pages", type=int, default=6,
                   help="Common Crawl index pages to read per vendor")
    p.add_argument("--cc-snapshots", type=int, default=1,
                   help="how many recent monthly crawls to accumulate over; "
                        "each samples the web independently, so more snapshots "
                        "means more tenants (be polite — these are free "
                        "public research indexes)")
    p.add_argument("--use-crtsh", action="store_true",
                   help="also query crt.sh (often unavailable)")
    p.add_argument("--dry-run", action="store_true",
                   help="list candidates without validating or writing")
    p.set_defaults(func=cmd_discover_tenants)

    p = sub.add_parser("revalidate", help="re-check known endpoints")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--budget", type=float, default=600.0)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_revalidate)

    p = sub.add_parser("probe", help="fingerprint ONE company (no writes)")
    p.add_argument("--name", required=True)
    p.add_argument("--domain")
    p.add_argument("--budget", type=float, default=180.0)
    p.add_argument("--no-validate", action="store_true")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("ingest-federal", help="EDGAR tickers + optional H-1B LCA")
    p.add_argument("--lca", help="path to DOL LCA CSV")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--skip-edgar", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ingest_federal)

    p = sub.add_parser("loop", help="measure coverage and attack the worst goal")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--goal", action="append")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_loop)

    args = ap.parse_args()
    _setup_logging(args.verbose)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        logger.warning("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
