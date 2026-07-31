"""Signed magic-link approval tokens for the auto-apply email loop (Tranche 1 §2).

The temporary approval channel is signed magic-link BUTTONS in email (not
reply-parsing); the same token machinery later backs WhatsApp buttons. A link
authorizes exactly ONE action for ONE user over a FIXED set of application ids,
expires, and is single-use.

Token format (spec §2, exact):

    base64url(payload) + "." + base64url(HMAC_SHA256(payload, APPROVAL_LINK_SECRET))

  * ``payload`` = canonical JSON of
    ``{jti, user_email, action, application_ids, exp}`` (sorted keys, compact
    separators — the HMAC is computed over the base64url payload segment, so
    what is signed is byte-for-byte what travels).
  * ``APPROVAL_LINK_SECRET`` is a DEDICATED secret from SSM/env (never the JWT
    secret) — see ``_secret()``.

Verification (``verify_and_claim``) rejects on ANY failure, in this order:
  1. constant-time HMAC compare (``hmac.compare_digest``) — nothing in the
     payload is trusted before this passes;
  2. payload shape + ``exp`` check;
  3. atomic single-use claim of ``jti`` in the ``approval_tokens`` collection
     (status-guarded ``find_one_and_update`` — two racing clicks resolve to
     exactly one winner, same idiom as ``auto_submit_service._atomic_transition``).

Collection **approval_tokens** (see scripts/migrate_indexes.py for the unique
``jti`` index + the TTL index on ``exp``; GDPR cascade via account_service):

    { jti: uuid, user_email, action, application_ids: [..],
      exp: datetime, used: bool, used_at, created_at }

Nothing here calls an LLM and nothing here is request-scoped — the scheduler
(§5) mints links via ``make_link`` and the authless
``GET /api/apply/approve-link`` route consumes them via ``verify_and_claim``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets as _pysecrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from utils.config import _get_config_value
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

# The only actions a link may carry (spec §2). Anything else fails verification.
ACTIONS = ("approve_all", "approve_one", "review", "skip")

# Approval emails go out hourly; links stay clickable for 3 days, then the TTL
# index reaps the jti record and the link is dead even if the clock check were
# somehow bypassed.
DEFAULT_TTL_SECONDS = 72 * 3600

# Hygiene caps — a token is a URL query param, keep it bounded.
_MAX_APPLICATION_IDS = 100
_MAX_TOKEN_LENGTH = 8192
_MAX_ID_LENGTH = 256


# ---------------------------------------------------------------------------
# Typed errors — the route maps each to a friendly HTML page + status. Never
# 401 (reserved for missing/invalid JWT); link failures are 400/403/409/503.
# ---------------------------------------------------------------------------
class ApprovalTokenError(Exception):
    status = 400
    error = "approval_token_error"

    def __init__(self, message: Optional[str] = None):
        self.message = message or self.error
        super().__init__(self.message)


class TokenInvalid(ApprovalTokenError):
    """Bad signature, malformed payload, unknown/revoked jti — never trusted."""
    status = 403
    error = "invalid_link"


class TokenExpired(ApprovalTokenError):
    status = 403
    error = "link_expired"


class TokenAlreadyUsed(ApprovalTokenError):
    """Single-use claim lost — the link already did its job. Carries the
    (signature-verified) payload so the route can answer idempotently, e.g.
    still 302 a re-clicked `review` link into the app."""
    status = 409
    error = "link_already_used"

    def __init__(self, message: Optional[str] = None,
                 payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.payload = payload or {}


class TokenServiceNotConfigured(ApprovalTokenError):
    """APPROVAL_LINK_SECRET missing outside development — fail closed."""
    status = 503
    error = "approval_links_unavailable"


# ---------------------------------------------------------------------------
# Secret + collection
# ---------------------------------------------------------------------------
_dev_secret: Optional[str] = None


def _secret() -> bytes:
    """The dedicated link-signing secret.

    Resolution mirrors config._get_jwt_secret_key: SSM/env via
    ``_get_config_value('APPROVAL_LINK_SECRET')``; in production a missing
    secret FAILS CLOSED (mint and verify both refuse); in development we fall
    back to a per-process random secret so local flows work — links simply die
    on restart. NEVER reuse JWT_SECRET_KEY here (spec §2).
    """
    value = _get_config_value("APPROVAL_LINK_SECRET", None)
    if value:
        return value.encode("utf-8")
    env = os.getenv("ENVIRONMENT", "development").strip().lower()
    if env != "development":
        raise TokenServiceNotConfigured(
            "APPROVAL_LINK_SECRET is not configured — refusing to sign/verify links"
        )
    global _dev_secret
    if _dev_secret is None:
        _dev_secret = _pysecrets.token_hex(32)
        logger.warning(
            "approval_token_service: APPROVAL_LINK_SECRET unset — using a "
            "per-process dev secret (links break on restart)"
        )
    return _dev_secret.encode("utf-8")


def _tokens():
    return DBConnect().get_db().approval_tokens


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# base64url (no padding) + HMAC helpers
# ---------------------------------------------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(seg: str) -> bytes:
    seg = str(seg or "")
    pad = (-len(seg)) % 4
    return base64.urlsafe_b64decode(seg + "=" * pad)


def _sign(payload_b64: str) -> bytes:
    """HMAC-SHA256 over the base64url payload segment — signing the exact bytes
    that travel removes any JSON re-canonicalization ambiguity on verify."""
    return hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_ids(application_ids: Any) -> List[str]:
    if application_ids is None:
        application_ids = []
    if not isinstance(application_ids, list):
        raise TokenInvalid("application_ids must be a list")
    if len(application_ids) > _MAX_APPLICATION_IDS:
        raise TokenInvalid("too many application_ids for one link")
    out: List[str] = []
    for aid in application_ids:
        # Plain bounded strings ONLY — these ids end up in a Mongo $in filter,
        # so nothing operator-shaped (dicts, lists) may ever pass through.
        if not isinstance(aid, str) or not aid or len(aid) > _MAX_ID_LENGTH:
            raise TokenInvalid("invalid application id")
        out.append(aid)
    return out


def _validate_payload(payload: Any) -> Dict[str, Any]:
    """Shape-check a decoded payload. Called AFTER the signature has verified,
    so this is belt-and-braces against a malformed mint, not against forgery."""
    if not isinstance(payload, dict):
        raise TokenInvalid("payload is not an object")
    jti = payload.get("jti")
    user_email = payload.get("user_email")
    action = payload.get("action")
    exp = payload.get("exp")
    if not isinstance(jti, str) or not jti or len(jti) > 64:
        raise TokenInvalid("bad jti")
    if not isinstance(user_email, str) or "@" not in user_email or len(user_email) > 320:
        raise TokenInvalid("bad user_email")
    if action not in ACTIONS:
        raise TokenInvalid("unknown action")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise TokenInvalid("bad exp")
    return {
        "jti": jti,
        "user_email": user_email,
        "action": action,
        "application_ids": _validate_ids(payload.get("application_ids")),
        "exp": int(exp),
    }


# ---------------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------------
def make_token(action: str, user_email: str, application_ids: Optional[List[str]] = None,
               ttl_seconds: Optional[int] = None) -> str:
    """Mint a signed, single-use approval token and persist its ``jti`` record.

    The record is inserted BEFORE the token string is returned, so any link a
    user can ever click is claimable exactly once (an unpersisted jti fails
    verification as unknown).
    """
    if action not in ACTIONS:
        raise ValueError(f"invalid action: {action!r} (expected one of {ACTIONS})")
    if not user_email or not isinstance(user_email, str):
        raise ValueError("user_email is required")
    ids = _validate_ids(list(application_ids or []))

    now = _now()
    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else max(60, int(ttl_seconds))
    exp_dt = now + timedelta(seconds=ttl)
    jti = str(uuid.uuid4())

    payload = {
        "jti": jti,
        "user_email": user_email,
        "action": action,
        "application_ids": ids,
        "exp": int(exp_dt.timestamp()),
    }
    payload_b64 = _b64e(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    sig_b64 = _b64e(_sign(payload_b64))

    _tokens().insert_one({
        "jti": jti,
        "user_email": user_email,
        "action": action,
        "application_ids": ids,
        "exp": exp_dt,          # datetime → the TTL index reaps expired records
        "used": False,
        "used_at": None,
        "created_at": now,
    })
    return f"{payload_b64}.{sig_b64}"


def make_link(action: str, user_email: str, application_ids: Optional[List[str]] = None,
              base_url: Optional[str] = None, ttl_seconds: Optional[int] = None) -> str:
    """A clickable approve-link URL for an email button.

    Targets the authless-but-signature-verified
    ``GET /api/apply/approve-link`` route on the jobs Lambda (rides the
    existing ``ANY /api/apply/{proxy+}`` Terraform route — no new prefix).
    ``base_url`` defaults to the backend's public URL.
    """
    token = make_token(action, user_email, application_ids, ttl_seconds=ttl_seconds)
    if not base_url:
        from utils.config import AppUrlsConfig
        base_url = AppUrlsConfig.BACKEND_PUBLIC_URL
    return f"{base_url.rstrip('/')}/api/apply/approve-link?token={quote(token, safe='')}"


# ---------------------------------------------------------------------------
# Verify + claim
# ---------------------------------------------------------------------------
def verify(token: str) -> Dict[str, Any]:
    """Verify a token WITHOUT consuming its single use (a read-only *peek*).

    Runs the same cryptographic authenticity + shape + expiry checks as
    ``verify_and_claim`` (steps 1-2), but does NOT touch the ``used`` flag, so
    the token stays claimable exactly once by a later ``verify_and_claim``.

    P1-APPROVE-GET: the emailed magic-link GET calls this to decide where to
    redirect (it must be side-effect-free — a bare GET must never mutate), and
    the authenticated, CSRF-safe POST calls ``verify_and_claim`` to actually
    consume the token and perform the approval. Raises a typed
    ``ApprovalTokenError`` on any failure; returns the verified payload
    ``{jti, user_email, action, application_ids, exp}`` on success.

    NOTE: this does not check whether the ``jti`` record still exists / has been
    used — that is the claim's job. A peek is authenticity + freshness only.
    """
    claim, _payload_b64 = _authenticate(token)
    if _now().timestamp() >= claim["exp"]:
        raise TokenExpired("link expired")
    return claim


def _authenticate(token: str) -> tuple[Dict[str, Any], str]:
    """Shared verify core: constant-time HMAC compare + payload shape check.

    Returns ``(claim, payload_b64)``. Does NOT check expiry or single-use — the
    two public entrypoints (``verify`` peek, ``verify_and_claim``) layer those on.
    """
    if not token or not isinstance(token, str) or len(token) > _MAX_TOKEN_LENGTH:
        raise TokenInvalid("missing or oversized token")
    parts = token.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise TokenInvalid("malformed token")
    payload_b64, sig_b64 = parts

    # 1. Cryptographic authenticity FIRST — constant-time compare; nothing in
    #    the payload is even parsed until the signature holds.
    expected = _sign(payload_b64)
    try:
        provided = _b64d(sig_b64)
    except (ValueError, TypeError):
        raise TokenInvalid("bad signature encoding")
    if not hmac.compare_digest(expected, provided):
        raise TokenInvalid("signature mismatch")

    try:
        payload = json.loads(_b64d(payload_b64).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        raise TokenInvalid("bad payload encoding")
    return _validate_payload(payload), payload_b64


def verify_and_claim(token: str) -> Dict[str, Any]:
    """Verify a token and atomically claim its single use.

    Order (spec §2): constant-time HMAC compare → ``exp`` check → atomic
    single-use claim of ``jti``. Raises a typed ``ApprovalTokenError`` on any
    failure; returns the verified payload
    ``{jti, user_email, action, application_ids, exp}`` on success.
    """
    claim, _payload_b64 = _authenticate(token)

    # 2. Expiry (the TTL index is the backstop; this is the authoritative check).
    if _now().timestamp() >= claim["exp"]:
        raise TokenExpired("link expired")

    # 3. Atomic single-use claim — compare-and-set on used, so two racing
    #    clicks (or Lambdas) resolve to exactly one winner.
    now = _now()
    rec = _tokens().find_one_and_update(
        {"jti": claim["jti"], "used": {"$ne": True}},
        {"$set": {"used": True, "used_at": now}},
    )
    if rec is None:
        if _tokens().find_one({"jti": claim["jti"]}) is not None:
            raise TokenAlreadyUsed(payload=claim)
        # Signed but no record: TTL-reaped, purged (account deletion), or never
        # persisted — treat as invalid, never honor it.
        raise TokenInvalid("unknown or revoked token")

    # 4. Defense-in-depth: the stored record must agree with the signed payload.
    if rec.get("user_email") != claim["user_email"] or rec.get("action") != claim["action"]:
        logger.warning("approval_token: payload/record mismatch for jti=%s", claim["jti"])
        raise TokenInvalid("token record mismatch")

    return claim
