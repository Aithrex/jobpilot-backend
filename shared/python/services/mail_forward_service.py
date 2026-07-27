"""Managed Application Mailbox — LINK-ONLY notification to the personal inbox.

This is the "tell the user at their ORIGINAL inbox that something happened" half
of the managed mailbox. When an inbound message is durably stored by
``mail_store_service.ingest_committed`` and the commit route calls
``maybe_forward``, we send a SHORT POINTER to the user's ``forward_to`` — from a
JobPilot-owned sender, never spoofing the recruiter.

Two halves, two policies (spec §6):
  - the IN-APP inbox keeps EVERYTHING (``ingest_committed`` stores every message
    unconditionally — nothing here drops content);
  - the PERSONAL inbox gets an ALLOW-LIST only, as a link.

What we send (link-only notification):
  - the ORIGINAL subject on the header,
  - a one-line headline derived from the allow-listed classifier status
    ("<Employer> wants to interview you"),
  - a clean-attachment count, and one "Read it in <app>" button
    (``${FRONTEND_URL}/app/apply/<thread_id>``).
  The recruiter's BODY is deliberately NOT relayed — it stays in-app, which is
  the entire point of the managed mailbox and keeps recruiter content out of a
  third-party inbox.

What we SKIP (``should_forward``, now an ALLOW-LIST — silence is the default):
  - ``system_noise`` — OTP / verification / activate-account / password-reset /
    magic-login-link mail (during auto-apply an application OTP is captured by
    the EXECUTOR, not surfaced to the user);
  - ``not_allowlisted`` — anything the closed-schema classifier
    (``mail_ingress_service.classify``) does not land on an employer-response
    status, INCLUDING unclassifiable mail: an allow-list fails closed. The
    message is still fully readable in-app.

DOUBLE-NOTIFY: ``applied`` (the ATS "we received your application" confirmation)
is excluded by default because ``apply_scheduler``'s end-of-day
``apply_daily_summary`` already reports every application submitted that day.
The digest owns "we applied for you"; this path owns "the employer got back to
you". See ``_allowed_statuses``.

Hard rules (spec §0 D5 + the forwarding requirements):
  - Outbound goes through ``email_service.send_email`` (ZeptoMail), from
    ``MAIL_FORWARD_FROM`` (JobPilot-owned, verified) with a DISPLAY NAME derived
    from the original sender. We NEVER spoof (the actual From address is ours).
    Reply-To is a PRIVACY-PRESERVING per-thread reply-bound alias
    (``reply.<hex>@<APPLY_MAIL_DOMAIN>``), so a reply from the user's inbox routes
    through ``relay_reply`` back to the recruiter without ever exposing the user's
    real address. We add ``X-JP-Forwarded: 1`` as an outbound loop marker.
  - Send ONLY when the mailbox is not paused AND forwarding_enabled AND
    forward_to_verified AND forward_to is present AND forward_to is not
    suppressed. (Auto-provision now satisfies all of these out of the box:
    forward_to = the user's OWN registered address, enabled + verified.)
    ``trust`` is a CORRELATION signal (it drives application STATUS), NOT a
    forwarding gate — the notification is sent regardless of trust.
    ``MAIL_FORWARD_UNVERIFIED_MODE=suppress`` is an optional kill-switch to
    suppress everything unverified (default OFF = forward).
  - OPEN-RELAY / LOOP GUARD: never forward a message whose sender is our own
    ``APPLY_MAIL_DOMAIN`` or equals ``MAIL_FORWARD_FROM`` (self/loop), or that
    already carried ``X-JP-Forwarded`` on the way in.
  - RATE LIMIT: a per-user daily cap (atomic ``$inc``) — over cap we skip (log),
    never raise.
  - Failures degrade gracefully: log a reason, return ``{sent, reason}``, never
    raise into the commit path.
  - NEVER log the forward_to address, the subject, or the body — only booleans,
    reasons, and the thread_id.

ALSO here: the privacy-preserving REPLY-RELAY (``relay_reply``) — when the user
answers a forwarded recruiter email from their own inbox, the reply lands on a
per-thread reply-bound alias and is relayed back to the recruiter from a
JobPilot-owned address, never exposing the user's real email and never as an open
relay (every defense is fail-closed).
"""

from __future__ import annotations

