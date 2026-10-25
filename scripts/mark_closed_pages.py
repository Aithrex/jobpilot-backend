#!/usr/bin/env python3
"""Mark postings whose OWN job page is closed or gone, from a legitimacy check.

DRY RUN by default; --apply writes. Reads the newest
`.agents/tasks/feed-fidelity/legitimacy_*.json` (scripts/feed_legitimacy_check.py)
and, for every card whose link verdict was `closed` or `dead`, stamps
`page_closed_at` on the posting. `posting_profile.availability` reads that as
closed for PAGE_CLOSED_DAYS, so the feed stops showing it even while the board
keeps LISTING it (a re-crawl does not clear the stamp; it ages out).

Why not `close_posting`: the board still lists these (Qualtrics, Entergy on
2026-10-25), so the next crawl would simply reopen a closed row.

Rollback: --rollback --apply unsets `page_closed_at` on rows this script stamped.
Usage:  PYTHONPATH=shared/python python3 scripts/mark_closed_pages.py [--json PATH] [--apply] [--rollback]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "shared" / "python"))
MARK = "legitimacy_check"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--json", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:  # pragma: no cover
        pass
    from utils.db_connect import DBConnect
    P = DBConnect().get_db().job_postings

    if args.rollback:
        n = P.count_documents({"page_closed_by": MARK})
        print(f"{'APPLY' if args.apply else 'dry-run'} rollback: {n} postings")
        if args.apply:
            P.update_many({"page_closed_by": MARK},
                          {"$unset": {"page_closed_at": "", "page_closed_by": "", "page_closed_why": ""}})
        return 0

    src = Path(args.json) if args.json else max(
        (ROOT / ".agents" / "tasks" / "feed-fidelity").glob("legitimacy_*.json"),
        key=lambda p: p.stat().st_mtime)
    report = json.loads(src.read_text())
    targets = {}
    for rec in report:
        for card in rec.get("detail") or []:
            if card.get("link") in ("closed", "dead"):
                key = card.get("job_id") or card.get("url")
                if key:
                    targets[key] = card
    print(f"from {src.name}: {len(targets)} postings with a closed or dead page")
    now = datetime.now(timezone.utc)
    marked = 0
    for key, card in targets.items():
        q = ({"job_id": key} if card.get("job_id") == key else
             {"$or": [{"canonical_url": key}, {"apply_url": key}, {"canonical_apply_url": key}]})
        doc = P.find_one({**q, "status": "active"}, {"_id": 1, "title_raw": 1, "employer_name": 1})
        if not doc:
            continue
        print(f"  {card['link']:6} {str(doc.get('employer_name'))[:24]:24} {str(doc.get('title_raw'))[:60]}")
        if args.apply:
            P.update_one({"_id": doc["_id"]}, {"$set": {
                "page_closed_at": now, "page_closed_by": MARK, "page_closed_why": card["link"]}})
        marked += 1
    print(("APPLIED: " if args.apply else "DRY RUN: would mark ") + str(marked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
