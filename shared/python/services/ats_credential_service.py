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

ONE WORKDAY PASSWORD PER USER (product decision, 2026-07)
---------------------------------------------------------
Workday is not one site: every employer runs its own tenant with its own
candidate account. Asking the user to keep 30 different passwords defeats the
"you can log in yourself and check status" goal, so for ``ats_family ==
"workday"`` the vault serves ONE password per user across ALL tenants:

  * the CANONICAL row is ``(user_email, "workday", "")`` — it owns the blob;
  * each tenant gets a POINTER row ``(user_email, "workday", <tenant>)`` with
    ``shared_credential: True`` and NO blob. It exists so the UI can list which
    employers the shared login is used at, and so :func:`reveal` can follow it.

The sharing boundary is EXPLICIT and narrow (:data:`SHARED_PASSWORD_FAMILIES`).
Greenhouse/Lever/Ashby/iCIMS must NEVER inherit the Workday password — a future
vendor gets per-tenant credentials unless it is added to that set on purpose
(``tests/test_ats_credential_vault.py`` pins this).

MIGRATION / "do not break an account that already exists": a per-tenant row that
already OWNS a password was, by construction, created because we were about to
register at that employer — the employer's side now has that exact password.
:func:`resolve_login` therefore KEEPS it (``scope="tenant"``, ``legacy=True``)
and never re-points it at the shared secret; showing the user the shared
password for such an employer would show them a password that does not work.
The canonical shared secret is MINTED FRESH rather than adopted from one of the
old per-tenant blobs: adopting one would require a decrypt+re-encrypt outside
:func:`reveal` (breaking the single-audited-egress rule) and would spread a
password that has already been typed into a third-party ATS across every other
employer. New tenants use the shared secret from day one.

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
from services.kms_cipher import credential_cipher, KmsCipherError

logger = logging.getLogger(__name__)

# Generated-password policy: 24 chars from all four classes comfortably clears
# every major ATS password rule (and models/user.py's own complexity floor).
_PASSWORD_LENGTH = 24
# Symbols chosen to be broadly accepted by ATS forms (no quotes/backslashes
# that some Workday tenants reject or that invite copy/paste escaping bugs).
_PASSWORD_SYMBOLS = "!@#$%^*-_=+?"

_MAX_FIELD_LEN = 200
_FIELD_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,199}$")

WORKDAY = "workday"
#: Families whose password is SHARED across all of that vendor's tenants for a
#: given user. Deliberately a one-element allow-list: adding a vendor here means
#: one breach at one employer exposes every account at that vendor, so it is a
#: decision, never a default. (See the module docstring.)
SHARED_PASSWORD_FAMILIES = frozenset({WORKDAY})
#: Tenant key of the canonical shared row. "" is what ``_norm_key`` yields for an
#: absent tenant, and ``_FIELD_RE`` cannot produce it from user input, so a real
#: employer tenant can never collide with it.
SHARED_TENANT = ""

_cipher = credential_cipher()

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


def _norm_login_url(value: Any) -> str:
    """The employer portal link shown next to the credential. HTTPS-only, query
    and fragment stripped (an apply URL can carry a one-use token), hard capped.
    Anything else normalizes to "" — a bad URL is dropped, never stored."""
    if not isinstance(value, str):
        return ""
    raw = value.strip()[:2000]
    if not raw:
        return ""
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(raw)
        if parts.scheme != "https" or not parts.hostname:
            return ""
        return urlunsplit(("https", parts.netloc, parts.path[:300], "", ""))[:500]
    except Exception:
        return ""


