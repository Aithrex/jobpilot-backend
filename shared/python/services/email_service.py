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


def _fallback_provider() -> str:
    """The transport to retry on when the primary one fails.

    A single-provider mailer turns any provider-side problem — a revoked API
    token, an expired credential, a regional outage — into total, SILENT email
    loss, because `send_email` swallows failures so mail can never break signup
    or password reset. That combination is what let a dead ZeptoMail token stop
    every password-reset email without anything visibly breaking. The two
    transports have independent credentials, so the other one is very unlikely
    to be down at the same moment.

    Defaults to "the one we aren't using"; set EMAIL_FALLBACK_PROVIDER to
    `none` to disable, or name a provider explicitly.
    """
    configured = (_get_config_value("EMAIL_FALLBACK_PROVIDER", "") or "").strip().lower()
    if configured:
        return "" if configured in ("none", "off", "disabled") else configured
    return "ses" if _provider() == "zeptomail" else "zeptomail"


def _can_use(provider: str) -> bool:
    """True if `provider` has the credentials it needs. Used to skip a fallback
    that would only fail again."""
    if not _from_address():
        return False
    if provider == "zeptomail":
        return bool(_zepto_token())
    return provider == "ses"


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


def provider_name() -> str:
    """The transport that would be used for the next send. Exposed so callers
    can log/report which provider a failed send belonged to."""
    return _provider()


def is_suppressed(email: str) -> bool:
    """True if `email` is on the bounce/complaint suppression list. Never raises
    — a suppression-store outage reports False (not suppressed)."""
    try:
        from services import email_suppression
        return email_suppression.is_suppressed(email)
    except Exception:
        return False


def diagnose(to_email: str = "") -> dict:
    """Why would (or wouldn't) a send to `to_email` go out?

    Returns a secret-free snapshot for the admin ops panel: no token value, only
    whether one is present. `blocked_by` is the first reason a send would be a
    no-op, or None when the send should reach the provider.
    """
    from_addr = _from_address()
    provider = _provider()
    fallback = _fallback_provider()
    has_token = bool(_zepto_token())
    suppressed = is_suppressed(to_email) if to_email else False
    fallback_usable = bool(fallback and fallback != provider and _can_use(fallback))

    blocked_by = None
    if not from_addr:
        blocked_by = "no_from_address"
    elif suppressed:
        blocked_by = "recipient_suppressed"
    elif provider == "zeptomail" and not has_token and not fallback_usable:
        blocked_by = "no_zeptomail_token"

    return {
        "provider": provider,
        "fallback_provider": fallback or None,
        "fallback_usable": fallback_usable,
        "from_address": from_addr,
        "reply_to": _reply_to(),
        "api_url": _zepto_url() if provider == "zeptomail" else None,
        "region": _ses_region() if provider == "ses" else None,
        "token_present": has_token,
        "configured": email_configured(),
        "recipient": to_email or None,
        "recipient_suppressed": suppressed,
        "blocked_by": blocked_by,
    }


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
    # An unconfigured PRIMARY is only a dead end when the fallback can't cover
    # for it — otherwise let the send proceed and fail over below.
    if not from_addr or (
        _provider() == "zeptomail" and not _zepto_token() and not _can_use(_fallback_provider())
    ):
        logger.info(
            "[email:dev-fallback] (provider=%s not configured) To=%s | Subject=%s\n%s",
            _provider(), to_email, subject, text_body,
        )
        return False

    def _dispatch(provider: str) -> bool:
        if provider == "zeptomail":
            return _send_via_zeptomail(
                to_email, subject, html_body, text_body, from_addr,
                reply_to=reply_to, headers=headers,
            )
        return _send_via_ses(
            to_email, subject, html_body, text_body, from_addr,
            reply_to=reply_to, headers=headers,
        )

    primary = _provider()
    try:
        return _dispatch(primary)
    except Exception as e:
        primary_error = e

    # Primary failed. Try the other transport before giving up — see
    # _fallback_provider() for why a single-provider mailer is a liability here.
    fallback = _fallback_provider()
    if fallback and fallback != primary and _can_use(fallback):
        try:
            sent = _dispatch(fallback)
            logger.warning(
                "[email:failover] primary=%s failed (%s); delivered via %s. To=%s | Subject=%s",
                primary, primary_error, fallback, to_email, subject,
            )
            return sent
        except Exception as e2:
            logger.error(
                "[email:all-providers-failed] primary=%s (%s) fallback=%s (%s). To=%s | Subject=%s\n%s",
                primary, primary_error, fallback, e2, to_email, subject, text_body,
            )
            return False

    # Log the content so a dev/ops can still recover the link if the
    # provider is down.
    logger.warning(
        "[email:send-failed] provider=%s %s — falling back to log. To=%s | Subject=%s\n%s",
        primary, primary_error, to_email, subject, text_body,
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
        # ZeptoMail's custom-header field is `mime_headers` (NOT `headers` — a top-
        # level `headers` key returns TM_3301 "extra key found ... target:[headers]"
        # and drops the send). Used for the X-JP-Forwarded outbound loop marker.
        # Best-effort: only string values.
        payload["mime_headers"] = {str(k): str(v) for k, v in headers.items()}

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


def send_test(to_email: str, *, ignore_suppression: bool = False) -> dict:
    """Admin diagnostic: attempt a real send and REPORT the provider's error.

    `send_email` deliberately swallows failures so a dead mail provider can never
    break signup or password reset. That makes "no email arrived" undebuggable
    from the outside, so this sibling keeps the provider's actual response
    (e.g. ZeptoMail's "sender domain not verified") instead of logging it away.
    Admin-gated — the error text can name internal config.
    """
    result = diagnose(to_email)
    if result["blocked_by"] and not (ignore_suppression and result["blocked_by"] == "recipient_suppressed"):
        result.update(sent=False, error=f"blocked before dispatch: {result['blocked_by']}")
        return result

    subject = f"{APP_NAME} email delivery test"
    html_body = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">'
        f"<p>This is a delivery test from {APP_NAME}. If you can read it, transactional "
        "email (password resets included) is reaching this address.</p></div>"
    )
    try:
        from_addr = _from_address()
        text_body = _strip_html(html_body)
        if _provider() == "zeptomail":
            _send_via_zeptomail(to_email, subject, html_body, text_body, from_addr)
        else:
            _send_via_ses(to_email, subject, html_body, text_body, from_addr)
        result.update(sent=True, error=None)
    except Exception as e:
        result.update(sent=False, error=f"{type(e).__name__}: {e}")
    return result


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
