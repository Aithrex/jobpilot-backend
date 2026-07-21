"""Progressive challenge (CAPTCHA) for guest write endpoints - issue + verify.

Provider-agnostic. No provider ships enabled (`GUEST_CHALLENGE_PROVIDER`
defaults to "none" = no-op). When an operator configures a SATISFIABLE
provider AND a per-IP-hash velocity counter exceeds the threshold, guest write
routes (onboard / matches / tailor) return ``429 {error:"challenge_required",
provider, challenge_id}`` BEFORE any budget is spent. Never triggered on a
normal first attempt (the threshold floor is 1, and the first call can only
reach count 1).

The loop is CLOSED: every issued challenge is a stored, expiring, one-time,
subject-bound record, and ``POST /api/resume/guest/challenge/verify`` calls
``verify_challenge`` to redeem it. A successful redemption clears the velocity
counter for that (kind, ip_hash) so the client can proceed. Without this, a
429 was pure theatre - the client had nothing to solve and no way to clear it.

Satisfiability rule ("never present a challenge that cannot be solved"): a
challenge is only ever issued when the configured provider has a registered
verifier AND a verification secret is configured. A provider that is
misconfigured degrades to the no-op instead of hard-blocking guests behind an
unsolvable wall.

Threat notes:
  * Replay - redemption is a conditional atomic update on ``used_at: None``,
    so a captured token is spendable exactly once even under concurrency.
  * Expiry - short TTL (default 5 min), checked before redemption.
  * Confused deputy - each challenge is bound to the issuing
    (kind, ip_hash, subject_id) via a keyed digest; solving a challenge issued
    to another client does nothing for you.
  * Forgery - challenge ids are 128 bits of os.urandom; binding digests are
    HMAC-SHA256 under GUEST_SIGNAL_HMAC_KEY and compared with compare_digest.

Velocity is keyed on the salted ip_hash (never a raw IP) with atomic $inc
per-hour counter docs (`guest_challenge_velocity`) - concurrent-Lambda safe.
With no ip_hash (GUEST_IP_HASH_SALT unset) velocity cannot be tracked and the
hook is a no-op: the seam never blocks service on missing config. No PII, no
raw tokens and no secrets are logged.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from utils.config import _get_config_value
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

PROVIDER_NONE = "none"
DEFAULT_THRESHOLD = 20  # per ip_hash per kind per hour
DEFAULT_TTL_SECONDS = 300
MAX_TTL_SECONDS = 3600

# verify_challenge error codes (all surface as 400 - NEVER 401, per the auth
# error contract: the FE force-logs-out on any 401).
ERR_NOT_REQUIRED = "challenge_not_required"
ERR_INVALID = "challenge_invalid"
ERR_EXPIRED = "challenge_expired"
ERR_USED = "challenge_already_used"
ERR_SUBJECT_MISMATCH = "challenge_subject_mismatch"
ERR_SOLUTION_REJECTED = "challenge_solution_rejected"
ERR_UNAVAILABLE = "challenge_unavailable"

_CHALLENGE_ID_LEN = 32  # uuid4().hex


def _velocity():
    return DBConnect().get_db().guest_challenge_velocity


def _challenges():
    return DBConnect().get_db().guest_challenges


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: Any) -> Optional[datetime]:
    """Mongo may hand back naive datetimes (and mongomock always does)."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# Provider registry
# ----------------------------------------------------------------------
# A verifier is ``fn(solution: str, remote_ip: str, secret: str) -> bool``.
# It must return True ONLY on a positive provider response; any error, timeout
# or malformed body must return False (fail closed).
_VERIFIERS: Dict[str, Callable[[str, str, str], bool]] = {}


def register_verifier(provider_id: str, fn: Callable[[str, str, str], bool]) -> None:
    """Register (or override) the server-side verifier for a provider id."""
    _VERIFIERS[str(provider_id or "").strip().lower()] = fn


def _siteverify(url: str):
    """Build a verifier that POSTs to a standard CAPTCHA siteverify endpoint."""

    def _verify(solution: str, remote_ip: str, secret: str) -> bool:
        try:
            import requests

            payload = {"secret": secret, "response": solution}
            if remote_ip:
                payload["remoteip"] = remote_ip
            resp = requests.post(url, data=payload, timeout=8)
            if not resp.ok:
                logger.warning("challenge siteverify HTTP %s", resp.status_code)
                return False
            return bool((resp.json() or {}).get("success") is True)
        except Exception as e:  # fail closed - a broken verifier never passes
            logger.warning("challenge siteverify failed: %s", type(e).__name__)
            return False

    return _verify


register_verifier(
    "turnstile",
    _siteverify("https://challenges.cloudflare.com/turnstile/v0/siteverify"),
)
register_verifier("hcaptcha", _siteverify("https://api.hcaptcha.com/siteverify"))
register_verifier(
    "recaptcha", _siteverify("https://www.google.com/recaptcha/api/siteverify")
)


