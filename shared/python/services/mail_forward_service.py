"""Managed Application Mailbox — SAFE forwarding (notify-to-original-inbox).

This is the "user gets the rejection at their ORIGINAL email" half of the
Greenhouse acceptance scenario (managed-mailbox-v1 §8.1 step 7). When an inbound
message is durably stored by ``mail_store_service.ingest_committed`` and the
commit route calls ``maybe_forward``, we send a SANITIZED notification to the
user's verified original inbox — from a JobPilot-owned sender, never the
recruiter.

What we send (the DEFAULT and only path here) is deliberately minimal and safe:
  - the employer/company, the subject, and the deterministic status suggestion
    (e.g. "looks like: Rejected"),
  - a SECURE DEEP LINK to ``${FRONTEND_URL}/next/applications/<thread_id>`` (the
    app auth-gates it; there is NO token in the link),
  - NEVER the raw recruiter MIME/body, and NEVER any attachment.

Hard rules (spec §0 D5 + the forwarding requirements):
  - Outbound goes through ``email_service.send_email`` (ZeptoMail), from
    ``MAIL_FORWARD_FROM`` (JobPilot-owned, verified). From/Reply-To are NEVER the
    recruiter's address — we do not spoof. We add ``X-JP-Forwarded: 1`` as an
    outbound loop marker.
  - Send ONLY when the mailbox is not paused AND forwarding_enabled AND
    forward_to_verified AND forward_to is present AND forward_to is not
    suppressed.
  - OPEN-RELAY / LOOP GUARD: never forward a message whose sender is our own
    ``APPLY_MAIL_DOMAIN`` or equals ``MAIL_FORWARD_FROM`` (self/loop), or that
    already carried ``X-JP-Forwarded`` on the way in.
  - RATE LIMIT: a per-user daily cap (atomic ``$inc``) — over cap we skip (log),
    never raise.
  - Failures degrade gracefully: log a reason, return ``{sent, reason}``, never
    raise into the commit path.
  - NEVER log the forward_to address, the subject, or the body — only booleans,
    reasons, and the thread_id.

NOT in scope here (leave a clearly-named stub): the recruiter reply-relay /
reply-bound-alias inbound relay — see ``reply_relay_stub``.
"""

from __future__ import annotations

import html as _html
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any, Dict, Optional

from utils.config import _get_config_value, AppUrlsConfig
from utils.db_connect import DBConnect
from utils.security import InputSanitizer

from services import managed_mailbox_service as mailbox

logger = logging.getLogger(__name__)

APP_NAME = "Aspirely"

# Per-user daily forward cap (config-overridable). A safety valve against a
# misbehaving sender/loop flooding a user's real inbox — not a product quota.
_DAILY_CAP_DEFAULT = 50


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _forward_from() -> str:
    """The JobPilot-owned verified relay sender (``MAIL_FORWARD_FROM``). Empty ->
    forwarding is inert (we cannot send without a verified sender)."""
    return (_get_config_value("MAIL_FORWARD_FROM", "") or "").strip()


def _daily_cap() -> int:
    raw = _get_config_value("MAIL_FORWARD_DAILY_CAP", None)
    if raw is None or str(raw).strip() == "":
        return _DAILY_CAP_DEFAULT
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return _DAILY_CAP_DEFAULT


def _unverified_mode() -> str:
    """How to forward an UNVERIFIED (uncorrelated) message (P1-FWD-TRUST / spec
    §8/D8):

      - ``generic`` (DEFAULT): still alert the user, but with a subject-less,
        employer-less GENERIC notice — never surface attacker-controlled text.
      - ``suppress``: send nothing for unverified mail.

    Anything other than ``suppress`` (incl. unset) is ``generic`` so the user is
    still safely alerted by default."""
    raw = (_get_config_value("MAIL_FORWARD_UNVERIFIED_MODE", "") or "").strip().lower()
    return "suppress" if raw == "suppress" else "generic"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    return DBConnect().get_db()


def _counters():
    return _db().mail_forward_counters


# ---------------------------------------------------------------------------
# Rate limit (atomic per-user daily counter)
# ---------------------------------------------------------------------------

