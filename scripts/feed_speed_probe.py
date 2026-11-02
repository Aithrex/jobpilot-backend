"""Read-only probe: how long the For-you board takes, and how much it shows.

Answers two questions with production evidence instead of a guess:

1. What did real users get? Every board scan in `resume_jobs` (the last
   48 hours): how long it took, whether it finished, where it was served from
   (inventory or the live crawl), how many jobs it returned, and every scan that
   never finished at all.
2. What does every user get right now? For each account with a parsed resume,
   replay the two inventory tabs exactly as the board requests them, timing
   each one and measuring its peak memory, then apply the board's default view
   (Today, own levels, sponsor-friendly) and count what is left.

It also prints the crawl's intake for the last 7 days, so a drop in new
postings shows up next to the feed numbers.

READ-ONLY, enforced: every pymongo write method raises before anything else is
imported. Emails are masked in the output.

Run it inside the app image on the box, where the real settings and code are:

    docker exec -i <worker container> python - < scripts/feed_speed_probe.py

or from a checkout with a `.env`: `python scripts/feed_speed_probe.py --env PATH`.
Pass `--no-replay` for the observed half only, `--users a,b` to replay some.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

for candidate in ("/app/shared/python", str(Path.cwd() / "shared" / "python")):
    if os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)

ap = argparse.ArgumentParser()
ap.add_argument("--env", default="", help="a .env to load (off the box only)")
ap.add_argument("--hours", type=int, default=48)
ap.add_argument("--no-replay", action="store_true")
ap.add_argument("--users", default="", help="comma-separated email prefixes")
ap.add_argument("--tz", type=int, default=-300, help="viewer offset, minutes (US Eastern)")
args = ap.parse_args()

if args.env:
    from dotenv import load_dotenv
    load_dotenv(args.env)

import pymongo.collection as _pc  # noqa: E402

for _m in ("insert_one", "insert_many", "update_one", "update_many", "replace_one",
           "delete_one", "delete_many", "find_one_and_update", "find_one_and_replace",
           "find_one_and_delete", "bulk_write", "create_index", "create_indexes",
           "drop", "drop_index"):
    def _deny(self, *a, _m=_m, **k):
        raise RuntimeError(f"read-only probe: refused {_m} on {self.name}")
    setattr(_pc.Collection, _m, _deny)

from utils.db_connect import DBConnect  # noqa: E402

db = DBConnect().get_db()
NOW = datetime.now(timezone.utc)
SCAN_TYPES = ("workday_jobs", "career_pages", "mnc_jobs", "tech_feed_search")


def mask(email: str) -> str:
    local = str(email or "").split("@", 1)[0]
    return (local[:3] + "***") if local else "***"


def aware(d: Any) -> Optional[datetime]:
    if not isinstance(d, datetime):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def pct(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))], 1)


def section(title: str) -> None:
    print(f"\n=== {title}")


# --------------------------------------------------------------------------
# 1. What users actually got
# --------------------------------------------------------------------------
section(f"Observed board scans, last {args.hours}h (resume_jobs)")
since = NOW - timedelta(hours=args.hours)
rows = list(db.resume_jobs.find(
    {"job_type": {"$in": list(SCAN_TYPES)}, "created_at": {"$gte": since}},
    {"job_type": 1, "status": 1, "created_at": 1, "completed_at": 1, "user_email": 1,
     "error": 1, "result.total": 1, "result.served_from": 1, "result.tenants_done": 1,
     "result.tenants_total": 1, "result.errors": {"$slice": 3},
     "result.diagnostics.candidates": 1, "result.diagnostics.task_errors": 1},
))
# Sorted here, not by Mongo: `created_at` is unindexed and the server would
# sort whole job documents (results up to 4.5 MB each) in its 32 MB budget.
rows.sort(key=lambda r: aware(r.get("created_at")) or NOW)
print(f"{len(rows)} scans")
per_user: Dict[str, Dict[str, Any]] = {}
for jt in SCAN_TYPES:
    mine = [r for r in rows if r.get("job_type") == jt]
    if not mine:
        continue
    by_status: Dict[str, int] = {}
    for r in mine:
        by_status[r.get("status") or "?"] = by_status.get(r.get("status") or "?", 0) + 1
    done = [r for r in mine if r.get("status") == "completed" and aware(r.get("completed_at"))]
    secs = [(aware(r["completed_at"]) - aware(r["created_at"])).total_seconds() for r in done]
    stuck = [r for r in mine if r.get("status") == "processing"
             and (NOW - aware(r["created_at"])).total_seconds() > 900]
    served: Dict[str, int] = {}
    totals: List[int] = []
    for r in done:
        res = r.get("result") or {}
        key = res.get("served_from") or "crawl"
        served[key] = served.get(key, 0) + 1
        if isinstance(res.get("total"), int):
            totals.append(res["total"])
    print(f"\n{jt}: {len(mine)} scans  status={by_status}")
    print(f"  seconds to finish: p50={pct(secs, .5)} p90={pct(secs, .9)} max={pct(secs, 1)}")
    print(f"  never finished (processing > 15 min): {len(stuck)}")
    print(f"  served from: {served}")
    print(f"  jobs returned: p50={pct(totals, .5)} min={min(totals) if totals else None}"
          f" max={max(totals) if totals else None}")
    errs = [str(e)[:120] for r in mine for e in ((r.get("result") or {}).get("errors") or [])]
    errs += [str(r.get("error"))[:120] for r in mine if r.get("error")]
    if errs:
        common: Dict[str, int] = {}
        for e in errs:
            common[e] = common.get(e, 0) + 1
        for e, n in sorted(common.items(), key=lambda kv: -kv[1])[:5]:
            print(f"  error x{n}: {e}")
    for r in mine:
        u = per_user.setdefault(mask(r.get("user_email")), {})
        took = None
        if aware(r.get("completed_at")):
            took = round((aware(r["completed_at"]) - aware(r["created_at"])).total_seconds())
        u.setdefault(jt, []).append(
            (r.get("status"), took, (r.get("result") or {}).get("total"),
             (r.get("result") or {}).get("served_from") or "-"))

section("Per user: scans (status, seconds, jobs, served_from), newest last")
for user, by_type in sorted(per_user.items()):
    parts = []
    for jt, scans in by_type.items():
        last = scans[-1]
        parts.append(f"{jt.split('_')[0]} n={len(scans)} last={last}")
    print(f"{user:10s} " + " | ".join(parts))

# Hourly arrival of scans and how many never finished, to line up with the move.
section("Scans per hour (UTC) and how many never finished")
hours: Dict[str, List[int]] = {}
for r in rows:
    h = aware(r["created_at"]).strftime("%m-%d %H:00")
    slot = hours.setdefault(h, [0, 0])
    slot[0] += 1
    if r.get("status") == "processing" and (NOW - aware(r["created_at"])).total_seconds() > 900:
        slot[1] += 1
for h in sorted(hours):
    print(f"{h}  scans={hours[h][0]:3d}  never_finished={hours[h][1]}")

# --------------------------------------------------------------------------
# 2. Crawl intake, last 7 days
# --------------------------------------------------------------------------
section("Crawl intake: postings first seen per day (UTC), and active total")
day0 = (NOW - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
for row in db.job_postings.aggregate([
        {"$match": {"first_seen_at": {"$gte": day0}}},
        {"$group": {"_id": {"$dateToString": {"format": "%m-%d", "date": "$first_seen_at"}},
                    "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}}]):
    print(f"{row['_id']}  first_seen={row['n']}")
print("active:", db.job_postings.count_documents({"status": "active"}))
hb = db.ingest_runner_heartbeat.find_one(sort=[("_id", -1)]) if "ingest_runner_heartbeat" in db.list_collection_names() else None
if hb:
    print("crawler heartbeat:", {k: hb.get(k) for k in ("updated_at", "at", "cycle", "maintenance") if k in hb})

if args.no_replay:
    sys.exit(0)

# --------------------------------------------------------------------------
# 3. Replay every user's board now
# --------------------------------------------------------------------------
from services import experience_view, inventory_feed, job_title_seed, resume_access  # noqa: E402
from services.experience_view import _VIEW_OF_BAND  # noqa: E402


def rss_kb(field: str) -> int:
    try:
        for line in open("/proc/self/status"):
            if line.startswith(field + ":"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def reset_peak() -> None:
    try:
        with open("/proc/self/clear_refs", "w") as fh:
            fh.write("5")
    except OSError:
        pass


def in_levels(row: Dict[str, Any], levels: List[str]) -> bool:
    band = str(row.get("seniority") or row.get("seniority_band") or "unknown").lower()
    if not levels or band in ("", "unknown"):
        return True
    return _VIEW_OF_BAND.get(band, "mid") in levels


section(f"Replay: every user's board now (viewer offset {args.tz} min)")
print("user       titles  workday(s,MB,rows)        careers(s,MB,rows)        merged  today  3d  7d  30d")
prefixes = [p.strip().lower() for p in args.users.split(",") if p.strip()]
times: List[float] = []
peaks: List[float] = []
for user in db.users.find({}, {"email": 1}).sort("created_at", 1):
    email = str(user.get("email") or "")
    if not email or (prefixes and not any(email.lower().startswith(p) for p in prefixes)):
        continue
    assets = resume_access.list_parsed_assets(email)
    if not assets:
        continue
    view = job_title_seed.search_titles_view(email, assets, job_title_seed.load_title_seed(email))
    titles = view["match_titles"]
    levels = list((experience_view.for_user(email) or {}).get("levels") or [])
    merged: Dict[str, Dict[str, Any]] = {}
    cells = []
    for tab in (inventory_feed.SOURCE_WORKDAY, inventory_feed.SOURCE_CAREER):
        reset_peak()
        base = rss_kb("VmRSS")
        t0 = time.monotonic()
        try:
            res = inventory_feed.build_feed(tab, {"titles": titles, "user_email": email,
                                                  "us_only": True, "tz_offset_minutes": args.tz})
            err = None
        except Exception as exc:  # the real path would fall back to the crawl
            res, err = None, type(exc).__name__
        took = time.monotonic() - t0
        peak_mb = max(0, rss_kb("VmHWM") - base) / 1024
        times.append(took)
        peaks.append(peak_mb)
        if res is None:
            cells.append(f"{took:5.1f},{peak_mb:5.0f},{err or 'CRAWL'}")
            continue
        jobs = res.get("jobs") or []
        cells.append(f"{took:5.1f},{peak_mb:5.0f},{len(jobs):5d}")
        for row in jobs:
            key = inventory_feed.dedupe_key_of(row)
            prev = merged.get(key)
            if prev is None or (row.get("match_score") or 0) > (prev.get("match_score") or 0):
                merged[key] = row
    shown = [r for r in merged.values() if in_levels(r, levels) and r.get("sponsorship") != "refused"]

    def window(lo: int, hi: int) -> int:
        n = 0
        for r in shown:
            age = r.get("days_ago")
            if age is None:
                age = r.get("first_seen_days_ago")
            if age is None or lo <= age <= hi:
                n += 1
        return n

    print(f"{mask(email):10s} {len(titles):6d}  {cells[0]:24s}  {cells[1]:24s}  {len(merged):6d}"
          f"  {window(0, 0):5d} {window(0, 3):3d} {window(0, 7):3d} {window(0, 30):4d}")

if times:
    section("Replay summary")
    print(f"tab build seconds: p50={pct(times, .5)} p90={pct(times, .9)} max={pct(times, 1)}")
    print(f"tab peak memory MB: p50={pct(peaks, .5)} p90={pct(peaks, .9)} max={pct(peaks, 1)}")