def _norm_label(value: Any) -> str:
    """A display-only label (the employer name). Control chars stripped, capped;
    never reaches a Mongo filter."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", value.strip())[:120]


def is_shared_family(ats_family: Any) -> bool:
    """True when this vendor uses ONE password per user across all its tenants.
    The single place that decides; callers must not hard-code "workday"."""
    if not isinstance(ats_family, str):
        return False
    return ats_family.strip().lower() in SHARED_PASSWORD_FAMILIES


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


# The canonical shared-family row (one Workday password per user) is keyed with
# an EMPTY tenant. KMS rejects an empty-string VALUE in an EncryptionContext with
# `ValidationException: Invalid EncryptionContext`, so binding `tenant: ""`
# made every generate_data_key fail — no credential could be stored at all, and
# every Workday application parked on the account gate with the opaque
# "Credential encryption is unavailable."
#
# Substitute an explicit sentinel rather than dropping the key: omitting `tenant`
# for the shared row would make its context a strict SUBSET of a per-tenant row's,
# weakening the binding. The sentinel keeps every row's context the same shape and
# the same arity. It must be a value no real tenant can take — Workday tenants are
# hostname-safe (alphanumeric/hyphen), so underscores make this unreachable.
_SHARED_TENANT_SENTINEL = "__shared__"


def _context(user_email: str, ats_family: str, tenant: str) -> Dict[str, str]:
    """Identity binding for the KMS envelope (encryption context + GCM AAD).

    Every value must be a NON-EMPTY string: KMS validates the context, and the
    same map is the GCM associated data, so a change here invalidates existing
    blobs. Safe to introduce now only because no credential has ever been stored
    (the empty-tenant bug meant encrypt never succeeded).
    """
    return {
        "purpose": "ats_credentials",
        "user_email": user_email,
        "ats_family": ats_family,
        "tenant": tenant or _SHARED_TENANT_SENTINEL,
    }


def _public_view(doc: Dict[str, Any], *,
                 shared_available: Optional[bool] = None) -> Dict[str, Any]:
    """Metadata-only view — the blob NEVER leaves the service layer.

    ``shared_available`` tells a POINTER row (no blob of its own) whether the
    canonical shared secret it points at actually exists, so the UI does not
    render "no password stored" for an employer that is perfectly revealable.
    """
    def _iso(v):
        return v.isoformat() if hasattr(v, "isoformat") else None
    family = doc.get("ats_family", "")
    tenant = doc.get("tenant", "")
    own_blob = bool(doc.get("password_blob"))
    pointer = bool(doc.get("shared_credential")) and not own_blob
    if is_shared_family(family) and tenant == SHARED_TENANT and own_blob:
        scope = "shared"          # the canonical "one Workday password" row
    elif pointer:
        scope = "shared"          # this employer uses that same password
    else:
        scope = "tenant"          # a password unique to this employer
    return {
        "ats_family": family,
        "tenant": tenant,
        "username": doc.get("username", ""),
        "has_password": own_blob or (pointer and bool(shared_available)),
        "scope": scope,
        # True only for a per-tenant password kept from BEFORE the shared-Workday
        # rule (the employer's side already has this exact password).
        "legacy_per_tenant": bool(
            is_shared_family(family) and tenant != SHARED_TENANT and own_blob),
        "company": doc.get("company", ""),
        "login_url": doc.get("login_url", ""),
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
    supplies one. Returns metadata only.

    Shared-password vendors (Workday): a tenant that does not already own its
    own password is redirected onto the user's ONE canonical row, so this entry
    point can never re-fragment what :func:`resolve_login` unified.
    """
    _ensure_indexes()
    ats_family = _norm_key(ats_family, "ats_family")
    tenant = _norm_key(tenant, "tenant", required=False)
    username = _norm_username(username) or user_email

    if is_shared_family(ats_family) and tenant != SHARED_TENANT:
        existing = _find(user_email, ats_family, tenant)
        if existing is None or not existing.get("password_blob"):
            _upsert_pointer_row(user_email, ats_family, tenant, username, "", "")
            return store_or_rotate(user_email, ats_family, SHARED_TENANT, username)

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
# resolve_login — the ONE entry point the auto-apply pipeline uses to decide
# which vault row backs a login, creating what is missing. It NEVER rotates an
# existing password (an employer already has it) and it NEVER decrypts.
# ---------------------------------------------------------------------------
def _find(user_email: str, ats_family: str, tenant: str) -> Optional[Dict[str, Any]]:
    return _coll().find_one({"user_email": user_email,
                             "ats_family": ats_family, "tenant": tenant})


