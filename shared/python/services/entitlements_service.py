"""Freemium plan + entitlements service.

Single source of truth for what each plan (free / premium / premium_plus) is
allowed to do and how much. Three kinds of limits live here:

  1. Daily/monthly METERED counters (events, posts, salary, etc.) — tracked in
     the `feature_usage` collection, one doc per (user_email, period, feature),
     incremented atomically with $inc upsert (same pattern as usage_service).
  2. Boolean FEATURE GATES (interview-fit, advisor, personalized event feed …)
     — a feature either is or isn't in the plan's feature set.
  3. Structural caps that aren't time-windowed (batch-tailor size, number of
     active learning paths) — exposed as plain limit lookups the caller checks
     against live state.

Resume *tailoring* is deliberately NOT metered here — it stays on
`usage_service`/`tailor_usage` to avoid double counting. `usage_service` calls
back into `quota(email, "tailor")` for the per-plan number (see the patch in
usage_service.get_user_limit), and an admin `users.daily_tailor_limit` override
still wins over the plan default.

Error contract: gated routes return HTTP 403 with
`{"error": "upgrade_required", ...}` — NEVER 401 (the frontend turns any 401
into a forced logout via the auth:session-expired event).
"""
from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------
FREE = "free"
PREMIUM = "premium"
PREMIUM_PLUS = "premium_plus"
VALID_PLANS = (FREE, PREMIUM, PREMIUM_PLUS)
DEFAULT_PLAN = FREE

# Display metadata (pricing is a business decision; the matrix is data-driven
# so prices/limits can change without code redeploys touching call sites).
PLAN_META = {
    FREE: {"label": "Free", "price_monthly": 0.0},
    PREMIUM: {"label": "Premium", "price_monthly": 19.99},
    PREMIUM_PLUS: {"label": "Premium+", "price_monthly": 39.99},
}

# ---------------------------------------------------------------------------
# Metered features — integer limits per period. None == unlimited.
# `period` is "day" or "month" (calendar UTC). Counters live in feature_usage.
# `tailor` is listed for usage_service's fallback lookup but is metered THERE.
# ---------------------------------------------------------------------------
PERIOD_DAY = "day"
PERIOD_MONTH = "month"

FEATURE_PERIODS = {
    "tailor": PERIOD_DAY,
    "ats_scores": PERIOD_DAY,
    "cover_letter": PERIOD_DAY,
    "autoapply_answer": PERIOD_DAY,
    "quiz": PERIOD_DAY,
    "practice_question": PERIOD_DAY,
    "mock_evaluate": PERIOD_DAY,
    "salary": PERIOD_DAY,
    "post_draft": PERIOD_MONTH,
    "learning_plan_generate": PERIOD_DAY,
    "authored_resume": PERIOD_MONTH,   # Voice Graph — §6.12.10
    "graph_extract": PERIOD_DAY,       # Voice Graph — meter LLM extraction spend
    "six_seconds": PERIOD_DAY,         # Recruiter's Six Seconds — §6.11.2
    "differentiation_analysis": PERIOD_DAY,  # Learning §6.6 — market-gap analysis
    "task_verify": PERIOD_DAY,               # Learning §6.6 — proof-of-work verification
    "completion_post": PERIOD_MONTH,         # Learning §6.6 — verified-win LinkedIn post
}