def _secret() -> str:
    return (_get_config_value("GUEST_CHALLENGE_SECRET", "") or "").strip()


def provider() -> str:
    """The CONFIGURED challenge provider id ("none" = seam disabled)."""
    value = (_get_config_value("GUEST_CHALLENGE_PROVIDER", "") or "").strip().lower()
    return value or PROVIDER_NONE


def active_provider() -> str:
    """The provider actually in force: the configured one, but ONLY when it is
    satisfiable (a verifier is registered AND a secret is configured).

    A configured-but-unsatisfiable provider degrades to "none" with a loud log
    rather than serving guests a challenge no one can solve.
    """
    prov = provider()
    if prov == PROVIDER_NONE:
        return PROVIDER_NONE
    if prov not in _VERIFIERS:
        logger.error(
            "GUEST_CHALLENGE_PROVIDER=%s has no registered verifier; "
            "challenge seam disabled (no unsolvable challenges are issued)",
            prov,
        )
        return PROVIDER_NONE
    if not _secret():
        logger.error(
            "GUEST_CHALLENGE_PROVIDER=%s is set but GUEST_CHALLENGE_SECRET is "
            "not; challenge seam disabled (no unsolvable challenges are issued)",
            prov,
        )
        return PROVIDER_NONE
    return prov


