#!/usr/bin/env python3
"""Seed company-intake candidates. Dry-run by default.

    PYTHONPATH=shared/python python scripts/seed_company_discovery_candidates.py
    PYTHONPATH=shared/python python scripts/seed_company_discovery_candidates.py --from-queue
    PYTHONPATH=shared/python python scripts/seed_company_discovery_candidates.py --from-queue --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--from-queue", action="store_true",
                    help="harvest uncovered tech employers that already have a domain")
    ap.add_argument("--researched", action="store_true",
                    help="seed the curated research batch (SSI, TML, …)")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--mix-days", type=int, default=30)
    args = ap.parse_args()

    # Default: researched batch, for backward compatibility with ops docs.
    if not args.from_queue and not args.researched:
        args.researched = True

    from services import company_discovery_intake as intake

    out = {"apply": args.apply}
    if args.researched:
        rows = [
            {"company_name": "Safe Superintelligence", "owner_domain": "ssi.inc"},
            {"company_name": "Thinking Machines Lab", "owner_domain": "thinkingmachines.ai"},
            {"company_name": "Periodic Labs", "owner_domain": "periodic.com"},
            {"company_name": "OpenEvidence", "owner_domain": "openevidence.com"},
        ]
        out["researched"] = {"would_upsert": rows}
        if args.apply:
            written = intake.seed_researched_intake(db=_db())
            out["researched"]["written"] = len(written)

    if args.from_queue:
        import coverage_work_queue as cwq
        built = cwq.build(_db(), mix_days=args.mix_days)
        harvest = intake.harvest_uncovered_from_queue(
            built["queue"], db=_db(), limit=args.limit, apply=args.apply)
        out["from_queue"] = harvest

    print(json.dumps(out, indent=2, default=str))
    if not args.apply:
        print("dry-run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
