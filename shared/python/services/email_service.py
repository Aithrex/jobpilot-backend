"""Transactional email with a pluggable provider, plus a dev console fallback.

Used for password-reset links today; structured so notifications (interview
reminders, event nudges, weekly digests) can reuse `send_email` later.

Provider is chosen by `EMAIL_PROVIDER` (default `ses`):
  - `zeptomail` — Zoho's transactional API (host in a Zoho org; sidesteps the
    SES sandbox). Needs `ZEPTOMAIL_TOKEN` + a verified sender domain.
  - `ses`       — AWS SES (legacy default).

If the selected provider isn't configured (missing token/from), or the send
errors, we DON'T fail the caller — we log the full message (including any reset
link) to the console so local development works without an email provider.
Callers must treat send as best-effort.
"""
from __future__ import annotations

import logging
from email.utils import parseaddr
from typing import Optional

from utils.config import _get_config_value

logger = logging.getLogger(__name__)

APP_NAME = "Aspirely"

# ZeptoMail's API host is region-specific (.com default, .eu, .in, .com.au, .jp).
# Override via ZEPTOMAIL_API_URL if your Mail Agent lives in a non-US DC.
_DEFAULT_ZEPTO_URL = "https://api.zeptomail.com/v1.1/email"


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _provider() -> str:
    """Selected transport. Default `ses` keeps prod byte-identical until the
    env var is flipped to `zeptomail`."""
    return (_get_config_value("EMAIL_PROVIDER", "ses") or "ses").strip().lower()


def _from_address() -> str:
    """The From header, e.g. `Aspirely <no-reply@aspirely.me>`. Shared across
    providers — reads EMAIL_FROM_ADDRESS first, falling back to the legacy
    SES_FROM_ADDRESS so nothing has to be re-seeded on the switch."""
    return _get_config_value("EMAIL_FROM_ADDRESS", "") or _get_config_value("SES_FROM_ADDRESS", "")


def _reply_to() -> str:
    """Optional Reply-To (e.g. a monitored support alias). The From address is
    no-reply@, so replies would otherwise bounce; this routes them somewhere real."""
    return _get_config_value("EMAIL_REPLY_TO", "") or _get_config_value("SES_REPLY_TO", "")


def _ses_region() -> str:
    return _get_config_value("SES_REGION", _get_config_value("AWS_REGION", "us-east-1"))


def _zepto_token() -> str:
    return _get_config_value("ZEPTOMAIL_TOKEN", "")


def _zepto_url() -> str:
    return _get_config_value("ZEPTOMAIL_API_URL", "") or _DEFAULT_ZEPTO_URL


def email_configured() -> bool:
    """True if the selected provider has what it needs to send."""
    if not _from_address():
        return False
    if _provider() == "zeptomail":
        return bool(_zepto_token())
    return True  # ses only needs a From address


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: Optional[str] = None,
    *,
    from_addr: Optional[str] = None,
    reply_to: Optional[str] = None,
    headers: Optional[dict] = None,
) -> bool:
    """Send an email. Returns True if dispatched via the configured provider,
    False if it fell back to console logging (dev) or failed. Never raises.

    ``from_addr`` overrides the default EMAIL_FROM_ADDRESS sender — the managed
    mailbox forwarder passes MAIL_FORWARD_FROM here so a forwarded notification
    is sent from a JobPilot-owned verified sender, NEVER spoofing the recruiter.
    ``reply_to`` overrides the default Reply-To (pass an empty string to suppress
    it entirely). ``headers`` is a best-effort extra-header map (e.g.
    ``{"X-JP-Forwarded": "1"}`` for the outbound loop marker); the transactional
    providers below apply what they support."""
    from_addr = from_addr or _from_address()
    text_body = text_body or _strip_html(html_body)

    # Skip addresses that have hard-bounced or complained — mailing them again
    # hurts our sending reputation. Best-effort: a suppression-store error must
    # never block a legitimate send, so we swallow and proceed.
    try:
        from services import email_suppression
        if email_suppression.is_suppressed(to_email):
            logger.info("[email:suppressed] skipping To=%s", to_email)
            return False
    except Exception:
        pass

    # Gate on the EFFECTIVE sender (respecting a from_addr override), not just the
    # default EMAIL_FROM_ADDRESS — otherwise a forward from MAIL_FORWARD_FROM would
    # fall to the dev console whenever the default sender is unset.
    if not from_addr or (_provider() == "zeptomail" and not _zepto_token()):
        logger.info(
            "[email:dev-fallback] (provider=%s not configured) To=%s | Subject=%s\n%s",
            _provider(), to_email, subject, text_body,
        )
        return False

    provider = _provider()
    try:
        if provider == "zeptomail":
            return _send_via_zeptomail(
                to_email, subject, html_body, text_body, from_addr,
                reply_to=reply_to, headers=headers,
            )
        return _send_via_ses(
            to_email, subject, html_body, text_body, from_addr,
            reply_to=reply_to, headers=headers,
        )
    except Exception as e:
        # Log the content so a dev/ops can still recover the link if the
        # provider is down.
        logger.warning(
            "[email:send-failed] provider=%s %s — falling back to log. To=%s | Subject=%s\n%s",
            provider, e, to_email, subject, text_body,
        )
        return False


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

