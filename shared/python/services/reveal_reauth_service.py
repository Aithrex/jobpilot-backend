"""Step-up re-authentication for the ATS credential reveal (Tranche 1 §4).

Revealing a stored employer password is the ONE plaintext egress in the vault
(``ats_credential_service.reveal``), so the route demands a FRESH proof of
identity on top of the request's JWT. This module owns that proof.

WHY A SECOND METHOD EXISTS
--------------------------
The original gate was "re-type your Aspirely password". Google is the primary
signup path, and an OAuth-only account has NO ``password_hash`` at all — those
users hit ``reauth_unavailable`` and could NEVER reach a password we generated
for them and stored on their behalf. The honest error had no way forward.

So the gate is now a set of METHODS, and the account decides which apply:

  * ``password``   — bcrypt re-auth against ``users.password_hash``. Offered
                     only when the account actually has one. Unchanged, and not
                     weakened: same hash, same constant-time bcrypt compare.
  * ``email_code`` — a 6-digit one-time code mailed to the REGISTERED address
                     (``users.email``, which is also the JWT identity). Always
                     available, because the account is bound to that address by
                     construction.

Is the email code weaker than the password? No — it is exactly the ceiling that
already existed. Anyone who controls the registered inbox can already run
"Forgot password", set a password, and use the password path. The code removes a
pointless detour, it does not lower the bar. It is therefore also offered to
password users as a fallback (they forget passwords too).

WHAT THE CODE COSTS AN ATTACKER
-------------------------------
Holding a stolen JWT is NOT enough for either method. For ``email_code`` the
attacker additionally needs the user's inbox, and:

  * the code is 6 CSPRNG digits (``secrets.randbelow``);
  * it lives ``_CODE_TTL_SECONDS`` (10 min) and is reaped by a TTL index;
  * it is SINGLE-USE, claimed atomically (a status-guarded update — two racing
    submissions resolve to exactly one winner);
  * at most ONE challenge is live per user; sending a new code invalidates the
    previous one, so codes never pool;
  * ``_MAX_ATTEMPTS`` (5) wrong guesses burn the challenge outright;
  * sends are rate-limited across containers (``check_rate_limit``), and the
    reveal route keeps its own 5-per-5-min budget on top;
  * only the bcrypt HASH of the code is stored — a Mongo read cannot replay it,
    and offline recovery of a 6-digit code at 12 rounds costs far longer than
    the 10-minute window it would be valid for.

Collection ``credential_reveal_challenges`` (one doc per user; indexes are
lazily ensured here, mirroring ``ats_credential_service``, and re-asserted by
scripts/migrate_indexes.py)::

    { user_email, code_hash, created_at, expires_at, attempts,
      consumed_at | None }

The plaintext code exists only inside :func:`send_email_code`, is handed
straight to the mail template, and is NEVER logged or returned to the caller.
If the mail provider does not confirm the send we DELETE the challenge — the
dev/ops console fallback in ``email_service`` prints the body, and a printed
code that still works would be a real leak.

Typed errors carry the platform's status contract: business failures are
400/403/429/503 — NEVER 401 (401 is reserved for a missing/invalid JWT).
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from models.user import User
from utils.db_connect import DBConnect
from utils.security import check_rate_limit

logger = logging.getLogger(__name__)

#: Re-auth methods this module can verify. Order is the UI's preference order.
METHOD_PASSWORD = "password"
METHOD_EMAIL_CODE = "email_code"

_CODE_DIGITS = 6
_CODE_TTL_SECONDS = 600          # 10 minutes
_MAX_ATTEMPTS = 5                # wrong guesses before the challenge is burned
_SEND_MAX = 3                    # codes a user may request ...
_SEND_WINDOW_SECONDS = 900       # ... per 15 minutes (distributed counter)

_indexes_ensured = False


class ReauthError(Exception):
    """Typed business error → (error, message, status). Never 401."""

    def __init__(self, error: str, message: str, status: int = 400):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coll():
    return DBConnect().get_db().credential_reveal_challenges


def _users():
    return DBConnect().get_db().users


def _ensure_indexes() -> None:
    """Lazy one-time ensure (ats_credential_service precedent). Names match
    scripts/migrate_indexes.py so the migration re-assert is a no-op."""
    global _indexes_ensured
    if _indexes_ensured:
        return
    try:
        _coll().create_index(
            [("user_email", 1)], unique=True,
            name="user_email_unique", background=True,
        )
        # Server-side reaping: a forgotten challenge cannot outlive its window.
        _coll().create_index(
            [("expires_at", 1)], expireAfterSeconds=0,
            name="ttl_expires_at", background=True,
        )
        _indexes_ensured = True
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("credential_reveal_challenges index ensure failed: %s", e)


def _normalize_email(user_email: Any) -> str:
    """The JWT identity, normalized. It is the ONLY address a code is ever sent
    to — no request field may name a destination."""
    if not isinstance(user_email, str):
        raise ReauthError("invalid_request", "A valid account is required.", 400)
    email = User.sanitize_email(user_email)
    if not email:
        raise ReauthError("invalid_request", "A valid account is required.", 400)
    return email


def mask_email(email: str) -> str:
    """``sandeep@example.com`` → ``s*****p@example.com``. Enough for the user to
    recognize their own inbox, not enough to disclose an unknown address."""
    if not isinstance(email, str) or "@" not in email:
        return ""
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"{local[:1]}*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


def _has_password(user_email: str) -> bool:
    """True when the account can do a bcrypt re-auth. THE single source of the
    old ``reauth_unavailable`` decision — the route no longer decides."""
    try:
        doc = _users().find_one({"email": user_email}, {"password_hash": 1})
    except Exception as e:
        logger.exception("reveal reauth: user lookup failed: %s", e)
        raise ReauthError(
            "reauth_unavailable_temporarily",
            "We couldn't verify your account just now. Please try again.",
            503,
        ) from None
    return bool(doc and isinstance(doc.get("password_hash"), str)
                and doc["password_hash"])


def available_methods(user_email: Any) -> List[str]:
    """Which re-auth methods this account can actually complete.

    ``email_code`` is always present: the account IS an email address, so the
    factor exists for every signup path (that is the whole point). ``password``
    is present only when a hash exists — an OAuth-only user never sees a
    password box they cannot fill.
    """
    email = _normalize_email(user_email)
    methods = [METHOD_EMAIL_CODE]
    if _has_password(email):
        methods.insert(0, METHOD_PASSWORD)
    return methods


def methods_view(user_email: Any) -> Dict[str, Any]:
    """The FE-facing shape, so the client never has to infer capability from an
    error response."""
    email = _normalize_email(user_email)
    return {
        "methods": available_methods(email),
        "email_hint": mask_email(email),
        "code_ttl_seconds": _CODE_TTL_SECONDS,
        "code_length": _CODE_DIGITS,
    }


# ---------------------------------------------------------------------------
# Email one-time code
# ---------------------------------------------------------------------------
def _generate_code() -> str:
    """CSPRNG, zero-padded, fixed width (so a leading zero is never dropped)."""
    return f"{secrets.randbelow(10 ** _CODE_DIGITS):0{_CODE_DIGITS}d}"


def _normalize_code(value: Any) -> str:
    """Accept what a human pastes out of an email (spaces, hyphens, a stray
    zero-width) and reduce it to digits. Anything else is not a code."""
    if not isinstance(value, str):
        raise ReauthError("code_invalid", "That code isn't valid.", 403)
    digits = re.sub(r"\D", "", value.strip()[:64])
    if len(digits) != _CODE_DIGITS:
        raise ReauthError("code_invalid", "That code isn't valid.", 403)
    return digits


def send_email_code(user_email: Any) -> Dict[str, Any]:
    """Mint a one-time code, mail it to the REGISTERED address, and store only
    its bcrypt hash. Replaces any previous challenge for this user.

    Returns metadata only — the code is never in the response, never logged.
    """
    email = _normalize_email(user_email)
    _ensure_indexes()

    if check_rate_limit(f"cred_reveal_code:{email}",
                        max_requests=_SEND_MAX,
                        window_seconds=_SEND_WINDOW_SECONDS):
        raise ReauthError(
            "code_send_rate_limited",
            "Too many codes requested. Try again in a few minutes.",
            429,
        )

    from services import email_service
    if not email_service.email_configured():
        # Refuse rather than fall through to the console fallback: a code
        # printed to CloudWatch would be a re-auth factor in the logs.
        raise ReauthError(
            "email_unavailable",
            "We can't send a verification code right now. Please try again later.",
            503,
        )

    code = _generate_code()
    now = _now()
    doc = {
        "user_email": email,
        "code_hash": User.hash_password(code),
        "created_at": now,
        "expires_at": now + timedelta(seconds=_CODE_TTL_SECONDS),
        "attempts": 0,
        "consumed_at": None,
    }
    try:
        # One live challenge per user — a new send invalidates the old code.
        _coll().replace_one({"user_email": email}, doc, upsert=True)
    except Exception as e:
        logger.exception("reveal reauth: challenge store failed: %s", e)
        raise ReauthError(
            "email_unavailable",
            "We can't send a verification code right now. Please try again later.",
            503,
        ) from None

    try:
        sent = _send_code_email(email, code)
    except Exception as e:  # pragma: no cover - send_email never raises
        logger.warning("reveal reauth: code send raised: %s", e)
        sent = False
    finally:
        del code  # the plaintext code's lifetime ends here

    if not sent:
        # The provider did not accept it. email_service logs the body on the
        # fallback path, so the code may now be in a log — kill it.
        try:
            _coll().delete_one({"user_email": email})
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("reveal reauth: challenge cleanup failed: %s", e)
        raise ReauthError(
            "email_unavailable",
            "We couldn't send the verification code. Please try again later.",
            503,
        )

    logger.info("reveal reauth: emailed a one-time code (user=%s)", email)
    return {
        "sent": True,
        "method": METHOD_EMAIL_CODE,
        "email_hint": mask_email(email),
        "expires_in_seconds": _CODE_TTL_SECONDS,
        "code_length": _CODE_DIGITS,
    }


def _send_code_email(to_email: str, code: str) -> bool:
    from services import email_service

    app = email_service.APP_NAME
    minutes = _CODE_TTL_SECONDS // 60
    subject = f"Your {app} verification code"
    html_body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:480px;margin:auto;color:#0f172a">
  <h2 style="margin:0 0 12px">Verification code</h2>
  <p style="color:#444;line-height:1.6">Use this code to reveal a saved employer
  login in {app}. It expires in {minutes} minutes and can be used once.</p>
  <p style="font-size:32px;font-weight:700;letter-spacing:8px;margin:24px 0">{code}</p>
  <p style="color:#888;font-size:13px;line-height:1.6">If you didn't ask to see a
  saved employer password, ignore this email and change your {app} password —
  someone may have your session.</p>
</div>"""
    text_body = (
        f"{app} verification code: {code}\n\n"
        f"Use it to reveal a saved employer login. It expires in {minutes} "
        f"minutes and can be used once.\n\n"
        f"If you didn't ask for it, ignore this email."
    )
    return email_service.send_email(to_email, subject, html_body, text_body)