def _meta_set(username: str, login_url: str, company: str) -> Dict[str, Any]:
    """Display-only fields we refresh on every resolve (never the blob)."""
    out: Dict[str, Any] = {"updated_at": _now()}
    if username:
        out["username"] = username
    if login_url:
        out["login_url"] = login_url
    if company:
        out["company"] = company
    return out


def _ensure_password_row(user_email: str, ats_family: str, tenant: str,
                         username: str, login_url: str,
                         company: str) -> Dict[str, Any]:
    """Return the row for (user, family, tenant), MINTING a password only when
    the row does not exist yet. Idempotent and non-destructive: an existing
    password is never touched (rotation is an explicit, separate action).

    Race-safe — the insert uses ``$setOnInsert`` under the unique index, so a
    concurrent loser's freshly generated blob is discarded, not written.
    """
    existing = _find(user_email, ats_family, tenant)
    if existing is not None and existing.get("password_blob"):
        meta = _meta_set(username if not existing.get("username") else "",
                         login_url, company)
        if len(meta) > 1:
            _coll().update_one({"_id": existing["_id"]}, {"$set": meta})
            existing.update({k: v for k, v in meta.items() if k != "updated_at"})
        return existing

    password = generate_password()
    try:
        blob = _cipher.encrypt(password,
                               context=_context(user_email, ats_family, tenant))
    except KmsCipherError as e:
        raise CredentialError("vault_unavailable", str(e), 503) from None
    finally:
        del password  # plaintext lifetime ends here

    now = _now()
    flt = {"user_email": user_email, "ats_family": ats_family, "tenant": tenant}
    res = _coll().update_one(
        flt,
        {"$setOnInsert": {
            "user_email": user_email,
            "ats_family": ats_family,
            "tenant": tenant,
            "username": username,
            "login_url": login_url,
            "company": company,
            "password_blob": blob,
            "created_at": now,
            "updated_at": now,
            "last_used_at": None,
            "rotation_count": 1,
            "reset_count": 0,
            "pending_ats_reset": False,
        }},
        upsert=True,
    )
    # `upserted_id` is the only unambiguous "I inserted it" signal — do NOT
    # compare created_at to `now`, Mongo truncates datetimes to milliseconds.
    created = getattr(res, "upserted_id", None) is not None
    if not created:
        # A row was already there. Normally it owns a blob (the concurrent
        # winner). Defensively adopt ours if — and only if — it still has none,
        # guarding the write so the winner's secret is never overwritten.
        _coll().update_one(
            {**flt, "password_blob": {"$exists": False}},
            {"$set": {"password_blob": blob, "updated_at": now,
                      "rotation_count": 1}},
        )
    else:
        _audit_soft(user_email, "stored", ats_family=ats_family, tenant=tenant,
                    meta={"scope": "shared" if tenant == SHARED_TENANT
                          and is_shared_family(ats_family) else "tenant"})
    return _find(user_email, ats_family, tenant) or {}


def _upsert_pointer_row(user_email: str, ats_family: str, tenant: str,
                        username: str, login_url: str,
                        company: str) -> Dict[str, Any]:
    """A per-tenant row that carries NO secret and points at the canonical
    shared one. Purely a listing/─audit convenience; never holds a blob."""
    now = _now()
    flt = {"user_email": user_email, "ats_family": ats_family, "tenant": tenant}
    return _coll().find_one_and_update(
        flt,
        {"$set": {"shared_credential": True, **_meta_set(username, login_url, company)},
         "$setOnInsert": {
             "user_email": user_email,
             "ats_family": ats_family,
             "tenant": tenant,
             "created_at": now,
             "last_used_at": None,
             "rotation_count": 0,
             "reset_count": 0,
             "pending_ats_reset": False,
         }},
        upsert=True,
        return_document=True,
    ) or dict(flt)


