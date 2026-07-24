"""Executor capability + callback surface (GCP browser-execution plane, defect P5).

This is the AWS-side security seam between the resume Lambda (the orchestrator +
the credential/artifact vault) and the PRIVATE Cloud Run Playwright worker. The
worker holds NO durable AWS credentials, NO Mongo access, NO SSM, NO bucket-wide
S3 grant and NO general KMS grant. Everything it needs to submit exactly one
application, it fetches here — each grant owner-scoped, one-use, and short-TTL.

Token model (mirrors mail_store_service's one-use commit token; TTL-swept):

  capability   (in the Cloud Task payload — the worker's ONLY bearer secret)
      A random opaque token bound to {execution_id, user_email, application_id}.
      Claimed EXACTLY ONCE via ``claim_capability`` (atomic CAS). A redelivered
      Cloud Task that races a second claim loses → 409 → the worker aborts. This
      is the double-dispatch backstop BEFORE any browser launches.

  callback_token  (returned by the claim; the worker's session bearer)
      Scoped to the same execution. Authenticates every follow-up call
      (resume-url / credential / receipt-url / precheck / milestone / terminal)
      for the TTL of the run. Each RESOURCE issuance under it is independently
      one-use (a per-execution flag flips), so a leaked callback token cannot
      re-pull the résumé or re-reveal a credential.

Security invariants (do NOT weaken):
  * Machine auth only — a bad/absent capability or callback token is a **403**,
    NEVER a 401 (invariant #1: a machine caller has no session to expire; a 401
    would trip the FE force-logout). ``@jwt_required`` stays for user routes.
  * Owner identity is ALWAYS read from the bound token, NEVER from a request body
    (IDOR defense — same posture as mail_inbound's commit token).
  * Every identifier reaching a Mongo filter is sanitized to a plain string.
  * The presigned GET is scoped to the ONE pinned résumé S3 key; the presigned
    PUT to the ONE receipt key under an audit prefix — never a bucket-wide grant.
  * Plaintext ATS credentials exist only transiently in the reveal response and
    are NEVER logged. Milestones are structured + PII-free by construction.

Collections (TTL-indexed, lazily ensured):
  ``executor_capabilities`` — one doc per dispatched execution (the lease + the
  per-resource one-use flags + the callback token digest).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from pymongo import ReturnDocument

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

# Short-lived by design: a run is seconds-to-a-minute; 15 min is generous
# headroom for a slow multi-step form while keeping the blast radius tiny.
_CAPABILITY_TTL_SECONDS = 15 * 60
# Presigned artifact URLs are even shorter — the worker uses them immediately.
_RESUME_GET_TTL_SECONDS = 300
_RECEIPT_PUT_TTL_SECONDS = 600

_indexes_ensured = False


class CapabilityError(Exception):
    """Typed business error → (error, status). Never 401 (machine routes)."""

    def __init__(self, error: str, status: int = 403):
        super().__init__(error)
        self.error = error
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coll():
    return DBConnect().get_db().executor_capabilities


def _ensure_indexes() -> None:
    global _indexes_ensured
    if _indexes_ensured:
        return
    try:
        _coll().create_index("capability_digest", unique=True,
                             name="uniq_capability_digest", background=True)
        _coll().create_index("callback_digest",
                             name="callback_digest", background=True)
        _coll().create_index("expires_at", expireAfterSeconds=0,
                             name="ttl_expires_at", background=True)
        _indexes_ensured = True
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("executor_capabilities index ensure failed: %s", e)


def _digest(token: str) -> str:
    """Store only the SHA-256 of a bearer token, never the token itself — a DB
    read can never recover a live capability."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _san(value: Any, n: int = 128) -> str:
    return InputSanitizer.sanitize_string(str(value or ""), n)


def _executor_kms_key_id() -> Optional[str]:
    """P1-ENC (control C8/C15): the CMK the executor audit RECEIPT (a proof
    capture of a filled form — PII) is pinned to at rest. Prefer a dedicated
    executor key (``APPLY_EXECUTOR_KMS_KEY_ID``); fall back to the apply-mail CMK
    (``APPLY_MAIL_KMS_KEY_ID``) so a single apply-domain key still protects it.
    ``None`` locally / when unset → the bucket's default SSE applies (no downgrade,
    just not a caller-pinned CMK)."""
    from utils.config import _get_config_value
    for key in ("APPLY_EXECUTOR_KMS_KEY_ID", "APPLY_MAIL_KMS_KEY_ID"):
        val = (_get_config_value(key, "") or "").strip()
        if val:
            return val
    return None


