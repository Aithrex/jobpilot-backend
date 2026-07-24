"""Runtime platform configuration (`platform_config` collection).

Generic admin-editable knobs that must change WITHOUT a deploy. First (and so
far only) consumer: Guest Onboarding — one doc `{_id: "guest_onboarding"}`
holding quotas/thresholds the admin tunes from /api/admin/guest-config.

Read path is Lambda-warm-container friendly: a 60s in-process TTL cache so a
hot container does at most one Mongo read per minute per key. Writes go
through `update_guest_config` which also invalidates the local cache (other
warm containers converge within the TTL — acceptable for quota knobs).

No user input ever reaches a Mongo filter here: the `_id` is a hardcoded
constant and every written field name comes from the DEFAULTS allowlist.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

GUEST_CONFIG_ID = "guest_onboarding"
_CACHE_TTL_SECONDS = 60

# Spec'd defaults (docs/specs/guest-onboarding.md v2 §platform_config).
DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "guest_tailor_limit": 1,          # LIFETIME guest tailors per quota subject (v2: exactly one trial)
    # jd_match_scorer.score_one = matched/max(8, total_skills)*100 — real resumes
    # land ~20-55. Real postings name only 3-6 specific technologies, so with
    # the scorer's denominator clamp (max 14) a typical genuine fit scores
    # 21-43: the bar is 25 (posting names ~a quarter of the resume's core
    # stack). 40 starved skill-rich resumes to zero results (2026-07-20
    # incident). Don't raise it without re-tuning the scorer.
    "guest_jobs_min_score": 25,       # match bar: see scorer tuning note above
    "guest_jobs_max": 5,              # v2: exactly 5 strong matches, server-enforced
    "free_daily_tailors": 5,          # free plan daily tailors (post-signup)
    "free_jobs_daily_cap": 10,        # free plan daily job-match cap (post-signup)
    "global_daily_guest_tailors": 200,  # platform-wide anonymous-LLM circuit breaker
    "global_daily_guest_parses": 500,   # platform-wide cap on anonymous resume parses
    "global_daily_guest_matches": 1000,  # platform-wide cap on anonymous match scrapes (cache misses)
    "retention_days": 30,             # v2: unclaimed guest data kept 30 days (0 = forever)
}

# Validation bounds for admin PUTs: key -> (type, min, max). `enabled` is bool.
BOUNDS: Dict[str, Tuple[int, int]] = {
    "guest_tailor_limit": (0, 100),
    "guest_jobs_min_score": (0, 100),
    "guest_jobs_max": (1, 50),
    "free_daily_tailors": (0, 1000),
    "free_jobs_daily_cap": (1, 1000),
    "global_daily_guest_tailors": (0, 100000),
    "global_daily_guest_parses": (0, 100000),
    "global_daily_guest_matches": (0, 100000),
    "retention_days": (0, 3650),
}

# In-process TTL cache: {"at": monotonic_seconds, "value": dict}
_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def _collection():
    return DBConnect().get_db().platform_config


def invalidate_cache() -> None:
    """Drop the in-process cache (used after writes and in tests)."""
    _cache["at"] = 0.0
    _cache["value"] = None


def get_guest_config(force_refresh: bool = False) -> Dict[str, Any]:
    """Effective guest-onboarding config: stored doc merged over DEFAULTS.

    Never raises — a Mongo hiccup degrades to the last cached value or the
    code defaults, so a config-read failure can't take guest routes down.
    """
    now = time.monotonic()
    if (
        not force_refresh
        and _cache["value"] is not None
        and now - _cache["at"] < _CACHE_TTL_SECONDS
    ):
        return dict(_cache["value"])

    merged = dict(DEFAULTS)
    try:
        doc = _collection().find_one({"_id": GUEST_CONFIG_ID}) or {}
        for key in DEFAULTS:
            if key in doc:
                merged[key] = doc[key]
        merged["updated_at"] = doc.get("updated_at")
        merged["updated_by"] = doc.get("updated_by")
        _cache["value"] = dict(merged)
        _cache["at"] = now
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("platform_config read failed (using defaults/cache): %s", e)
        if _cache["value"] is not None:
            return dict(_cache["value"])
        merged.setdefault("updated_at", None)
        merged.setdefault("updated_by", None)
    return merged


def get_int(key: str, default: Optional[int] = None) -> int:
    """Typed getter for an integer knob (falls back to DEFAULTS)."""
    cfg = get_guest_config()
    val = cfg.get(key, DEFAULTS.get(key, default))
    try:
        return int(val)
    except (TypeError, ValueError):
        return int(DEFAULTS.get(key, default or 0))


def get_bool(key: str, default: bool = False) -> bool:
    cfg = get_guest_config()
    return bool(cfg.get(key, DEFAULTS.get(key, default)))


def validate_guest_config_patch(patch: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Validate an admin PUT body against the allowlist + bounds.

    Returns (clean_patch, errors). Unknown keys are rejected (not ignored) so
    admin typos surface instead of silently doing nothing.
    """
    clean: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    if not isinstance(patch, dict):
        return {}, {"_body": "Body must be a JSON object"}

    for key, value in patch.items():
        if key == "enabled":
            if not isinstance(value, bool):
                errors[key] = "must be a boolean"
            else:
                clean[key] = value
            continue
        if key not in BOUNDS:
            errors[key] = "unknown config key"
            continue
        # bools are ints in Python — reject them explicitly for numeric knobs.
        if isinstance(value, bool) or not isinstance(value, int):
            errors[key] = "must be an integer"
            continue
        lo, hi = BOUNDS[key]
        if not (lo <= value <= hi):
            errors[key] = f"must be between {lo} and {hi}"
            continue
        clean[key] = value
    return clean, errors


def update_guest_config(patch: Dict[str, Any], updated_by: str) -> Dict[str, Any]:
    """Apply a PRE-VALIDATED patch (see validate_guest_config_patch) with audit
    fields, invalidate the cache, and return the effective config."""
    if patch:
        _collection().update_one(
            {"_id": GUEST_CONFIG_ID},
            {
                "$set": {
                    **{k: v for k, v in patch.items() if k in DEFAULTS},
                    "updated_at": datetime.now(timezone.utc),
                    "updated_by": updated_by,
                }
            },
            upsert=True,
        )
    invalidate_cache()
    return get_guest_config(force_refresh=True)