def _within_daily_cap(user_email: str) -> bool:
    """Atomically increment today's forward counter for ``user_email`` and return
    True if we are still within the cap. Concurrent Lambdas are safe (single
    ``$inc``). Best-effort: a counter-store error never blocks a send."""
    cap = _daily_cap()
    day = _now().strftime("%Y-%m-%d")
    try:
        from pymongo import ReturnDocument
        doc = _counters().find_one_and_update(
            {"user_email": user_email, "day": day},
            {"$inc": {"count": 1}, "$setOnInsert": {"created_at": _now()}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(doc.get("count", 1)) <= cap
    except Exception as e:  # pragma: no cover - best effort
        logger.warning("mail-forward: rate-limit read failed: %s", type(e).__name__)
        return True


# ---------------------------------------------------------------------------
# Notification builder (sanitized — never the raw recruiter body)
# ---------------------------------------------------------------------------

def _esc(value: Any) -> str:
    return _html.escape(str(value or ""), quote=True)


def _status_label(message: Dict[str, Any], thread: Dict[str, Any]) -> Optional[str]:
    """Best available deterministic status suggestion, if any: the message's
    classification first, then the thread's status_suggestion. Returns a Title
    Cased label (e.g. ``Rejected``) or None."""
    classification = message.get("classification") or {}
    suggested = classification.get("status") if isinstance(classification, dict) else None
    if not suggested:
        sugg = thread.get("status_suggestion") or {}
        if isinstance(sugg, dict):
            suggested = sugg.get("suggested")
    if not suggested:
        return None
    return str(suggested).replace("_", " ").strip().title() or None


def build_notification(message: Dict[str, Any], thread: Dict[str, Any], deep_link: str) -> Dict[str, str]:
    """Build the sanitized forward notification (subject/html/text). All
    recruiter-derived text (employer, subject) is HTML-escaped; the raw body is
    NEVER included."""
    employer = message.get("from_name") or message.get("from_domain") or "the employer"
    subject_in = message.get("subject") or thread.get("subject") or "your application"
    status = _status_label(message, thread)

    if status:
        subject = f"Update on your application — looks like: {status}"
        headline = f"Update on your application — looks like: {status}"
    else:
        subject = "New message about your application"
        headline = "New message about your application"

    body_html = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:520px;margin:auto;color:#0f172a">'
        f'<h2 style="margin:0 0 12px">{_esc(headline)}</h2>'
        f'<p style="color:#444;line-height:1.6">You have a new message from '
        f'<strong>{_esc(employer)}</strong> about a role you applied to.</p>'
        f'<p style="color:#444;line-height:1.6;margin:8px 0"><strong>Subject:</strong> '
        f'{_esc(subject_in)}</p>'
        + (f'<p style="color:#444;line-height:1.6"><strong>Looks like:</strong> '
           f'{_esc(status)}</p>' if status else '')
        + '<p style="margin:24px 0">'
        f'<a href="{_esc(deep_link)}" style="background:#4f46e5;color:#fff;padding:12px 20px;'
        'border-radius:8px;text-decoration:none;display:inline-block;font-weight:600">'
        'Open in ' + _esc(APP_NAME) + '</a></p>'
        '<p style="color:#888;font-size:13px;line-height:1.6">For your security we do not '
        'include the full message here — open it in the app to read and reply.</p>'
        '</div>'
    )

    text_lines = [
        headline,
        "",
        f"You have a new message from {employer} about a role you applied to.",
        f"Subject: {subject_in}",
    ]
    if status:
        text_lines.append(f"Looks like: {status}")
    text_lines += [
        "",
        f"Open it in {APP_NAME}: {deep_link}",
        "",
        "For your security we do not include the full message here — open it in "
        "the app to read and reply.",
    ]
    return {"subject": subject, "html": body_html, "text": "\n".join(text_lines)}


def build_generic_notification(deep_link: str) -> Dict[str, str]:
    """Build the GENERIC forward notification for an UNVERIFIED (uncorrelated,
    possibly guessable-alias-spoofed) message (P1-FWD-TRUST / spec §8/D8).

    It alerts the user that a new message arrived and links them into the app —
    but includes NO attacker-controlled content: no sender/employer, no subject,
    no body, no status. The ONLY variable is the secure in-app deep link (which
    the app auth-gates). ``deep_link`` is HTML-escaped by the caller/here."""
    headline = f"You have a new message in your {APP_NAME} inbox"

    body_html = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:520px;margin:auto;color:#0f172a">'
        f'<h2 style="margin:0 0 12px">{_esc(headline)}</h2>'
        '<p style="color:#444;line-height:1.6">A new message arrived at your '
        f'{_esc(APP_NAME)} application inbox. For your security we have not included '
        'any of its content here — open it in the app to review it safely.</p>'
        '<p style="margin:24px 0">'
        f'<a href="{_esc(deep_link)}" style="background:#4f46e5;color:#fff;padding:12px 20px;'
        'border-radius:8px;text-decoration:none;display:inline-block;font-weight:600">'
        'Open in ' + _esc(APP_NAME) + '</a></p>'
        '</div>'
    )

    text_lines = [
        headline,
        "",
        f"A new message arrived at your {APP_NAME} application inbox. For your "
        "security we have not included any of its content here — open it in the "
        "app to review it safely.",
        "",
        f"Open it in {APP_NAME}: {deep_link}",
    ]
    return {"subject": headline, "html": body_html, "text": "\n".join(text_lines)}