def _verify_email_code(user_email: str, code: Any) -> None:
    """Consume the live challenge for this user. Raises on every failure."""
    submitted = _normalize_code(code)
    now = _now()

    # Atomically take an attempt off a LIVE challenge. Doing the $inc in the
    # same round trip means a burst of parallel guesses cannot all read
    # attempts=0 and slip past the cap.
    doc = _coll().find_one_and_update(
        {"user_email": user_email, "consumed_at": None,
         "expires_at": {"$gt": now}},
        {"$inc": {"attempts": 1}, "$set": {"last_attempt_at": now}},
        return_document=True,
    )
    if not doc:
        raise ReauthError(
            "code_expired",
            "That code has expired or was already used. Send a new one.",
            403,
        )

    if int(doc.get("attempts", 0)) > _MAX_ATTEMPTS:
        _coll().delete_one({"_id": doc["_id"]})
        raise ReauthError(
            "code_attempts_exceeded",
            "Too many incorrect codes. Request a new one.",
            429,
        )

    if not User.verify_password(submitted, doc.get("code_hash") or ""):
        raise ReauthError("code_invalid", "That code isn't right.", 403)

    # Single use: only the writer that flips consumed_at from None wins. A
    # replay (or a concurrent twin) finds nothing to flip and is refused.
    claimed = _coll().update_one(
        {"_id": doc["_id"], "consumed_at": None},
        {"$set": {"consumed_at": now}},
    )
    if int(getattr(claimed, "modified_count", 0) or 0) != 1:
        raise ReauthError(
            "code_expired",
            "That code has expired or was already used. Send a new one.",
            403,
        )


