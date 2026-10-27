#!/usr/bin/env python3
"""READ-ONLY: why does a user's job feed still search their OLD titles?

Reported 2026-10-26 for two accounts that changed resume and kept seeing the
old titles. The search titles have three server-side inputs, and each can keep
an old career alive after a new resume arrives:

  * every PARSED resume still in the library - the seed is their union, newest
    first, so an old resume that was never deleted keeps its titles in it;
  * the target roles declared at onboarding (`user_preferences.target_roles`),
    which LEAD the search input whatever the newest resume says;
  * the stored seed (`job_filter_preferences.title_seed`), refreshed on the
    next title-seed GET whenever the resume set's fingerprint moved.

This prints all three per user, plus the titles the product would search with
right now (`job_title_seed.search_titles_view`, the same function the
title-seed endpoint serves). The browser's own saved chip set lives in
localStorage and is not visible here; the frontend handles that side.

Usage (from a jobpilot-backend checkout or worktree):
    python3 scripts/title_sync_check.py --users tej,bhavya [--env ../jobpilot-backend/.env]
`--users` takes email PREFIXES. Nothing is written: the audit harness makes
every pymongo write raise.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _short(titles, n: int = 10) -> str:
    titles = [str(t) for t in (titles or []) if str(t).strip()]
    more = f" (+{len(titles) - n} more)" if len(titles) > n else ""
    return ", ".join(titles[:n]) + more if titles else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--users", required=True, help="comma-separated email prefixes")
    ap.add_argument("--env", default=str(ROOT / ".env"))
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(args.env)
    sys.path.insert(0, str(ROOT / "shared" / "python"))
    spec = importlib.util.spec_from_file_location(
        "audit", ROOT / "scripts" / "user_feed_fidelity_audit.py")
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)                     # installs the write guard

    from services import experience_view, fit_score, job_title_seed, resume_access

    db = audit._db()
    prefixes = [p.strip().lower() for p in args.users.split(",") if p.strip()]
    found = 0
    for user in db.users.find({}, {"email": 1, "active_resume_id": 1, "created_at": 1}):
        email = str(user.get("email") or "").lower()
        if not any(email.startswith(p) for p in prefixes):
            continue
        found += 1
        active = str(user.get("active_resume_id") or "")
        print(f"\n=== {audit._mask(email)}  (account created {user.get('created_at')})")

        all_assets = list(db.resume_assets.find(
            {"user_email": email},
            {"_id": 0, "resume_id": 1, "filename": 1, "created_at": 1, "parsed_at": 1,
             "parse_status": 1, "job_titles": 1}).sort("created_at", -1))
        print(f"resumes in library: {len(all_assets)}")
        for a in all_assets:
            mark = "ACTIVE " if a.get("resume_id") == active else "       "
            print(f"  {mark}{str(a.get('filename') or '?')[:40]:40} uploaded {a.get('created_at')} "
                  f"parse={a.get('parse_status')} parsed_at={a.get('parsed_at')}")
            print(f"         held titles: {_short(a.get('job_titles'), 6)}")

        parsed = resume_access.list_parsed_assets(email)
        targets = fit_score.load_target_roles(email) or []
        stored = job_title_seed.load_title_seed(email)
        view = job_title_seed.search_titles_view(email, parsed, stored)
        levels = (experience_view.for_user(email) or {}).get("levels")

        print(f"onboarding target roles (lead the search): {_short(targets)}")
        print(f"stored seed fingerprint {stored.get('fingerprint') or '-'}  "
              f"(current {view['fingerprint'] or '-'})  updated {stored.get('union_updated_at')}")
        print(f"union of held titles: {_short(view['resume_titles'])}")
        print(f"SEARCHES WITH NOW (match_titles): {_short(view['match_titles'], 40)}")
        print(f"experience levels: {levels}")

        flags = []
        if len(parsed) > 1:
            older = parsed[1:]
            newest = {t.lower() for t in (parsed[0].get("job_titles") or [])}
            only_old = sorted({t for a in older for t in (a.get("job_titles") or [])
                               if t.lower() not in newest})
            if only_old:
                flags.append(f"{len(older)} older parsed resume(s) still in the library keep "
                             f"their titles in the seed: {_short(only_old, 8)}")
        if targets:
            flags.append("onboarding target roles lead the search - stale if the seeker has "
                         "changed direction (Settings > Profile)")
        if stored.get("fingerprint") and stored.get("fingerprint") != view["fingerprint"]:
            flags.append("stored seed predates the current resume set; the next Jobs visit refreshes it")
        if active and parsed and parsed[0].get("resume_id") != active:
            flags.append("the ACTIVE resume is not the newest parsed one")
        print("FLAGS:" if flags else "FLAGS: none")
        for f in flags:
            print(f"  - {f}")

    if not found:
        print(f"no user matches {prefixes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
