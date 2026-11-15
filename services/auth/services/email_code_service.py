"""Emailed one-time codes for sign-in, sign-up and email verification.

One six-digit code per email address at a time. A code:
  * is stored as a keyed hash (HMAC with a key derived from the JWT secret),
    so a database read alone cannot recover it or brute-force it offline;
  * lives 10 minutes and works once;
  * dies after 5 wrong tries, counted atomically so parallel guesses cannot
    share one allowance;
  * is replaced, not joined, by a newer code for the same address.

The guess budget an attacker gets is (codes per hour) x (tries per code) =
5 x 5 = 25 out of 1,000,000 per hour per address; the per-address and per-IP
request limits that enforce the first factor live in the blueprint, next to
the other auth limits.

This module knows nothing about accounts. `check` proves that the caller
holds the code that was mailed to `email`; what that proof is allowed to do
(sign in, create an account, set a password) is decided in auth.py.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

COLLECTION = "auth_email_codes"
CODE_TTL_MINUTES = 10
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60

_index_ready = False


class CodeError(Exception):
    """A code that cannot be accepted. `code` is the machine-readable reason
    the client switches on; `message` is safe to show."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _codes():
    global _index_ready
    collection = DBConnect().get_db()[COLLECTION]
    if not _index_ready:
        # Same pattern as DistributedRateLimiter: best effort, once a process.
        try:
            collection.create_index("expires_at", expireAfterSeconds=0, name="ttl_expires")
            collection.create_index([("email", 1), ("created_at", -1)], name="email_created")
        except Exception:  # pragma: no cover - exists already / no permission
            pass
        _index_ready = True
    return collection


def _key() -> bytes:
    """Purpose-scoped key: the JWT secret itself is never used as a MAC key."""
    from utils.config import AppConfig
    secret = str(AppConfig.JWT_SECRET_KEY or "").encode("utf-8")
    return hmac.new(secret, b"aspirely:auth-email-code", hashlib.sha256).digest()


def _hash(email: str, code: str) -> str:
    return hmac.new(_key(), f"{email}\x00{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_code(value) -> str:
    """Digits only, so "123 456" and "123-456" pasted from the email work."""
    if not isinstance(value, (str, int)):
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())[:12]


def cooldown_remaining(email: str) -> int:
    """Seconds until this address may be sent another code (0 = now)."""
    latest = _codes().find_one({"email": email}, sort=[("created_at", -1)])
    if not latest:
        return 0
    age = (_now() - _aware(latest["created_at"])).total_seconds()
    return max(0, int(RESEND_COOLDOWN_SECONDS - age + 0.999))


def issue(email: str) -> str:
    """Create the one live code for `email` and return it for mailing."""
    code = f"{secrets.randbelow(10 ** 6):06d}"
    now = _now()
    codes = _codes()
    codes.update_many({"email": email, "used": False},
                      {"$set": {"used": True, "superseded_at": now}})
    codes.insert_one({
        "email": email,
        "code_hash": _hash(email, code),
        "attempts": 0,
        "used": False,
        "created_at": now,
        "expires_at": now + timedelta(minutes=CODE_TTL_MINUTES),
    })
    return code


def discard(email: str) -> None:
    """Kill the live code (the email carrying it was never sent). The row
    stays until its TTL so the resend cooldown still holds."""
    _codes().update_many({"email": email, "used": False},
                         {"$set": {"used": True, "discarded_at": _now()}})


def withdraw(email: str) -> None:
    """Remove the live code entirely: its email could not be sent, so there is
    nothing to wait for and no cooldown to serve."""
    _codes().delete_many({"email": email, "used": False})


def check(email: str, code: str) -> dict:
    """Return the live code row when `code` matches it. Raises CodeError.

    A match does NOT consume the code: the caller may still have to refuse
    the request for an unrelated reason (missing consent, weak password) and
    the person should be able to fix that and send the same code again. Call
    `consume` once the sign-in is going ahead. Every call counts as a try.
    """
    from pymongo import ReturnDocument

    expired = CodeError("code_expired", "That code has expired. Request a new one.")
    code = normalize_code(code)
    codes = _codes()
    row = codes.find_one({"email": email, "used": False}, sort=[("created_at", -1)])
    if not row or _aware(row["expires_at"]) <= _now():
        raise expired

    row = codes.find_one_and_update(
        {"_id": row["_id"], "used": False, "attempts": {"$lt": MAX_ATTEMPTS}},
        {"$inc": {"attempts": 1}},
        return_document=ReturnDocument.AFTER,
    )
    locked = CodeError("code_locked", "Too many wrong tries. Request a new code.")
    if not row:
        discard(email)
        raise locked

    if len(code) == 6 and hmac.compare_digest(row["code_hash"], _hash(email, code)):
        return row

    left = MAX_ATTEMPTS - int(row.get("attempts") or 0)
    if left <= 0:
        discard(email)
        raise locked
    plural = "try" if left == 1 else "tries"
    raise CodeError("code_invalid", f"That code is not right. {left} {plural} left.",
                    attempts_left=left)


def consume(row: dict) -> bool:
    """Single use: True for exactly one caller per code."""
    res = _codes().update_one({"_id": row["_id"], "used": False},
                              {"$set": {"used": True, "used_at": _now()}})
    return res.modified_count == 1
