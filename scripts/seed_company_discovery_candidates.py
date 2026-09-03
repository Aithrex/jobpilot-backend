#!/usr/bin/env python3
"""Seed researched company-intake candidates. Dry-run by default."""
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
    args = ap.parse_args()
    from services import company_discovery_intake as intake
    rows = [
        {"company_name": "Safe Superintelligence", "owner_domain": "ssi.inc"},
        {"company_name": "Thinking Machines Lab", "owner_domain": "thinkingmachines.ai"},
        {"company_name": "Periodic Labs", "owner_domain": "periodic.com"},
        {"company_name": "OpenEvidence", "owner_domain": "openevidence.com"},
    ]
    print(json.dumps({"would_upsert": rows, "apply": args.apply}, indent=2))
    if not args.apply:
        print("dry-run: nothing written (pass --apply)")
        return 0
    written = intake.seed_researched_intake(db=_db())
    print(f"wrote {len(written)} candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