import html as _html
import logging
import re
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any, Dict, Optional, Tuple

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


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _send_as_alias() -> bool:
    """``APPLY_MAIL_SEND_AS_ALIAS`` (default OFF): when true, a relayed user reply
    goes out FROM the user's PRIMARY managed alias
    (``firstname.lastname@<APPLY_MAIL_DOMAIN>``) instead of the fallback
    ``MAIL_FORWARD_FROM`` sender. Flip it ON only AFTER ``APPLY_MAIL_DOMAIN`` is
    verified as a ZeptoMail sending domain (SPF/DKIM), which the operator asserts
    via ``APPLY_MAIL_SENDING_VERIFIED`` — BOTH must be truthy. Either unset ->
    fallback sender. NEVER the user's real email in either mode."""
    flag = (_get_config_value("APPLY_MAIL_SEND_AS_ALIAS", "") or "").strip().lower()
    if flag not in _TRUE:
        return False
    # The sending domain must also be asserted verified. Default: mirror the flag
    # (setting SEND_AS_ALIAS=true is the operator's assertion) unless explicitly
    # set false, so a deployment can arm/disarm with a single var.
    verified = (_get_config_value("APPLY_MAIL_SENDING_VERIFIED", "") or "").strip().lower()
    if verified == "":
        return True
    return verified in _TRUE


def _unverified_mode() -> str:
    """Optional kill-switch for UNVERIFIED (uncorrelated) mail:

      - ``forward`` (DEFAULT): relay full content for ALL non-noise mail,
        regardless of trust. ``trust`` is a correlation signal that drives the
        application STATUS suggestion — it is NOT a forwarding gate. This is the
        plain full-content relay the product wants.
      - ``suppress``: send NOTHING for unverified mail (opt-in lockdown for a
        deployment that wants to relay only correlated recruiter mail).

    Anything other than ``suppress`` (incl. unset) is ``forward``."""
    raw = (_get_config_value("MAIL_FORWARD_UNVERIFIED_MODE", "") or "").strip().lower()
    return "suppress" if raw == "suppress" else "forward"


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
# Message filter — forward MEANINGFUL mail, skip system noise
# ---------------------------------------------------------------------------

def _esc(value: Any) -> str:
    return _html.escape(str(value or ""), quote=True)


# Deterministic system-noise detectors. OTP / verification / verify-or-confirm-
# your-email / activate-account / password-reset / magic-login-link mail is
# transactional and the user did not ask us to relay it. We match on the subject
# + extracted text; ANY hit -> noise (skip). Ordered list of standalone signals.
_NOISE_PATTERNS = [
    re.compile(r"\bone[\s\-]?time\s+(?:code|passcode|password|pin|link|token)\b", re.I),
    re.compile(r"\b(?:verification|security|login|log[\s\-]?in|sign[\s\-]?in|"
               r"access|auth(?:entication)?|confirmation)\s+code\b", re.I),
    re.compile(r"\bpass\s?code\b", re.I),
    re.compile(r"\bverify\s+your\s+(?:e[\s\-]?mail(?:\s+address)?|account|identity|"
               r"sign[\s\-]?in|login)\b", re.I),
    re.compile(r"\bconfirm(?:ing)?\s+your\s+(?:e[\s\-]?mail(?:\s+address)?|account)\b", re.I),
    re.compile(r"\bactivate\s+your\s+account\b", re.I),
    re.compile(r"\b(?:reset|set|change)\s+your\s+password\b", re.I),
    re.compile(r"\bpassword\s+reset\b", re.I),
    re.compile(r"\b(?:sign[\s\-]?in|log[\s\-]?in|magic|one[\s\-]?time)\s+link\b", re.I),
    re.compile(r"\byour\s+(?:otp|one[\s\-]?time\s+password)\b", re.I),
    re.compile(r"\bverification\s+(?:code|link|email|required)\b", re.I),
]

# A standalone 4-8 digit code counts as noise ONLY alongside an OTP-ish context
# word — so a salary figure or "500 applicants" in a recruiter email is never
# mistaken for a one-time code.
_OTP_CONTEXT = re.compile(
    r"\b(?:code|otp|passcode|pin|verify|verification|confirm|authenticate|2fa|mfa)\b", re.I
)
_STANDALONE_CODE = re.compile(r"(?<![\w])\d{4,8}(?![\w])")


