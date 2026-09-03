#!/usr/bin/env python3
"""coverage_campaign.py — measure and plan the path to 100% tech coverage.

DRY-RUN BY DEFAULT.

"100%" = covered ∪ evidenced-terminal over the live evidence-backed technology
employer denominator. This is not a claim that every technology company on
Earth is in the crawl.

    PYTHONPATH=shared/python python scripts/coverage_campaign.py
    PYTHONPATH=shared/python python scripts/coverage_campaign.py --json /tmp/campaign.json
    PYTHONPATH=shared/python python scripts/coverage_campaign.py --execute-dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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


def _print_progress(progress: Dict[str, Any]) -> None:
    print(progress.get("honesty") or "")
    print()
    print(f"measured_at                 {progress['measured_at']}")
    print(f"tech denominator            {progress['technology_employer_denominator']:,}")
    print(f"covered                     {progress['covered_technology_employers']:,}  "
          f"({progress['employer_coverage_pct']}%)")
    print(f"uncovered                   {progress['uncovered_technology_employers']:,}")
    print(f"evidenced-terminal (uncov.) {progress['evidenced_terminal_uncovered']:,}")
    print(f"resolved toward 100%        {progress['resolved_toward_100']:,}  "
          f"({progress['measured_100_pct']}%)")
    print(f"gate employer 100%          {progress['gate_employer_100']}")
    print(f"pending+unproven employers  {progress['pending_unproven_employers']:,}")
    print(f"pending+unproven core jobs  {progress['pending_unproven_core_tech_jobs']:,}")
    print(f"gate pending/unproven clear {progress['gate_pending_unproven_cleared']}")
    print()
    print("buckets:")
    for bucket, stats in (progress.get("by_bucket") or {}).items():
        print(f"  {bucket:22} employers={stats['employers']:5}  "
              f"core={stats['core_tech_postings']:6}")
    print()
    print("top priority:")
    for row in progress.get("top_priority") or []:
        mark = "T" if row.get("terminal") else "*"
        print(f"  {mark} {row.get('display_name') or '?':28} "
              f"{row['bucket']:20} core={row['core_tech_postings']:4}  "
              f"vendors={','.join(row.get('vendors') or []) or '-'}")


def _run_verify_dry(step: Dict[str, Any], *, workers: int) -> int:
    cmd = [
        sys.executable, str(ROOT / "scripts" / "verify_career_sources.py"),
        "--mode", step["mode"],
        "--order", "tech-yield",
        "--limit", str(step["employer_count"]),
        "--workers", str(workers),
    ]
    for eid in step.get("employer_ids") or []:
        cmd.extend(["--employer-id", eid])
    print("\n$ " + " ".join(cmd[:8]) + f" ... ({step['employer_count']} employer ids)")
    return subprocess.call(cmd, cwd=str(ROOT), env={**os.environ, "PYTHONPATH": "shared/python"})


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--mix-days", type=int, default=30)
    ap.add_argument("--limit-per-step", type=int, default=200)
    ap.add_argument("--json", default=None)
    ap.add_argument("--execute-dry-run", action="store_true",
                    help="run the first verify step without --apply")
    ap.add_argument("--execute-all-dry-run", action="store_true",
                    help="run every planned verify step without --apply")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--apply", action="store_true",
                    help="REFUSED unless --i-authorize-live-writes is also set")
    ap.add_argument("--i-authorize-live-writes", action="store_true",
                    help="operator acknowledgment required for any --apply")
    args = ap.parse_args()

    if args.apply and not args.i_authorize_live_writes:
        print("refusing --apply without --i-authorize-live-writes", file=sys.stderr)
        return 2

    from services import coverage_campaign as camp

    snap = camp.campaign_snapshot(
        _db(), mix_days=args.mix_days, limit_per_step=args.limit_per_step)
    progress = snap["progress"]
    # surface honesty on the progress blob for printing
    progress = {**progress, "honesty": snap["honesty"]}
    _print_progress(progress)

    print("planned verify steps:")
    for i, step in enumerate(snap["verify_steps"], 1):
        print(f"  {i}. [{step['mode']}] {step['bucket']}  "
              f"n={step['employer_count']}  — {step['why']}")
        print(f"     dry:   {step['command_dry_run']}")
        print(f"     apply: {step['command_apply']}")

    print("\nnext operator actions:")
    for a in snap["next_operator_actions"]:
        print(f"  - {a}")

    if args.json:
        Path(args.json).write_text(json.dumps(snap, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")

    if args.apply and args.i_authorize_live_writes:
        print("\n--apply orchestration is not inlined here. Run the printed "
              "apply commands one mode at a time after reviewing dry-run output.",
              file=sys.stderr)
        return 3

    steps: List[Dict[str, Any]] = list(snap["verify_steps"])
    if args.execute_dry_run and steps:
        return _run_verify_dry(steps[0], workers=args.workers)
    if args.execute_all_dry_run:
        rc = 0
        for step in steps:
            rc = _run_verify_dry(step, workers=args.workers) or rc
        return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
