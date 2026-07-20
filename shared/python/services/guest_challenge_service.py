"""Progressive challenge (CAPTCHA) hook for guest write endpoints — v2 seam.

Provider-agnostic: NO provider ships in v2 (`GUEST_CHALLENGE_PROVIDER`
defaults to "none" = no-op). When an operator configures a provider AND a
per-IP-hash velocity counter exceeds the threshold, guest write routes
(onboard / matches / tailor) return ``429 {error:"challenge_required",
provider}`` BEFORE any budget is spent. Never triggered on a normal first
attempt (the threshold floor is 1, and the first call can only reach count 1).

Velocity is keyed on the salted ip_hash (never a raw IP) with atomic $inc
per-hour counter docs (`guest_challenge_velocity`) — concurrent-Lambda safe.
With no ip_hash (GUEST_IP_HASH_SALT unset) velocity cannot be tracked and the
hook is a no-op: the seam never blocks service on missing config. No PII and
no raw tokens are logged.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.config import _get_config_value
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

PROVIDER_NONE = "none"
DEFAULT_THRESHOLD = 20  # per ip_hash per kind per hour


def _velocity():
    return DBConnect().get_db().guest_challenge_velocity


def _now() -> datetime:
    return datetime.now(timezone.utc)


def provider() -> str:
    """The configured challenge provider id ("none" = seam disabled)."""
    value = (_get_config_value("GUEST_CHALLENGE_PROVIDER", "") or "").strip().lower()
    return value or PROVIDER_NONE


def threshold() -> int:
    """Per-ip_hash hourly velocity threshold (config
    `GUEST_CHALLENGE_THRESHOLD`, floor 1 so a first attempt can never trip)."""
    raw = _get_config_value("GUEST_CHALLENGE_THRESHOLD", "") or ""
    try:
        return max(1, int(str(raw).strip() or DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def maybe_challenge(
    kind: str, ip_hash: Optional[str], subject_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return ``{"provider": <id>}`` when the caller must solve a challenge,
    else None. Call BEFORE any breaker/quota/reservation spend.

    ``subject_id`` is accepted for future risk signals; v2 keys velocity on
    ip_hash only. Never raises — a counter hiccup degrades to no challenge.
    """
    prov = provider()
    if prov == PROVIDER_NONE:
        return None  # default: complete no-op, nothing counted
    if not ip_hash:
        return None  # no salted ip_hash → no velocity signal, never block
    try:
        from pymongo import ReturnDocument

        hour = _now().strftime("%Y-%m-%dT%H")
        key = f"{str(kind)[:32]}:{str(ip_hash)[:80]}:{hour}"
        doc = _velocity().find_one_and_update(
            {"_id": key},
            {"$inc": {"count": 1}, "$setOnInsert": {"created_at": _now()}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        count = int((doc or {}).get("count") or 0)
        if count > threshold():
            logger.info("guest challenge required (kind=%s count=%d)", kind, count)
            return {"provider": prov}
        return None
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("guest challenge velocity check failed (skipping): %s", e)
        return None