def is_system_noise(message: Dict[str, Any]) -> bool:
    """Deterministic, offline, free: is this OTP / verification / activate-
    account / password-reset / magic-login-link mail?

    ┌─ OTP-CAPTURE HOOK ─┐ During auto-apply an APPLICATION OTP is meant for the
    EXECUTOR (``application_otp_service``), never the user's inbox — this is the
    predicate ``mail_store_service._maybe_capture_application_otp`` keys on.
    ``message`` supplies ``subject`` and ``text``; both are treated as DATA."""
    blob = f"{str(message.get('subject') or '')}\n{str(message.get('text') or '')}"
    for pat in _NOISE_PATTERNS:
        if pat.search(blob):
            return True
    return bool(_OTP_CONTEXT.search(blob) and _STANDALONE_CODE.search(blob))


def _allowed_statuses() -> set:
    """The ALLOW-LIST: which classifier statuses earn a personal-inbox ping.

    Default = employer RESPONSES only — ``viewed``, ``oa_sent``,
    ``interview_invite``, ``offer``, ``rejected``, ``ghosted``.

    ``applied`` (the ATS "we received your application" confirmation) is
    DELIBERATELY EXCLUDED: ``apply_scheduler``'s end-of-day
    ``apply_daily_summary`` already reports every application submitted that day
    ("Applied for you (N)"), so per-event forwarding of the matching ATS
    acknowledgement would double-notify the same event. The digest owns
    "we applied for you"; per-event forwarding owns "the employer got back to
    you". Set ``MAIL_FORWARD_INCLUDE_APPLIED=true`` to re-arm confirmations if
    the digest is ever turned off. ``MAIL_FORWARD_ALLOWLIST`` (comma-separated)
    overrides the set wholesale; unknown names are dropped (the closed
    ``STATUS_ENUM`` is the authority — config can never widen it)."""
    from services.mail_ingress_service import STATUS_ENUM
    raw = (_get_config_value("MAIL_FORWARD_ALLOWLIST", "") or "").strip()
    if raw:
        return {s for s in (p.strip().lower() for p in raw.split(",")) if s in STATUS_ENUM}
    allowed = set(STATUS_ENUM) - {"applied"}
    include_applied = (_get_config_value("MAIL_FORWARD_INCLUDE_APPLIED", "") or "").strip().lower()
    if include_applied in _TRUE:
        allowed.add("applied")
    return allowed


# A relay into the user's PERSONAL inbox is an automatic action, so it requires a
# CONFIDENT classification. ``mail_ingress_service`` caps a classification whose
# evidence quote is missing or not actually in the body at
# ``_UNGROUNDED_CONFIDENCE_CAP`` (0.60) precisely so a hallucinated verdict can
# never drive one; this floor therefore sits ABOVE that cap. Overridable via
# ``MAIL_FORWARD_CONFIDENCE_FLOOR`` (a value at/below the ungrounded cap would
# re-open that hole, so it is clamped).
_CONFIDENCE_FLOOR_DEFAULT = 0.65


def _confidence_floor() -> float:
    from services.mail_ingress_service import _UNGROUNDED_CONFIDENCE_CAP
    raw = (_get_config_value("MAIL_FORWARD_CONFIDENCE_FLOOR", "") or "").strip()
    floor = _CONFIDENCE_FLOOR_DEFAULT
    if raw:
        try:
            floor = float(raw)
        except (TypeError, ValueError):
            floor = _CONFIDENCE_FLOOR_DEFAULT
    # Never below the ungrounded cap + epsilon: an ungrounded call must not relay.
    return max(floor, _UNGROUNDED_CONFIDENCE_CAP + 0.01)


def _confident_enough(result: Any) -> bool:
    """Is this classification confident enough to justify a personal-inbox relay?

    A classification carrying NO ``confidence`` field is a verdict written by our
    own deterministic code rather than a model score, and is honoured as-is (the
    classifier path always emits one). A malformed score fails CLOSED."""
    if not isinstance(result, dict) or "confidence" not in result:
        return True
    try:
        return float(result.get("confidence") or 0.0) >= _confidence_floor()
    except (TypeError, ValueError):
        return False


