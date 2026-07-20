"""Per-user daily tailor quota tracking (reader over the v3 atomic ledger).

Default limit: 5 tailored resumes per UTC day per user. Admins can raise
limits per user via `users.daily_tailor_limit`. Regenerate is NOT counted —
only fresh tailor / tailor-with-jd / batch-tailor-item sessions are.

Resume Platform v3 (spec 1.6): the atomic quota AUTHORITY moved to
`quota_counters` / `quota_ledger` (services/quota_ledger_service.py) — tailor
routes reserve through `quota_ledger_service.reserve_tailor`, never through
`check_and_increment` (retired for tailor). This module stays as:
  * the READER (`get_usage` — QuotaMeter endpoints keep their shapes; counts
    now come from quota_counters);
  * the non-reserving WRITER (`increment_usage` — guest-claim day-1 carry-in
    seeding), which $incs the pool counter AND keeps a per-actor event doc in
    the legacy `tailor_usage` collection for admin per-user visibility.
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
    """Return the user's daily tailor limit.

    Precedence: an admin-set per-user override (`users.daily_tailor_limit`)
    wins; otherwise the limit comes from the user's freemium plan via
    `entitlements_service.quota(plan, "tailor")`; DEFAULT_DAILY_LIMIT is the
    final fallback if entitlements is unavailable. Tailoring is metered HERE
    (tailor_usage), not in feature_usage, to avoid double counting — this hook
    just resolves the per-plan number.
    """
    if not email:
        return DEFAULT_DAILY_LIMIT
    db = DBConnect().get_db()
    user = db.users.find_one({"email": email}, {"daily_tailor_limit": 1, "plan": 1})
    if user and isinstance(user.get("daily_tailor_limit"), int) and user["daily_tailor_limit"] >= 0:
        return user["daily_tailor_limit"]
    # Fall back to the plan-based tailor quota.
    try:
        from services import entitlements_service
        plan = (user or {}).get("plan")
        plan = plan if plan in entitlements_service.VALID_PLANS else entitlements_service.DEFAULT_PLAN
        limit = entitlements_service.quota(plan, "tailor")
        if isinstance(limit, int) and limit >= 0:
            return limit
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("entitlements tailor-quota lookup failed for %s: %s", email, e)
    return DEFAULT_DAILY_LIMIT


def _linked_pool_emails(email: str) -> list:
    """FREE-plan shared daily pool (Guest Onboarding v2).

    Accounts confidently linked to the same guest quota subject (each linked
    via an authenticated /guest/claim — `guest_quota_subjects.linked_accounts`)
    share ONE free daily tailor pool. Returns the pool emails (always includes
    the caller). PREMIUM PLANS ARE UNTOUCHED: a non-free caller always meters
    solo, and non-free linked accounts are excluded from the pool. Defensive —
    any failure degrades to solo metering. Each tailor event is still recorded
    exactly once under the acting account (no double count); only the COUNT
    aggregates across the pool.
    """
    if not email:
        return [email]
    try:
        from services import entitlements_service

        if entitlements_service.get_plan(email) != entitlements_service.FREE:
            return [email]
        db = DBConnect().get_db()
        linked = {email}
        # `email` comes from the JWT identity; value-position equality only.
        for doc in db.guest_quota_subjects.find(
            {"linked_accounts": email}, {"linked_accounts": 1}
        ).limit(20):
            for other in doc.get("linked_accounts") or []:
                if isinstance(other, str) and other:
                    linked.add(other)
        if len(linked) <= 1:
            return [email]
        return [
            e for e in sorted(linked)
            if e == email or entitlements_service.get_plan(e) == entitlements_service.FREE
        ]
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("linked free-pool resolution failed (solo metering): %s", e)
        return [email]


def get_usage(email: str) -> Dict:
    """Today's usage snapshot for the user.

    Backed by `quota_counters` (spec 1.6): the caller's current pool counter
    plus any residual solo counters from a mid-day pool-membership change —
    same linked free-pool semantics as before, now atomic at reserve time.
    """
    from services import quota_ledger_service

    today = _today_key()
    used = quota_ledger_service.used_today(email, date=today)
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
    """Atomically increment the user's daily count. Returns refreshed usage.

    Non-reserving writer (guest-claim carry-in seeding / admin adjustments):
    $incs the pool authority counter, and keeps the legacy per-actor event doc
    in `tailor_usage` so admin per-user visibility survives. Tailor routes
    must reserve via `quota_ledger_service.reserve_tailor` instead.
    """
    from services import quota_ledger_service

    quota_ledger_service.increment_counter(email, n=int(n))
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
    """LEGACY combined check + increment — retired for the tailor routes
    (spec 1.6: they reserve atomically via quota_ledger_service). Kept for
    non-route callers; still check-then-increment, so it must never guard a
    concurrent spend path.
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
    """Bulk fetch of today's PER-ACTOR usage counts for an email list.

    Combines the legacy `tailor_usage` event docs (carry-in seeds and other
    increment_usage writers) with today's live quota_ledger entries
    (reserved + completed — refunded slots don't count). Returns
    {email: count}; missing emails default to 0.
    """
    if not emails:
        return {}
    db = DBConnect().get_db()
    today = _today_key()
    docs = db.tailor_usage.find(
        {"email": {"$in": list(emails)}, "date": today},
        {"email": 1, "count": 1, "_id": 0},
    )
    out = {d["email"]: int(d.get("count", 0)) for d in docs}
    try:
        from services import quota_ledger_service
        for email, n in quota_ledger_service.entries_count_for_users(
            list(emails), date=today
        ).items():
            out[email] = out.get(email, 0) + n
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("ledger per-user counts unavailable: %s", e)
    return out
