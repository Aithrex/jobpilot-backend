"""Read-only probe: why the crawl's intake dropped, by day, hour and source.

New postings fell from ~33,000 a day (10-27..10-30) to 5,300 on 10-31 and
3,733 on 11-01. This prints, from `job_ingestion_runs` (kept 14 days) and
`job_postings`:

* per day and source: shard runs, how many failed, postings created, fetched,
  errors, and the median run length;
* per hour across the dip: runs and postings created, so a stop shows as a gap
  and a slowdown as thin hours;
* the commonest failure messages during the dip;
* postings first seen per day by source.

READ-ONLY, enforced: every pymongo write method raises. Run inside the app
image on the box:

    docker exec -i <worker container> python - < scripts/crawl_intake_probe.py
"""
from __future__ import annotations

import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

for candidate in ("/app/shared/python", os.path.join(os.getcwd(), "shared", "python")):
    if os.path.isdir(candidate) and candidate not in sys.path:
        sys.path.insert(0, candidate)

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
START = (NOW - timedelta(days=8)).replace(hour=0, minute=0, second=0, microsecond=0)
DIP_FROM = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def section(title: str) -> None:
    print(f"\n=== {title}")


def aware(d: Any):
    if not isinstance(d, datetime):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


runs = list(db.job_ingestion_runs.find(
    {"started_at": {"$gte": START}},
    {"_id": 0, "source": 1, "status": 1, "started_at": 1, "duration_ms": 1,
     "counts.created": 1, "counts.fetched": 1, "counts.errors": 1, "counts.closed": 1,
     "failures": {"$slice": -2}}))
print(f"{len(runs)} shard runs since {START:%m-%d}")

section("Per day and source: runs, failed, created, fetched, errors, closed, median run s")
table: Dict[tuple, Dict[str, Any]] = {}
for r in runs:
    day = aware(r["started_at"]).strftime("%m-%d")
    t = table.setdefault((day, r.get("source") or "?"),
                         {"runs": 0, "failed": 0, "running": 0, "created": 0, "fetched": 0,
                          "errors": 0, "closed": 0, "ms": []})
    t["runs"] += 1
    t["failed"] += r.get("status") == "failed"
    t["running"] += r.get("status") == "running"
    c = r.get("counts") or {}
    for k in ("created", "fetched", "errors", "closed"):
        t[k] += int(c.get(k) or 0)
    if r.get("duration_ms"):
        t["ms"].append(r["duration_ms"])
for (day, src), t in sorted(table.items()):
    med = round(statistics.median(t["ms"]) / 1000, 1) if t["ms"] else None
    print(f"{day} {src:13s} runs={t['runs']:5d} failed={t['failed']:4d} stuck={t['running']:3d}"
          f" created={t['created']:6d} fetched={t['fetched']:7d} errors={t['errors']:5d}"
          f" closed={t['closed']:6d} median_s={med}")

section("Per hour from 10-30 12:00 UTC: runs and postings created (all sources)")
hours: Dict[str, List[int]] = {}
for r in runs:
    at = aware(r["started_at"])
    if at < DIP_FROM:
        continue
    h = hours.setdefault(at.strftime("%m-%d %H"), [0, 0, 0])
    h[0] += 1
    h[1] += int((r.get("counts") or {}).get("created") or 0)
    h[2] += r.get("status") == "failed"
cursor = DIP_FROM
while cursor <= NOW:
    key = cursor.strftime("%m-%d %H")
    runs_n, created, failed = hours.get(key, [0, 0, 0])
    bar = "#" * min(60, created // 50)
    print(f"{key}:00 runs={runs_n:4d} failed={failed:3d} created={created:5d} {bar}")
    cursor += timedelta(hours=1)

section("Commonest failure messages since 10-30 12:00 UTC")
msgs: Dict[str, int] = {}
for r in runs:
    if aware(r["started_at"]) < DIP_FROM:
        continue
    for f in r.get("failures") or []:
        m = str(f.get("message") or "")[:140]
        msgs[m] = msgs.get(m, 0) + 1
for m, n in sorted(msgs.items(), key=lambda kv: -kv[1])[:12]:
    print(f"x{n:5d}  {m}")

section("Postings first seen per day, by source")
for row in db.job_postings.aggregate([
        {"$match": {"first_seen_at": {"$gte": START}}},
        {"$group": {"_id": {"d": {"$dateToString": {"format": "%m-%d", "date": "$first_seen_at"}},
                            "s": "$source"}, "n": {"$sum": 1}}},
        {"$sort": {"_id.d": 1, "_id.s": 1}}]):
    print(f"{row['_id']['d']} {str(row['_id']['s']):13s} {row['n']}")

section("Daily jobs the runner recorded (ingest_runner_daily_runs)")
if "ingest_runner_daily_runs" in db.list_collection_names():
    for d in db.ingest_runner_daily_runs.find({}, {"_id": 0}).sort([("_id", -1)]).limit(12):
        print({k: d.get(k) for k in list(d)[:6]})