# ---------------------------------------------------------------------------
# The forward decision + send
# ---------------------------------------------------------------------------

def _skip(reason: str, thread_id: Optional[str] = None) -> Dict[str, Any]:
    # NEVER log forward_to / subject / body — only booleans, reasons, thread_id.
    logger.info("mail-forward: skip reason=%s thread=%s", reason, thread_id or "-")
    return {"sent": False, "reason": reason}


def forward_notification(message_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Send the SAFE notification for a just-ingested message, honoring every
    gate/guard above. ``message_doc`` is the ``ingest_committed`` result
    (``{thread_id, message_id, deduplicated}``); the durable message + thread are
    loaded from Mongo by ``message_id``. Returns ``{sent, reason}``; never raises.
    """
    message_doc = message_doc or {}

    # A dedup re-delivery was already forwarded on first store — do not resend.
    if message_doc.get("deduplicated"):
        return _skip("deduplicated", message_doc.get("thread_id"))

    message_id = InputSanitizer.sanitize_string(str(message_doc.get("message_id") or ""), 64)
    if not message_id:
        return _skip("no_message")

    # Load the durable, stored message (canonical fields — envelope owner,
    # from_domain, loop marker — never a body identity).
    message = _db().mail_messages.find_one({"message_id": message_id})
    if not message:
        return _skip("no_message")

    thread_id = message.get("thread_id")

    # --- Loop / open-relay guards (before we resolve anything user-facing). ---
    from_addr = str(message.get("from_addr") or "").strip().lower()
    from_domain = str(message.get("from_domain") or "").strip().lower()
    forward_from = _forward_from()
    _, forward_from_email = parseaddr(forward_from)
    forward_from_email = (forward_from_email or forward_from).strip().lower()

    if message.get("x_jp_forwarded"):
        return _skip("loop_forwarded_header", thread_id)
    apply_domain = mailbox.apply_mail_domain()
    if apply_domain and from_domain and (
        from_domain == apply_domain or from_domain.endswith("." + apply_domain)
    ):
        return _skip("loop_own_domain", thread_id)
    if forward_from_email and from_addr and from_addr == forward_from_email:
        return _skip("loop_self", thread_id)

    if not forward_from:
        # No verified relay sender configured -> forwarding is inert.
        return _skip("forward_from_unconfigured", thread_id)

    # --- Mailbox gates (owner is the stored message's owner). ----------------
    user_email = message.get("user_email")
    mb = mailbox.get_mailbox(user_email)
    if mb.get("paused"):
        return _skip("paused", thread_id)
    if not mb.get("forwarding_enabled"):
        return _skip("forwarding_disabled", thread_id)
    forward_to = mb.get("forward_to")
    if not forward_to:
        return _skip("no_forward_to", thread_id)
    if not mb.get("forward_to_verified"):
        return _skip("forward_to_unverified", thread_id)

    # Suppression: check BEFORE any send (hard bounce / complaint).
    try:
        from services import email_suppression
        if email_suppression.is_suppressed(forward_to):
            return _skip("suppressed", thread_id)
    except Exception:  # pragma: no cover - best effort
        pass

    # --- Trust gate (P1-FWD-TRUST / spec §8/D8). -----------------------------
    # A message is FULLY UNTRUSTED unless correlation upgraded it to "verified"
    # (domain+timing matched a real submitted application). An UNVERIFIED
    # (uncorrelated, possibly guessable-alias-spoofed) message must NEVER push an
    # attacker-controlled Subject/employer/body into the user's real inbox
    # (T15). Fail-safe: anything that is not exactly "verified" is untrusted.
    #   verified -> full sanitized notification (subject/employer + deep link);
    #   unverified + generic (default) -> a subject-less GENERIC "new message"
    #                notice (still alerts the user, safely);
    #   unverified + suppress -> send nothing.
    # We use the MESSAGE's own trust (per-message correlation), not the thread's:
    # a spoofed message that lands in an otherwise-verified thread must not
    # inherit that thread's trust.
    is_verified = str(message.get("trust") or "").strip().lower() == "verified"
    if not is_verified and _unverified_mode() == "suppress":
        # Skip BEFORE spending rate budget — a suppressed message never sends.
        return _skip("unverified_suppressed", thread_id)

    # Rate limit LAST (only spend budget on a message that would otherwise send).
    if not _within_daily_cap(user_email):
        return _skip("rate_limited", thread_id)

    # --- Build + send the sanitized notification. ----------------------------
    thread = _db().mail_threads.find_one({"thread_id": thread_id}) or {}
    base = AppUrlsConfig.FRONTEND_URL
    safe_thread = InputSanitizer.sanitize_string(str(thread_id or ""), 64)
    deep_link = f"{base}/next/applications/{safe_thread}"

    # Verified -> full sanitized notification; unverified -> generic (no
    # attacker-controlled subject/employer/body, deep link only).
    if is_verified:
        tmpl = build_notification(message, thread, deep_link)
    else:
        tmpl = build_generic_notification(deep_link)

    try:
        from services import email_service
        ok = email_service.send_email(
            forward_to,
            tmpl["subject"],
            tmpl["html"],
            tmpl["text"],
            from_addr=forward_from,          # JobPilot-owned, NEVER the recruiter
            reply_to="",                     # no Reply-To to the recruiter
            headers={"X-JP-Forwarded": "1"},  # outbound loop marker
        )
    except Exception as e:  # pragma: no cover - send is best-effort
        logger.warning("mail-forward: send raised: %s thread=%s", type(e).__name__, thread_id)
        return {"sent": False, "reason": "send_error"}

    logger.info("mail-forward: forwarded=%s thread=%s", bool(ok), thread_id)
    return {"sent": bool(ok), "reason": "sent" if ok else "send_failed"}


# ---------------------------------------------------------------------------
# Verification email (used by the forwarding-settings endpoint)
# ---------------------------------------------------------------------------

def send_forward_verification(forward_to: str, token: str) -> bool:
    """Send the forward_to verification email FROM ``MAIL_FORWARD_FROM`` with a
    link the FE POSTs the token from. Checks suppression first. Best-effort:
    returns False (never raises) if it could not send. NEVER logs the address."""
    target = InputSanitizer.sanitize_email(forward_to)
    if not target or not token:
        return False

    forward_from = _forward_from()
    if not forward_from:
        logger.info("mail-forward: verification skipped (no MAIL_FORWARD_FROM)")
        return False

    try:
        from services import email_suppression
        if email_suppression.is_suppressed(target):
            logger.info("mail-forward: verification skipped (suppressed)")
            return False
    except Exception:  # pragma: no cover - best effort
        pass

    base = AppUrlsConfig.FRONTEND_URL
    link = f"{base}/next/settings/mailbox/verify-forward?token={_html.escape(str(token), quote=True)}"
    subject = f"Confirm forwarding for your {APP_NAME} mailbox"
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:480px;margin:auto;color:#0f172a">'
        f'<h2 style="margin:0 0 12px">Confirm forwarding</h2>'
        f'<p style="color:#444;line-height:1.6">You asked {APP_NAME} to forward updates '
        f'about your job applications to this address. Confirm it is yours to turn '
        f'forwarding on.</p>'
        '<p style="margin:24px 0">'
        f'<a href="{link}" style="background:#4f46e5;color:#fff;padding:12px 20px;'
        'border-radius:8px;text-decoration:none;display:inline-block;font-weight:600">'
        'Confirm this address</a></p>'
        f'<p style="color:#888;font-size:13px;word-break:break-all">Or paste this link into '
        f'your browser:<br>{link}</p>'
        "<p style=\"color:#888;font-size:13px\">If you didn't request this, you can ignore "
        "this email.</p>"
        '</div>'
    )
    text_body = (
        f"Confirm forwarding for your {APP_NAME} mailbox\n\n"
        f"You asked {APP_NAME} to forward job-application updates to this address. "
        f"Confirm it is yours:\n{link}\n\n"
        "If you didn't request this, ignore this email."
    )
    try:
        from services import email_service
        return bool(email_service.send_email(
            target, subject, html_body, text_body,
            from_addr=forward_from,
            headers={"X-JP-Forwarded": "1"},
        ))
    except Exception as e:  # pragma: no cover - best effort
        logger.warning("mail-forward: verification send raised: %s", type(e).__name__)
        return False


# ---------------------------------------------------------------------------
# NOT IMPLEMENTED HERE — recruiter reply-relay (later P2 task)
# ---------------------------------------------------------------------------

def reply_relay_stub(*_args, **_kwargs):
    """Intentional stub for the FUTURE recruiter reply-relay / reply-bound-alias
    INBOUND relay: sending a user's reply from a per-thread reply-bound alias back
    to the recruiter, and routing the recruiter's response back into the thread.

    That is a two-way conversation surface with its own approval/vault/anti-spoof
    controls and ships with the reply UI (managed-mailbox-v1 §5
    ``/threads/<id>/reply`` -> ``{job_id}``). It is deliberately NOT built as part
    of the safe notify-to-original-inbox forwarding path. Do NOT wire this into
    the inbound commit path — forwarding here is notification-only."""
    raise NotImplementedError("recruiter reply-relay is a later P2 task (reply UI)")