def _verify_password(user_email: str, password: Any) -> None:
    """Unchanged bcrypt re-auth. ``reauth_unavailable`` still means "this
    account has no password" — but the caller now has ``email_code`` to offer."""
    if not password or not isinstance(password, str):
        raise ReauthError(
            "reauth_required",
            "Your account password is required to reveal a stored credential.",
            400,
        )
    try:
        doc = _users().find_one({"email": user_email}, {"password_hash": 1})
    except Exception as e:
        logger.exception("reveal reauth: password lookup failed: %s", e)
        raise ReauthError(
            "reauth_unavailable_temporarily",
            "We couldn't verify your account just now. Please try again.",
            503,
        ) from None
    if not doc or not doc.get("password_hash"):
        raise ReauthError(
            "reauth_unavailable",
            "This account signs in with a connected provider, so there is no "
            "password to check. Use an emailed code instead.",
            403,
        )
    if not User.verify_password(password, doc["password_hash"]):
        # 403, never 401: a failed re-auth is a business failure, not a
        # missing/invalid JWT.
        raise ReauthError("reauth_failed", "Password verification failed.", 403)


def verify(user_email: Any, *, password: Any = None, code: Any = None) -> str:
    """Prove it is really the account owner. Returns the method that satisfied
    the check; raises :class:`ReauthError` otherwise. NEVER logs either secret.

    An emailed code wins when both are supplied — it is the explicit,
    single-use factor the user just completed.
    """
    email = _normalize_email(user_email)
    if code not in (None, ""):
        _verify_email_code(email, code)
        return METHOD_EMAIL_CODE
    if password not in (None, ""):
        _verify_password(email, password)
        return METHOD_PASSWORD
    raise ReauthError(
        "reauth_required",
        "Confirm it's you before revealing a stored credential.",
        400,
    )


def clear_for_user(user_email: Any) -> None:
    """Drop any live challenge (used after a successful reveal and by tests).
    Best-effort — never breaks the caller."""
    try:
        email = _normalize_email(user_email)
    except ReauthError:
        return
    try:
        _coll().delete_one({"user_email": email})
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("reveal reauth: challenge clear failed: %s", e)


def peek_challenge(user_email: Any) -> Optional[Dict[str, Any]]:
    """Metadata about the live challenge (never the code/hash). Read-only, used
    by the route to tell the FE whether a code is already outstanding."""
    try:
        email = _normalize_email(user_email)
    except ReauthError:
        return None
    doc = _coll().find_one(
        {"user_email": email, "consumed_at": None, "expires_at": {"$gt": _now()}},
        {"expires_at": 1, "attempts": 1},
    )
    if not doc:
        return None
    return {
        "expires_at": doc["expires_at"].isoformat()
        if hasattr(doc.get("expires_at"), "isoformat") else None,
        "attempts_remaining": max(0, _MAX_ATTEMPTS - int(doc.get("attempts", 0))),
    }
