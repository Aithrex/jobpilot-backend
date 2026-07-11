"""Mongo-backed email suppression list (SES bounce / complaint handling).

When SES reports a permanent bounce or a complaint (via SNS → the auth
`/api/auth/ses-notifications` webhook), we record the recipient here so we
never mail them again. Continuing to send to a hard-bounced or complaining
address damages our SES sending reputation and can get the account throttled.

Storage: `email_suppressions`, one document per address:
    { "email": <lowercased+stripped>, "reason": "bounce"|"complaint",
      "detail": <short subtype str>, "created_at": <utc-aware datetime> }

Everything here is best-effort: `is_suppressed` must NEVER raise (a suppression
store outage must not block a legitimate password-reset email). Import-safe — no
I/O at import time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

COLLECTION = "email_suppressions"


def _collection():
    return DBConnect().get_collection(COLLECTION)


def _normalize(email: str) -> str:
    """Lowercase + strip + sanitize an address for use in a Mongo filter.

    We route through InputSanitizer to strip control characters / operator
    prefixes so an SES-reported (i.e. externally-influenced) address can never
    be used to smuggle a NoSQL operator into the filter.
    """
    if not email:
        return ""
    cleaned = InputSanitizer.sanitize_email(email)
    if cleaned:
        return cleaned
    # Fall back to a defensive sanitize for addresses SES considers valid but
    # that don't match our strict RFC pattern — still never an operator.
    return InputSanitizer.sanitize_string(str(email).strip().lower(), 320)


def add_suppression(email: str, reason: str, detail: str = "") -> None:
    """Upsert a suppression record for `email` (idempotent by address)."""
    e = _normalize(email)
    if not e:
        return
    reason = str(reason or "").strip()[:32]
    detail = InputSanitizer.sanitize_string(str(detail or ""), 256)
    now = datetime.now(timezone.utc)
    try:
        _collection().update_one(
            {"email": e},
            {
                "$set": {"email": e, "reason": reason, "detail": detail},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        logger.info("[email:suppressed] recorded email=%s reason=%s", e, reason)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("[email:suppression] add failed for %s: %s", e, exc)


def is_suppressed(email: str) -> bool:
    """True if `email` is on the suppression list. Never raises."""
    e = _normalize(email)
    if not e:
        return False
    try:
        return _collection().find_one({"email": e}, {"_id": 1}) is not None
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("[email:suppression] lookup failed for %s: %s", e, exc)
        return False


def remove_suppression(email: str) -> None:
    """Delete a suppression record (manual un-suppress)."""
    e = _normalize(email)
    if not e:
        return
    try:
        _collection().delete_one({"email": e})
        logger.info("[email:suppression] removed email=%s", e)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("[email:suppression] remove failed for %s: %s", e, exc)