PLAN_LIMITS: Dict[str, Dict[str, Optional[int]]] = {
    FREE: {
        "tailor": 5,
        "ats_scores": 5,
        "cover_letter": 2,
        "autoapply_answer": 10,
        "quiz": 3,
        "practice_question": 5,
        "mock_evaluate": 2,
        "salary": 3,
        "post_draft": 1,
        "learning_plan_generate": 1,
        "authored_resume": 1,         # Voice Graph — §6.12.10 (raw mode is always free)
        "graph_extract": 5,           # Voice Graph — LLM extractions per day
        "six_seconds": 3,             # Recruiter's Six Seconds — §6.11.2
        "differentiation_analysis": 1,  # Learning §6.6
        "task_verify": 5,
        "completion_post": 1,
        # Structural caps (checked against live state, not time-windowed):
        "batch_tailor_max": 0,        # batch tailoring disabled on free
        "active_learning_paths": 1,
        "writing_samples": 1,         # Voice Graph — count of finalized samples
    },
    PREMIUM: {
        "tailor": 30,
        "ats_scores": None,
        "cover_letter": None,
        "autoapply_answer": 100,
        "quiz": 20,
        "practice_question": 30,
        "mock_evaluate": 15,
        "salary": None,
        "post_draft": 8,
        "learning_plan_generate": 10,
        "authored_resume": 10,
        "graph_extract": 30,
        "six_seconds": 30,
        "differentiation_analysis": 10,
        "task_verify": 50,
        "completion_post": 8,
        "batch_tailor_max": 10,
        "active_learning_paths": 5,
        "writing_samples": 5,
    },
    PREMIUM_PLUS: {
        "tailor": 100,               # soft cap to deter abuse; effectively unlimited
        "ats_scores": None,
        "cover_letter": None,
        "autoapply_answer": None,
        "quiz": None,
        "practice_question": None,
        "mock_evaluate": None,
        "salary": None,
        "post_draft": None,
        "learning_plan_generate": None,
        "authored_resume": 100,       # soft cap to deter runaway Bedrock cost; effectively unlimited
        "graph_extract": 200,
        "six_seconds": 100,
        "differentiation_analysis": 100,
        "task_verify": None,
        "completion_post": None,
        "batch_tailor_max": 25,
        "active_learning_paths": None,
        "writing_samples": 50,        # soft cap; effectively unlimited for a real user
    },
}

# ---------------------------------------------------------------------------
# Boolean feature gates — capability is present (or not) for the plan.
# Free always gets the core: auth, onboarding, dashboard, resume tailoring,
# job search, applications tracker, basic interview prep, browse events.
# ---------------------------------------------------------------------------
_CORE = {
    "dashboard",
    "resume_tailor",
    "job_search",
    "applications",
    "interview_prep",
    "events_browse",
    "learning",
    "post_draft",
    "salary",
    "voice_graph",     # capture + raw-mode resume available on every tier (§6.12.10)
}

PLAN_FEATURES: Dict[str, set] = {
    FREE: set(_CORE) | {
        "interview_analytics_basic",   # last 5 attempts, no trends
    },
    PREMIUM: set(_CORE) | {
        "batch_tailor",
        "interview_analytics_basic",
        "interview_analytics_trends",
        "interview_fit",
        "events_personalized",
        "advisor",
        "salary_percentiles",
    },
    PREMIUM_PLUS: set(_CORE) | {
        "batch_tailor",
        "interview_analytics_basic",
        "interview_analytics_trends",
        "interview_analytics_predictive",
        "interview_fit",
        "events_personalized",
        "events_curated",
        "advisor",
        "advisor_autoactions",
        "salary_percentiles",
        "salary_negotiation",
        "post_history",
        "referral_finder",     # LinkedIn-archive-powered warm referrals (§6.11.3)
        "auto_post",           # Learning §6.6 — auto-publish a verified-win post
    },
}


# ---------------------------------------------------------------------------
# Period keys
# ---------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _period_key(feature: str) -> str:
    period = FEATURE_PERIODS.get(feature, PERIOD_DAY)
    now = _now()
    return now.strftime("%Y-%m") if period == PERIOD_MONTH else now.strftime("%Y-%m-%d")


def _resets_at(feature: str) -> datetime:
    now = _now()
    if FEATURE_PERIODS.get(feature, PERIOD_DAY) == PERIOD_MONTH:
        # First of next month, 00:00 UTC.
        year = now.year + (1 if now.month == 12 else 0)
        month = 1 if now.month == 12 else now.month + 1
        return now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Plan lookup
