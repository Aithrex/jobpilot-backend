#!/usr/bin/env python3
"""verify_official_career_urls.py — fetch the URLs the registry claims, and
record what actually happened.

DRY-RUN BY DEFAULT.

Why this exists
---------------
`official_career_registry`'s docstring has always said "`verified_at` is the
date the URL was last confirmed by a live fetch, and it is written by
`scripts/verify_official_career_urls.py`, never by hand." The script did not
exist. `_row` hard-coded `verified_at: None` and nothing could ever have
written anything else, so the field documented a workflow rather than
recording one — a promise in a docstring is not a verification event.

This is that workflow. For each registry row it:

  1. verifies the DOMAIN — fetches it and checks the page names the employer
     in VISIBLE TEXT, never in markup (`career_source_ownership.verify_domain`);
  2. fetches the registered careers URL and records the redirect chain;
  3. fingerprints ATS endpoints out of the page's outgoing links;
  4. writes ONE row to `official_career_url_checks` with the outcome.

`verified_at` moves only on `verified`. A 403 is recorded as `blocked` with
its own timestamp and does NOT clear an earlier verification, for the reason
`career_source_service.record_failure` records: one blocked sweep run beside
two others turned 830 sources `blocked` and cut validated coverage by a third.
A fact about our request is not a fact about the employer.

Safety
------
Every fetch goes through `career_source_ownership.Fetcher`: SSRF check on the
URL and on EVERY redirect hop, robots.txt per origin, a per-host pace, a 12 s
timeout and a 1.5 MB response cap. Nothing here takes a URL from a user; the
inputs are a checked-in registry and, with `--url`, an operator's own argument.

`--register` additionally hands what it found to `career_source_service`,
which decides the state. It cannot write `validated` on the strength of this
script alone: the ownership rule still needs a domain chain or two independent
signals, and this script supplies the chain only when the walk actually found
one.

Usage
-----
    PYTHONPATH=shared/python python scripts/verify_official_career_urls.py
    PYTHONPATH=shared/python python scripts/verify_official_career_urls.py --apply
    PYTHONPATH=shared/python python scripts/verify_official_career_urls.py \
        --apply --register --only x.ai,cognition.com --workers 4
    PYTHONPATH=shared/python python scripts/verify_official_career_urls.py \
        --apply --url "Cognition|cognition.com|https://cognition.com/careers"
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
from services.career_source_service import evidence      # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


#: One global limit across every worker. A verification sweep that gets us
#: blocked costs more coverage than it buys, and per-host pacing alone does
#: not bound the TOTAL rate when the hosts are all different.
class GlobalPace:
    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / max(0.1, float(per_second))
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            due = max(now, self._next)
            self._next = due + self._interval
        if due > now:
            time.sleep(min(10.0, due - now))


class PacedFetcher(cso.Fetcher):
    def __init__(self, pace: GlobalPace, **kw: Any) -> None:
        super().__init__(**kw)
        self._global = pace

    def get(self, url: str) -> Dict[str, Any]:
        self._global.wait()
        return super().get(url)


def check_row(row: Dict[str, Any], *, pace: GlobalPace) -> Dict[str, Any]:
    """Verify ONE registry row. Pure with respect to the database."""
    fetcher = PacedFetcher(pace, min_interval=1.0)
    name = row.get("display_name") or ""
    domain = str(row.get("domain") or "").strip().lower()
    url = str(row.get("career_landing_url") or "").strip()

    out: Dict[str, Any] = {
        "display_name": name, "domain": domain, "career_landing_url": url,
        "outcome": ocr.CHECK_ERROR, "detail": "", "http_status": None,
        "final_url": "", "redirect_chain": [], "endpoints": [],
        "domain_confirmed": False, "aliases": [],
    }

    # 1. The domain itself. Without it, "their site links here" is unfounded.
    domain_ok, domain_url, domain_ev = cso.verify_domain(
        fetcher, display_name=name, domain=domain)
    out["domain_confirmed"] = bool(domain_ok)
    out["domain_evidence"] = domain_ev

    # 2. The registered careers URL.
    got = fetcher.get(url) if url else {"status": None, "error": "no url",
                                        "chain": [], "final_url": "", "text": ""}
    out["http_status"] = got.get("status")
    out["final_url"] = got.get("final_url") or ""
    out["redirect_chain"] = list(got.get("chain") or [])

    err = str(got.get("error") or "")
    status = got.get("status")
    if err.startswith("robots"):
        out["outcome"] = ocr.CHECK_BLOCKED
        out["detail"] = "robots.txt disallows this path for our agent"
        return out
    if err.startswith("ssrf"):
        out["outcome"] = ocr.CHECK_ERROR
        out["detail"] = err
        return out
    if err:
        out["outcome"] = ocr.CHECK_ERROR
        out["detail"] = err
        return out
    if status in (403, 429):
        out["outcome"] = ocr.CHECK_BLOCKED
        out["detail"] = f"HTTP {status} — a fact about our request, not the board"
        return out
    if status in (404, 410):
        out["outcome"] = ocr.CHECK_MISSING
        out["detail"] = f"HTTP {status} at the registered path"
        return out
    if status != 200 or not got.get("text"):
        out["outcome"] = ocr.CHECK_ERROR
        out["detail"] = f"HTTP {status}"
        return out

    # 3. Endpoints fingerprinted out of what the page links to.
    urls = cso.extract_urls(got["text"], got["final_url"])
    eps = cso.endpoints_from_page(urls)
    out["endpoints"] = [f"{getattr(e, 'vendor', '')}:{getattr(e, 'tenant', '')}"
                        for e in eps]
    out["_endpoint_objs"] = eps

    # A careers page that links to no board is normal for a JS shell; follow
    # this page's own careers links once before giving up, which is what the
    # ownership walk does and for the same reason.
    if not eps:
        for link in cso.career_links(got["text"], got["final_url"], limit=3):
            sub = fetcher.get(link)
            if sub.get("error") or sub.get("status") != 200 or not sub.get("text"):
                continue
            sub_eps = cso.endpoints_from_page(
                cso.extract_urls(sub["text"], sub["final_url"]))
            if sub_eps:
                out["endpoints"] = [
                    f"{getattr(e, 'vendor', '')}:{getattr(e, 'tenant', '')}"
                    for e in sub_eps]
                out["_endpoint_objs"] = sub_eps
                out["redirect_chain"] = list(out["redirect_chain"]) + list(
                    sub.get("chain") or [])
                break

    # 4. Does the page name the employer? This is what separates "the URL
    #    answers" from "the URL is theirs".
    page_ok, why = cso.page_confirms_employer(got["text"], name, host=domain)
    from urllib.parse import urlparse
    start_host = urlparse(url).netloc.lower().lstrip("www.")
    final_host = urlparse(out["final_url"]).netloc.lower().lstrip("www.")
    if final_host and start_host and final_host != start_host:
        out["outcome"] = ocr.CHECK_MOVED
        out["detail"] = f"redirects to {final_host}"
        # A move to the employer's OWN ATS is a normal, correct redirect and
        # still verifies the employer, so the name check is kept either way.
        if page_ok or domain_ok:
            out["detail"] += "; page still names the employer"
        return out

    if page_ok:
        out["outcome"] = ocr.CHECK_VERIFIED
        out["detail"] = why
    else:
        out["outcome"] = ocr.CHECK_REACHABLE
        out["detail"] = f"200, but {why}"
    return out


def register_source(db, row: Dict[str, Any], result: Dict[str, Any],
                    *, apply: bool) -> List[str]:
    """Hand what the walk found to `career_source_service`. It decides.

    The evidence offered is exactly what was OBSERVED: a domain chain when the
    employer's own verified domain led to the endpoint, plus whatever
    corroborating signals the provider and catalogs supply. This script cannot
    hand over a `validated` state — `build_source` derives it — which is the
    property that keeps an eager script from doing what the old probe path did.
    """
    eps = result.get("_endpoint_objs") or []
    if not eps:
        return []
    name = row.get("display_name") or ""
    domain = str(row.get("domain") or "").lower()
    eid = er.employer_id_for(domain=domain, name=name)

    # Mint the EMPLOYER row too, or the career source points at an id the
    # registry has never heard of and the Tier-A roll call reports the
    # employer as `not_in_registry` — which reads as "we lost them" rather
    # than "we just verified them". The registry row carries exactly what the
    # official registry states (the name, the domain, the declared cohorts and
    # categories) plus `official_career_url` evidence, which is one of the
    # documentary kinds that puts an employer in the technology denominator.
    if apply:
        try:
            er.upsert_employer(er.build_employer(
                display_name=name,
                canonical_domain=domain if result.get("domain_confirmed") else None,
                aliases=(name,),
                verified_domains=(domain,) if result.get("domain_confirmed") else (),
                employer_categories=row.get("categories") or (),
                category_source="official_career_registry",
                cohorts=row.get("cohorts") or (),
                evidence=[{"kind": "official_career_url",
                           "source": "official_career_registry",
                           "value": str(row.get("career_landing_url") or "")[:200]}],
            ))
        except Exception as exc:      # pragma: no cover - never block the pass
            print(f"    could not upsert employer for {domain}: "
                  f"{type(exc).__name__}: {exc}")

    written: List[str] = []
    for ep in eps:
        own_ev: List[Dict[str, Any]] = list(result.get("domain_evidence") or [])
        if result.get("domain_confirmed"):
            own_ev.append(evidence(
                css.SIGNAL_DOMAIN_LINK, source=result.get("final_url") or domain,
                value=f"{domain} -> {getattr(ep, 'member_key', '')}",
                confirmed=True))
            if len(result.get("redirect_chain") or []) > 1:
                own_ev.append(evidence(
                    css.SIGNAL_DOMAIN_REDIRECT,
                    source=" -> ".join(result["redirect_chain"][:6]),
                    value=f"{len(result['redirect_chain'])} hop(s)", confirmed=True))
        sig = cso.curated_row_signal(ep, display_name=name)
        if sig:
            own_ev.append(sig)
        own_ev.append(cso.slug_similarity_signal(ep, display_name=name))
        rec = css.build_source(
            employer_id=eid,
            ats_vendor=getattr(ep, "vendor", ""),
            tenant=getattr(ep, "tenant", ""),
            verified_domain=domain if result.get("domain_confirmed") else "",
            career_landing_url=row.get("career_landing_url"),
            canonical_endpoint=cso._endpoint_url(ep),
            redirect_chain=result.get("redirect_chain") or (),
            discovery_method=(css.METHOD_OFFICIAL_REDIRECT
                              if len(result.get("redirect_chain") or []) > 1
                              else css.METHOD_OFFICIAL_LINK),
            ownership_evidence=own_ev,
            member_key=getattr(ep, "member_key", ""))
        if apply:
            css.upsert_source(rec)
        written.append(f"{rec['ats_vendor']}:{rec['tenant']}={rec['state']}")
    return written


def _parse_url_arg(value: str) -> Optional[Dict[str, Any]]:
    parts = [p.strip() for p in str(value or "").split("|")]
    if len(parts) != 3 or not all(parts):
        return None
    return {"display_name": parts[0], "domain": parts[1].lower(),
            "career_landing_url": parts[2], "cohorts": [], "categories": [],
            "notes": "supplied on the command line"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="record the verification events. Without it nothing is written.")
    ap.add_argument("--register", action="store_true",
                    help="also offer what was found to career_source_service")
    ap.add_argument("--only", default="",
                    help="comma-separated domains to check (default: all)")
    ap.add_argument("--url", action="append", default=[],
                    help="'Display Name|domain|url' — check a row not in the registry")
    ap.add_argument("--unchecked-only", action="store_true",
                    help="skip domains that already carry a verification event")
    ap.add_argument("--stale-days", type=int, default=0,
                    help="re-check rows whose last check is older than this")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--rate", type=float, default=2.0,
                    help="global requests/second ceiling across all workers")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    db = _db()
    if args.apply:
        ocr.ensure_indexes(db=db)

    rows: List[Dict[str, Any]] = [dict(r) for r in ocr.OFFICIAL_CAREER_URLS]
    for raw in args.url:
        parsed = _parse_url_arg(raw)
        if parsed is None:
            print(f"skipping unparseable --url {raw!r} "
                  f"(expected 'Name|domain|url')")
            continue
        rows = [r for r in rows if r["domain"] != parsed["domain"]] + [parsed]

    if args.only:
        wanted = {d.strip().lower() for d in args.only.split(",") if d.strip()}
        rows = [r for r in rows if str(r["domain"]).lower() in wanted]

    existing = ocr.checks_for(db=db)
    if args.unchecked_only:
        rows = [r for r in rows if r["domain"] not in existing]
    if args.stale_days:
        cutoff = _now().timestamp() - args.stale_days * 86400
        def _stale(r):
            chk = existing.get(r["domain"])
            if not chk or not chk.get("checked_at"):
                return True
            when = chk["checked_at"]
            ts = when.timestamp() if hasattr(when, "timestamp") else 0
            return ts < cutoff
        rows = [r for r in rows if _stale(r)]
    if args.limit:
        rows = rows[:args.limit]

    print(f"checking {len(rows)} registry row(s) "
          f"at <= {args.rate}/s with {args.workers} worker(s)"
          + ("" if args.apply else "   [DRY RUN — nothing written]"))

    pace = GlobalPace(args.rate)
    results: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(check_row, r, pace=pace): r for r in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                res = fut.result()
            except Exception as exc:      # pragma: no cover
                res = {"display_name": row.get("display_name"),
                       "domain": row.get("domain"),
                       "career_landing_url": row.get("career_landing_url"),
                       "outcome": ocr.CHECK_ERROR,
                       "detail": f"{type(exc).__name__}: {exc}",
                       "endpoints": [], "redirect_chain": [], "final_url": "",
                       "http_status": None, "domain_confirmed": False}
            results.append((row, res))
            flag = "OK " if res["outcome"] == ocr.CHECK_VERIFIED else "   "
            print(f"  {flag}{res['domain']:28s} {res['outcome']:10s} "
                  f"{(res.get('detail') or '')[:70]}"
                  + (f"  -> {', '.join(res['endpoints'][:3])}"
                     if res.get("endpoints") else ""))

    registered: Dict[str, List[str]] = {}
    if args.apply:
        for row, res in results:
            ocr.record_check(
                db, domain=res["domain"], display_name=res["display_name"],
                career_landing_url=res["career_landing_url"],
                outcome=res["outcome"], detail=res.get("detail"),
                http_status=res.get("http_status"),
                final_url=res.get("final_url"),
                redirect_chain=res.get("redirect_chain") or (),
                endpoints=res.get("endpoints") or ())
    if args.register:
        for row, res in results:
            written = register_source(db, row, res, apply=bool(args.apply))
            if written:
                registered[res["domain"]] = written

    counts = collections.Counter(res["outcome"] for _r, res in results)
    print("\noutcomes:")
    for outcome, n in counts.most_common():
        print(f"  {outcome:12s} {n:>4}")
    with_eps = sum(1 for _r, res in results if res.get("endpoints"))
    print(f"\nrows whose page linked an ATS endpoint: {with_eps} / {len(results)}")
    if registered:
        print("\nsources offered to career_source_service"
              + ("" if args.apply else "  [DRY RUN]") + ":")
        for dom, states in sorted(registered.items()):
            print(f"  {dom:28s} {', '.join(states)}")
    if not args.apply:
        print("\nDRY RUN — no verification event recorded. Re-run with --apply.")

    if args.json:
        payload = [{k: v for k, v in res.items() if not k.startswith("_")}
                   for _r, res in results]
        Path(args.json).write_text(
            json.dumps({"generated_at": _now().isoformat(),
                        "registry_version": ocr.REGISTRY_VERSION,
                        "results": payload,
                        "registered": registered}, indent=2, default=str),
            encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