def _audit_bucket() -> Optional[str]:
    """Explicit bucket for the audit prefix. Defaults to the résumé store's
    bucket (where the ``auto-apply-audit/`` prefix lives); an operator can split
    it out via ``APPLY_AUDIT_BUCKET`` without touching code."""
    from utils.config import _get_config_value
    val = (_get_config_value("APPLY_AUDIT_BUCKET", "") or "").strip()
    return val or None


# ---------------------------------------------------------------------------
# Mint — called by CloudRunExecutor.submit() at dispatch. Returns the raw
# capability that rides in the Cloud Task payload (the ONLY secret the worker
# starts with). Only its digest is persisted.
# ---------------------------------------------------------------------------
def mint_capability(*, execution_id: str, user_email: str,
                    application_id: str) -> str:
    """Create a one-use capability for a dispatched execution and return the raw
    token. Bound to {execution_id, user_email, application_id}; TTL-swept."""
    _ensure_indexes()
    execution_id = _san(execution_id, 64)
    application_id = _san(application_id, 64)
    if not execution_id or not application_id or not user_email:
        raise CapabilityError("invalid_request", 400)

    capability = secrets.token_urlsafe(32)
    now = _now()
    _coll().insert_one({
        "capability_digest": _digest(capability),
        "callback_digest": None,          # set on claim
        "execution_id": execution_id,
        "user_email": user_email,
        "application_id": application_id,
        "claimed": False,
        "claimed_at": None,
        # per-resource one-use flags
        "resume_issued": False,
        "credential_issued": False,
        "receipt_issued": False,
        "terminal_reported": False,
        "created_at": now,
        "expires_at": now + timedelta(seconds=_CAPABILITY_TTL_SECONDS),
    })
    logger.info("executor capability minted execution_id=%s", execution_id)
    return capability


