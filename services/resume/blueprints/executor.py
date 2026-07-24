"""Executor callback surface — the AWS-side capability/callback API for the
private Cloud Run Playwright worker (GCP browser-execution plane, defect P5).

These are JWT-EXEMPT, CAPABILITY/CALLBACK-token-authed machine routes. They ride
the ALREADY-ROUTED ``/api/resume/*`` edge (mounted at ``/api/resume/executor/*``),
exactly like ``/api/resume/mail-inbound/*`` rides it — so NO new top-level prefix
and NO new external Terraform edge route are required. The resume Lambda already
holds the S3 (résumé + audit) and KMS (credential vault) access this flow needs.

Confirmed: no new edge route. A new top-level ``/api/<x>`` WOULD need a matching
external Terraform ``ANY /api/<x>/{proxy+}`` route or it 404s in prod; mounting
under ``/api/resume`` avoids that entirely.

Auth model (invariant #1 — a bad/absent machine credential is **403, NEVER 401**;
401 would trip the FE force-logout and there is no session to expire):
  * ``/claim``  authenticates with the one-use CAPABILITY from the Cloud Task
    body → atomically leases it once and returns the envelope + a callback token.
  * every other route authenticates with the scoped CALLBACK token
    (``X-Executor-Callback`` header). Owner identity is read from the bound token,
    NEVER from the request body (IDOR defense).

The worker fetches artifacts/credentials via one-use sub-endpoints, re-checks the
pre-submit gate right before it clicks Submit, uploads a receipt via a one-use
presigned PUT, and reports the terminal outcome — which finalizes the ledger.

LOGGING RULE: never log the capability/callback token, answers, credentials, or
any PII — only execution/application ids, milestone names, and status codes.
"""
from __future__ import annotations

import logging

from flask import Blueprint, request

from services import executor_capability_service as caps
from services import auto_apply_service as aas
from services import application_ledger_service as ledger
from services.executor_adapter import (
    SUBMIT_SUCCESS, SUBMIT_UNKNOWN, SUBMIT_WAITING_VERIFY, SUBMIT_FAILED,
)

executor_bp = Blueprint("executor", __name__)
logger = logging.getLogger(__name__)

_CALLBACK_HEADER = "X-Executor-Callback"

# Milestone names the worker may emit (bounded allow-list — anything else is
# recorded as "unknown" so a compromised/buggy worker can't flood arbitrary text).
_MILESTONES = {
    "claimed", "envelope_loaded", "resume_downloaded", "credential_received",
    "form_filled", "presubmit_checked", "submit_clicked", "receipt_uploaded",
    "artifacts_deleted",
}
# Terminal status → executor SUBMIT_* value the ledger finalizer understands.
_TERMINAL_MAP = {
    "submitted": SUBMIT_SUCCESS,
    "submission_unknown": SUBMIT_UNKNOWN,
    "waiting_verify": SUBMIT_WAITING_VERIFY,
    "failed": SUBMIT_FAILED,
}


def _forbidden(reason: str = "forbidden"):
    # 403, NOT 401 — machine route, invariant #1.
    return {"error": reason}, 403


def _callback_doc():
    """Resolve + validate the callback token from the header. Returns the bound
    execution doc, or raises caps.CapabilityError (mapped to 403 by the caller)."""
    return caps.verify_callback(request.headers.get(_CALLBACK_HEADER, ""))


@executor_bp.route("/claim", methods=["POST"])
def claim():
    """Phase 0: the worker presents the one-use capability (from the Cloud Task
    body) and atomically leases it. Returns the minimal execution envelope + a
    scoped callback token. A missing/used/expired capability → 403 (the worker
    aborts — the double-dispatch backstop, BEFORE any browser launches)."""
    body = request.get_json(silent=True) or {}
    capability = str(body.get("capability") or "")
    try:
        claimed = caps.claim_capability(capability)
    except caps.CapabilityError as e:
        logger.warning("executor claim: %s", e.error)
        return _forbidden(e.error)

    doc = claimed["doc"]
    envelope = aas.execution_envelope(doc["application_id"], doc["user_email"])
    if envelope is None:
        return {"error": "execution_gone"}, 409
    logger.info("executor claim: leased execution_id=%s app=%s",
                doc.get("execution_id"), doc.get("application_id"))
    return {"callback_token": claimed["callback_token"], "envelope": envelope}, 200


