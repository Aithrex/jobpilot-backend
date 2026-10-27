#!/usr/bin/env python3
"""READ-ONLY: does every user's feed say Today / Yesterday right, in every US zone?

Reported 2026-10-26 from Phoenix: roles the employer's board showed as posted
today sat under "Since yesterday". Date-only postings are stored as midnight UTC
and were aged as INSTANTS - the previous evening anywhere west of UTC. The
per-user harness never saw it because it replays every feed at UTC (offset 0),
the one offset where the bug cannot appear.

For every user with a parsed resume this replays the For-you board through the
real pipeline in a US zone, then judges each card three ways in all seven US
zones (ET, CT, MT, AZ, PT, AK, HI, at today's real offsets):

  1. OLD vs NEW age: what moved between days, and that nothing ever moved OLDER
     (so no card can leave a window it used to be in).
  2. NEW vs the employer's OWN page date (JSON-LD datePosted / a visible
     "Posted on" date) - an independent reading, judged on the exact day and on
     the Today / Yesterday / older bucket a seeker actually sees.
  3. The served row: the pipeline's `days_ago` equals the NEW reading.

The database is read-only: the harness replaces every pymongo write method with
one that raises. The only network traffic is one polite GET per job page
(1.5 s per host), and none at all with --no-pages.

Usage (from a jobpilot-backend checkout or worktree):
    python3 scripts/recency_label_audit.py --env ../jobpilot-backend/.env
    python3 scripts/recency_label_audit.py --users bnb,san --pages 40
    python3 scripts/recency_label_audit.py --no-pages
Writes .agents/tasks/feed-fidelity/recency_<run>.json and prints the tables.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent

#: The zones a US seeker is in. Offsets are read for NOW, so DST is right.
ZONES = {
    "ET": "America/New_York", "CT": "America/Chicago", "MT": "America/Denver",
    "AZ": "America/Phoenix", "PT": "America/Los_Angeles",
    "AK": "America/Anchorage", "HI": "Pacific/Honolulu",
}
#: The zone the board is replayed in (where the bug was reported).
REPLAY_ZONE = "AZ"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bucket(days: Optional[int]) -> str:
    if days is None:
        return "unknown"
    return "today" if days <= 0 else "yesterday" if days == 1 else "older"


def _parse(iso: str) -> datetime:
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def old_days(iso: str, offset: int, now: datetime) -> int:
    """`workday_jobs_service._days_ago` as it was before 2026-10-26."""
    raw = iso.replace("Z", "+00:00")
    d = _parse(iso)
    off = timedelta(minutes=offset)
    posted = d.date() if len(raw) == 10 else (d.astimezone(timezone.utc) + off).date()
    return max(0, ((now + off).date() - posted).days)


def new_days(wjs, iso: str, offset: int, now: datetime) -> int:
    """The fixed `_days_ago`, evaluated at the audit's single `now`."""
    raw = iso.replace("Z", "+00:00")
    d = _parse(iso)
    off = timedelta(minutes=offset)
    if len(raw) == 10:
        posted = d.date()
    else:
        posted = (d.astimezone(timezone.utc) + off).date()
        cal = wjs._calendar_date_of(d)
        if cal is not None and cal > posted:
            posted = cal
    return max(0, ((now + off).date() - posted).days)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env", default=str(ROOT / ".env"),
                    help="the .env naming the database (a worktree has none)")
    ap.add_argument("--users", default="", help="comma-separated email prefixes")
    ap.add_argument("--pages", type=int, default=25,
                    help="employer pages to read per user (Today/Yesterday-eligible cards, best first)")
    ap.add_argument("--no-pages", action="store_true", help="skip the page check (no network)")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(args.env)
    sys.path.insert(0, str(ROOT / "shared" / "python"))
    audit = _load("user_feed_fidelity_audit")          # installs the write guard
    legit = None if args.no_pages else _load("feed_legitimacy_check")

    from services import experience_view, resume_access
    from services import workday_jobs_service as wjs
    if not hasattr(wjs, "_calendar_date_of"):
        print("this checkout predates the fix (no _calendar_date_of); run it from the fixed branch")
        return 2

    now = datetime.now(timezone.utc)
    offsets = {z: int(now.astimezone(ZoneInfo(n)).utcoffset().total_seconds() // 60)
               for z, n in ZONES.items()}
    local_today = {z: (now + timedelta(minutes=o)).date() for z, o in offsets.items()}
    prefixes = [p.strip().lower() for p in args.users.split(",") if p.strip()]
    db = audit._db()

    page_cache: Dict[str, Dict[str, Any]] = {}
    report: List[Dict[str, Any]] = []
    tot: Dict[str, collections.Counter] = {z: collections.Counter() for z in ZONES}
    tot_all = collections.Counter()

    print(f"now {now.isoformat(timespec='minutes')}  offsets {offsets}\n")
    for u in db.users.find({}, {"email": 1}).sort("created_at", 1):
        email = str(u.get("email") or "").lower()
        if prefixes and not any(email.startswith(p) for p in prefixes):
            continue
        if not resume_access.list_parsed_assets(email):
            continue
        seed = audit.board_titles(email)
        levels = list((experience_view.for_user(email) or {}).get("levels") or [])
        board = audit.replay_board(email, seed["match_titles"], levels,
                                   tz_offset_minutes=offsets[REPLAY_ZONE])
        rows = board["rows"]
        docs = {d["job_id"]: d for d in db.job_postings.find(
            {"job_id": {"$in": [r.get("job_id") for r in rows]}},
            {"_id": 0, "job_id": 1, "posted_at": 1, "first_seen_at": 1, "ats": 1})}

        z_counts: Dict[str, collections.Counter] = {z: collections.Counter() for z in ZONES}
        cards: List[Dict[str, Any]] = []
        c = collections.Counter(cards=len(rows))
        for r in rows:
            doc = docs.get(r.get("job_id")) or {}
            posted = doc.get("posted_at")
            if not isinstance(posted, datetime) or not r.get("date_posted"):
                c["undated"] += 1      # aged by first_seen_at, an instant: untouched by the fix
                continue
            iso = (posted if posted.tzinfo else posted.replace(tzinfo=timezone.utc)).isoformat()
            c["dated"] += 1
            encoded = wjs._calendar_date_of(_parse(iso)) is not None
            c["date_encoded"] += int(encoded)
            card = {"job_id": r.get("job_id"), "title": str(r.get("title") or "")[:80],
                    "company": r.get("company"), "ats": doc.get("ats"), "stored": iso,
                    "encoded": encoded, "score": r.get("match_score"),
                    "url": r.get("apply_link") or r.get("url"), "zones": {}}
            for z, off in offsets.items():
                o, n = old_days(iso, off, now), new_days(wjs, iso, off, now)
                card["zones"][z] = {"old": o, "new": n}
                zc = z_counts[z]
                zc[f"old_{_bucket(o)}"] += 1
                zc[f"new_{_bucket(n)}"] += 1
                if n != o:
                    zc["moved"] += 1
                if n > o:
                    zc["moved_older"] += 1   # must stay 0
            served = r.get("days_ago")
            if served != card["zones"][REPLAY_ZONE]["new"]:
                c["served_mismatch"] += 1    # must stay 0 (pipeline == fixed rule)
            if wjs._days_ago(iso, offsets[REPLAY_ZONE]) != card["zones"][REPLAY_ZONE]["new"]:
                c["function_mismatch"] += 1  # must stay 0 unless the run crossed midnight
            cards.append(card)

        # Independent truth: the employer's own page, for the cards a seeker
        # sees first among those Today / Yesterday could hold.
        if legit is not None:
            near = [k for k in cards
                    if min(min(v["old"], v["new"]) for v in k["zones"].values()) <= 2 and k["url"]]
            near.sort(key=lambda k: -(k["score"] or 0))
            todo = [k for k in near[: args.pages] if k["url"] not in page_cache]
            with cf.ThreadPoolExecutor(8) as pool:
                for k, res in zip(todo, pool.map(lambda k: legit.check_link(k["url"]), todo)):
                    page_cache[k["url"]] = res
            for k in near[: args.pages]:
                page = (page_cache.get(k["url"]) or {}).get("page_posted")
                if not page:
                    c["page_no_date"] += 1
                    continue
                c["page_checked"] += 1
                p_date = date.fromisoformat(page)
                stored_date = _parse(k["stored"]).astimezone(timezone.utc).date()
                k["page_posted"] = page
                if abs((p_date - stored_date).days) > 1:
                    # The page shows a different publication than we hold
                    # (a repost keeps its ORIGINAL date by design, ADR-001) -
                    # not a timezone question, reported apart.
                    c["page_other_date"] += 1
                    k["page_other_date"] = True
                    continue
                for z in ZONES:
                    truth = max(0, (local_today[z] - p_date).days)
                    v = k["zones"][z]
                    zc = z_counts[z]
                    zc["page_cards"] += 1
                    zc["old_day_ok"] += int(v["old"] == truth)
                    zc["new_day_ok"] += int(v["new"] == truth)
                    zc["old_bucket_ok"] += int(_bucket(v["old"]) == _bucket(truth))
                    zc["new_bucket_ok"] += int(_bucket(v["new"]) == _bucket(truth))
                    v["truth"] = truth

        user = audit._mask(email)
        az = z_counts[REPLAY_ZONE]
        print(f"{user:10} cards {c['cards']:4} dated {c['dated']:4} | {REPLAY_ZONE} Today "
              f"{az['old_today']:3}->{az['new_today']:3}  Yesterday {az['old_yesterday']:3}->"
              f"{az['new_yesterday']:3}  moved {az['moved']:3}  older {az['moved_older']} | "
              f"page {az['page_cards']:2}: bucket ok {az['old_bucket_ok']:2}->{az['new_bucket_ok']:2}"
              f"  served_mismatch {c['served_mismatch']}", flush=True)
        for z in ZONES:
            tot[z].update(z_counts[z])
        tot_all.update(c)
        report.append({"user": user, "levels": levels, "counts": dict(c),
                       "zones": {z: dict(v) for z, v in z_counts.items()}, "cards": cards})

    # Inventory-wide: how much of the fresh market is date-encoded, by vendor.
    inv = collections.Counter()
    by_ats = collections.Counter()
    for d in db.job_postings.find(
            {"status": "active", "posted_at": {"$gte": now - timedelta(days=3)}},
            {"_id": 0, "posted_at": 1, "ats": 1}):
        p = d.get("posted_at")
        if not isinstance(p, datetime):
            continue
        p = p if p.tzinfo else p.replace(tzinfo=timezone.utc)
        inv["fresh_dated"] += 1
        if wjs._calendar_date_of(p) is not None:
            inv["date_encoded"] += 1
            by_ats[str(d.get("ats") or "?")] += 1

    print("\nALL USERS", dict(tot_all))
    print(f"\n{'zone':4} {'off':>5} {'Today old->new':>15} {'Yest old->new':>14} {'moved':>6} "
          f"{'older':>5} {'page':>5} {'day ok old->new':>16} {'bucket ok old->new':>19}")
    for z in ZONES:
        t = tot[z]
        print(f"{z:4} {offsets[z]:>5} {t['old_today']:>7}->{t['new_today']:<7} "
              f"{t['old_yesterday']:>6}->{t['new_yesterday']:<7} {t['moved']:>6} {t['moved_older']:>5} "
              f"{t['page_cards']:>5} {t['old_day_ok']:>8}->{t['new_day_ok']:<7} "
              f"{t['old_bucket_ok']:>9}->{t['new_bucket_ok']:<9}")
    print(f"\ninventory, active postings dated in the last 3 days: {inv['fresh_dated']}, "
          f"of which date-encoded (aged wrongly west of UTC before the fix): {inv['date_encoded']}")
    print("  by vendor:", dict(by_ats.most_common(15)))

    run = now.strftime("%Y%m%dT%H%M%SZ")
    out = audit.OUT_DIR / f"recency_{run}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"now": now.isoformat(), "offsets": offsets,
                               "totals": {z: dict(v) for z, v in tot.items()},
                               "all": dict(tot_all), "inventory": dict(inv),
                               "inventory_by_ats": dict(by_ats), "users": report},
                              indent=1, default=str))
    print("wrote", out)
    bad = sum(tot[z]["moved_older"] for z in ZONES) + tot_all["served_mismatch"]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