def _classify_status(message: Dict[str, Any]) -> Optional[str]:
    """Resolve this message to a closed-schema status, reusing the EXISTING
    classifier (``mail_ingress_service.classify`` — Claude Haiku/FLASH via the
    provider abstraction, closed ``STATUS_ENUM``, prompt-injection hardened).

    A classification already stored on the message doc wins (free); otherwise we
    run one FLASH pass. Returns None when there is no signal, when the classifier
    is unavailable, or when the verdict is not CONFIDENT enough to relay
    (``_confident_enough``). Never raises."""
    stored = message.get("classification")
    result: Any = stored
    if not (isinstance(stored, dict) and str(stored.get("status") or "").strip()):
        if (_get_config_value("MAIL_FORWARD_CLASSIFY_ENABLED", "") or ""
                ).strip().lower() in _FALSE:
            return None
        try:
            from services.mail_ingress_service import classify
            result = classify(
                str(message.get("from_domain") or ""),
                str(message.get("subject") or ""),
                str(message.get("text") or ""),
            )
        except Exception as e:  # pragma: no cover - classifier degrades internally
            logger.warning("mail-forward: classify failed: %s", type(e).__name__)
            return None

    status = str((result or {}).get("status") or "").strip().lower()
    if not status:
        return None
    if not _confident_enough(result):
        # Below the floor (typically an ungrounded-evidence classification) — the
        # message stays fully readable in-app; we just do not ping the real inbox.
        logger.info("mail-forward: classification below confidence floor")
        return None
    return status


def should_forward(message: Dict[str, Any], *,
                   classify: bool = True) -> Tuple[bool, str]:
    """ALLOW-LIST gate for the PERSONAL inbox (spec §6).

    The in-app inbox keeps EVERYTHING — ``mail_store_service.ingest_committed``
    stores every message unconditionally and nothing here changes that. Only the
    relay to the user's own email is filtered, and it is filtered POSITIVELY:
    silence is the default, a ping is the exception.

    Two stages:
      1. ``is_system_noise`` — deterministic, offline DENY of OTP/verification
         noise -> ``(False, "system_noise")``.
      2. ALLOW-LIST on the classifier status (``_allowed_statuses``) ->
         ``(True, "<status>")``. Anything else -> ``(False, ...)``, INCLUDING an
         unclassifiable message, a message with no classification CONTEXT, and a
         classification below the confidence floor: an allow-list fails CLOSED,
         and the message is still fully readable in-app.

    ``classify=False`` is the EXPLICIT noise-oracle contract: stage 1 only, no
    LLM call, verdict ``(True, "application")`` for anything that is not noise.
    Its one caller is ``mail_store_service._maybe_capture_application_otp`` on the
    webhook commit path, which needs the deterministic OTP predicate and must
    never gain a model call. It used to be INFERRED from the absence of
    ``from_domain``, which made the carve-out reachable by DATA: a message with an
    empty from_domain (a null return-path ``<>``, a bounce, a malformed From) took
    the oracle branch and was RELAYED to the user's real inbox — a `return True`
    default inside an allow-list."""
    if is_system_noise(message):
        return False, "system_noise"

    if not classify:
        # EXPLICIT noise-oracle contract: stage 1 only (no LLM, no relay decision).
        return True, "application"

    if not (bool(message.get("from_domain"))
            or isinstance(message.get("classification"), dict)):
        # Nothing to classify with -> fail CLOSED. Never relay on no evidence.
        return False, "no_context"

    status = _classify_status(message)
    if status and status in _allowed_statuses():
        return True, status
    return False, "not_allowlisted"


# ---------------------------------------------------------------------------
# Full-content relay builders
# ---------------------------------------------------------------------------

def _fetch_bodies(message: Dict[str, Any]) -> Tuple[str, str]:
    """Fetch the SANITIZED text + html bodies from S3 (the SAME objects the read
    API serves via ``mail_store_service._message_view``). Best-effort: a
    missing/failed fetch yields empty strings so the forward degrades to a short
    notice rather than crashing the commit path."""
    from services import mail_store_service as store
    try:
        storage = store.get_storage_service()
        bucket = store._apply_mail_bucket()
    except Exception:  # pragma: no cover - defensive
        return "", ""
    text = ""
    tk = message.get("text_s3_key")
    if tk:
        try:
            text = storage.get_object_bytes(tk, bucket=bucket).decode("utf-8", "replace")
        except Exception:
            text = ""
    safe_html = ""
    hk = message.get("safe_html_s3_key")
    if hk:
        try:
            safe_html = storage.get_object_bytes(hk, bucket=bucket).decode("utf-8", "replace")
        except Exception:
            safe_html = ""
    return text, safe_html


def _clean_attachment_count(message_id: str) -> int:
    """Count CLEAN (downloadable) attachments on a message. Suspicious/blocked
    ones are NEVER referenced in the forward."""
    try:
        return int(_db().mail_attachments.count_documents(
            {"message_id": message_id, "scan_verdict": "clean"}
        ))
    except Exception:  # pragma: no cover - defensive
        return 0