# ---------------------------------------------------------------------------
def get_plan(email: str) -> str:
    """Return the user's normalized plan, defaulting to free."""
    if not email:
        return DEFAULT_PLAN
    try:
        db = DBConnect().get_db()
        doc = db.users.find_one({"email": email}, {"plan": 1})
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("get_plan failed for %s: %s", email, e)
        return DEFAULT_PLAN
    plan = (doc or {}).get("plan")
    plan = plan if plan in VALID_PLANS else DEFAULT_PLAN
    # Admin per-user override escape hatch (§6.10) wins over the stored plan.
    # Defensive: any lookup/shape error simply ignores the override.
    try:
        ov = db.entitlement_overrides.find_one({"user_email": email}, {"overrides": 1})
        ov_plan = ((ov or {}).get("overrides") or {}).get("plan")
        if ov_plan in VALID_PLANS:
            return ov_plan
    except Exception:
        pass
    return plan


def quota(email_or_plan: str, feature: str) -> Optional[int]:
    """Return the integer limit for a feature (None == unlimited).

    Accepts either an email (looked up) or a plan name directly, so
    usage_service can pass a plan it already resolved.
    """
    plan = email_or_plan if email_or_plan in VALID_PLANS else get_plan(email_or_plan)
    return PLAN_LIMITS.get(plan, PLAN_LIMITS[DEFAULT_PLAN]).get(feature)


def feature_allowed(email: str, feature: str) -> bool:
    """True if the plan's feature set includes `feature` (boolean gate)."""
    plan = get_plan(email)
    return feature in PLAN_FEATURES.get(plan, PLAN_FEATURES[DEFAULT_PLAN])


def required_plans_for(feature: str) -> list:
    """Which plans unlock a boolean feature — used in upgrade prompts."""
    return [p for p in VALID_PLANS if feature in PLAN_FEATURES.get(p, set())]


# ---------------------------------------------------------------------------
# Metered usage (feature_usage collection)
# ---------------------------------------------------------------------------
def _feature_usage():
    return DBConnect().get_db().feature_usage


def get_usage(email: str, feature: str) -> Dict:
    """Snapshot of the current period's usage for a metered feature."""
    limit = quota(email, feature)
    key = _period_key(feature)
    used = 0
    try:
        rec = _feature_usage().find_one(
            {"user_email": email, "date": key, "feature": feature}, {"count": 1}
        )
        used = int(rec.get("count", 0)) if rec else 0
    except Exception as e:  # pragma: no cover
        logger.warning("get_usage failed for %s/%s: %s", email, feature, e)
    resets = _resets_at(feature)
    remaining = None if limit is None else max(0, limit - used)
    return {
        "feature": feature,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "unlimited": limit is None,
        "period": FEATURE_PERIODS.get(feature, PERIOD_DAY),
        "resets_at": resets.isoformat(),
        "resets_in_seconds": int((resets - _now()).total_seconds()),
    }


def check_quota(email: str, feature: str, n: int = 1) -> Tuple[bool, Dict]:
    """Check (without incrementing) whether n more uses are allowed."""
    usage = get_usage(email, feature)
    if usage["limit"] is None:
        return True, usage
    return usage["remaining"] >= n, usage


def increment_usage(email: str, feature: str, n: int = 1) -> Dict:
    """Atomically increment the metered counter and return refreshed usage."""
    key = _period_key(feature)
    try:
        _feature_usage().update_one(
            {"user_email": email, "date": key, "feature": feature},
            {
                "$inc": {"count": int(n)},
                "$set": {"last_at": _now()},
                "$setOnInsert": {"user_email": email, "date": key, "feature": feature},
            },
            upsert=True,
        )
    except Exception as e:  # pragma: no cover
        logger.warning("increment_usage failed for %s/%s: %s", email, feature, e)
    return get_usage(email, feature)