# ---------------------------------------------------------------------------
# Claim — the worker's first call. Atomically leases the capability EXACTLY once
# and hands back the execution envelope + a scoped callback token.
# ---------------------------------------------------------------------------
def claim_capability(capability: str) -> Dict[str, Any]:
    """Atomically flip the capability to claimed (CAS). Returns
    {doc, callback_token}. A missing / already-claimed / expired capability
    raises CapabilityError(403) — the worker aborts, never double-submits."""
    _ensure_indexes()
    cap = str(capability or "").strip()
    if not cap:
        raise CapabilityError("missing_capability", 403)

    callback_token = secrets.token_urlsafe(32)
    doc = _coll().find_one_and_update(
        {"capability_digest": _digest(cap), "claimed": False},
        {"$set": {"claimed": True, "claimed_at": _now(),
                  "callback_digest": _digest(callback_token)}},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        # Unknown, already claimed (duplicate dispatch), or TTL-swept.
        raise CapabilityError("capability_unusable", 403)
    if _expired(doc):
        raise CapabilityError("capability_expired", 403)
    logger.info("executor capability claimed execution_id=%s",
                doc.get("execution_id"))
    return {"doc": doc, "callback_token": callback_token}


def _expired(doc: Dict[str, Any]) -> bool:
    exp = doc.get("expires_at")
    if exp is None:
        return False
    # mongomock can hand back a tz-naive datetime; compare tolerantly.
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < _now()


# ---------------------------------------------------------------------------
# Callback-token verifier — authenticates every follow-up worker call. Returns
# the bound execution doc (owner identity lives HERE, not in the request body).
# ---------------------------------------------------------------------------
def verify_callback(callback_token: str) -> Dict[str, Any]:
    """Resolve + validate a callback token → the claimed execution doc. Raises
    CapabilityError(403) on any miss (unknown / unclaimed / expired)."""
    _ensure_indexes()
    tok = str(callback_token or "").strip()
    if not tok:
        raise CapabilityError("missing_callback_token", 403)
    doc = _coll().find_one({"callback_digest": _digest(tok), "claimed": True})
    if not doc:
        raise CapabilityError("callback_unusable", 403)
    if _expired(doc):
        raise CapabilityError("callback_expired", 403)
    return doc


def _mark_issued(execution_id: str, flag: str) -> bool:
    """Atomically flip a per-resource one-use flag (resume/credential/receipt).
    Returns True if THIS call won the flip (first issuance), False if it was
    already issued — the second attempt is refused (one-use)."""
    res = _coll().update_one(
        {"execution_id": _san(execution_id, 64), flag: False},
        {"$set": {flag: True}},
    )
    return res.modified_count == 1


# ---------------------------------------------------------------------------
# One-use resource issuance (each authenticated by a verified callback doc).
# ---------------------------------------------------------------------------
def issue_resume_get(doc: Dict[str, Any], s3_key: str) -> str:
    """One short-TTL presigned GET for the ONE pinned résumé object. One-use.
    Bound to an explicit bucket (the audit/résumé store) — never a bucket-wide
    grant."""
    if not _mark_issued(doc["execution_id"], "resume_issued"):
        raise CapabilityError("resume_already_issued", 409)
    from services.s3_service import get_storage_service
    # Only pass an explicit bucket when one is configured (APPLY_AUDIT_BUCKET) —
    # else default to the résumé store's bucket, keeping the call byte-compatible.
    kwargs: Dict[str, Any] = {"expiry": _RESUME_GET_TTL_SECONDS}
    bucket = _audit_bucket()
    if bucket:
        kwargs["bucket"] = bucket
    return get_storage_service().generate_presigned_get(_san(s3_key, 512), **kwargs)


def issue_receipt_put(doc: Dict[str, Any]) -> Dict[str, Any]:
    """One short-TTL presigned PUT for the ONE receipt/proof object under the
    per-application audit prefix. One-use. Returns {key, url, headers}.

    P1-ENC (C8/C15): the receipt is a proof capture of a filled form (PII), so
    the PUT is pinned to the apply CMK via SSE-KMS — mirroring the mail raw-MIME
    PUT (``mail_inbound`` → ``store._kms_key_id()``). The SSE-KMS headers are part
    of the SIGNED policy, so the worker MUST echo them verbatim or S3 rejects the
    upload; that is what forces the object onto our CMK at rest."""
    if not _mark_issued(doc["execution_id"], "receipt_issued"):
        raise CapabilityError("receipt_already_issued", 409)
    aid = _san(doc.get("application_id"), 64) or "unknown"
    eid = _san(doc.get("execution_id"), 64) or "unknown"
    key = f"auto-apply-audit/{aid}/{eid}/receipt.png"
    from services.s3_service import get_storage_service
    presigned = get_storage_service().generate_presigned_put(
        key, bucket=_audit_bucket(), content_type="image/png",
        kms_key_id=_executor_kms_key_id(), expiry=_RECEIPT_PUT_TTL_SECONDS)
    return {"key": key, "url": presigned["url"], "headers": presigned["headers"]}


def issue_credential(doc: Dict[str, Any], *, ats_family: str,
                     tenant: str = "") -> Dict[str, str]:
    """One-use, in-memory hand-off of the decrypted ATS credential for the
    OWNING user only. Reuses the KMS-envelope vault. One-use per execution; the
    plaintext is NEVER logged."""
    if not _mark_issued(doc["execution_id"], "credential_issued"):
        raise CapabilityError("credential_already_issued", 409)
    from services import ats_credential_service as creds
    try:
        return creds.reveal(doc["user_email"], ats_family, tenant)
    except creds.CredentialError as e:
        # Map the vault's typed error into the machine-route contract (never 401).
        raise CapabilityError(e.error, 404 if e.status == 404 else 403)


def mark_terminal_reported(execution_id: str) -> bool:
    """Flip the terminal flag exactly once (idempotency guard for the terminal
    callback). Returns True on the first report, False on a duplicate."""
    res = _coll().update_one(
        {"execution_id": _san(execution_id, 64), "terminal_reported": False},
        {"$set": {"terminal_reported": True, "terminal_at": _now()}},
    )
    return res.modified_count == 1