def resolve_login(user_email: str, ats_family: Any, tenant: Any = "",
                  username: Any = "", login_url: Any = "",
                  company: Any = "") -> Dict[str, Any]:
    """Decide WHICH vault row backs the login at ``(ats_family, tenant)`` and
    make sure it exists. Returns metadata only — no plaintext, ever.

      * shared family (Workday) + a tenant that has NO password of its own
        → the canonical ``tenant=""`` row (minted on first use) + a pointer row
          for this tenant.  ``scope="shared"``
      * shared family + a tenant that ALREADY owns a password
        → that row, untouched. The employer's side has this exact password, so
          re-pointing it would hand the user a password that does not work.
          ``scope="tenant"``, ``legacy_per_tenant=True``
      * every other vendor → its own per-tenant row. ``scope="tenant"``

    The returned ``source_tenant`` is the row that actually holds the blob;
    :func:`reveal` re-derives it independently, so callers never need it to
    decrypt — it is reported for logging/tests only.
    """
    _ensure_indexes()
    family = _norm_key(ats_family, "ats_family")
    tenant = _norm_key(tenant, "tenant", required=False)
    username = _norm_username(username) or user_email
    login_url = _norm_login_url(login_url)
    company = _norm_label(company)

    shared = is_shared_family(family)

    if not shared or tenant == SHARED_TENANT:
        doc = _ensure_password_row(user_email, family, tenant, username,
                                   login_url, company)
        return {"credential": _public_view(doc, shared_available=True),
                "source_tenant": tenant, "shared": shared and tenant == SHARED_TENANT}

    existing = _find(user_email, family, tenant)
    if existing is not None and existing.get("password_blob"):
        # Pre-existing per-tenant password — keep it (see the module docstring).
        meta = _meta_set("", login_url, company)
        if len(meta) > 1:
            _coll().update_one({"_id": existing["_id"]}, {"$set": meta})
            existing.update({k: v for k, v in meta.items() if k != "updated_at"})
        return {"credential": _public_view(existing, shared_available=True),
                "source_tenant": tenant, "shared": False}

    # New tenant on a shared-password vendor → the ONE password for this user.
    _ensure_password_row(user_email, family, SHARED_TENANT, username, "", "")
    pointer = _upsert_pointer_row(user_email, family, tenant, username,
                                  login_url, company)
    return {"credential": _public_view(pointer, shared_available=True),
            "source_tenant": SHARED_TENANT, "shared": True}


def _resolve_source(user_email: str, family: str, tenant: str
                    ) -> tuple:  # (requested_doc | None, source_doc | None, source_tenant)
    """Follow a shared-password pointer to the row that owns the blob.
    Pure lookup — no writes, no decrypt."""
    doc = _find(user_email, family, tenant)
    if doc is not None and doc.get("password_blob"):
        return doc, doc, tenant
    if is_shared_family(family) and tenant != SHARED_TENANT:
        # A pointer row, or a tenant we have a login for but no row yet: both
        # are served by the user's ONE canonical secret for this vendor.
        if doc is None or doc.get("shared_credential"):
            canonical = _find(user_email, family, SHARED_TENANT)
            if canonical is not None and canonical.get("password_blob"):
                return doc, canonical, SHARED_TENANT
    return doc, None, tenant


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

    # A shared-password pointer row resolves to the user's ONE canonical secret
    # for that vendor; every other row owns its blob. Either way the row is
    # user_email-scoped and the blob is bound to {user, family, source_tenant}.
    doc, source, source_tenant = _resolve_source(user_email, ats_family, tenant)
    if source is None:
        raise CredentialError("not_found", "No stored credential for that ATS.", 404)

    # Audit BEFORE decrypt, and hard-fail if it can't be written. The audit
    # records the tenant the USER asked about plus the row that served it.
    try:
        _audit(user_email, "revealed", ats_family=ats_family, tenant=tenant,
               meta={"source_tenant": source_tenant,
                     "shared": source_tenant != tenant})
    except Exception as e:
        logger.error("ats_credential reveal REFUSED — audit write failed: %s", e)
        raise CredentialError(
            "audit_unavailable", "Reveal is temporarily unavailable.", 503
        ) from None

    try:
        password = _cipher.decrypt(
            source["password_blob"],
            context=_context(user_email, ats_family, source_tenant),
        )
    except KmsCipherError:
        raise CredentialError(
            "decrypt_failed",
            "Stored credential could not be decrypted. Rotate it to recover.",
            409,
        ) from None

    now = _now()
    _coll().update_one({"_id": source["_id"]}, {"$set": {"last_used_at": now}})
    if doc is not None and doc.get("_id") != source.get("_id"):
        _coll().update_one({"_id": doc["_id"]}, {"$set": {"last_used_at": now}})
    username = ((doc or {}).get("username") or source.get("username") or "")
    return {"username": username, "password": password}


