#!/usr/bin/env python3
"""coverage_report.py — measure employer coverage against the ADR-004 targets.

This is the instrument for the loop in ADR-004 §5. Coverage is a measured number
with a target, not a claim, and "we added Oracle and iCIMS" is not evidence of
anything until this says so against live data.

    PYTHONPATH=shared/python python scripts/coverage_report.py
    PYTHONPATH=shared/python python scripts/coverage_report.py --json
    PYTHONPATH=shared/python python scripts/coverage_report.py --goal G1 --goal G2

Exit code is 0 when every measured goal meets its target and 1 otherwise, so it
can gate a runbook step or CI. Goals that cannot be measured (no Mongo, no live
credentials) are reported as UNKNOWN and do NOT count as passing - a metric that
did not run is not a metric that succeeded.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Goal:
    key: str
    title: str
    target: float
    unit: str = ""
    #: True when higher is better (the usual case).
    higher_is_better: bool = True
    value: Optional[float] = None
    detail: str = ""
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> str:
        if self.error or self.value is None:
            return "UNKNOWN"
        met = (self.value >= self.target if self.higher_is_better
               else self.value <= self.target)
        return "MET" if met else "MISS"

    def render(self) -> str:
        if self.value is None:
            shown = "—"
        elif self.unit == "%":
            shown = f"{self.value:.1f}%"
        else:
            shown = f"{self.value:,.0f}{self.unit}"
        target = (f"{self.target:.0f}%" if self.unit == "%"
                  else f"{self.target:,.0f}{self.unit}")
        mark = {"MET": "PASS", "MISS": "MISS", "UNKNOWN": "????"}[self.state]
        line = (f"  [{mark}] {self.key}  {self.title:<44} "
                f"{shown:>14} / {target:<12}")
        note = self.error or self.detail
        return f"{line}\n         {note}" if note else line


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------

def g1_oracle(goal: Goal) -> None:
    """ORC requisitions in inventory vs what the vendor says exists.

    The denominator is `TotalJobsCount` summed over validated ORC endpoints -
    the vendor's own count - rather than anything we derived, so this cannot be
    satisfied by crawling less and calling it complete.
    """
    db = _db()
    endpoints = list(db["company_ats_endpoints"].find(
        {"vendor": "oracle_orc", "status": "validated"},
        {"member_key": 1, "job_count_last": 1}))
    if not endpoints:
        goal.error = "no validated oracle_orc endpoints (run discovery/seed)"
        return
    expected = sum(int(e.get("job_count_last") or 0) for e in endpoints)
    have = db["job_postings"].count_documents(
        {"ats": "oracle_orc", "active": True})
    goal.extra = {"endpoints": len(endpoints), "expected": expected,
                  "ingested": have}
    if expected <= 0:
        goal.error = "endpoints report zero total reqs"
        return
    goal.value = 100.0 * have / expected
    goal.detail = f"{have:,} ingested / {expected:,} reported by {len(endpoints)} pods"


def g2_icims(goal: Goal) -> None:
    db = _db()
    eps = db["company_ats_endpoints"]
    validated = eps.count_documents({"vendor": "icims", "status": "validated"})
    blocked = eps.count_documents({"vendor": "icims", "status": "blocked"})
    postings = db["job_postings"].count_documents({"ats": "icims",
                                                   "active": True})
    goal.value = float(validated)
    goal.extra = {"validated": validated, "blocked": blocked,
                  "postings": postings}
    goal.detail = (f"{validated:,} validated endpoints, {postings:,} postings, "
                   f"{blocked:,} currently blocked")
    if postings == 0 and validated:
        goal.detail += "  (endpoints validated but nothing ingested yet)"


def g3_vendors(goal: Goal) -> None:
    from services.ats_providers import registry as ats_registry

    registered = ats_registry.vendors()
    goal.value = float(len(registered))
    goal.extra = {"registered": registered}
    try:
        rows = _db()["company_ats_endpoints"].aggregate([
            {"$match": {"status": "validated"}},
            {"$group": {"_id": "$vendor", "n": {"$sum": 1}}}])
        live = {str(r["_id"]): r["n"] for r in rows}
    except Exception:
        live = {}
    goal.detail = "registered: " + ", ".join(registered)
    if live:
        goal.detail += ("\n         validated in prod: "
                        + ", ".join(f"{k}={v:,}" for k, v in sorted(live.items())))


def g4_companies(goal: Goal) -> None:
    from services import company_registry_service as reg
    counts = reg.company_counts()
    goal.value = float(counts.get("total") or 0)
    top = sorted((counts.get("by_state") or {}).items(),
                 key=lambda kv: -kv[1])[:8]
    goal.extra = counts
    goal.detail = (f"{counts.get('with_domain', 0):,} with a resolved domain; "
                   f"top states: "
                   + ", ".join(f"{s}={n:,}" for s, n in top))


def g5_states(goal: Goal) -> None:
    try:
        from services.state_registries import base as sr_base
    except Exception as exc:
        goal.error = f"state_registries not available ({type(exc).__name__})"
        return
    adapters = list(sr_base.all_adapters())
    usable = [a for a in adapters if getattr(a, "enabled", False)
              and getattr(a, "method", "") != "unavailable"]
    goal.value = float(len(usable))
    by_method: Dict[str, int] = {}
    for a in adapters:
        by_method[a.method] = by_method.get(a.method, 0) + 1
    goal.extra = {"total": len(adapters), "usable": len(usable),
                  "by_method": by_method}
    goal.detail = (f"{len(adapters)} registered; "
                   + ", ".join(f"{k}={v}" for k, v in sorted(by_method.items())))


def g6_yield(goal: Goal) -> None:
    db = _db()
    probed = db["companies"].count_documents(
        {"discovery_status": {"$in": ["done", "no_portal"]}})
    with_ep = db["companies"].count_documents({"endpoints_found": {"$gt": 0}})
    goal.extra = {"probed": probed, "with_endpoint": with_ep}
    if probed <= 0:
        goal.error = "no companies probed yet"
        return
    goal.value = 100.0 * with_ep / probed
    goal.detail = f"{with_ep:,} of {probed:,} probed companies yielded a board"


def g7_health(goal: Goal) -> None:
    db = _db()
    eps = db["company_ats_endpoints"]
    total = eps.count_documents({"status": "validated"})
    if total <= 0:
        goal.error = "no validated endpoints"
        return
    cutoff = _now() - timedelta(days=7)
    fresh = eps.count_documents({"status": "validated",
                                 "last_seen_at": {"$gte": cutoff}})
    goal.value = 100.0 * fresh / total
    goal.extra = {"validated": total, "seen_7d": fresh}
    goal.detail = f"{fresh:,} of {total:,} validated endpoints seen in 7 days"


def g8_regression(goal: Goal) -> None:
    """Existing sources must not lose ground while new ones are added.

    Measured as postings ingested in the last 24h from the two ORIGINAL sources.
    A large refactor that quietly halves Workday throughput while adding Oracle
    is a net loss, and nothing else in this report would catch it.
    """
    db = _db()
    cutoff = _now() - timedelta(hours=24)
    n = db["job_postings"].count_documents({
        "source": {"$in": ["workday", "career-pages"]},
        "last_seen_at": {"$gte": cutoff}})
    goal.value = float(n)
    goal.detail = f"{n:,} legacy-source postings touched in the last 24h"
    if n == 0:
        goal.detail += "  (crawl may not be running — check EventBridge rules)"


GOALS: List[tuple] = [
    ("G1", "Oracle ORC requisitions ingested", 95.0, "%", g1_oracle),
    ("G2", "iCIMS validated endpoints", 500.0, "", g2_icims),
    ("G3", "ATS vendors registered", 12.0, "", g3_vendors),
    ("G4", "Companies in registry", 2_000_000.0, "", g4_companies),
    ("G5", "States with a working adapter", 51.0, "", g5_states),
    ("G6", "Discovery yield", 8.0, "%", g6_yield),
    ("G7", "Endpoint health (seen in 7d)", 90.0, "%", g7_health),
    ("G8", "No regression on legacy sources", 1.0, "", g8_regression),
]


def run(selected: Optional[List[str]] = None) -> List[Goal]:
    out: List[Goal] = []
    for key, title, target, unit, fn in GOALS:
        if selected and key not in selected:
            continue
        goal = Goal(key=key, title=title, target=target, unit=unit)
        try:
            fn(goal)
        except Exception as exc:
            goal.error = f"{type(exc).__name__}: {exc}"
        out.append(goal)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--goal", action="append", help="measure only these (repeatable)")
    args = ap.parse_args()

    goals = run(args.goal)

    if args.json:
        print(json.dumps([{
            "key": g.key, "title": g.title, "target": g.target, "unit": g.unit,
            "value": g.value, "state": g.state, "detail": g.detail,
            "error": g.error, "extra": g.extra,
        } for g in goals], indent=2, default=str))
    else:
        print("\n" + "=" * 92)
        print("  ADR-004 employer coverage  —  " +
              _now().strftime("%Y-%m-%d %H:%M UTC"))
        print("=" * 92)
        for g in goals:
            print(g.render())
        met = sum(1 for g in goals if g.state == "MET")
        miss = sum(1 for g in goals if g.state == "MISS")
        unknown = sum(1 for g in goals if g.state == "UNKNOWN")
        print("-" * 92)
        print(f"  {met} met · {miss} missed · {unknown} unmeasurable")
        if miss or unknown:
            worst = next((g for g in goals if g.state == "MISS"), None) or \
                next((g for g in goals if g.state == "UNKNOWN"), None)
            print(f"\n  Next: {worst.key} — {worst.title}")
            print(f"        {worst.error or worst.detail}")
        print("=" * 92 + "\n")

    return 0 if all(g.state == "MET" for g in goals) else 1


if __name__ == "__main__":
    raise SystemExit(main())
