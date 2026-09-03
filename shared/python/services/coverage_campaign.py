"""Measured path toward 100% evidence-backed technology-employer coverage.

"100%" is NOT every tech company on Earth. It is:

  covered ∪ evidenced_terminal == technology_employer_denominator

where ``evidenced_terminal`` means blocked / dead / moved / marketplace /
ownership-conflict quarantine with recorded evidence — never a silent skip.

This module only *plans and measures*. Writes go through existing scripts
(``verify_career_sources``, registry backfill, intake seed) and stay dry-run
unless an operator passes ``--apply``.
"""
from __future__ import annotations

import collections
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _ROOT / "scripts"
for _p in (str(_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: Source states that count as "honestly done" for the 100% gate even when
#: the employer is not in COVERED_STATES. Keep tight — guessing is not terminal.
TERMINAL_FAILURE_CODES = frozenset({
    "blocked",
    "dead",
    "moved",
    "empty",
    "marketplace",
    "staffing",
    "shared_board",
    "ownership_conflict",
    "legally_unusable",
    "robots_disallowed",
})

VERIFY_PLAN: Tuple[Dict[str, Any], ...] = (
    {
        "bucket": "content_pending",
        "mode": "content",
        "why": "Ownership already proven; content validation is the cheapest coverage.",
    },
    {
        "bucket": "endpoint_unproven",
        "mode": "employers",
        "why": "Board known; need domain walk / ownership evidence.",
    },
    {
        "bucket": "endpoint_unproven",
        "mode": "backlink",
        "why": "One-signal-away boards often close via employer-domain backlink.",
    },
    {
        "bucket": "source_no_endpoint",
        "mode": "employers",
        "why": "Fingerprint careers page before an endpoint exists (Salesforce, Google, …).",
    },
    {
        "bucket": "no_source",
        "mode": "employers",
        "why": "Real discovery; thinnest yield — still required for 100% of the denominator.",
    },
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pct(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return round(100.0 * num / den, 2)


def terminal_employer_ids(db) -> set:
    """Employers whose only remaining sources are evidenced-terminal."""
    from services import career_source_service as css

    by_emp: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for src in db[css.CAREER_SOURCES].find(
            {}, {"_id": 0, "employer_id": 1, "state": 1, "failure_code": 1}):
        by_emp[str(src.get("employer_id") or "")].append(src)

    out: set = set()
    for eid, srcs in by_emp.items():
        if not eid or not srcs:
            continue
        if any((s.get("state") in css.COVERED_STATES) for s in srcs):
            continue
        # Every source must carry a terminal failure, or we are still working.
        if all(str(s.get("failure_code") or "") in TERMINAL_FAILURE_CODES
               for s in srcs):
            out.add(eid)
    return out


def measure(db, *, mix_days: int = 30) -> Dict[str, Any]:
    """Fresh progress against the 100% measured gate."""
    import coverage_work_queue as cwq
    from services import career_source_service as css

    built = cwq.build(db, mix_days=mix_days)
    queue: List[Dict[str, Any]] = built["queue"]
    by_bucket = cwq.summarize(queue)
    terminal = terminal_employer_ids(db)

    uncovered = len(queue)
    covered = int(built["covered"])
    denominator = int(built["denominator"])
    terminal_among_uncovered = sum(
        1 for row in queue if row["employer_id"] in terminal)

    # Gate numerator: covered + uncovered rows that are evidenced-terminal.
    resolved = covered + terminal_among_uncovered
    pending_unproven = [
        row for row in queue
        if row["bucket"] in ("content_pending", "endpoint_unproven")
        and row["employer_id"] not in terminal
    ]
    pending_unproven_core = sum(r["core_tech_postings"] for r in pending_unproven)

    return {
        "measured_at": _now().isoformat(),
        "technology_employer_denominator": denominator,
        "covered_technology_employers": covered,
        "uncovered_technology_employers": uncovered,
        "evidenced_terminal_uncovered": terminal_among_uncovered,
        "resolved_toward_100": resolved,
        "employer_coverage_pct": _pct(covered, denominator),
        "measured_100_pct": _pct(resolved, denominator),
        "gate_employer_100": resolved >= denominator and denominator > 0,
        "pending_unproven_employers": len(pending_unproven),
        "pending_unproven_core_tech_jobs": pending_unproven_core,
        "gate_pending_unproven_cleared": len(pending_unproven) == 0,
        "by_bucket": by_bucket,
        "top_priority": [
            {
                "employer_id": r["employer_id"],
                "display_name": r["display_name"],
                "domain": r["domain"],
                "bucket": r["bucket"],
                "core_tech_postings": r["core_tech_postings"],
                "vendors": r["vendors"],
                "terminal": r["employer_id"] in terminal,
            }
            for r in queue[:40]
        ],
        "covered_states": sorted(css.COVERED_STATES),
        "terminal_failure_codes": sorted(TERMINAL_FAILURE_CODES),
    }


def plan_verify_steps(
    queue: Sequence[Dict[str, Any]],
    *,
    limit_per_step: int = 200,
    terminal_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Ordered verify invocations that drain the posting-weighted gap."""
    terminal_ids = terminal_ids or set()
    steps: List[Dict[str, Any]] = []
    for spec in VERIFY_PLAN:
        ids = [
            r["employer_id"] for r in queue
            if r["bucket"] == spec["bucket"]
            and r["employer_id"] not in terminal_ids
        ][:limit_per_step]
        if not ids:
            continue
        steps.append({
            "bucket": spec["bucket"],
            "mode": spec["mode"],
            "why": spec["why"],
            "employer_count": len(ids),
            "employer_ids": ids,
            "command_dry_run": (
                "PYTHONPATH=shared/python python scripts/verify_career_sources.py "
                f"--mode {spec['mode']} --order tech-yield --limit {len(ids)}"
            ),
            "command_apply": (
                "PYTHONPATH=shared/python python scripts/verify_career_sources.py "
                f"--apply --mode {spec['mode']} --order tech-yield --limit {len(ids)}"
            ),
        })
    return steps


def campaign_snapshot(db, *, mix_days: int = 30,
                      limit_per_step: int = 200) -> Dict[str, Any]:
    import coverage_work_queue as cwq

    progress = measure(db, mix_days=mix_days)
    built = cwq.build(db, mix_days=mix_days)
    terminal = terminal_employer_ids(db)
    steps = plan_verify_steps(
        built["queue"], limit_per_step=limit_per_step, terminal_ids=terminal)
    return {
        "progress": progress,
        "verify_steps": steps,
        "next_operator_actions": _next_actions(progress, steps),
        "honesty": (
            "100% is covered UNION evidenced-terminal over the live "
            "evidence-backed technology denominator. It is not a claim to "
            "crawl every tech company on Earth."
        ),
    }


def _next_actions(progress: Dict[str, Any],
                  steps: Sequence[Dict[str, Any]]) -> List[str]:
    actions: List[str] = []
    if progress.get("gate_employer_100") and progress.get(
            "gate_pending_unproven_cleared"):
        actions.append(
            "Gates met on this measurement. Persist coverage snapshot only "
            "with authorization: "
            "python scripts/employer_coverage_report.py --markdown --persist")
        return actions
    if steps:
        first = steps[0]
        actions.append(
            f"Dry-run first: {first['command_dry_run']}")
        actions.append(
            f"After operator authorization: {first['command_apply']}")
    actions.append(
        "Harvest uncovered domains into intake (dry-run): "
        "python scripts/seed_company_discovery_candidates.py --from-queue")
    actions.append(
        "Registry rows missing from live employers (dry-run): "
        "python scripts/backfill_employer_registry.py")
    return actions