# ---------------------------------------------------------------------------
# User-initiated reset — Tranche 1 stub. Rotates the stored secret NOW and
# flags the record; the Tranche 2 Oracle picks up pending_ats_reset and runs
# the ATS forgot-password flow to converge the remote account.
# ---------------------------------------------------------------------------
def request_reset(user_email: str, ats_family: Any, tenant: Any = "") -> Dict[str, Any]:
    ats_family = _norm_key(ats_family, "ats_family")
    tenant = _norm_key(tenant, "tenant", required=False)

    # Resetting a SHARED credential rotates the one password for every tenant of
    # that vendor — so it must rotate the canonical row, not the pointer, and
    # every pointer must be flagged for the Oracle's forgot-password run.
    _doc, source, source_tenant = _resolve_source(user_email, ats_family, tenant)
    if source is None:
        raise CredentialError("not_found", "No stored credential for that ATS.", 404)

    password = generate_password()
    try:
        blob = _cipher.encrypt(
            password, context=_context(user_email, ats_family, source_tenant))
    except KmsCipherError as e:
        raise CredentialError("vault_unavailable", str(e), 503) from None
    finally:
        del password

    doc = _coll().find_one_and_update(
        {"_id": source["_id"]},
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
    shared = source_tenant == SHARED_TENANT and is_shared_family(ats_family)
    affected = 1
    if shared:
        # Each remote account still holds the OLD password: flag them all.
        res = _coll().update_many(
            {"user_email": user_email, "ats_family": ats_family,
             "shared_credential": True},
            {"$set": {"pending_ats_reset": True, "updated_at": _now()}},
        )
        affected += int(getattr(res, "modified_count", 0) or 0)
    _audit_soft(user_email, "reset_requested", ats_family=ats_family, tenant=tenant,
                meta={"reset_count": int(doc.get("reset_count", 1)) if doc else 1,
                      "source_tenant": source_tenant, "shared": shared,
                      "accounts_flagged": affected})
    return {"credential": _public_view(doc or {}, shared_available=True),
            "reset_queued": True, "shared": shared,
            "accounts_flagged": affected}


# ---------------------------------------------------------------------------
# Read (metadata only) — for the settings UI.
# ---------------------------------------------------------------------------
def list_credentials(user_email: str) -> List[Dict[str, Any]]:
    """Every stored login for this user, metadata only. Pointer rows report
    ``has_password`` from the canonical shared row they point at, so the UI can
    offer Reveal for an employer whose password lives on the shared record."""
    docs = list(_coll().find({"user_email": user_email})
                .sort("updated_at", -1).limit(100))
    # Which families actually have a usable canonical shared secret?
    shared_ok = {
        d.get("ats_family") for d in docs
        if d.get("tenant") == SHARED_TENANT and d.get("password_blob")
        and is_shared_family(d.get("ats_family"))
    }
    return [_public_view(d, shared_available=d.get("ats_family") in shared_ok)
            for d in docs]
