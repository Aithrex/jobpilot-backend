"""Managed Application Mailbox — inbound Cloudflare machine webhook (managed-mailbox-v1 §4).

Two JWT-EXEMPT, HMAC-authed routes that receive inbound mail for a managed
address. They ride the ALREADY-ROUTED ``/api/resume/*`` edge (like
``/api/auth/ses-notifications`` rides ``/api/auth``), so NO new top-level prefix
and NO new Terraform edge route are required (§4.2). The resume Lambda already
has the S3 + Bedrock access this pipeline needs.

Why HMAC and not a JWT: the caller is the Cloudflare Email Worker, a machine
with no user session. Per invariant #1 a bad/absent credential here is a **403**,
NEVER a 401 — a 401 would trip the frontend's force-logout, and there is no
session to expire anyway. (401 stays reserved for ``@jwt_required`` on the
user-facing routes.)

Two-phase protocol (dodges the API Gateway 10 MB body cap — raw MIME up to
25 MiB goes STRAIGHT to S3 via a presigned PUT, never through the gateway):

  slot   POST /cloudflare/slot    HMAC over `to\\nfrom\\nid\\nsize`
         -> resolve owner, mint a one-use commit token + presigned S3 PUT.
  commit POST /cloudflare/commit  HMAC over `commit_token\\nraw_sha256`
         -> claim the token, verify the uploaded raw's sha256, ingest, forward.

Owner identity is ALWAYS resolved from the envelope via ``resolve_alias`` and
bound into the commit token — never taken from a request body (D7 / IDOR).
"""

from __future__ import annotations

import logging

from flask import Blueprint, request

from utils.config import _get_config_value
from utils.webhook_hmac import verify as hmac_verify
from services import managed_mailbox_service as mailbox
from services import mail_store_service as store

mail_inbound_bp = Blueprint("mail_inbound", __name__)
logger = logging.getLogger(__name__)

# Header the Worker sends the hex HMAC in (see infra/cloudflare/email-worker).
_SIG_HEADER = "X-JP-Signature"


def _hmac_secret() -> str:
    """Shared secret with the Cloudflare Worker (SSM in prod). Empty -> every
    ``verify`` fails closed, so a misconfiguration rejects rather than admits."""
    return _get_config_value("MAIL_INBOUND_HMAC_SECRET", "") or ""


def _forbidden():
    # 403, NOT 401 — machine route, invariant #1.
    return {"error": "forbidden"}, 403


@mail_inbound_bp.route("/cloudflare/slot", methods=["POST"])
def cloudflare_slot():
    """Phase A: authenticate the Worker, resolve the owner, and hand back a
    one-use presigned S3 PUT + commit token. JWT-exempt (HMAC-authed)."""
    body = request.get_json(silent=True) or {}
    envelope_to = str(body.get("envelope_to") or "")
    envelope_from = str(body.get("envelope_from") or "")
    message_id = str(body.get("message_id") or "")
    raw_size = body.get("raw_size")

    # Canonical string MUST match the Worker byte-for-byte (§4.1.2):
    #   envelope_to\nenvelope_from\nmessage_id\nraw_size
    canonical = f"{envelope_to}\n{envelope_from}\n{message_id}\n{raw_size}"
    signature = request.headers.get(_SIG_HEADER, "")
    if not hmac_verify(_hmac_secret(), canonical, signature):
        logger.warning("mail-inbound slot: HMAC verification failed")
        return _forbidden()

    # Envelope recipient is authoritative (NOT the MIME To). resolve_alias treats
    # its input as fully hostile and only resolves active/paused aliases.
    alias_info = mailbox.resolve_alias(envelope_to)
    if not alias_info:
        # Unknown / retired -> opaque 404 (never reveal another user's ownership).
        return {"error": "no_such_user"}, 404

    raw_s3_key = store.raw_key_for(alias_info["user_email"], message_id)

    # Idempotency pre-check: if we've already stored this (alias, message id),
    # mint a DONE-flagged token so the eventual commit is a safe no-op.
    from utils.security import InputSanitizer
    already = store._messages().find_one(
        {
            "alias_id": InputSanitizer.sanitize_string(str(alias_info["alias_id"]), 64),
            "provider_message_id": InputSanitizer.sanitize_string(str(message_id), 998),
        },
        {"_id": 1},
    )

    # Inbound ingest rate limit (P1-RATE-IN). A per-alias + per-sender-domain
    # atomic counter (spam/flood backstop) — checked BEFORE we mint a token,
    # presign a PUT, or enqueue any work. An idempotent replay (``already``) is a
    # safe no-op and must NOT consume budget, so we only rate-limit genuinely new
    # inbound. Over cap -> 429 (never 401 — machine route, invariant #1); nothing
    # is stored/enqueued and the Worker rejects with a 4xx. The reason logged is a
    # PII-free counter label only.
    if not already:
        allowed, reason = store.check_inbound_rate(alias_info["alias_id"], envelope_from)
        if not allowed:
            logger.warning("mail-inbound slot: rate limited (%s)", reason)
            return {"error": "rate_limited"}, 429

    commit_token = store.mint_commit_token(
        alias_info=alias_info,
        envelope_to=envelope_to,
        envelope_from=envelope_from,
        message_id=message_id,
        raw_s3_key=raw_s3_key,
        done=bool(already),
    )

    presigned = store.get_storage_service().generate_presigned_put(
        raw_s3_key,
        bucket=store._apply_mail_bucket(),
        content_type="message/rfc822",
        kms_key_id=store._kms_key_id(),
    )

    return {
        "upload_url": presigned["url"],
        "put_headers": presigned["headers"],
        "commit_token": commit_token,
    }, 200