@executor_bp.route("/resume-url", methods=["POST"])
def resume_url():
    """One-use presigned GET for the ONE pinned résumé PDF (owner-scoped)."""
    try:
        doc = _callback_doc()
    except caps.CapabilityError as e:
        return _forbidden(e.error)
    s3_key = aas.pinned_resume_s3_key(doc["application_id"], doc["user_email"])
    if not s3_key:
        return {"error": "resume_not_pinned"}, 409
    try:
        url = caps.issue_resume_get(doc, s3_key)
    except caps.CapabilityError as e:
        return {"error": e.error}, e.status
    return {"url": url}, 200


@executor_bp.route("/credential", methods=["POST"])
def credential():
    """One-use, in-memory hand-off of the decrypted ATS credential (owner-scoped,
    KMS-envelope vault). Body supplies ats_family/tenant; identity is from the
    token. The plaintext is returned once and NEVER logged."""
    try:
        doc = _callback_doc()
    except caps.CapabilityError as e:
        return _forbidden(e.error)
    body = request.get_json(silent=True) or {}
    ats_family = str(body.get("ats_family") or "")
    tenant = str(body.get("tenant") or "")
    try:
        cred = caps.issue_credential(doc, ats_family=ats_family, tenant=tenant)
    except caps.CapabilityError as e:
        return {"error": e.error}, e.status
    return {"username": cred.get("username", ""),
            "password": cred.get("password", "")}, 200


@executor_bp.route("/receipt-url", methods=["POST"])
def receipt_url():
    """One-use presigned PUT for the ONE receipt/proof object (audit prefix)."""
    try:
        doc = _callback_doc()
    except caps.CapabilityError as e:
        return _forbidden(e.error)
    try:
        put = caps.issue_receipt_put(doc)
    except caps.CapabilityError as e:
        return {"error": e.error}, e.status
    return {"key": put["key"], "url": put["url"], "headers": put["headers"]}, 200


@executor_bp.route("/precheck", methods=["POST"])
def precheck():
    """The pre-submit re-check gate. The worker calls this immediately BEFORE it
    clicks Submit and aborts on ``ok=False``. Re-computes every gate server-side
    (kill switch, pause/revocation, approval freshness/expiry, immutable form
    hash + résumé/answer versions, entitlement reservation, adapter health,
    posting availability)."""
    try:
        doc = _callback_doc()
    except caps.CapabilityError as e:
        return _forbidden(e.error)
    result = aas.presubmit_recheck(doc["application_id"], doc["user_email"])
    return result, 200


@executor_bp.route("/milestone", methods=["POST"])
def milestone():
    """Record a structured, PII-free execution milestone (audit breadcrumb)."""
    try:
        doc = _callback_doc()
    except caps.CapabilityError as e:
        return _forbidden(e.error)
    body = request.get_json(silent=True) or {}
    name = str(body.get("name") or "")
    if name not in _MILESTONES:
        name = "unknown"
    logger.info("executor milestone=%s execution_id=%s app=%s", name,
                doc.get("execution_id"), doc.get("application_id"))
    try:
        ledger._audit(doc["user_email"], "auto_apply_executor_milestone",
                      application_id=doc["application_id"], meta={"milestone": name})
    except Exception:  # pragma: no cover - audit is best-effort
        pass
    return {"ok": True}, 200


@executor_bp.route("/terminal", methods=["POST"])
def terminal():
    """The worker's terminal outcome report → finalize the ledger. Idempotent:
    the first report wins the one-use terminal flag; a duplicate is a safe no-op.

    A ``submission_unknown`` (timeout/lost connection AFTER the Submit click) is
    HELD, never auto-retried (duplicate-application guard)."""
    try:
        doc = _callback_doc()
    except caps.CapabilityError as e:
        return _forbidden(e.error)
    body = request.get_json(silent=True) or {}
    raw_status = str(body.get("status") or "")
    status = _TERMINAL_MAP.get(raw_status)
    if status is None:
        return {"error": "invalid_status"}, 400

    # One-use terminal guard (idempotent redelivery).
    if not caps.mark_terminal_reported(doc["execution_id"]):
        logger.info("executor terminal: duplicate report execution_id=%s — no-op",
                    doc.get("execution_id"))
        return {"ok": True, "deduplicated": True}, 200

    try:
        result = aas.finalize_submission(
            doc["application_id"], doc["user_email"],
            status=status,
            confirmation=body.get("confirmation"),
            screenshot_ref=body.get("screenshot_ref"),
            wall=body.get("wall") if isinstance(body.get("wall"), dict) else None,
            error=body.get("error"))
    except ledger.ApplicationNotFound:
        return {"error": "application_not_found"}, 404
    return {"ok": True, "result": result}, 200