def _forward_display_name(message: Dict[str, Any]) -> str:
    """Display name for the forwarded From: the ORIGINAL sender, tagged so the
    user sees who really wrote it while the ACTUAL From address stays
    ``MAIL_FORWARD_FROM`` (we never spoof). Sanitized to a single header-safe
    token (no CR/LF/quotes/angle brackets — header-injection defense)."""
    original = str(message.get("from_name") or message.get("from_addr") or "").strip()
    original = InputSanitizer.sanitize_string(original, 120)
    original = re.sub(r'[\r\n"<>]', "", original).strip()
    if not original:
        original = "A recruiter"
    return f"{original} (via My Private Mail)"


# Human-readable headline per allow-listed classifier status. `{sender}` is the
# escaped original sender; the closed STATUS_ENUM is the only key space.
_STATUS_HEADLINE = {
    "viewed": "{sender} viewed your application",
    "oa_sent": "{sender} sent you an assessment",
    "interview_invite": "{sender} wants to interview you",
    "offer": "{sender} sent you an offer",
    "rejected": "{sender} passed on your application",
    "ghosted": "{sender} went quiet on your application",
    "applied": "{sender} received your application",
}


def build_forward(
    message: Dict[str, Any],
    thread: Dict[str, Any],
    *,
    manage_link: str,
    alias: str,
    clean_attachments: int,
    category: str = "",
    sender: str = "",
) -> Dict[str, str]:
    """Build the LINK-ONLY notification (spec §6).

    The personal inbox gets a POINTER, not a copy: who it is from, what kind of
    update it is (the allow-listed classifier status), and one button into the
    app. The full message body, the sanitized html and the attachments stay
    IN-APP — that is the whole point of the managed mailbox, and it also means we
    never re-emit recruiter content into a third-party inbox.

    Everything we inject (sender / alias / link / status) is HTML-escaped, and the
    ONLY untrusted strings that appear are the subject (header, sanitized by the
    caller's send path) and the sender display name."""
    subject = message.get("subject") or (thread or {}).get("subject") or "New message"
    who = _esc(sender or "An employer")
    headline = _STATUS_HEADLINE.get(category, "{sender} replied about your application").format(
        sender=who
    )

    att_html = ""
    att_text = ""
    if clean_attachments:
        att_html = (
            f'<p style="margin:0 0 4px;font-size:13px;color:#64748b">'
            f'{int(clean_attachments)} attachment(s) included.</p>'
        )
        att_text = f"\n{int(clean_attachments)} attachment(s) included."

    html = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:480px;margin:auto;color:#0f172a">'
        f'<h2 style="margin:0 0 8px;font-size:17px">{headline}</h2>'
        f'<p style="margin:0 0 4px;color:#475569;font-size:14px">'
        f'Subject: {_esc(subject)}</p>'
        f'{att_html}'
        f'<p style="margin:0 0 20px;color:#64748b;font-size:13px">'
        f'The full message is waiting in your {_esc(APP_NAME)} inbox — we keep the '
        f'content there so your personal inbox stays clean.</p>'
        f'<p style="margin:0 0 20px">'
        f'<a href="{_esc(manage_link)}" style="background:#4f46e5;color:#fff;'
        'padding:12px 20px;border-radius:8px;text-decoration:none;display:inline-block;'
        f'font-weight:600">Read it in {_esc(APP_NAME)}</a></p>'
        f'<p style="margin:0;color:#94a3b8;font-size:12px">Sent to your private '
        f'application address {_esc(alias)}.</p>'
        '</div>'
    )

    text = (
        f"{sender or 'An employer'} — update on your application\n\n"
        f"Subject: {subject}{att_text}\n\n"
        f"The full message is waiting in your {APP_NAME} inbox — we keep the content "
        f"there so your personal inbox stays clean.\n\n"
        f"Read it in {APP_NAME}: {manage_link}\n\n"
        f"Sent to your private application address {alias}."
    )
    return {"subject": subject, "html": html, "text": text}


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

    # --- ALLOW-LIST filter: ping only for application-status events. ----------
    # Fetch the sanitized text (the same S3 object the read API serves) so the
    # noise filter + the classifier see the REAL body. The body is NEVER relayed —
    # it is classifier input only; the notification we send is link-only.
    # A body-fetch failure degrades to an empty text (never crash).
    text, _safe_html = _fetch_bodies(message)

    forward_ok, category = should_forward({
        "subject": message.get("subject"),
        "text": text,
        "from_domain": message.get("from_domain"),
        "classification": message.get("classification"),
    }, classify=True)
    if not forward_ok:
        # system_noise: ingest already captured any OTP (see
        # mail_store_service._maybe_capture_application_otp).
        # not_allowlisted / no_context: the message IS in the in-app inbox — we
        # just don't ping the personal inbox for it.
        return _skip(category, thread_id)

    # --- Trust is a CORRELATION signal, NOT a forwarding gate. ----------------
    # The allow-listed notification is sent regardless of trust (trust still
    # drives the application STATUS suggestion elsewhere). The optional
    # MAIL_FORWARD_UNVERIFIED_MODE=suppress kill-switch (default OFF = forward)
    # lets a deployment notify only for correlated recruiter mail. Fail-safe:
    # anything not exactly "verified" is unverified.
    is_verified = str(message.get("trust") or "").strip().lower() == "verified"
    if not is_verified and _unverified_mode() == "suppress":
        # Skip BEFORE spending rate budget — a suppressed message never sends.
        return _skip("unverified_suppressed", thread_id)

    # Rate limit LAST (only spend budget on a message that would otherwise send).
    if not _within_daily_cap(user_email):
        return _skip("rate_limited", thread_id)

    # --- Build + send the LINK-ONLY notification (content stays in-app). ------
    thread = _db().mail_threads.find_one({"thread_id": thread_id}) or {}
    base = AppUrlsConfig.FRONTEND_URL
    safe_thread = InputSanitizer.sanitize_string(str(thread_id or ""), 64)
    manage_link = f"{base}/app/apply/{safe_thread}"
    alias = str(mb.get("address") or message.get("to_addr") or "your managed address")
    clean_attachments = _clean_attachment_count(message_id)

    tmpl = build_forward(
        message, thread,
        manage_link=manage_link, alias=alias,
        clean_attachments=clean_attachments,
        category=category,
        sender=str(message.get("from_name") or message.get("from_addr") or ""),
    )

    # From: JobPilot-owned MAIL_FORWARD_FROM with a display name derived from the
    # ORIGINAL sender — we NEVER spoof (the actual From address is ours).
    display = _forward_display_name(message)
    from_header = f"{display} <{forward_from_email}>" if forward_from_email else forward_from

    # Reply-To: a PRIVACY-PRESERVING per-thread reply-bound alias
    # (``reply.<hex>@<APPLY_MAIL_DOMAIN>``) minted/reused for this (thread, sender),
    # NOT the raw recruiter. When the user replies from their inbox, the reply
    # lands on that alias and the inbound reply-relay (``relay_reply``) sends it on
    # to the recruiter from a JobPilot-owned address — the user's real email is
    # never exposed. Fail-safe: if minting is unavailable (no managed domain), fall
    # back to the original sender so replying still works (no privacy relay).
    correspondent = str(message.get("from_addr") or "").strip()
    reply_to = correspondent
    try:
        reply_alias = mailbox.mint_reply_alias(
            user_email, message.get("mailbox_id"), thread_id, correspondent
        )
        if reply_alias and "@" in str(reply_alias.get("address") or ""):
            reply_to = reply_alias["address"]
    except Exception as e:  # pragma: no cover - never fail the forward on minting
        logger.warning("mail-forward: reply-alias mint failed: %s thread=%s",
                       type(e).__name__, thread_id)

    try:
        from services import email_service
        ok = email_service.send_email(
            forward_to,
            tmpl["subject"],
            tmpl["html"],
            tmpl["text"],
            from_addr=from_header,             # JobPilot-owned sender + via display name
            reply_to=reply_to,                 # original sender (reply-relay is a follow-up)
            headers={"X-JP-Forwarded": "1"},   # outbound loop marker
        )
    except Exception as e:  # pragma: no cover - send is best-effort
        logger.warning("mail-forward: send raised: %s thread=%s", type(e).__name__, thread_id)
        return {"sent": False, "reason": "send_error"}

    logger.info("mail-forward: forwarded=%s category=%s thread=%s", bool(ok), category, thread_id)
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
    link = f"{base}/app/settings/mailbox/verify-forward?token={_html.escape(str(token), quote=True)}"
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
# Reply-relay — the user replies from their inbox; we relay to the recruiter
# WITHOUT exposing the user's real address. NEVER an open relay (every defense
# below is fail-closed).
# ---------------------------------------------------------------------------

