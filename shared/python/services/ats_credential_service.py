"""ATS credential vault — server-managed logins for auto-apply (Tranche 1 §4).

The existing ``application_profiles.vault`` is user-edited PLAINTEXT and must
never hold passwords. This service owns the encrypted store:

Collection ``ats_credentials`` (unique index (user_email, ats_family, tenant)
— see scripts/migrate_indexes.py; also lazily ensured here like
job_service's runtime index):

    { user_email, ats_family, tenant, username,
      password_blob: {v, encrypted_dek, iv, ciphertext, tag},   # KMS envelope
      created_at, updated_at, last_used_at, last_revealed_at,
      rotation_count, reset_count, pending_ats_reset }

Security invariants (do not weaken):
  * The password is ALWAYS server-generated (secrets module) — the user never
    supplies one, so a phished/reused personal password can never land here.
  * Plaintext exists only transiently inside :func:`reveal` (and the Tranche 2
    Oracle); it is NEVER logged, never returned from store/rotate, and never
    stored outside the KMS envelope (kms_cipher.KmsEnvelopeCipher).
  * Every ciphertext is bound to {user_email, ats_family, tenant} via KMS
    encryption context + GCM AAD — a blob copied into another user's document
    will not decrypt (structural IDOR/blob-swap defense).
  * EVERY query filter is user_email-scoped, and ats_family/tenant are
    normalized to plain lowercase strings before use (no dict/list can reach
    an operator position — NoSQL-injection safe by construction).
  * Reveals/rotations/resets are audit-logged (``ats_credential_audit``,
    PII-light: no password material, ever).

Typed errors map to the platform's status contract in the route layer —
business failures are 400/403/404/409/429, NEVER 401.
"""
from __future__ import annotations

import logging
import re
import secrets
import string
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect
from services.kms_cipher import KmsEnvelopeCipher, KmsCipherError

logger = logging.getLogger(__name__)

# Generated-password policy: 24 chars from all four classes comfortably clears
# every major ATS password rule (and models/user.py's own complexity floor).
_PASSWORD_LENGTH = 24
# Symbols chosen to be broadly accepted by ATS forms (no quotes/backslashes
# that some Workday tenants reject or that invite copy/paste escaping bugs).
_PASSWORD_SYMBOLS = "!@#$%^*-_=+?"

_MAX_FIELD_LEN = 200
_FIELD_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,199}$")

_cipher = KmsEnvelopeCipher()

# Lazy one-time index ensure (job_service precedent). Matches the
# migrate_indexes.py spec name so the migration re-assert is a no-op.
_indexes_ensured = False


class CredentialError(Exception):
    """Typed business error → (error, message, status). Never 401."""

    def __init__(self, error: str, message: str, status: int = 400):
        super().__init__(message)
        self.error = error
        self.message = message
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coll():
    return DBConnect().get_db().ats_credentials


def _audit_coll():
    return DBConnect().get_db().ats_credential_audit


def _ensure_indexes() -> None:
    global _indexes_ensured
    if _indexes_ensured:
        return
    try:
        _coll().create_index(
            [("user_email", 1), ("ats_family", 1), ("tenant", 1)],
            unique=True,
            name="user_family_tenant_unique",
            background=True,
        )
        _indexes_ensured = True
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("ats_credentials index ensure failed: %s", e)


def _audit(user_email: str, action: str, *, ats_family: str = "",
           tenant: str = "", meta: Optional[Dict[str, Any]] = None) -> None:
    """Append-only audit row (who/what/when). NEVER carries password material.
    Soft-fail: an audit write must not break the user action — but reveals
    hard-require it (see reveal())."""
    _audit_coll().insert_one({
        "user_email": user_email,
        "action": action,   # stored | rotated | revealed | reset_requested | deleted
        "ats_family": ats_family,
        "tenant": tenant,
        "at": _now(),
        "meta": meta or {},
    })


def _audit_soft(user_email: str, action: str, **kw) -> None:
    try:
        _audit(user_email, action, **kw)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("ats_credential audit write failed (%s): %s", action, e)