def _send_via_zeptomail(
    to_email: str, subject: str, html_body: str, text_body: str, from_addr: str,
    *, reply_to: Optional[str] = None, headers: Optional[dict] = None,
) -> bool:
    """Send via the ZeptoMail transactional API (HTTPS, token auth). Raises on
    any HTTP/transport error so the caller's fallback logs the content."""
    import requests

    from_name, from_email = parseaddr(from_addr)
    token = _zepto_token()
    # The header value is `Zoho-enczapikey <token>`. Accept either a bare token
    # or a value the user already prefixed, so paste-in mistakes still work.
    auth = token if token.lower().startswith("zoho-enczapikey") else f"Zoho-enczapikey {token}"

    payload = {
        "from": {"address": from_email or from_addr, "name": from_name or APP_NAME},
        "to": [{"email_address": {"address": to_email}}],
        "subject": subject,
        "htmlbody": html_body,
        "textbody": text_body,
    }
    # An explicit reply_to override wins (empty string suppresses Reply-To
    # entirely); otherwise fall back to the configured default.
    rt = reply_to if reply_to is not None else _reply_to()
    if rt:
        rt_name, rt_email = parseaddr(rt)
        payload["reply_to"] = [{"address": rt_email or rt, "name": rt_name or APP_NAME}]
    if headers:
        # ZeptoMail forwards custom headers verbatim (used for the X-JP-Forwarded
        # outbound loop marker). Best-effort: only string values.
        payload["headers"] = {str(k): str(v) for k, v in headers.items()}

    resp = requests.post(
        _zepto_url(),
        json=payload,
        headers={
            "Authorization": auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=15,
    )
    # ZeptoMail returns 201 on accept; anything else is a failure worth logging
    # (bad token, unverified sender, suppressed recipient, etc.).
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"ZeptoMail HTTP {resp.status_code}: {resp.text[:300]}")
    return True


def _send_via_ses(
    to_email: str, subject: str, html_body: str, text_body: str, from_addr: str,
    *, reply_to: Optional[str] = None, headers: Optional[dict] = None,
) -> bool:
    """Send via AWS SES (legacy path). Raises on error for the caller's fallback.

    Custom ``headers`` are not supported by the SES ``send_email`` (non-raw) API
    and are ignored here — the managed-mailbox loop guard enforces on INBOUND
    (``X-JP-Forwarded`` is read on the way in), so the outbound marker is a
    best-effort belt-and-suspenders only."""
    import boto3  # bundled in the Lambda runtime

    client = boto3.client("ses", region_name=_ses_region())
    params = {
        "Source": from_addr,
        "Destination": {"ToAddresses": [to_email]},
        "Message": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Html": {"Data": html_body, "Charset": "UTF-8"},
                "Text": {"Data": text_body, "Charset": "UTF-8"},
            },
        },
    }
    rt = reply_to if reply_to is not None else _reply_to()
    if rt:
        params["ReplyToAddresses"] = [rt]
    client.send_email(**params)
    return True


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def send_password_reset(to_email: str, reset_link: str) -> bool:
    subject = f"Reset your {APP_NAME} password"
    html_body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:480px;margin:auto">
  <h2 style="margin:0 0 12px">Reset your password</h2>
  <p style="color:#444;line-height:1.5">We received a request to reset your {APP_NAME} password.
  This link expires in 30 minutes. If you didn't request it, you can safely ignore this email.</p>
  <p style="margin:24px 0">
    <a href="{reset_link}" style="background:#4f46e5;color:#fff;padding:12px 20px;border-radius:8px;
       text-decoration:none;display:inline-block;font-weight:600">Reset password</a>
  </p>
  <p style="color:#888;font-size:13px;word-break:break-all">Or paste this link into your browser:<br>{reset_link}</p>
</div>"""
    text_body = (
        f"Reset your {APP_NAME} password\n\n"
        f"Open this link to choose a new password (expires in 30 minutes):\n{reset_link}\n\n"
        f"If you didn't request this, ignore this email."
    )
    return send_email(to_email, subject, html_body, text_body)


def send_welcome(to_email: str, app_url: str = "") -> bool:
    """Welcome a newly-registered user. Best-effort — the caller must not block
    signup on the result."""
    app_url = (app_url or "https://aspirely.me").rstrip("/")
    subject = f"Welcome to {APP_NAME}"
    html_body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:480px;margin:auto;color:#0f172a">
  <h2 style="margin:0 0 12px">Welcome to {APP_NAME} 👋</h2>
  <p style="color:#444;line-height:1.6">Your account is ready. {APP_NAME} helps you tailor your
  résumé to each job, track applications, and prep for interviews — built to be honest about your
  real experience, never to fabricate it.</p>
  <p style="color:#444;line-height:1.6;margin:14px 0 0">To get started:</p>
  <ol style="color:#444;line-height:1.7;padding-left:20px;margin:6px 0 0">
    <li>Upload your résumé</li>
    <li>See your best-matched jobs</li>
    <li>Tailor and apply</li>
  </ol>
  <p style="margin:24px 0">
    <a href="{app_url}" style="background:#4f46e5;color:#fff;padding:12px 20px;border-radius:8px;
       text-decoration:none;display:inline-block;font-weight:600">Open {APP_NAME}</a>
  </p>
  <p style="color:#888;font-size:13px;line-height:1.6">Questions? Just reply to this email — it reaches a real person.</p>
</div>"""
    text_body = (
        f"Welcome to {APP_NAME}\n\n"
        f"Your account is ready. {APP_NAME} helps you tailor your resume to each job, track "
        f"applications, and prep for interviews.\n\n"
        f"Get started:\n  1. Upload your resume\n  2. See your best-matched jobs\n  3. Tailor and apply\n\n"
        f"Open {APP_NAME}: {app_url}\n\n"
        f"Questions? Just reply to this email."
    )
    return send_email(to_email, subject, html_body, text_body)


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()
