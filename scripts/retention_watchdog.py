#!/usr/bin/env python3
"""Daily retention watchdog: keep storage bounded, and fail loudly when it is not.

Run every day by `.github/workflows/retention-watchdog.yml`. A failed run is a
red workflow, and GitHub emails whoever last edited that schedule - which is the
whole point. The cluster reached ~4 GB twice before anyone noticed, and both
times the thing that should have prevented it had stopped silently.

Retention itself does not depend on this script. `job_postings` is bounded by a
TTL index on `expire_at` that MongoDB enforces every 60 seconds (see the
Retention section of `job_inventory_service`), and `resume_jobs` by its own TTL.
This script exists for the two things a TTL cannot do for itself:

  REPAIR - everything a TTL relies on but cannot create:
    * the TTL index exists (recreated if someone dropped it);
    * every posting a user saved, tailored or applied to is pinned, and no
      pinned posting carries `expire_at` (a TTL deletes whatever has one);
    * every other posting HAS `expire_at` - a crawler running a build older than
      the TTL change inserts rows without it, and those would live forever;
    * closed postings past `purge_at` are deleted (the TTL keys on dates, and a
      board closing a posting early is not a date);
    * job-feed results in `resume_jobs` carry the short expiry.

  VERIFY - that deletion is actually happening, not merely configured:
    * no posting is more than OVERDUE_GRACE past its `expire_at` (if there are,
      the TTL monitor is not running and the index is decoration);
    * total size (documents + indexes) is under `--max-gib`.

  CRAWLER - that the EC2 ingest runner's own daily jobs ran, from what it
  records in MongoDB (`ingest_runner_heartbeat`, `ingest_runner_daily_runs`,
  `coverage_snapshots`). Its maintenance was switched off by a `--no-maintenance`
  in a systemd unit that no deploy ever replaced, and its coverage job failed
  every morning on a missing module - for three weeks, with every deploy green.

    python scripts/retention_watchdog.py                   # check only, writes nothing
    python scripts/retention_watchdog.py --apply           # repair, then check (CI)
    python scripts/retention_watchdog.py --apply --max-gib 2.5

Exit status: 0 healthy, 1 unhealthy or any step failed.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "python"))
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

from services import job_inventory_service as inv  # noqa: E402
from services.resume_service import (  # noqa: E402
    FEED_RESULT_TTL_HOURS, _SHORT_LIVED_JOB_TYPES)
from utils.db_connect import DBConnect  # noqa: E402

GIB = 1024 ** 3
#: The TTL monitor wakes every 60s; a busy server can lag a few minutes. Two
#: hours overdue is not lag, it is a monitor that is not running.
OVERDUE_GRACE = timedelta(hours=2)
OVERDUE_TOLERANCE = 1000
#: Unpinned rows with no `expire_at` after the backfill are rows with no date at
#: all (kept on purpose). A large number means something is inserting without
#: the field faster than this job repairs it.
UNEXPIRING_TOLERANCE = 5000
#: The runner heartbeats every minute; half an hour of silence is a dead crawl.
HEARTBEAT_STALE = timedelta(minutes=30)
#: Daily jobs: a day plus slack for a slow run.
DAILY_STALE = timedelta(hours=26)
#: A run that started this long ago and never finished was killed or is hung.
DAILY_HUNG = timedelta(hours=2)
#: Coverage is served with its age; the product calls it stale past a day.
COVERAGE_STALE = timedelta(hours=30)


def _load_pin_backfill():
    spec = importlib.util.spec_from_file_location(
        "backfill_pins", ROOT / "scripts" / "backfill_pins.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def shorten_feed_results(db, now: datetime) -> int:
    """Re-stamp settled job-feed rows written before `complete_job` gave them
    the short expiry. A few hundred rows at most, so one write each."""
    lifetime = timedelta(hours=FEED_RESULT_TTL_HOURS)
    fixed = 0
    for row in db.resume_jobs.find(
            {"job_type": {"$in": list(_SHORT_LIVED_JOB_TYPES)},
             "completed_at": {"$type": "date"}},
            {"_id": 1, "completed_at": 1, "expire_at": 1}):
        due = inv._safe_dt(row["completed_at"]) + lifetime
        current = inv._safe_dt(row.get("expire_at"))
        if current is None or current > due:
            fixed += db.resume_jobs.update_one(
                {"_id": row["_id"]}, {"$set": {"expire_at": max(due, now)}}
            ).modified_count
    return fixed


def crawler_checks(db, now: datetime):
    """Did the EC2 runner's daily jobs actually run? Returns (lines, problems)."""
    lines, problems = [], []
    hb = db.ingest_runner_heartbeat.find_one(sort=[("updated_at", -1)])
    if not hb:
        return ["- crawler heartbeat: **none recorded**"], [
            "the ingest runner has never written a heartbeat"]
    beat = inv._safe_dt(hb.get("updated_at"))
    started = inv._safe_dt(hb.get("started_at"))
    age = now - beat if beat else None
    lines.append(f"- crawler heartbeat: {age and str(age).split('.')[0]} ago "
                 f"(`{hb.get('runner_id')}`)")
    if age is None or age > HEARTBEAT_STALE:
        problems.append(f"the ingest runner's last heartbeat was {age} ago - "
                        "the crawl is not running")

    enabled = hb.get("maintenance")
    lines.append(f"- runner maintenance: **{ {True: 'enabled', False: 'DISABLED'}.get(enabled, 'unknown (older runner build)') }**")
    if enabled is False:
        problems.append("the ingest runner is running with --no-maintenance; "
                        "check the systemd unit on the box")

    last = db.ingest_runner_daily_runs.find_one({"job": "maintenance"},
                                                sort=[("started_at", -1)])
    last_ok = db.ingest_runner_daily_runs.find_one(
        {"job": "maintenance", "ok": True}, sort=[("started_at", -1)])
    if last:
        ran = inv._safe_dt(last.get("started_at"))
        state = ("running" if "finished_at" not in last
                 else "ok" if last.get("ok") else f"FAILED ({last.get('error')})")
        lines.append(f"- last runner maintenance: {ran:%Y-%m-%d %H:%M} UTC, {state}")
        if last.get("finished_at") is None and now - ran > DAILY_HUNG:
            problems.append(f"runner maintenance started {ran:%Y-%m-%d %H:%M} UTC "
                            "and never finished - killed or hung")
        elif last.get("ok") is False:
            problems.append(f"runner maintenance failed: {last.get('error')}: "
                            f"{last.get('message', '')}".rstrip(": "))
    else:
        lines.append("- last runner maintenance: none recorded yet")
    ok_at = inv._safe_dt((last_ok or {}).get("started_at"))
    runner_old_enough = started is not None and now - started > DAILY_STALE
    if enabled is not False and runner_old_enough and (
            ok_at is None or now - ok_at > DAILY_STALE):
        problems.append("no successful runner maintenance in the last "
                        f"{DAILY_STALE}; last success: {ok_at or 'never recorded'}")

    snap = db.coverage_snapshots.find_one(sort=[("measured_at", -1)])
    measured = inv._safe_dt((snap or {}).get("measured_at"))
    lines.append(f"- coverage snapshot: {measured:%Y-%m-%d %H:%M} UTC" if measured
                 else "- coverage snapshot: none")
    if measured is None or now - measured > COVERAGE_STALE:
        run = db.coverage_snapshot_runs.find_one(sort=[("at", -1)]) or {}
        problems.append(f"the coverage snapshot is {now - measured if measured else 'missing'} "
                        f"old; last run: {run.get('status')} {run.get('error') or ''}".rstrip())
    return lines, problems