# ---------------------------------------------------------------------------
# Input normalization — every identifier that reaches a Mongo filter goes
# through here first. Output is a plain lowercase [a-z0-9._-] string, so no
# request-shaped dict/operator can ever occupy a filter position.
# ---------------------------------------------------------------------------
def _norm_key(value: Any, field: str, *, required: bool = True) -> str:
    if value is None or value == "":
        if required:
            raise CredentialError("invalid_request", f"{field} is required", 400)
        return ""
    if not isinstance(value, str):
        raise CredentialError("invalid_request", f"{field} must be a string", 400)
    value = value.strip().lower()[:_MAX_FIELD_LEN]
    if not _FIELD_RE.match(value):
        raise CredentialError(
            "invalid_request",
            f"{field} may only contain lowercase letters, digits, '.', '_' and '-'",
            400,
        )
    return value


def _norm_username(value: Any) -> str:
    """Username at the ATS is usually the user's email — allow email shape,
    strip control chars, hard cap. Optional (defaults to the account email)."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise CredentialError("invalid_request", "username must be a string", 400)
    value = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", value.strip())[:320]
    return value


def generate_password() -> str:
    """Server-generated strong password: 24 chars, all four classes guaranteed,
    CSPRNG (secrets), order shuffled with the same CSPRNG."""
    alphabet = string.ascii_lowercase + string.ascii_uppercase + string.digits + _PASSWORD_SYMBOLS
    chars = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice(_PASSWORD_SYMBOLS),
    ]
    chars += [secrets.choice(alphabet) for _ in range(_PASSWORD_LENGTH - len(chars))]
    # Fisher–Yates with secrets.randbelow — random.shuffle is not CSPRNG-backed.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


def _context(user_email: str, ats_family: str, tenant: str) -> Dict[str, str]:
    """Identity binding for the KMS envelope (encryption context + GCM AAD)."""
    return {
        "purpose": "ats_credentials",
        "user_email": user_email,
        "ats_family": ats_family,
        "tenant": tenant,
    }


def _public_view(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Metadata-only view — the blob NEVER leaves the service layer."""
    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else None
    return {
        "ats_family": doc.get("ats_family", ""),
        "tenant": doc.get("tenant", ""),
        "username": doc.get("username", ""),
        "has_password": bool(doc.get("password_blob")),
        "created_at": _iso(doc.get("created_at")),
        "updated_at": _iso(doc.get("updated_at")),
        "last_used_at": _iso(doc.get("last_used_at")),
        "rotation_count": int(doc.get("rotation_count", 0)),
        "reset_count": int(doc.get("reset_count", 0)),
        "pending_ats_reset": bool(doc.get("pending_ats_reset", False)),
    }


# ---------------------------------------------------------------------------
# Store / rotate — the ONLY writer of password_blob. The plaintext is
# generated here, encrypted, and dropped; it is deliberately NOT returned
# (reveal-on-demand is the single, audited plaintext egress).
# ---------------------------------------------------------------------------
def store_or_rotate(user_email: str, ats_family: Any, tenant: Any = "",
                    username: Any = "") -> Dict[str, Any]:
    """Create the credential for (user, ats_family, tenant), or rotate the
    password if one exists. Server generates the password; caller never
    supplies one. Returns metadata only."""
    _ensure_indexes()
    ats_family = _norm_key(ats_family, "ats_family")
    tenant = _norm_key(tenant, "tenant", required=False)
    username = _norm_username(username) or user_email

    password = generate_password()
    try:
        blob = _cipher.encrypt(password, context=_context(user_email, ats_family, tenant))
    except KmsCipherError as e:
        raise CredentialError("vault_unavailable", str(e), 503) from None
    finally:
        del password  # plaintext lifetime ends here

    now = _now()
    flt = {"user_email": user_email, "ats_family": ats_family, "tenant": tenant}
    doc = _coll().find_one_and_update(
        flt,
        {
            "$set": {
                "username": username,
                "password_blob": blob,
                "updated_at": now,
                "pending_ats_reset": False,
            },
            "$setOnInsert": {
                "user_email": user_email,
                "ats_family": ats_family,
                "tenant": tenant,
                "created_at": now,
                "last_used_at": None,
                "reset_count": 0,
            },
            "$inc": {"rotation_count": 1},
        },
        upsert=True,
        return_document=True,  # pymongo ReturnDocument.AFTER truthy equivalent
    )
    # rotation_count is $inc'd on every write, so 1 ⇒ this call inserted the doc.
    # (Don't compare created_at == now — Mongo truncates datetimes to ms.)
    created = bool(doc and int(doc.get("rotation_count", 0)) == 1)
    _audit_soft(
        user_email,
        "stored" if created else "rotated",
        ats_family=ats_family,
        tenant=tenant,
        meta={"rotation_count": int(doc.get("rotation_count", 1)) if doc else 1},
    )
    return {"credential": _public_view(doc or {**flt, "username": username,
                                               "password_blob": blob,
                                               "created_at": now, "updated_at": now}),
            "created": created}