def _reply_auth_aligned(spf: str, dkim: str, dmarc: str) -> bool:
    """Auth-evidence gate for a relayed reply: the user's own message must carry
    aligned/passing SPF/DKIM/DMARC (evidence the user's domain really sent it).

    Fail-closed: a DMARC ``fail`` is rejected outright; a DMARC ``pass`` is the
    strong alignment signal; when DMARC is absent/unknown we require BOTH SPF and
    DKIM to pass. Anything else -> do not relay."""
    spf = (spf or "").strip().lower()
    dkim = (dkim or "").strip().lower()
    dmarc = (dmarc or "").strip().lower()
    if dmarc == "fail":
        return False
    if dmarc == "pass":
        return True
    return spf == "pass" and dkim == "pass"


def _first_name_from(primary_alias: str, forward_to: str) -> str:
    """A header-safe first name for the fallback relay display name. Derived from
    the primary alias local part (``firstname.lastname`` -> ``Firstname``); never
    exposes the user's real email (the local part of the MANAGED address only)."""
    local = str(primary_alias or "").split("@", 1)[0]
    first = (local.split(".", 1)[0] or "").strip()
    if not first:
        first = "Your contact"
    first = InputSanitizer.sanitize_string(first, 60)
    first = re.sub(r'[\r\n"<>]', "", first).strip()
    return (first[:1].upper() + first[1:]) if first else "Your contact"