def _sizes(db):
    stats = db.command("dbstats")
    rows = []
    for name in db.list_collection_names():
        try:
            s = db.command("collstats", name)
        except Exception:
            continue
        rows.append((s.get("size", 0) + s.get("totalIndexSize", 0),
                     s.get("size", 0), s.get("totalIndexSize", 0),
                     s.get("count", 0), name))
    rows.sort(reverse=True)
    return stats, rows


def run(*, apply: bool, max_gib: float) -> int:
    db = DBConnect().get_db()
    now = datetime.now(timezone.utc)
    lines, problems = [], []

    def step(label, fn):
        try:
            result = fn()
            lines.append(f"- {label}: `{result}`")
            return result
        except Exception as exc:
            problems.append(f"{label} FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            return None

    if apply:
        lines.append("### Repairs")
        step("TTL index", inv.ensure_retention_index)
        step("sightings TTL index", inv.ensure_sightings_index)
        step("pins", lambda: _load_pin_backfill().pin_user_attached(db, apply=True, now=now))
        step("expiry backfilled", inv.backfill_retention_expiry)
        step("closed & due purged", lambda: inv.purge_expired(limit=500_000, now=now))
        step("feed results re-stamped", lambda: shorten_feed_results(db, now))
        step("runner daily-run TTL", lambda: db.ingest_runner_daily_runs.create_index(
            [("expire_at", 1)], name="ttl_expire_at", expireAfterSeconds=0))

    lines.append("### Checks")
    postings = db.job_postings
    index_ok = any(
        v.get("key") == [("expire_at", 1)] and v.get("expireAfterSeconds") == 0
        for v in postings.index_information().values())
    overdue = postings.count_documents({"expire_at": {"$lt": now - OVERDUE_GRACE}})
    unexpiring = postings.count_documents(
        {"expire_at": {"$exists": False}, "pinned": {"$ne": True}})
    pinned = postings.count_documents({"pinned": True})
    pinned_expiring = postings.count_documents(
        {"pinned": True, "expire_at": {"$exists": True}})
    lines += [
        f"- TTL index on `job_postings.expire_at`: **{'present' if index_ok else 'MISSING'}**",
        f"- postings past expiry by > {OVERDUE_GRACE}: **{overdue:,}**",
        f"- unpinned postings with no expiry: **{unexpiring:,}**",
        f"- pinned (user-attached) postings: {pinned:,}; still carrying an expiry: **{pinned_expiring:,}**",
    ]
    if not index_ok:
        problems.append("the TTL index on job_postings.expire_at is missing - "
                        "nothing is deleting expired postings")
    if overdue > OVERDUE_TOLERANCE:
        problems.append(f"{overdue:,} postings are more than {OVERDUE_GRACE} past "
                        "expire_at - the TTL monitor is not deleting")
    if unexpiring > UNEXPIRING_TOLERANCE:
        problems.append(f"{unexpiring:,} unpinned postings have no expire_at - "
                        "something is inserting without it (a stale crawler build?)")
    if pinned_expiring:
        problems.append(f"{pinned_expiring:,} pinned postings carry expire_at "
                        "and WILL be deleted - run with --apply")

    lines.append("### Crawler (EC2 ingest runner)")
    c_lines, c_problems = crawler_checks(db, now)
    lines += c_lines
    problems += c_problems

    stats, rows = _sizes(db)
    total = stats.get("dataSize", 0) + stats.get("indexSize", 0)
    lines += [
        "### Size",
        f"**{total / GIB:.2f} GiB** (documents {stats.get('dataSize', 0) / GIB:.2f} + "
        f"indexes {stats.get('indexSize', 0) / GIB:.2f}); budget {max_gib:.2f} GiB",
        "",
        "| collection | docs | data MiB | index MiB |",
        "|---|---:|---:|---:|",
    ]
    lines += [f"| {n} | {c:,} | {d / 2**20:.1f} | {i / 2**20:.1f} |"
              for _, d, i, c, n in rows[:10]]
    if total > max_gib * GIB:
        problems.append(
            f"database is {total / GIB:.2f} GiB, over the {max_gib:.2f} GiB budget. "
            f"Largest: {rows[0][4]} ({rows[0][0] / GIB:.2f} GiB)")

    verdict = "HEALTHY" if not problems else "UNHEALTHY"
    report = [f"## Retention watchdog: {verdict}", f"_{now:%Y-%m-%d %H:%M} UTC, "
              f"{'repair + check' if apply else 'check only'}_", ""]
    if problems:
        report += ["### Problems"] + [f"- :x: {p}" for p in problems] + [""]
    report += lines
    text = "\n".join(report)
    print(text)
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    for p in problems:
        # Surfaces as an annotation on the run page, not only in the log.
        print(f"::error::{p}")
    return 0 if not problems else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="repair before checking")
    ap.add_argument("--max-gib", type=float,
                    default=float(os.getenv("RETENTION_MAX_GIB", "3.0")),
                    help="fail when documents + indexes exceed this (default 3.0)")
    args = ap.parse_args()
    return run(apply=args.apply, max_gib=args.max_gib)


if __name__ == "__main__":
    raise SystemExit(main())
