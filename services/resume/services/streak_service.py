"""
Streak service — tracks daily resume-application streaks.

One "application" = one tailoring_record (the unit of work users send to a job
posting outside our app). Edits/regenerations of the same record are NOT
counted as separate applications — increments only fire on initial record
creation.

All "today" / "yesterday" calculations are done in the **user's local
timezone** (IANA name passed by the client). The doc stores the tz that was
used to bucket the daily counts so reads in a different tz can detect drift
and rebuild from tailoring_records.

Storage: a single document per user in `parse_streaks`:

    {
      user_email: str,                       # unique
      tz: str,                               # IANA tz used to bucket dates
      current_streak: int,                   # days in a row ending at last_date
      longest_streak: int,
      last_application_date: "YYYY-MM-DD",   # local date of most recent application
      total_applications: int,
      daily_counts: { "YYYY-MM-DD": int },   # trimmed to last 90 days
      updated_at: datetime,
    }
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo  # py3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

_HISTORY_DAYS = 90
_HEATMAP_DAYS = 30


def _collection():
    return DBConnect().get_db().parse_streaks


def _records_collection():
    return DBConnect().get_db().tailoring_records


def _resolve_tz(tz: Optional[str]):
    if not tz or ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz)
    except Exception:
        return timezone.utc


def _today(tz: Optional[str] = None) -> str:
    return datetime.now(_resolve_tz(tz)).strftime("%Y-%m-%d")


def _yesterday(tz: Optional[str] = None) -> str:
    return (datetime.now(_resolve_tz(tz)) - timedelta(days=1)).strftime("%Y-%m-%d")


def _date_key(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def _trim_history(daily_counts: Dict[str, int], tz: Optional[str] = None) -> Dict[str, int]:
    cutoff = _date_key(datetime.now(_resolve_tz(tz)) - timedelta(days=_HISTORY_DAYS))
    return {k: v for k, v in daily_counts.items() if k >= cutoff}


def _backfill_from_records(user_email: str, tz: Optional[str] = None) -> Dict[str, Any]:
    """Build a streak doc from existing tailoring_records.

    Counts each record's `created_at` (converted to the user's local tz) as
    one application. Versions/edits are ignored.
    """
    cursor = _records_collection().find(
        {"user_email": user_email},
        {"created_at": 1, "_id": 0},
    )
    local = _resolve_tz(tz)
    daily_counts: Dict[str, int] = {}
    total = 0
    for r in cursor:
        ts = r.get("created_at")
        if not ts:
            continue
        if hasattr(ts, "astimezone"):
            ts_utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            key = ts_utc.astimezone(local).strftime("%Y-%m-%d")
        else:
            key = str(ts)[:10]
        daily_counts[key] = daily_counts.get(key, 0) + 1
        total += 1

    if not daily_counts:
        return {}

    sorted_dates = sorted(daily_counts.keys())
    last_date = sorted_dates[-1]

    longest = 1
    current_run = 1
    for i in range(1, len(sorted_dates)):
        prev = datetime.strptime(sorted_dates[i - 1], "%Y-%m-%d")
        curr = datetime.strptime(sorted_dates[i], "%Y-%m-%d")
        if (curr - prev).days == 1:
            current_run += 1
        else:
            current_run = 1
        longest = max(longest, current_run)

    current_streak = 1
    for i in range(len(sorted_dates) - 1, 0, -1):
        prev = datetime.strptime(sorted_dates[i - 1], "%Y-%m-%d")
        curr = datetime.strptime(sorted_dates[i], "%Y-%m-%d")
        if (curr - prev).days == 1:
            current_streak += 1
        else:
            break

    update = {
        "user_email": user_email,
        "tz": tz or "UTC",
        "current_streak": current_streak,
        "longest_streak": max(longest, current_streak),
        "last_application_date": last_date,
        "total_applications": total,
        "daily_counts": _trim_history(daily_counts, tz),
        "updated_at": datetime.now(timezone.utc),
        "backfilled_at": datetime.now(timezone.utc),
    }
    _collection().update_one(
        {"user_email": user_email},
        {"$set": update},
        upsert=True,
    )
    return update


def record_application(user_email: str, tz: Optional[str] = None) -> Dict[str, Any]:
    """Increment the streak for a new application (new tailoring_record).

    `tz` is the user's IANA timezone (e.g. 'America/Phoenix'). When the doc's
    stored tz differs, the streak is rebuilt from tailoring_records under the
    new tz before applying the increment, so the doc stays consistent with
    how dates are bucketed locally.
    """
    if not user_email:
        return {}

    col = _collection()
    doc = col.find_one({"user_email": user_email}) or {}

    stored_tz = doc.get("tz")
    if doc and stored_tz != (tz or "UTC"):
        # tz drift — rebuild from records under the new tz first.
        try:
            doc = _backfill_from_records(user_email, tz=tz) or doc
        except Exception as e:
            logger.warning(f"Streak rebuild on tz drift failed for {user_email}: {e}")

    today = _today(tz)
    yesterday = _yesterday(tz)

    last_date = doc.get("last_application_date")
    current_streak = int(doc.get("current_streak") or 0)
    longest_streak = int(doc.get("longest_streak") or 0)
    total = int(doc.get("total_applications") or 0)
    daily_counts = dict(doc.get("daily_counts") or {})

    if last_date == today:
        pass
    elif last_date == yesterday:
        current_streak += 1
    else:
        current_streak = 1

    daily_counts[today] = int(daily_counts.get(today, 0)) + 1
    total += 1
    longest_streak = max(longest_streak, current_streak)
    daily_counts = _trim_history(daily_counts, tz)

    update = {
        "user_email": user_email,
        "tz": tz or "UTC",
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "last_application_date": today,
        "total_applications": total,
        "daily_counts": daily_counts,
        "updated_at": datetime.now(timezone.utc),
    }
    col.update_one({"user_email": user_email}, {"$set": update}, upsert=True)
    return _serialize(update, tz)


def get_streak(
    user_email: str,
    tz: Optional[str] = None,
    force_rebuild: bool = False,
) -> Dict[str, Any]:
    """Return streak summary + last-30-day heatmap for the user."""
    if not user_email:
        return _empty_summary(tz)

    doc = _collection().find_one({"user_email": user_email})

    needs_rebuild = (
        force_rebuild
        or doc is None
        or doc.get("tz") != (tz or "UTC")
    )
    if needs_rebuild:
        try:
            rebuilt = _backfill_from_records(user_email, tz=tz)
            if rebuilt:
                return _serialize(rebuilt, tz)
        except Exception as e:
            logger.warning(f"Streak backfill failed for {user_email}: {e}")
        if doc is None:
            return _empty_summary(tz)

    return _serialize(doc, tz)


def _serialize(doc: Dict[str, Any], tz: Optional[str] = None) -> Dict[str, Any]:
    """Always re-derive current_streak from daily_counts so stale stored
    counters can never lie. Walks backwards from today through the run of
    consecutive days that have applications.
    """
    today = _today(tz)
    yesterday = _yesterday(tz)
    daily_counts = dict(doc.get("daily_counts") or {})
    today_count = int(daily_counts.get(today, 0))

    # Find the most recent day with applications, but only if it's still
    # "alive" (today or yesterday). Otherwise the streak is 0.
    cursor: Optional[str]
    if int(daily_counts.get(today, 0)) > 0:
        cursor = today
    elif int(daily_counts.get(yesterday, 0)) > 0:
        cursor = yesterday
    else:
        cursor = None

    current_streak = 0
    if cursor:
        d = datetime.strptime(cursor, "%Y-%m-%d").date()
        while int(daily_counts.get(d.strftime("%Y-%m-%d"), 0)) > 0:
            current_streak += 1
            d = d - timedelta(days=1)

    last_application_date = (
        max(k for k, v in daily_counts.items() if int(v) > 0)
        if any(int(v) > 0 for v in daily_counts.values())
        else None
    )
    longest = max(int(doc.get("longest_streak") or 0), current_streak)
    total = sum(int(v) for v in daily_counts.values()) or int(doc.get("total_applications") or 0)

    return {
        "current_streak": current_streak,
        "longest_streak": longest,
        "today_count": today_count,
        "total_applications": total,
        "last_application_date": last_application_date,
        "heatmap": _build_heatmap(daily_counts, tz),
    }


def _build_heatmap(daily_counts: Dict[str, int], tz: Optional[str] = None) -> List[Dict[str, Any]]:
    today_dt = datetime.now(_resolve_tz(tz))
    out: List[Dict[str, Any]] = []
    for offset in range(_HEATMAP_DAYS - 1, -1, -1):
        d = today_dt - timedelta(days=offset)
        key = _date_key(d)
        out.append({"date": key, "count": int(daily_counts.get(key, 0))})
    return out


def _empty_summary(tz: Optional[str] = None) -> Dict[str, Any]:
    return {
        "current_streak": 0,
        "longest_streak": 0,
        "today_count": 0,
        "total_applications": 0,
        "last_application_date": None,
        "heatmap": _build_heatmap({}, tz),
    }