def threshold() -> int:
    """Per-ip_hash hourly velocity threshold (config
    `GUEST_CHALLENGE_THRESHOLD`, floor 1 so a first attempt can never trip)."""
    raw = _get_config_value("GUEST_CHALLENGE_THRESHOLD", "") or ""
    try:
        return max(1, int(str(raw).strip() or DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def ttl_seconds() -> int:
    """How long an issued challenge stays redeemable (short by design)."""
    raw = _get_config_value("GUEST_CHALLENGE_TTL_SECONDS", "") or ""
    try:
        return max(30, min(MAX_TTL_SECONDS, int(str(raw).strip() or DEFAULT_TTL_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS


# ----------------------------------------------------------------------
# Subject binding
# ----------------------------------------------------------------------
def _binding_key() -> bytes:
    """Keyed digests reuse the guest signal HMAC key (already required for any
    guest identity work). With no key we fall back to an unkeyed digest: the
    binding is still a correct equality check, it is just not unforgeable -
    and challenge ids themselves are unguessable, so this is not a bypass."""
    key = (_get_config_value("GUEST_SIGNAL_HMAC_KEY", "") or "").strip()
    return key.encode("utf-8") if key else b"guest-challenge-binding"


def _subject_digest(kind: Any, ip_hash: Any, subject_id: Any) -> str:
    material = "|".join([
        str(kind or "")[:32],
        str(ip_hash or "")[:80],
        str(subject_id or "")[:80],
    ])
    return hmac.new(
        _binding_key(), material.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _valid_challenge_id(value: Any) -> Optional[str]:
    """Format-validate BEFORE the id reaches any Mongo filter (NoSQL injection:
    a dict/operator can never survive this)."""
    if not value or not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if len(candidate) != _CHALLENGE_ID_LEN:
        return None
    if any(c not in "0123456789abcdef" for c in candidate):
        return None
    return candidate


def _velocity_key(kind: Any, ip_hash: Any) -> str:
    hour = _now().strftime("%Y-%m-%dT%H")
    return f"{str(kind)[:32]}:{str(ip_hash)[:80]}:{hour}"


# ----------------------------------------------------------------------
# Issue
# ----------------------------------------------------------------------
def issue_challenge(
    kind: str, ip_hash: Optional[str], subject_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Mint and persist one pending challenge. Returns
    ``{"provider", "challenge_id"}`` or None when nothing can be issued."""
    prov = active_provider()
    if prov == PROVIDER_NONE:
        return None
    challenge_id = uuid.uuid4().hex
    now = _now()
    _challenges().insert_one({
        "_id": challenge_id,
        "provider": prov,
        "kind": str(kind)[:32],
        "subject_digest": _subject_digest(kind, ip_hash, subject_id),
        # Onboard trips happen before any session exists, so those challenges
        # are bound to (kind, ip_hash) only. Recorded explicitly so verify
        # rebuilds the SAME digest instead of guessing.
        "subject_bound": bool(subject_id),
        "velocity_key": _velocity_key(kind, ip_hash),
        "created_at": now,
        "expires_at": now + timedelta(seconds=ttl_seconds()),
        "used_at": None,
    })
    return {"provider": prov, "challenge_id": challenge_id}


def maybe_challenge(
    kind: str, ip_hash: Optional[str], subject_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Return ``{"provider", "challenge_id"}`` when the caller must solve a
    challenge, else None. Call BEFORE any breaker/quota/reservation spend.

    Never raises - a counter hiccup degrades to no challenge (the velocity
    signal is anti-abuse hardening, not the quota/breaker control itself,
    which is separately atomic and fails closed on its own).
    """
    prov = active_provider()
    if prov == PROVIDER_NONE:
        return None  # default: complete no-op, nothing counted
    if not ip_hash:
        return None  # no salted ip_hash -> no velocity signal, never block
    try:
        from pymongo import ReturnDocument

        key = _velocity_key(kind, ip_hash)
        doc = _velocity().find_one_and_update(
            {"_id": key},
            {"$inc": {"count": 1}, "$setOnInsert": {"created_at": _now()}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        count = int((doc or {}).get("count") or 0)
        if count > threshold():
            logger.info("guest challenge required (kind=%s count=%d)", kind, count)
            return issue_challenge(kind, ip_hash, subject_id)
        return None
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("guest challenge velocity check failed (skipping): %s", e)
        return None


# ----------------------------------------------------------------------
# Verify
# ----------------------------------------------------------------------
def verify_challenge(
    challenge_id: Any,
    solution: Any,
    ip_hash: Optional[str],
    subject_id: Optional[str] = None,
    remote_ip: str = "",
) -> Dict[str, Any]:
    """Redeem a solved challenge. Returns ``{"ok": True}`` or
    ``{"ok": False, "error": <code>}``.

    Order of checks is deliberate: everything that could let an attacker BURN
    someone else's pending challenge (binding, expiry) is checked before the
    one-time consumption, and the provider solution is validated before the
    record is marked used. The final consumption is a conditional atomic
    update on ``used_at: None``, so two racing redemptions cannot both win.
    """
    prov = active_provider()
    if prov == PROVIDER_NONE:
        # Nothing to verify: either the seam is off or the provider is
        # unsatisfiable. Do not pretend success - and do not clear counters.
        return {"ok": False, "error": ERR_NOT_REQUIRED}

    cid = _valid_challenge_id(challenge_id)
    if not cid:
        return {"ok": False, "error": ERR_INVALID}
    if not isinstance(solution, str) or not solution.strip():
        return {"ok": False, "error": ERR_SOLUTION_REJECTED}
    solution = solution.strip()[:4096]

    try:
        doc = _challenges().find_one({"_id": cid})
    except Exception as e:
        logger.error("challenge lookup failed: %s", type(e).__name__)
        return {"ok": False, "error": ERR_UNAVAILABLE}
    if not doc:
        return {"ok": False, "error": ERR_INVALID}

    # Binding first: a challenge issued to another (kind, ip_hash, subject) is
    # rejected WITHOUT being consumed, so this cannot be used to burn it.
    expected = _subject_digest(
        doc.get("kind"), ip_hash, subject_id if doc.get("subject_bound") else None
    )
    if not hmac.compare_digest(str(doc.get("subject_digest") or ""), expected):
        logger.info("challenge rejected: subject binding mismatch")
        return {"ok": False, "error": ERR_SUBJECT_MISMATCH}

    expires_at = _as_aware(doc.get("expires_at"))
    if expires_at is None or expires_at <= _now():
        return {"ok": False, "error": ERR_EXPIRED}
    if doc.get("used_at") is not None:
        return {"ok": False, "error": ERR_USED}

    verifier = _VERIFIERS.get(prov)
    if verifier is None:  # pragma: no cover - active_provider already checked
        return {"ok": False, "error": ERR_UNAVAILABLE}
    if not verifier(solution, remote_ip or "", _secret()):
        return {"ok": False, "error": ERR_SOLUTION_REJECTED}

    # One-time use: only the update that flips used_at from None wins.
    try:
        result = _challenges().update_one(
            {"_id": cid, "used_at": None}, {"$set": {"used_at": _now()}}
        )
    except Exception as e:
        logger.error("challenge consumption failed: %s", type(e).__name__)
        return {"ok": False, "error": ERR_UNAVAILABLE}
    if getattr(result, "modified_count", 0) < 1:
        return {"ok": False, "error": ERR_USED}

    _clear_velocity(doc.get("velocity_key"))
    logger.info("guest challenge solved (kind=%s)", doc.get("kind"))
    return {"ok": True, "kind": doc.get("kind")}


def _clear_velocity(velocity_key: Any) -> None:
    """Reset the counter that produced the challenge so the client can proceed.

    Reset (not decrement): the point of solving is to buy the client a fresh
    allowance for the rest of the hour. Best effort - a failure here only means
    the client may be challenged again, never that it is wrongly admitted.
    """
    if not velocity_key or not isinstance(velocity_key, str):
        return
    try:
        _velocity().update_one(
            {"_id": velocity_key[:200]},
            {"$set": {"count": 0, "cleared_at": _now()}},
        )
    except Exception as e:  # pragma: no cover - best effort
        logger.warning("challenge velocity reset failed: %s", type(e).__name__)


def ensure_indexes() -> None:
    """TTL reaper for redeemed/abandoned challenge records (best effort)."""
    try:
        _challenges().create_index("expires_at", expireAfterSeconds=0)
    except Exception:  # pragma: no cover
        pass
