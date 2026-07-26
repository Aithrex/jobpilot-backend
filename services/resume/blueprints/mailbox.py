"""Managed Application Mailbox — user-facing forwarding controls (managed-mailbox-v1 §1.2/§5).

Two JWT-REQUIRED, owner-scoped routes for the "forward my application updates to
my original inbox" control:

  POST /api/resume/mailbox/forwarding
      body ``{enabled: bool, forward_to?: email}`` -> toggle forwarding and/or
      (re)set the destination. Changing ``forward_to`` resets it to UNVERIFIED
      (``managed_mailbox_service.set_forwarding``) and we mint + email a
      verification link to the new destination. Returns the mailbox object.

  POST /api/resume/mailbox/forwarding/verify
      body ``{token}`` -> confirm the destination (sets ``forward_to_verified``
      only if the token is this user's, unused, unexpired, and still matches the
      CURRENT forward_to). Returns the mailbox object.

These ride the ALREADY-ROUTED ``/api/resume/*`` edge (mounted UNDER
``/api/resume/mailbox``), so NO new top-level prefix and NO new Terraform edge
route are required — same pattern as ``mail_inbound.py``.

Error contract (invariant #1): every business failure is 400/404/409 — NEVER
401. 401 stays reserved for a missing/invalid JWT via ``@jwt_required``. Owner
identity is ALWAYS ``get_jwt_identity()`` (D7 / IDOR) — never a request-body
email.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import InputSanitizer
from services import managed_mailbox_service as mailbox
from services import mail_forward_service
from services import mail_store_service as store

mailbox_bp = Blueprint("mailbox", __name__)
logger = logging.getLogger(__name__)


def _resolve_display_name(user_email: str):
    """Resolve the user's display name SERVER-SIDE for provisioning (spec D7):
    the readable local part is seeded from the AUTHENTICATED user's own record,
    NEVER from the request body. Order: the ``users.name`` field (set at
    sign-up / OAuth), then the parsed resume's ``contact.name``. Returns None
    when nothing is resolvable — ``managed_mailbox_service.provision`` then
    falls back to an opaque privacy alias rather than exposing a guessed name.

    ``user_email`` is sanitized before it reaches any Mongo filter (invariant
    #4); every read is scoped to that email."""
    email = InputSanitizer.sanitize_email(user_email)
    if not email:
        return None
    try:
        from utils.db_connect import DBConnect
        user = DBConnect().get_db().users.find_one({"email": email}, {"name": 1, "_id": 0}) or {}
        name = str(user.get("name") or "").strip()
        if name:
            return name
    except Exception:  # pragma: no cover - name resolution is best-effort
        pass
    try:
        from services import resume_access
        structured = (resume_access.get_active_structured(email) or {}).get("structured") or {}
        contact = structured.get("contact") or {}
        name = str(contact.get("name") or "").strip()
        if name:
            return name
    except Exception:  # pragma: no cover - name resolution is best-effort
        pass
    return None


def _with_unread(mailbox_obj: dict, user_email: str) -> dict:
    """Overlay the real cross-thread unread total onto a serialized mailbox
    (the service returns a 0 placeholder)."""
    mailbox_obj["unread_count"] = store.unread_count(user_email)
    return mailbox_obj


@mailbox_bp.route("/forwarding", methods=["POST"])
@jwt_required()
def set_forwarding():
    """Toggle forwarding and/or (re)set the destination; email a verification
    link when a new/unverified destination is set. Owner-scoped by JWT."""
    user_email = get_jwt_identity()
    body = request.get_json(silent=True) or {}

    enabled = bool(body.get("enabled"))
    forward_to_raw = body.get("forward_to")
    forward_to = None
    if forward_to_raw is not None:
        forward_to = InputSanitizer.sanitize_email(forward_to_raw)
        if not forward_to:
            return jsonify({"error": "invalid_forward_to"}), 400

    try:
        result = mailbox.set_forwarding(user_email, enabled, forward_to)
    except LookupError:
        # No mailbox provisioned yet — 404 (never 401).
        return jsonify({"error": "mailbox_not_provisioned"}), 404
    except ValueError as e:
        return jsonify({"error": str(e) or "invalid_request"}), 400

    # If a destination is set but not yet verified, (re)mint + email a
    # verification link. This covers a brand-new destination and a re-request for
    # a destination whose earlier verification never completed. Best-effort: an
    # email-send failure must not fail the settings write.
    if result.get("forward_to") and not result.get("forward_to_verified"):
        try:
            token = mailbox.mint_forward_verification(user_email, result["forward_to"])
            mail_forward_service.send_forward_verification(result["forward_to"], token)
        except Exception as e:  # pragma: no cover - verification email best-effort
            logger.warning("mailbox forwarding: verification mint/send failed: %s", type(e).__name__)

    return jsonify(result), 200


@mailbox_bp.route("/forwarding/verify", methods=["POST"])
@jwt_required()
def verify_forwarding():
    """Confirm the forwarding destination from the emailed token. Owner-scoped by
    JWT (a token minted for another user can never verify this one)."""
    user_email = get_jwt_identity()
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    if not token:
        return jsonify({"error": "token_required"}), 400

    try:
        result = mailbox.confirm_forward_verification(user_email, token)
    except LookupError:
        # Not found / wrong owner / no mailbox — 404 (never reveal, never 401).
        return jsonify({"error": "verification_not_found"}), 404
    except ValueError as e:
        reason = str(e) or "invalid_request"
        # Consumed / expired / destination-changed are conflicts (409); the rest
        # are bad input (400). Never 401.
        if reason in ("verification_consumed", "verification_expired", "forward_to_changed"):
            return jsonify({"error": reason}), 409
        return jsonify({"error": reason}), 400

    return jsonify(result), 200


# ===========================================================================
# Mailbox state + controls (§5) — all JWT-required, owner = get_jwt_identity()
# ===========================================================================

@mailbox_bp.route("", methods=["GET"])
@jwt_required()
def get_mailbox():
    """Current mailbox state for the inbox header. Not provisioned yet ->
    ``{provisioned: false}`` (200, never 401). Owner-scoped by JWT."""
    user_email = get_jwt_identity()
    mb = mailbox.get_mailbox(user_email)
    if not mb.get("provisioned"):
        return jsonify({"provisioned": False}), 200
    return jsonify(_with_unread(mb, user_email)), 200


@mailbox_bp.route("/provision", methods=["POST"])
@jwt_required()
def provision_mailbox():
    """Mint (or return the existing) managed mailbox. Idempotent. The readable
    name is resolved SERVER-SIDE (D7) — a ``display_name`` in the body is
    IGNORED. ``{privacy_alias: true}`` mints an opaque alias instead."""
    user_email = get_jwt_identity()
    body = request.get_json(silent=True) or {}
    privacy_alias = bool(body.get("privacy_alias"))
    display_name = _resolve_display_name(user_email)  # NEVER body.get("display_name")
    try:
        result = mailbox.provision(user_email, display_name, privacy_alias=privacy_alias)
    except ValueError as e:
        return jsonify({"error": str(e) or "invalid_request"}), 400
    return jsonify(_with_unread(result, user_email)), 200


@mailbox_bp.route("/pause", methods=["POST"])
@jwt_required()
def pause_mailbox():
    """Pause/resume the mailbox. Body ``{paused: bool}``. Owner-scoped."""
    user_email = get_jwt_identity()
    body = request.get_json(silent=True) or {}
    if "paused" not in body:
        return jsonify({"error": "paused_required"}), 400
    try:
        result = mailbox.set_paused(user_email, bool(body.get("paused")))
    except LookupError:
        return jsonify({"error": "mailbox_not_provisioned"}), 404
    except ValueError as e:
        return jsonify({"error": str(e) or "invalid_request"}), 400
    return jsonify(_with_unread(result, user_email)), 200


@mailbox_bp.route("/rotate", methods=["POST"])
@jwt_required()
def rotate_mailbox():
    """Rotate to a new primary address; the old alias is tombstoned (never
    recycled). The new local part is seeded from the server-side name. Owner-
    scoped."""
    user_email = get_jwt_identity()
    display_name = _resolve_display_name(user_email)
    try:
        result = mailbox.rotate(user_email, display_name)
    except LookupError:
        return jsonify({"error": "mailbox_not_provisioned"}), 404
    except ValueError as e:
        return jsonify({"error": str(e) or "invalid_request"}), 400
    return jsonify(_with_unread(result, user_email)), 200


# ===========================================================================
# Threads + messages (§5) — every read/write owner-scoped; cross-user -> 404
# ===========================================================================

@mailbox_bp.route("/threads", methods=["GET"])
@jwt_required()
def list_mailbox_threads():
    """Inbox list (ThreadSummary[]), newest first. Optional filters
    ``?trust=&application_id=&unread=``. Owner-scoped."""
    user_email = get_jwt_identity()
    unread_raw = request.args.get("unread")
    unread = bool(unread_raw) and str(unread_raw).strip().lower() in ("1", "true", "yes", "on")
    threads = store.list_threads(
        user_email,
        trust=request.args.get("trust"),
        application_id=request.args.get("application_id"),
        unread=True if unread else None,
    )
    return jsonify({"threads": threads}), 200


@mailbox_bp.route("/threads/<thread_id>", methods=["GET"])
@jwt_required()
def get_mailbox_thread(thread_id):
    """Full thread: ``{thread, messages:[MessageView]}``. MessageView carries
    sanitized text inline + a short-TTL ``safe_html_url`` (never the raw MIME).
    Not the owner's thread -> 404 (never 403/leak)."""
    user_email = get_jwt_identity()
    detail = store.get_thread_detail(user_email, thread_id)
    if detail is None:
        return jsonify({"error": "thread_not_found"}), 404
    return jsonify(detail), 200


@mailbox_bp.route("/threads/<thread_id>/read", methods=["POST"])
@jwt_required()
def set_thread_read(thread_id):
    """Mark a thread read/unread. Body ``{read: bool}``. Owner-scoped -> 404 if
    not the user's."""
    user_email = get_jwt_identity()
    body = request.get_json(silent=True) or {}
    if "read" not in body:
        return jsonify({"error": "read_required"}), 400
    if not store.set_thread_read(user_email, thread_id, bool(body.get("read"))):
        return jsonify({"error": "thread_not_found"}), 404
    return jsonify({"ok": True}), 200


@mailbox_bp.route("/threads/<thread_id>/attach", methods=["POST"])
@jwt_required()
def attach_thread_application(thread_id):
    """Bind a thread to one of the user's OWN applications. Body
    ``{application_id}``. Foreign/missing application or thread -> 404; missing
    id -> 400. Owner-scoped."""
    user_email = get_jwt_identity()
    body = request.get_json(silent=True) or {}
    try:
        store.attach_thread(user_email, thread_id, body.get("application_id"))
    except LookupError as e:
        return jsonify({"error": str(e) or "not_found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e) or "invalid_request"}), 400
    return jsonify({"ok": True}), 200


@mailbox_bp.route("/threads/<thread_id>/detach", methods=["POST"])
@jwt_required()
def detach_thread_application(thread_id):
    """Unbind a thread from its application. Owner-scoped -> 404 if not the
    user's."""
    user_email = get_jwt_identity()
    if not store.detach_thread(user_email, thread_id):
        return jsonify({"error": "thread_not_found"}), 404
    return jsonify({"ok": True}), 200


@mailbox_bp.route("/threads/<thread_id>/report-phishing", methods=["POST"])
@jwt_required()
def report_thread_phishing(thread_id):
    """Report a thread as phishing: flag its messages and drop trust to
    ``unverified``. Owner-scoped -> 404 if not the user's."""
    user_email = get_jwt_identity()
    if not store.report_phishing(user_email, thread_id):
        return jsonify({"error": "thread_not_found"}), 404
    return jsonify({"ok": True}), 200


@mailbox_bp.route("/threads/<thread_id>", methods=["DELETE"])
@jwt_required()
def delete_mailbox_thread(thread_id):
    """Delete a thread + its messages + attachments (owner-scoped), best-effort
    removing their S3 objects. Not the owner's thread -> 404 (never 403/leak,
    never 401). Returns ``{ok, deleted:{messages, attachments}}``."""
    user_email = get_jwt_identity()
    deleted = store.delete_thread(user_email, thread_id)
    if deleted is None:
        return jsonify({"error": "thread_not_found"}), 404
    return jsonify({"ok": True, "deleted": deleted}), 200


# ===========================================================================
# Attachments (§5) — short-TTL presigned GET; quarantined -> 403
# ===========================================================================

@mailbox_bp.route("/attachments/<attachment_id>", methods=["GET"])
@jwt_required()
def get_mailbox_attachment(attachment_id):
    """Short-TTL presigned download URL for an owned, CLEAN attachment.
    Suspicious/blocked -> 403 (no URL). Not the owner's -> 404. Owner-scoped."""
    user_email = get_jwt_identity()
    try:
        result = store.get_attachment_download(user_email, attachment_id)
    except store.AttachmentBlocked:
        return jsonify({"error": "attachment_unavailable"}), 403
    except LookupError:
        return jsonify({"error": "attachment_not_found"}), 404
    return jsonify(result), 200


# ===========================================================================
# Reply — DEFERRED (managed-mailbox-v1 §5 ``/threads/<id>/reply`` -> {job_id}).
# Intentionally returns 501: the recruiter reply-relay / reply-bound-alias is a
# later P2 task that ships with the reply UI (see
# ``mail_forward_service.reply_relay_stub``). 501 is not 401 — it never trips
# the FE force-logout; the FE simply hides the reply affordance until this
# lands. Do NOT wire a real send here.
# ===========================================================================

@mailbox_bp.route("/threads/<thread_id>/reply", methods=["POST"])
@jwt_required()
def reply_thread(thread_id):
    return jsonify({"error": "not_implemented", "reason": "reply_relay_deferred"}), 501
