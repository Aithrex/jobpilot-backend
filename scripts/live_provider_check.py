#!/usr/bin/env python3
"""live_provider_check.py — prove every registered ATS provider still works.

The unit tests for `ats_providers/` feed each provider a recorded fixture, so
they answer "does the parser still parse that byte string". They cannot answer
the question that actually decides whether a job seeker sees a posting:

    does this vendor's live endpoint still respond, and does OUR code still
    speak its protocol TODAY?

Vendors change hosts, add anti-bot walls, rename JSON fields and retire
endpoints without notice, and every one of those failures is invisible to a
fixture-based test — the suite stays green while the crawler silently returns
zero. ADR-004 §3.2 is exactly this failure recorded once already: iCIMS was
marked "do not build" by an instrument that could only detect JSON APIs.

So this script is the network-side twin of the unit tests. One known-good
endpoint per vendor, listed through the SAME provider code the crawler uses
(`registry.get(vendor).list_jobs`), asserting a real posting comes back.

    PYTHONPATH=shared/python python3 scripts/live_provider_check.py
    PYTHONPATH=shared/python python3 scripts/live_provider_check.py --vendor icims
    PYTHONPATH=shared/python python3 scripts/live_provider_check.py --json

Exit code is 0 only when every checked vendor returned at least one posting.
A vendor with no probe endpoint is reported UNPROBED and fails the run: a
registered provider that nothing can exercise is indistinguishable from a
broken one, which is the "registered but never reached" trap ADR-004 §4.1 was
written to close.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

sys.path.insert(0, "shared/python")

from services.ats_providers import registry  # noqa: E402
from services.ats_providers.base import (ChallengeError, Endpoint,  # noqa: E402
                                         DATE_UNKNOWN)
from services.ats_providers.http import simple_context  # noqa: E402


# ---------------------------------------------------------------------------
# Probe endpoints
# ---------------------------------------------------------------------------
# One real, public board per vendor. These are deliberately large, long-lived
# employers: a probe that fails because the startup shut down tells us nothing
# about the provider. Where a vendor needs a host or site id that is not
# derivable from the tenant (Oracle pods, Phenom hosts), it is spelled out.

PROBES: Dict[str, Dict[str, Any]] = {
    # Every tenant below was confirmed to return >=1 posting on 2026-08-18.
    # A probe that yields nothing tells us nothing about the provider, which is
    # how the first run of this script mis-reported five working vendors as
    # EMPTY: `lever/netflix`, `workable/swordhealth` and `icims/us-careers-rivian`
    # are all boards that have MOVED, not providers that are broken.
    "greenhouse": {"tenant": "stripe", "display_name": "Stripe"},
    "lever": {"tenant": "15five", "display_name": "15Five"},
    "ashby": {"tenant": "ramp", "display_name": "Ramp"},
    "smartrecruiters": {"tenant": "AbbVie", "display_name": "AbbVie"},
    "workable": {"tenant": "anza-xyz", "display_name": "Anza"},
    "recruitee": {"tenant": "accelecom", "display_name": "Accelecom"},
    "jobvite": {"tenant": "actionet", "display_name": "ActioNet"},
    "bamboohr": {"tenant": "24slg", "display_name": "24SLG"},
    "workday": {"tenant": "nvidia", "site": "NVIDIAExternalCareerSite",
                "display_name": "NVIDIA", "extra": {"wd_host": "wd5"}},
    "oracle_orc": {"tenant": "jpmc", "site": "CX_1",
                   "host": "jpmc.fa.oraclecloud.com",
                   "display_name": "JPMorgan Chase"},
    # Rivian (the ADR-004 seed) migrated off iCIMS; `careers-emory` is a landing
    # page that iframes `staff-emory`, which is the board with the jobs.
    "icims": {"tenant": "staff-emory", "host": "staff-emory.icims.com",
              "display_name": "Emory University"},
    "phenom": {"tenant": "usbank", "host": "careers.usbank.com",
               "display_name": "US Bank"},
    "eightfold": {"tenant": "microsoft", "host": "apply.careers.microsoft.com",
                  "display_name": "Microsoft",
                  "extra": {"domain": "microsoft.com", "variant": "pcsx"}},
    "neogov": {"tenant": "washington", "display_name": "State of Washington"},
    "taleo": {"tenant": "aarcorp", "host": "aarcorp.taleo.net",
              "display_name": "AAR Corp"},
    # Payroll-attached ATS (see `ats_providers/payroll.py`). UKG and Paylocity
    # boards are identified by opaque ids, so these were taken from real URLs
    # in the Common Crawl index rather than guessed.
    "ukg": {"tenant": "AAM1000AAM",
            "site": "c5a88c41-a6d1-4e5d-bf94-4d0432a0df30",
            "display_name": "AAM"},
    "paylocity": {"tenant": "002dce18-ff77-4ac9-90f8-6a928b1044ef",
                  "display_name": "Your Home Improvement Company"},
    "rippling": {"tenant": "rippling", "display_name": "Rippling"},
    # Cornerstone site ids are not derivable and are not always 1: Arizona
    # serves 324 requisitions on site 4 and zero on site 1.
    "cornerstone": {"tenant": "arizona", "site": "4",
                    "host": "arizona.csod.com",
                    "display_name": "University of Arizona"},
    "jazzhr": {"tenant": "aamco", "host": "aamco.applytojob.com",
               "display_name": "AAMCO"},
}


def _endpoint_for(vendor: str) -> Optional[Endpoint]:
    spec = PROBES.get(vendor)
    if not spec:
        return None
    return Endpoint(
        vendor=vendor,
        tenant=str(spec.get("tenant") or ""),
        site=str(spec.get("site") or ""),
        host=str(spec.get("host") or ""),
        display_name=str(spec.get("display_name") or ""),
        extra=dict(spec.get("extra") or {}),
    )


# ---------------------------------------------------------------------------
# One vendor
# ---------------------------------------------------------------------------

def check(vendor: str, *, limit: int = 5, budget: float = 90.0,
          verbose: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {"vendor": vendor, "status": "UNPROBED",
                           "jobs": 0, "elapsed": 0.0, "detail": ""}
    provider = registry.get(vendor)
    if provider is None:
        out["detail"] = "no provider registered"
        return out
    endpoint = _endpoint_for(vendor)
    if endpoint is None:
        out["detail"] = "no probe endpoint defined in PROBES"
        return out

    out["tenant"] = endpoint.member_key
    ctx = simple_context(budget_seconds=budget)
    started = time.monotonic()
    rows: List[Any] = []
    try:
        for job in provider.list_jobs(endpoint, ctx):
            rows.append(job)
            if len(rows) >= limit:
                break
    except ChallengeError as exc:
        out.update(status="BLOCKED", detail=str(exc),
                   elapsed=round(time.monotonic() - started, 2))
        return out
    except Exception as exc:
        out.update(status="ERROR",
                   detail=f"{type(exc).__name__}: {exc}",
                   elapsed=round(time.monotonic() - started, 2))
        if verbose:
            out["traceback"] = traceback.format_exc()
        return out

    out["elapsed"] = round(time.monotonic() - started, 2)
    out["jobs"] = len(rows)
    if not rows:
        out.update(status="EMPTY", detail="listing returned zero postings")
        return out

    first = rows[0]
    out["status"] = "OK"
    out["sample_title"] = str(getattr(first, "title", ""))[:80]
    out["sample_url"] = str(getattr(first, "apply_url", ""))[:160]
    out["sample_location"] = str(getattr(first, "location", ""))[:60]
    out["sample_posted_at"] = str(getattr(first, "posted_at", ""))

    # Quality gates. A provider that yields rows the inventory will throw away
    # is not working, it is only appearing to work: `_raw_job_to_listing`
    # drops any row missing an id, a title or a url.
    problems: List[str] = []
    if not all(str(getattr(j, "source_job_id", "") or "").strip() for j in rows):
        problems.append("row(s) missing source_job_id")
    if not all(str(getattr(j, "title", "") or "").strip() for j in rows):
        problems.append("row(s) missing title")
    if not all(str(getattr(j, "apply_url", "") or "").strip() for j in rows):
        problems.append("row(s) missing apply_url")
    # ADR-001: a provider claiming exact dates must actually produce them.
    if provider.date_confidence != DATE_UNKNOWN:
        dated = sum(1 for j in rows if str(getattr(j, "posted_at", "") or ""))
        if dated == 0:
            problems.append(
                f"declares date_confidence={provider.date_confidence} but no "
                "row carried a posted_at")
    if problems:
        out.update(status="DEGRADED", detail="; ".join(problems))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_BAD = {"ERROR", "EMPTY", "UNPROBED", "BLOCKED", "DEGRADED"}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vendor", action="append", default=[],
                    help="check only these vendors (repeatable)")
    ap.add_argument("--limit", type=int, default=5,
                    help="postings to pull per vendor (default 5)")
    ap.add_argument("--budget", type=float, default=90.0,
                    help="seconds allowed per vendor (default 90)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--verbose", action="store_true", help="include tracebacks")
    args = ap.parse_args(argv)

    vendors = args.vendor or registry.vendors()
    results = [check(v, limit=args.limit, budget=args.budget,
                     verbose=args.verbose) for v in vendors]

    if args.json:
        print(json.dumps({"results": results}, indent=2))
    else:
        print(f"{'VENDOR':<18}{'STATUS':<11}{'JOBS':>5}{'SECS':>8}  SAMPLE")
        print("-" * 100)
        for r in results:
            sample = r.get("sample_title") or r.get("detail") or ""
            print(f"{r['vendor']:<18}{r['status']:<11}{r['jobs']:>5}"
                  f"{r['elapsed']:>8.1f}  {sample[:52]}")
            if r["status"] in _BAD and r.get("detail") and r.get("sample_title"):
                print(f"{'':<42}  ! {r['detail'][:80]}")
            if args.verbose and r.get("traceback"):
                print(r["traceback"])
        ok = sum(1 for r in results if r["status"] == "OK")
        print("-" * 100)
        print(f"{ok}/{len(results)} vendors OK")

    return 0 if all(r["status"] == "OK" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