def _reject(reason: str, thread_id: Optional[str] = None) -> Dict[str, Any]:
    # NEVER log the sender / correspondent / subject / body — reason + thread only.
    logger.info("mail-relay: not relayed reason=%s thread=%s", reason, thread_id or "-")
    return {"relayed": False, "reason": reason}


def relay_reply(
    alias_info: Dict[str, Any],
    envelope_from: str,
    raw_bytes: bytes,
    provider_message_id: str,
) -> Dict[str, Any]:
    """Relay a user's reply (received on a reply-bound alias) to the single bound
    correspondent — NEVER an open relay. Every check below is fail-closed:

      1. OWNER PROOF: the incoming From (and, when present, the envelope-from) MUST
         equal the mailbox owner's VERIFIED ``forward_to`` (case-insensitive). A
         recruiter or a spoofer hitting the reply alias fails here.
      2. AUTH EVIDENCE: SPF/DKIM/DMARC on the incoming message must be aligned/pass.
      3. LOOP GUARD: an ``X-JP-Relayed`` marker or a sender in our own managed /
         relay domains is dropped (never relay our own mail).
      4. SUPPRESSION + RATE LIMIT on the correspondent / owner.
      5. RELAY OUT to the ONE bound ``correspondent_email`` ONLY (never from the
         body): From = the primary managed alias (flag ON + sending domain
         verified) or the fallback ``MAIL_FORWARD_FROM`` display sender — NEVER the
         user's real email; Reply-To = the primary alias (so the recruiter's next
         reply returns to it and is forwarded to the user again, closing the loop);
         ``X-JP-Relayed: 1`` loop marker.
      6. STORE the reply as ``outbound`` in the SAME thread (idempotent — the store
         is the at-most-once send gate). Do NOT forward the user's own reply back.

    Returns ``{relayed, reason}``; never raises."""
    alias_info = alias_info or {}
    user_email = InputSanitizer.sanitize_email(alias_info.get("user_email"))
    thread_id = alias_info.get("thread_id")
    correspondent = InputSanitizer.sanitize_email(alias_info.get("correspondent_email"))
    primary_alias = str(alias_info.get("primary_alias") or "").strip()

    if not user_email or not correspondent:
        return _reject("relay_unbound", thread_id)

    # --- Owner gates: forwarding destination must be present AND verified. ------
    mb = mailbox.get_mailbox(user_email)
    forward_to = str(mb.get("forward_to") or "").strip()
    if not forward_to or not mb.get("forward_to_verified"):
        return _reject("owner_unverified", thread_id)

    # --- Parse the reply (reuse the ingest MIME parser + limits/sanitizer). -----
    from services import mail_store_service as store
    try:
        parsed = store._parse_mime(raw_bytes or b"")
    except store.MailRejected as e:
        return _reject(f"malformed:{e.reason}", thread_id)

    from_addr = str(parsed.get("from_addr") or "").strip().lower()
    _, env_from_email = parseaddr(str(envelope_from or ""))
    env_from_email = (env_from_email or "").strip().lower()
    fwd = forward_to.lower()

    # 1. OWNER PROOF — the reply must really be from the owner's verified inbox.
    #    The MIME From must match; a present envelope-from must match too.
    if from_addr != fwd:
        return _reject("sender_not_owner", thread_id)
    if env_from_email and env_from_email != fwd:
        return _reject("sender_not_owner", thread_id)

    # 2. AUTH EVIDENCE — SPF/DKIM/DMARC aligned/pass on the incoming message.
    if not _reply_auth_aligned(parsed.get("spf"), parsed.get("dkim"), parsed.get("dmarc")):
        return _reject("auth_unaligned", thread_id)

    # 3. LOOP GUARD — never relay our own already-relayed/forwarded mail, and never
    #    relay a "correspondent" that is one of our own domains.
    if parsed.get("x_jp_relayed") or parsed.get("x_jp_forwarded"):
        return _reject("loop_relayed_header", thread_id)
    apply_domain = mailbox.apply_mail_domain()
    corr_domain = correspondent.split("@", 1)[-1]
    if apply_domain and corr_domain and (
        corr_domain == apply_domain or corr_domain.endswith("." + apply_domain)
    ):
        return _reject("loop_own_domain", thread_id)
    forward_from = _forward_from()
    _, forward_from_email = parseaddr(forward_from)
    forward_from_email = (forward_from_email or forward_from).strip().lower()
    if forward_from_email and correspondent == forward_from_email:
        return _reject("loop_self", thread_id)

    # 4. SUPPRESSION on the correspondent (hard bounce / complaint).
    try:
        from services import email_suppression
        if email_suppression.is_suppressed(correspondent):
            return _reject("suppressed", thread_id)
    except Exception:  # pragma: no cover - best effort
        pass

    # We need a real relay sender: either the primary alias (flag) or MAIL_FORWARD_FROM.
    send_as_alias = _send_as_alias() and "@" in primary_alias
    if not send_as_alias and not forward_from:
        return _reject("relay_from_unconfigured", thread_id)

    # 5. RATE LIMIT (per-user daily cap; reuse the forward idiom) — LAST, so only a
    #    reply that would otherwise relay spends budget.
    if not _within_daily_cap(user_email):
        return _reject("rate_limited", thread_id)

    # --- STORE first (idempotent) — the unique (alias_id, provider_message_id)
    #     index is the at-most-once SEND gate. A Cloudflare retry re-stores nothing
    #     and never re-sends. ---------------------------------------------------
    if send_as_alias:
        # From = the user's PRIMARY managed alias (verified sending domain).
        from_shown = primary_alias
        from_header = f"{_first_name_from(primary_alias, forward_to)} <{primary_alias}>"
    else:
        # Fallback: JobPilot-owned MAIL_FORWARD_FROM with a "(via My Private Mail)"
        # display name. NEVER the user's real email.
        from_shown = forward_from_email or forward_from
        from_header = (
            f"{_first_name_from(primary_alias, forward_to)} (via My Private Mail) "
            f"<{forward_from_email}>"
        ) if forward_from_email else forward_from

    stored = store.store_outbound_reply(
        alias_info, parsed, provider_message_id,
        from_shown=from_shown, relay_to=correspondent,
    )
    if not stored.get("stored"):
        return _reject("store_failed", thread_id)
    if stored.get("deduplicated"):
        # Already relayed on a prior commit — do NOT send again (idempotent).
        return {"relayed": False, "reason": "duplicate"}

    # --- SEND to the ONE bound correspondent only. -----------------------------
    subject = parsed.get("subject") or "Re:"
    safe_html = store._sanitize_html(parsed.get("html") or "")
    text = parsed.get("text") or store._text_from_html(safe_html)
    if not safe_html:
        safe_html = f'<pre style="white-space:pre-wrap;font-family:inherit">{_esc(text)}</pre>'

    reply_to_primary = primary_alias if "@" in primary_alias else (forward_from_email or forward_from)
    try:
        from services import email_service
        ok = email_service.send_email(
            correspondent,              # the SINGLE bound correspondent — nothing else
            subject,
            safe_html,
            text,
            from_addr=from_header,      # primary alias or fallback — NEVER the real email
            reply_to=reply_to_primary,  # recruiter's next reply returns to the primary alias
            headers={"X-JP-Relayed": "1"},  # loop marker
        )
    except Exception as e:  # pragma: no cover - send is best-effort
        logger.warning("mail-relay: send raised: %s thread=%s", type(e).__name__, thread_id)
        return {"relayed": False, "reason": "send_error"}

    logger.info("mail-relay: relayed=%s thread=%s", bool(ok), thread_id)
    return {"relayed": bool(ok), "reason": "relayed" if ok else "send_failed"}