@mail_inbound_bp.route("/cloudflare/commit", methods=["POST"])
def cloudflare_commit():
    """Phase B: authenticate, claim the one-use token, verify the uploaded raw's
    hash, and ingest to durable inbox state. JWT-exempt (HMAC-authed)."""
    body = request.get_json(silent=True) or {}
    commit_token = str(body.get("commit_token") or "")
    raw_sha256 = str(body.get("raw_sha256") or "")

    canonical = f"{commit_token}\n{raw_sha256}"
    signature = request.headers.get(_SIG_HEADER, "")
    if not hmac_verify(_hmac_secret(), canonical, signature):
        logger.warning("mail-inbound commit: HMAC verification failed")
        return _forbidden()

    # Atomically claim (one-use). Missing / used / expired -> 409, treated as
    # "already done" by the Worker (idempotent).
    token = store.claim_commit_token(commit_token)
    if not token:
        return {"ok": True, "deduplicated": True, "note": "token_consumed_or_expired"}, 409

    # A DONE-flagged token means the message was already stored (slot idempotency
    # pre-check hit). Nothing to download/ingest.
    if token.get("done"):
        return {"ok": True, "deduplicated": True}, 200

    alias_info = {
        "user_email": token.get("user_email"),
        "mailbox_id": token.get("mailbox_id"),
        "alias_id": token.get("alias_id"),
    }

    # Download the raw the Worker PUT to S3 and verify integrity end-to-end.
    try:
        raw_bytes = store.get_storage_service().get_object_bytes(
            token["raw_s3_key"], bucket=store._apply_mail_bucket()
        )
    except Exception as e:
        logger.error("mail-inbound commit: raw fetch failed: %s", type(e).__name__)
        # 5xx so the Worker retries (the object may not be visible yet).
        return {"error": "raw_unavailable"}, 500

    actual_sha = store._sha256_bytes(raw_bytes)
    if raw_sha256 and actual_sha != raw_sha256.strip().lower():
        logger.warning("mail-inbound commit: raw sha256 mismatch")
        return {"error": "sha256_mismatch"}, 422

    try:
        result = store.ingest_committed(
            alias_info=alias_info,
            envelope_to=token.get("envelope_to") or "",
            envelope_from=token.get("envelope_from") or "",
            raw_bytes=raw_bytes,
            provider_message_id=token.get("message_id") or "",
        )
    except store.MailRejected as e:
        # Structural §6 violation — the message is intentionally not stored.
        logger.info("mail-inbound commit: rejected (%s)", e.reason)
        return {"error": "message_rejected", "reason": e.reason}, 422

    # Fire forwarding (no-op stub today — the NEXT task wires the real send).
    try:
        store.maybe_forward(result)
    except Exception as e:  # pragma: no cover - forwarding must never fail commit
        logger.warning("mail-inbound commit: maybe_forward raised: %s", type(e).__name__)

    return {
        "ok": True,
        "thread_id": result.get("thread_id"),
        "message_id": result.get("message_id"),
        "deduplicated": result.get("deduplicated", False),
    }, 200
