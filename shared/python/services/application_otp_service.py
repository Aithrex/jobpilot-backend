"""Application OTP / verify-link capture from the managed mailbox.

When ATS mail arrives as ``system_noise`` (OTP, email-verify, password-reset,
magic-login), we DO NOT forward it to the user's real inbox. Instead this module:

  1. Extracts a short OTP code and/or https verify/reset links (ATS-host only).
  2. Binds them to an active auto-apply freeze (``waiting_verify``) for the
     mailbox owner when one exists.
  3. Exposes ``claim_for_verify`` so ``BrowserExecutor.verify_state`` can fill the
     code or follow the link, then report ``ORACLE_READY``.

Fail-closed, best-effort, never raises into the mail commit path.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_TTL_MINUTES = 30
_OTP_RE = re.compile(r"(?<![\w])(\d{4,8})(?![\w])")
_OTP_CONTEXT = re.compile(
    r"\b(?:code|otp|passcode|pin|verify|verification|confirm|authenticate|"
    r"2fa|mfa|security|one[\s\-]?time)\b",
    re.I,
)
# Workday / Okta often put the code near the keyword in either order.
_OTP_NEAR_CODE = re.compile(
    r"(?:"
    r"(?<![\w])(\d{4,8})(?![\w]).{0,48}\b(?:code|passcode|otp|pin|verification)\b|"
    r"\b(?:code|passcode|otp|pin|verification)\b.{0,48}(?<![\w])(\d{4,8})(?![\w])"
    r")",
    re.I,
)
# Greenhouse "security code" is often alphanumeric (e.g. ORZTYWie), not digits.
_ALPHA_OTP_RE = re.compile(
    r"(?:security\s+code|verification\s+code|one[\s\-]?time(?:\s+(?:pass)?code)?|"
    r"passcode|otp)\b[^\nA-Za-z0-9]{0,80}\b([A-Za-z0-9]{6,12})\b",
    re.I,
)
_H1_CODE_RE = re.compile(r"<h1[^>]*>\s*([A-Za-z0-9]{6,12})\s*</h1>", re.I)
_WORKDAY_FROM = re.compile(r"@(?:.*\.)?workday\.com$", re.I)
# Digit / word matches that are almost never OTPs.
_OTP_DENY = {
    "2024", "2025", "2026", "2027", "2028", "10011", "10001", "0000", "1234",
    "passcode", "password", "security", "verify", "confirm", "greenhouse",
    "application", "code", "your", "this", "paste",
}


def _plausible_code(cand: Optional[str]) -> Optional[str]:
    if not cand:
        return None
    c = cand.strip()
    if not c or c.lower() in _OTP_DENY:
        return None
    # Pure digits 4–8, or mixed/upper alphanumeric 6–12 (Greenhouse style).
    if c.isdigit():
        return c if 4 <= len(c) <= 8 and c not in _OTP_DENY else None
    if not re.fullmatch(r"[A-Za-z0-9]{6,12}", c):
        return None
    # Reject all-lowercase dictionary-ish tokens; real GH codes are mixed/upper.
    if c.islower():
        return None
    return c
_HREF_RE = re.compile(r"""href=["'](https?://[^"']+)["']""", re.I)
_BARE_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_LINK_HINT = re.compile(
    r"(verify|confirm|activate|reset|password|sign[\s\-]?in|magic|one[\s\-]?time)",
    re.I,
)

# Links we will auto-follow must stay on ATS (or closely related) hosts — SSRF
# defense for password-reset / magic-login clicks.
_ATS_HOST_SUFFIXES = (
    "greenhouse.io", "grnh.se", "lever.co", "ashbyhq.com",
    "myworkdayjobs.com", "myworkdaysite.com", "myworkday.com",
    "workday.com", "okta.com", "auth0.com",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def _coll():
    return _db().application_otp_grants


def _ats_host_ok(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == s or host.endswith("." + s) for s in _ATS_HOST_SUFFIXES)


def extract_otp_payload(
    subject: str,
    text: str,
    html: str = "",
    from_addr: str = "",
) -> Dict[str, Any]:
    """Pull a one-time code and/or verify links from mail bodies."""
    blob = f"{subject or ''}\n{text or ''}\n{html or ''}"
    code = None

    # Prefer alphanumeric security codes (Greenhouse) before digit OTP.
    for m in _H1_CODE_RE.finditer(html or ""):
        code = _plausible_code(m.group(1))
        if code:
            break
    if not code:
        for m in _ALPHA_OTP_RE.finditer(blob):
            code = _plausible_code(m.group(1))
            if code:
                break

    if not code and _OTP_CONTEXT.search(blob):
        for m in _OTP_RE.finditer(blob):
            code = _plausible_code(m.group(1))
            if code:
                break
    if not code:
        m = _OTP_NEAR_CODE.search(blob)
        if m:
            code = _plausible_code(m.group(1) or m.group(2))

    from_workday = bool(_WORKDAY_FROM.search((from_addr or "").strip()))
    def link_ok(u: str) -> bool:
        return _ats_host_ok(u) and (
            _LINK_HINT.search(u)
            or _LINK_HINT.search(subject or "")
            or (from_workday and "workday" in (urlparse(u).hostname or "").lower())
        )

    links: List[str] = []
    for m in _HREF_RE.finditer(html or ""):
        u = m.group(1).strip()
        if link_ok(u):
            links.append(u)
    for m in _BARE_URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(").,;")
        if link_ok(u):
            links.append(u)
    # de-dupe preserve order
    seen = set()
    uniq = []
    for u in links:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return {"code": code, "links": uniq[:5]}


#: A freeze on a row in one of these can never use an emailed code.
_TERMINAL_FOR_OTP = frozenset({
    "submitted", "confirmed", "failed", "skipped", "skipped_duplicate",
    "cancelled", "withdrawn", "unsupported",
})
#: Sender-domain fragments too generic to identify an ATS.
_GENERIC_MAIL_BITS = frozenset({
    "mail", "email", "smtp", "send", "sendgrid", "amazonses", "mailgun",
    "notifications", "noreply", "no-reply", "reply", "info", "com", "net", "org",
})


def _active_freeze(user_email: str, from_addr: str = "",
                   subject: str = "") -> Optional[Dict[str, Any]]:
    """The waiting_verify freeze an emailed code/link can actually clear.

    A user routinely has SEVERAL live freezes at once (live on 2026-07-28:
    eight — five ``captcha``, three ``verify``). Binding the grant to whichever
    row was merely touched last used to hand a Workday email code to an Ashby
    CAPTCHA row; ``claim_for_verify`` then only serves that application_id, so
    the code was burned on a wall no email can ever clear and the row that
    asked for it never saw it.

    A mailed one-time code resolves a VERIFY/RESET wall by construction and
    never a CAPTCHA, so only a non-captcha freeze may claim it. When the user
    has nothing but CAPTCHA walls open we bind NOTHING and leave the grant
    unbound — an unbound grant stays claimable by whichever application asks
    next, which is strictly better than burning it on a wall no email can clear.
    """
    try:
        rows = list(_db().auto_apply_freezes
                    .find({"user_email": user_email, "status": "waiting_verify"})
                    .sort("updated_at", -1)
                    .limit(20))
    except Exception as e:
        logger.warning("application_otp: freeze lookup failed: %s", e)
        return None

    candidates = [r for r in rows
                  if str(r.get("kind") or "").strip().lower() != "captcha"]
    if not candidates:
        return None

    # A freeze on a row that already reached a terminal state can never use a
    # code, and while it lingers it STEALS codes meant for live applications.
    # (Live: a `submitted` Expedia row held a `waiting_verify` freeze and claimed
    # Greenhouse's code for Affirm.)
    live = []
    for row in candidates:
        try:
            from services import application_ledger_service as _ledger
            app = _ledger.get_application(user_email, row.get("application_id"))
        except Exception:
            app = None
        st = str((app or {}).get("status") or "").strip().lower()
        # Exclude only what we can POSITIVELY show is terminal. An unreadable or
        # missing row is not evidence of anything, and dropping it would silently
        # shrink the candidate set.
        if st in _TERMINAL_FOR_OTP:
            continue
        live.append((row, app or {}))
    if not live:
        return None

    # POSITIVE evidence only: the employer's name in the subject, or the sender's
    # ATS domain present in the apply_url. Without it we bind NOTHING — an unbound
    # grant stays claimable by whichever application asks next, which beats
    # burning it on a row that cannot use it.
    subj = (subject or "").lower()
    sender_host = (from_addr or "").split("@")[-1].lower()
    sender_bits = {p for p in re.split(r"[.\-]", sender_host)
                   if len(p) > 3 and p not in _GENERIC_MAIL_BITS}
    matched = []
    for row, app in live:
        company = str(app.get("company") or "").strip().lower()
        url = str(app.get("apply_url") or "").lower()
        if company and subj and company in subj:
            matched.append((row, "company in subject"))
            continue
        if sender_bits and any(b in url for b in sender_bits):
            matched.append((row, "sender ATS domain in apply_url"))
    if len(matched) == 1:
        logger.info("application_otp: bound to %s (%s)",
                    matched[0][0].get("application_id"), matched[0][1])
        return matched[0][0]
    if matched:
        logger.info("application_otp: %d candidates matched the sender — "
                    "ambiguous, leaving the grant UNBOUND", len(matched))
        return None

    # Nothing matched. If every candidate was READABLE and none is the employer
    # this mail names, that is CONTRARY evidence — refuse, even with a single
    # candidate. Binding anyway is exactly how a Greenhouse code for Cohere Health
    # landed on a Workday MUFG row (2026-08-24): the right freeze did not exist
    # yet (written 92s after the mail arrived) and the only live wall belonged to
    # a different employer on a different ATS.
    #
    # Absence of information is NOT contradiction: a candidate with no readable
    # ledger row contradicts nothing, so the single-candidate fallback still
    # applies to those. An unbound grant is safe — `claim_for_verify` serves
    # `application_id: None` to whichever row asks next.
    readable = [(r, a) for r, a in live if a]
    unreadable = [(r, a) for r, a in live if not a]
    if readable and not unreadable:
        logger.info("application_otp: %d live verify freeze(s), none matching the "
                    "sender/subject — leaving the grant UNBOUND", len(live))
        return None
    if len(live) == 1:
        return live[0][0]
    return None


def capture_from_noise_message(
    *,
    user_email: str,
    message_id: str,
    subject: str,
    text: str,
    html: str = "",
    from_addr: str = "",
    application_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Store an OTP/link grant when system_noise mail arrives for an active freeze.

    Safe to call always — no-ops when nothing extractable / no freeze.
    """
    user_email = (user_email or "").strip().lower()
    if not user_email:
        return {"captured": False, "reason": "no_user"}

    payload = extract_otp_payload(subject, text, html, from_addr=from_addr)
    if not payload.get("code") and not payload.get("links"):
        return {"captured": False, "reason": "nothing_extracted"}

    freeze = _active_freeze(user_email, from_addr=from_addr, subject=subject)
    app_id = application_id or (freeze or {}).get("application_id")
    if not app_id:
        # Keep the grant unbound briefly — verify_state can still claim by user.
        app_id = None

    now = _now()
    doc = {
        "user_email": user_email,
        "application_id": app_id,
        "message_id": message_id,
        "from_addr": (from_addr or "")[:200],
        "subject": (subject or "")[:300],
        "code": payload.get("code"),
        "links": payload.get("links") or [],
        "status": "available",
        "created_at": now,
        "expires_at": now + timedelta(minutes=_TTL_MINUTES),
        "kind": (freeze or {}).get("kind") or "verify",
    }
    try:
        _coll().update_one(
            {"user_email": user_email, "message_id": message_id},
            {"$set": doc},
            upsert=True,
        )
        logger.info(
            "application_otp: captured grant user=%s app=%s code=%s links=%d",
            user_email, app_id, bool(payload.get("code")), len(payload.get("links") or []),
        )
        # Nudge any waiting_verify freeze so the thaw sweep retries immediately.
        if freeze and freeze.get("application_id"):
            try:
                _db().auto_apply_freezes.update_one(
                    {
                        "user_email": user_email,
                        "application_id": freeze.get("application_id"),
                        "status": "waiting_verify",
                    },
                    {"$set": {"next_check_at": now, "updated_at": now}},
                )
            except Exception as e:
                logger.warning("application_otp: freeze nudge failed: %s", e)
        return {"captured": True, "application_id": app_id,
                "has_code": bool(payload.get("code")),
                "link_count": len(payload.get("links") or [])}
    except Exception as e:
        logger.warning("application_otp: store failed: %s", e)
        return {"captured": False, "reason": "store_error"}


