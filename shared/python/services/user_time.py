"""Per-user timezone resolution and civil-day labelling.

Two different kinds of time live in this codebase and they need different
storage:

  * **Instants** — *when* something happened (``applied_at``, ``created_at``).
    Always UTC. Used for sorting and staleness.
  * **Civil days** — *which day* something counts for. Not a moment in time, a
    label. Only meaningful relative to a zone.

Deriving a civil day from a UTC instant at read time is what makes an
application submitted at 8pm in Phoenix show up as the next day. So the day is
derived once, in the user's zone, and stored as a plain ``"YYYY-MM-DD"``
string. Frozen that way it never shifts when the user travels.

This mirrors ``parse_streaks`` (which stores ``tz`` alongside ``YYYY-MM-DD``
keys) and deliberately does NOT touch the daily quota, which buckets on a UTC
day in ``quota_ledger_service`` — that is a service-side rate limit, not a
user-facing date.

Lives in ``shared/python`` because both the resume service (PATCH application)
and the jobs service (tracker stubs) need it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

# Matches scheduled_jobs._DEFAULT_TZ / apply_scheduler._DEFAULT_TZ so every
# server-side "what time is it for this user" answer agrees.
DEFAULT_TZ = "America/New_York"


def resolve_tz(name: Optional[str]):
    """An IANA name -> tzinfo, falling back to UTC.

    Same semantics as ``streak_service._resolve_tz``: a missing or unresolvable
    name means we genuinely do not know where the user is, and UTC is the
    honest answer rather than a guess.
    """
    if not name:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def resolve_user_timezone(user_email: str) -> str:
    """The user's IANA zone from stored state, for paths with no live browser.

    Order: declared notification zone -> the zone their streak was bucketed
    under -> ``DEFAULT_TZ``. Never raises; a lookup failure degrades to the
    default rather than failing the caller's write.
    """
    if not user_email:
        return DEFAULT_TZ
    try:
        db = DBConnect().get_db()
    except Exception as e:
        logger.warning("[user_time] db unavailable for %s: %s", user_email, e)
        return DEFAULT_TZ

    try:
        prefs = db.notification_prefs.find_one(
            {"user_email": user_email}, {"_id": 0, "quiet_hours": 1}) or {}
        declared = ((prefs.get("quiet_hours") or {}).get("tz") or "").strip()
        if declared:
            return declared
    except Exception as e:
        logger.warning("[user_time] prefs tz lookup failed for %s: %s", user_email, e)

    try:
        streak = db.parse_streaks.find_one(
            {"user_email": user_email}, {"_id": 0, "tz": 1}) or {}
        bucketed = (streak.get("tz") or "").strip()
        # "UTC" here means the streak was built without a client zone, which is
        # a placeholder rather than a declaration — keep looking.
        if bucketed and bucketed != "UTC":
            return bucketed
    except Exception as e:
        logger.warning("[user_time] streak tz lookup failed for %s: %s", user_email, e)

    return DEFAULT_TZ


def civil_day(instant: datetime, tz_name: Optional[str]) -> str:
    """A UTC instant -> the ``YYYY-MM-DD`` it falls on in ``tz_name``."""
    aware = instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)
    return aware.astimezone(resolve_tz(tz_name)).strftime("%Y-%m-%d")


def applied_day_for_user(
    instant: datetime,
    user_email: str,
    tz_hint: Optional[str] = None,
) -> Tuple[str, str]:
    """``(YYYY-MM-DD, tz_name)`` for an application instant.

    ``tz_hint`` is the browser's zone when one is available (the client sends
    it on save-record and the application PATCH); otherwise we fall back to the
    user's stored zone so server-driven paths still get a sensible day.
    """
    tz_name = (tz_hint or "").strip() or resolve_user_timezone(user_email)
    return civil_day(instant, tz_name), tz_name
