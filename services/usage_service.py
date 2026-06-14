"""Per-user daily tailor quota tracking.

Default limit: 5 tailored resumes per UTC day per user. Admins can raise
limits per user via `users.daily_tailor_limit`. Regenerate is NOT counted —
only fresh tailor / tailor-with-jd / batch-tailor-item invocations are.

Storage: `tailor_usage` collection, one document per (email, date) day,
incremented atomically with $inc upsert.
"""
from datetime import datetime, timezone, timedelta
import logging
from typing import Dict, Tuple

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

DEFAULT_DAILY_LIMIT = 5


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _resets_at() -> datetime:
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def get_user_limit(email: str) -> int:
    """Return the user's daily tailor limit. Admin-set overrides win;
    otherwise DEFAULT_DAILY_LIMIT applies."""
    if not email:
        return DEFAULT_DAILY_LIMIT
    db = DBConnect().get_db()
    user = db.users.find_one({"email": email}, {"daily_tailor_limit": 1})
    if user and isinstance(user.get("daily_tailor_limit"), int) and user["daily_tailor_limit"] >= 0:
        return user["daily_tailor_limit"]
    return DEFAULT_DAILY_LIMIT


def get_usage(email: str) -> Dict:
    """Today's usage snapshot for the user."""
    db = DBConnect().get_db()
    today = _today_key()
    rec = db.tailor_usage.find_one({"email": email, "date": today}, {"count": 1})
    used = int(rec.get("count", 0)) if rec else 0
    limit = get_user_limit(email)
    resets = _resets_at()
    now = datetime.now(timezone.utc)
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used),
        "resets_at": resets.isoformat(),
        "resets_in_seconds": int((resets - now).total_seconds()),
        "date": today,
    }


def check_quota(email: str, n: int = 1) -> Tuple[bool, Dict]:
    """Returns (allowed, usage_dict). Does NOT increment."""
    usage = get_usage(email)
    return usage["remaining"] >= n, usage


def increment_usage(email: str, n: int = 1) -> Dict:
    """Atomically increment the user's daily count. Returns refreshed usage."""
    db = DBConnect().get_db()
    today = _today_key()
    db.tailor_usage.update_one(
        {"email": email, "date": today},
        {
            "$inc": {"count": int(n)},
            "$set": {"last_at": datetime.now(timezone.utc)},
            "$setOnInsert": {"email": email, "date": today},
        },
        upsert=True,
    )
    return get_usage(email)


def check_and_increment(email: str, n: int = 1) -> Tuple[bool, Dict]:
    """Combined check + increment.

    Returns (allowed, usage_dict). When not allowed, usage_dict reflects
    the pre-attempt state so callers can return it directly to the client.
    """
    allowed, usage = check_quota(email, n)
    if not allowed:
        return False, usage
    refreshed = increment_usage(email, n)
    return True, refreshed


# ----------------------------------------------------------------------
# Admin-side helpers
# ----------------------------------------------------------------------


def set_user_limit(email: str, limit: int) -> Dict:
    """Admin: set a custom daily limit for a user. Set limit < 0 to
    reset to default."""
    db = DBConnect().get_db()
    if limit is None or limit < 0:
        db.users.update_one({"email": email}, {"$unset": {"daily_tailor_limit": ""}})
    else:
        db.users.update_one(
            {"email": email}, {"$set": {"daily_tailor_limit": int(limit)}}
        )
    return {"email": email, "limit": get_user_limit(email)}


def get_today_usage_for_users(emails) -> Dict[str, int]:
    """Bulk fetch of today's usage counts for an email list. Returns
    {email: count} (missing emails default to 0)."""
    if not emails:
        return {}
    db = DBConnect().get_db()
    today = _today_key()
    docs = db.tailor_usage.find(
        {"email": {"$in": list(emails)}, "date": today},
        {"email": 1, "count": 1, "_id": 0},
    )
    return {d["email"]: int(d.get("count", 0)) for d in docs}