def wait_for_grant(
    user_email: str,
    application_id: str,
    *,
    timeout_sec: int = 90,
    poll_sec: float = 3.0,
) -> Optional[Dict[str, Any]]:
    """Poll Mongo until an OTP grant is available, then claim it.

    Used by the browser executor while a verify wall is on screen — the ATS
    email often lands a few seconds after the form asks for a code.
    """
    import time

    user_email = (user_email or "").strip().lower()
    application_id = (application_id or "").strip()
    if not user_email:
        return None
    deadline = time.time() + max(5, int(timeout_sec or 90))
    poll_sec = max(1.0, float(poll_sec or 3.0))
    while time.time() < deadline:
        grant = claim_for_verify(user_email, application_id)
        if grant:
            return grant
        time.sleep(poll_sec)
    return None


def ensure_indexes() -> None:
    """Idempotent indexes — run from scripts/migrate_indexes.py only."""
    col = _coll()
    col.create_index(
        [("user_email", 1), ("message_id", 1)],
        unique=True,
        name="user_message_unique",
    )
    col.create_index(
        [("user_email", 1), ("application_id", 1), ("status", 1)],
        name="user_app_status",
    )
    col.create_index(
        [("expires_at", 1)],
        expireAfterSeconds=0,
        name="ttl_expires_at",
    )


def claim_for_verify(user_email: str, application_id: str) -> Optional[Dict[str, Any]]:
    """Atomically claim one available grant for this user/app (or user-unbound)."""
    user_email = (user_email or "").strip().lower()
    application_id = (application_id or "").strip()
    now = _now()
    q: Dict[str, Any] = {
        "user_email": user_email,
        "status": "available",
        "expires_at": {"$gt": now},
        "$or": [
            {"application_id": application_id},
            {"application_id": None},
            {"application_id": {"$exists": False}},
        ],
    }
    try:
        doc = _coll().find_one_and_update(
            q,
            {"$set": {"status": "claimed", "claimed_at": now,
                      "application_id": application_id}},
            sort=[("created_at", -1)],
        )
        if not doc:
            return None
        return {
            "code": doc.get("code"),
            "links": list(doc.get("links") or []),
            "message_id": doc.get("message_id"),
            "from_addr": doc.get("from_addr"),
        }
    except Exception as e:
        logger.warning("application_otp: claim failed: %s", e)
        return None