# ---------------------------------------------------------------------------
# Reveal-on-demand — the single plaintext egress. Route layer MUST have
# re-authenticated the user (fresh password check) before calling this.
# ---------------------------------------------------------------------------
def reveal(user_email: str, ats_family: Any, tenant: Any = "") -> Dict[str, str]:
    """Decrypt and return {username, password} for the OWNING user only.
    The audit write is REQUIRED — if we cannot record the reveal, we refuse it
    (an unattributable plaintext egress is worse than a failed request)."""
    ats_family = _norm_key(ats_family, "ats_family")
    tenant = _norm_key(tenant, "tenant", required=False)

    flt = {"user_email": user_email, "ats_family": ats_family, "tenant": tenant}
    doc = _coll().find_one(flt)
    if not doc or not doc.get("password_blob"):
        raise CredentialError("not_found", "No stored credential for that ATS.", 404)

    # Audit BEFORE decrypt, and hard-fail if it can't be written.
    try:
        _audit(user_email, "revealed", ats_family=ats_family, tenant=tenant)
    except Exception as e:
        logger.error("ats_credential reveal REFUSED — audit write failed: %s", e)
        raise CredentialError(
            "audit_unavailable", "Reveal is temporarily unavailable.", 503
        ) from None

    try:
        password = _cipher.decrypt(
            doc["password_blob"], context=_context(user_email, ats_family, tenant)
        )
    except KmsCipherError:
        raise CredentialError(
            "decrypt_failed",
            "Stored credential could not be decrypted. Rotate it to recover.",
            409,
        ) from None

    _coll().update_one(flt, {"$set": {"last_used_at": _now()}})
    return {"username": doc.get("username", ""), "password": password}


# ---------------------------------------------------------------------------
# User-initiated reset — Tranche 1 stub. Rotates the stored secret NOW and
# flags the record; the Tranche 2 Oracle picks up pending_ats_reset and runs
# the ATS forgot-password flow to converge the remote account.
# ---------------------------------------------------------------------------
def request_reset(user_email: str, ats_family: Any, tenant: Any = "") -> Dict[str, Any]:
    ats_family = _norm_key(ats_family, "ats_family")
    tenant = _norm_key(tenant, "tenant", required=False)

    flt = {"user_email": user_email, "ats_family": ats_family, "tenant": tenant}
    if not _coll().find_one(flt, {"_id": 1}):
        raise CredentialError("not_found", "No stored credential for that ATS.", 404)

    password = generate_password()
    try:
        blob = _cipher.encrypt(password, context=_context(user_email, ats_family, tenant))
    except KmsCipherError as e:
        raise CredentialError("vault_unavailable", str(e), 503) from None
    finally:
        del password

    doc = _coll().find_one_and_update(
        flt,
        {
            "$set": {
                "password_blob": blob,
                "updated_at": _now(),
                "pending_ats_reset": True,  # Tranche 2 Oracle work item
            },
            "$inc": {"reset_count": 1, "rotation_count": 1},
        },
        return_document=True,
    )
    _audit_soft(user_email, "reset_requested", ats_family=ats_family, tenant=tenant,
                meta={"reset_count": int(doc.get("reset_count", 1)) if doc else 1})
    return {"credential": _public_view(doc or {}), "reset_queued": True}


# ---------------------------------------------------------------------------
# Read (metadata only) — for the settings UI.
# ---------------------------------------------------------------------------
def list_credentials(user_email: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for doc in _coll().find({"user_email": user_email}).sort("updated_at", -1).limit(100):
        out.append(_public_view(doc))
    return out
