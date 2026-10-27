#!/usr/bin/env python3
"""READ-ONLY: every user's search titles, today's code against this branch.

Reported 2026-10-27: a seeker's titles went from full-stack + cloud to
cloud-only, with "Polyglot Engineer" among them. For every user with a parsed
resume this computes the titles the feed searches with (`search_titles_view`,
the function the title-seed endpoint serves) twice - with `job_title_seed` as
it is on `--baseline` (default `main`) and as it is in this checkout - and
reports, per user:

  * the specialties the resumes evidence, strongest first;
  * every title added and removed;
  * evidenced specialties with NO title in the list (a "gap");
  * spellings nobody posts under ("junk").

The goal it checks: no seeker loses a real title, every evidenced specialty
is searched, no junk. Nothing is written - the audit harness makes every
pymongo write raise.

Usage (from a jobpilot-backend checkout or worktree):
    python3 scripts/title_breadth_audit.py --env ../jobpilot-backend/.env [--users a,b]
Writes .agents/tasks/feed-fidelity/title_breadth_<run>.json.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JUNK = {"polyglot engineer", "software craftsperson", "mts software", "software engr",
        "development engineer software", "product engineer"}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--users", default="", help="comma-separated email prefixes (default: all)")
    ap.add_argument("--baseline", default="main", help="git ref holding today's code")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(args.env)
    sys.path.insert(0, str(ROOT / "shared" / "python"))
    audit = _load_module("audit", ROOT / "scripts" / "user_feed_fidelity_audit.py")  # write guard

    from services import fit_score, job_title_seed as new_seed, resume_access
    from services import tech_taxonomy as tx

    src = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"{args.baseline}:shared/python/services/job_title_seed.py"],
        check=True, capture_output=True, text=True).stdout
    tmp = Path(tempfile.mkdtemp()) / "job_title_seed_baseline.py"
    tmp.write_text(src)
    old_seed = _load_module("job_title_seed_baseline", tmp)

    def gaps(titles, specs):
        low = {t.lower() for t in titles}
        return [s for s in specs
                if tx.title_phrases(specialties=[s])
                and not low & {p.lower() for p in tx.title_phrases(specialties=[s])}]

    db = audit._db()
    prefixes = [p.strip().lower() for p in args.users.split(",") if p.strip()]
    report, totals = [], {"users": 0, "lost_real_title": 0, "gaps_before": 0, "gaps_after": 0,
                          "junk_before": 0, "junk_after": 0, "changed": 0}
    for user in db.users.find({}, {"email": 1}).sort("created_at", 1):
        email = str(user.get("email") or "").lower()
        if prefixes and not any(email.startswith(p) for p in prefixes):
            continue
        assets = resume_access.list_parsed_assets(email)
        if not assets:
            continue
        stored = new_seed.load_title_seed(email)
        before = old_seed.search_titles_view(email, assets, stored)["match_titles"]
        after = new_seed.search_titles_view(email, assets, stored)["match_titles"]
        specs = []
        for entry in fit_score.load_profiles_for_user(email) or []:
            for s in ((entry.get("profile") or {}).get("tech") or {}).get("role_specialties") or []:
                if s not in specs:
                    specs.append(s)
        added = [t for t in after if t not in before]
        removed = [t for t in before if t not in after]
        # Lost = no spelling of it is searched any more ("Back-End Engineer"
        # going while "Back End Engineer" stays is the same search).
        key = getattr(new_seed, "_spelling_key", lambda t: re.sub(r"[\s\-]+", " ", t.lower()).strip())
        kept_keys = {key(t) for t in after}
        real_lost = [t for t in removed if t.lower() not in JUNK and key(t) not in kept_keys]
        g_before, g_after = gaps(before, specs), gaps(after, specs)
        j_before = [t for t in before if t.lower() in JUNK]
        j_after = [t for t in after if t.lower() in JUNK]
        rec = {"user": audit._mask(email), "resumes": len(assets), "specialties": specs,
               "real_lost": real_lost,
               "before": before, "after": after, "added": added, "removed": removed,
               "gaps_before": g_before, "gaps_after": g_after,
               "junk_before": j_before, "junk_after": j_after}
        report.append(rec)
        totals["users"] += 1
        totals["changed"] += int(before != after)
        totals["lost_real_title"] += int(bool(real_lost))
        totals["gaps_before"] += int(bool(g_before))
        totals["gaps_after"] += int(bool(g_after))
        totals["junk_before"] += int(bool(j_before))
        totals["junk_after"] += int(bool(j_after))
        print(f"\n=== {rec['user']}  resumes={len(assets)}  {len(before)} -> {len(after)} titles")
        print(f"  evidences: {', '.join(specs) or '-'}")
        print(f"  + {', '.join(added) or '-'}")
        print(f"  - {', '.join(removed) or '-'}")
        if real_lost:
            print(f"  LOST (no spelling kept): {', '.join(real_lost)}")
        if g_before or g_after:
            print(f"  specialties with no title: before {g_before or '-'}  after {g_after or '-'}")
        print(f"  searches now: {', '.join(after)}")

    print("\nTOTALS", json.dumps(totals))
    print("  users who would lose a real (non-junk) title:", totals["lost_real_title"])
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = audit.OUT_DIR / f"title_breadth_{run}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"totals": totals, "users": report}, indent=1, default=str))
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