def check_and_increment(email: str, feature: str, n: int = 1) -> Tuple[bool, Dict]:
    """Combined check + increment for a metered feature.

    When not allowed, the returned usage reflects the pre-attempt state so the
    caller can hand it straight back to the client (mirrors usage_service).
    """
    allowed, usage = check_quota(email, feature, n)
    if not allowed:
        return False, usage
    return True, increment_usage(email, feature, n)


# ---------------------------------------------------------------------------
# Route decorator for boolean-gated features
# ---------------------------------------------------------------------------
def require_feature(feature: str):
    """Decorator: 403 `upgrade_required` if the caller's plan lacks `feature`.

    Use on routes guarding a Premium/Premium+ capability. Must be applied
    INSIDE @jwt_required so get_jwt_identity() is populated.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import jsonify
            from flask_jwt_extended import get_jwt_identity
            email = get_jwt_identity()
            if not feature_allowed(email, feature):
                return jsonify({
                    "error": "upgrade_required",
                    "feature": feature,
                    "plan": get_plan(email),
                    "required_plans": required_plans_for(feature),
                    "message": "This feature requires an upgraded plan.",
                }), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def upgrade_required_response(email: str, feature: str, usage: Optional[Dict] = None):
    """Build a 403 upgrade payload for inline (non-decorator) metered checks."""
    from flask import jsonify
    body = {
        "error": "upgrade_required",
        "feature": feature,
        "plan": get_plan(email),
        "required_plans": required_plans_for(feature) or [PREMIUM, PREMIUM_PLUS],
        "message": "You've hit your plan limit for this feature.",
    }
    if usage is not None:
        body["usage"] = usage
    return jsonify(body), 403


# ---------------------------------------------------------------------------
# Views consumed by the frontend
# ---------------------------------------------------------------------------
def summary(email: str) -> Dict:
    """Compact entitlements snapshot embedded in GET /auth/profile.

    Cheap (no live usage reads): the plan, its boolean feature flags, and the
    integer limit map so the UI can gate/locked-badge without extra round trips.
    """
    plan = get_plan(email)
    return {
        "plan": plan,
        "plan_label": PLAN_META.get(plan, {}).get("label", plan),
        "features": sorted(PLAN_FEATURES.get(plan, PLAN_FEATURES[DEFAULT_PLAN])),
        "limits": dict(PLAN_LIMITS.get(plan, PLAN_LIMITS[DEFAULT_PLAN])),
    }


def to_billing_view(email: str) -> Dict:
    """Full billing/plan view for GET /api/billing/plan: plan + live usage."""
    plan = get_plan(email)
    metered = {f: get_usage(email, f) for f in FEATURE_PERIODS}
    try:
        doc = DBConnect().get_db().users.find_one(
            {"email": email}, {"plan_since": 1, "plan": 1}
        ) or {}
    except Exception:
        doc = {}
    plan_since = doc.get("plan_since")
    return {
        "plan": plan,
        "plan_label": PLAN_META.get(plan, {}).get("label", plan),
        "plan_since": plan_since.isoformat() if hasattr(plan_since, "isoformat") else None,
        "features": sorted(PLAN_FEATURES.get(plan, PLAN_FEATURES[DEFAULT_PLAN])),
        "limits": dict(PLAN_LIMITS.get(plan, PLAN_LIMITS[DEFAULT_PLAN])),
        "usage": metered,
        "plans": {
            p: {
                **PLAN_META[p],
                "features": sorted(PLAN_FEATURES[p]),
                "limits": dict(PLAN_LIMITS[p]),
            }
            for p in VALID_PLANS
        },
    }


def set_plan(email: str, plan: str) -> Dict:
    """Set a user's plan (used by the billing webhook / admin). Returns view."""
    if plan not in VALID_PLANS:
        raise ValueError(f"invalid plan: {plan}")
    DBConnect().get_db().users.update_one(
        {"email": email},
        {"$set": {"plan": plan, "plan_since": _now()}},
    )
    return to_billing_view(email)
