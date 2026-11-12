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

Transactional vs list mail (CAN-SPAM, RFC 8058), 2026-11-11
------------------------------------------------------------
A send WITHOUT ``list_id`` is transactional: password reset, verification
codes, security notices, account notices, forwarded recruiter mail. It gets no
unsubscribe header and no marketing footer.

A send WITH ``list_id`` (``job_alerts`` / ``digests`` / ``product_updates`` /
``marketing``) is list mail. It is refused when the recipient has left that
list, and otherwise carries:
  * ``List-Unsubscribe: <https://.../api/auth/email/unsubscribe?t=TOKEN>``
    (plus ``<mailto:...>`` when EMAIL_UNSUBSCRIBE_MAILTO is set) and
    ``List-Unsubscribe-Post: List-Unsubscribe=One-Click`` (RFC 8058 section 3.1);
  * a footer with a tokenized link to the ``/unsubscribe`` page (no sign-in
    needed, as CAN-SPAM 15 U.S.C. 7704(a)(3) requires), the legal name and the
    postal address (7704(a)(5)(A)(iii)).
If no token can be minted (no such account), list mail is NOT sent: commercial
mail without a working opt-out is the one thing this layer must never emit.
"""
from __future__ import annotations

import logging
import sys
from email.utils import parseaddr
from typing import Optional, Tuple

from utils.config import _get_config_value

logger = logging.getLogger(__name__)

APP_NAME = "Aspirely"

# Set once the missing-postal-address warning has been logged in this process.
_postal_warning_logged = False

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
    list_id: Optional[str] = None,
) -> bool:
    """Send an email. Returns True if dispatched via the configured provider,
    False if it fell back to console logging (dev) or failed. Never raises.

    ``from_addr`` overrides the default EMAIL_FROM_ADDRESS sender — the managed
    mailbox forwarder passes MAIL_FORWARD_FROM here so a forwarded notification
    is sent from a JobPilot-owned verified sender, NEVER spoofing the recruiter.
    ``reply_to`` overrides the default Reply-To (pass an empty string to suppress
    it entirely). ``headers`` is a best-effort extra-header map (e.g.
    ``{"X-JP-Forwarded": "1"}`` for the outbound loop marker); the transactional
    providers below apply what they support.

    ``list_id`` marks the message as list (non-transactional) mail; see the
    module docstring. Leave it unset for transactional mail."""
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

    # The hashed list outlives account erasure (bounces, complaints and the
    # "account deleted" marker are kept as a keyed hash, never the address).
    try:
        from services import privacy_service
        if privacy_service.is_hash_suppressed(to_email):
            logger.info("[email:suppressed-hash] skipping a suppressed recipient")
            return False
    except Exception:
        pass

    if list_id:
        prepared = _prepare_list_mail(to_email, list_id, html_body, text_body, headers)
        if prepared is None:
            return False
        html_body, text_body, headers = prepared

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
# List mail: opt-out gate, RFC 8058 headers, CAN-SPAM footer
# ---------------------------------------------------------------------------

def _company_identity() -> Tuple[str, str]:
    """(legal name, postal address). An empty address is a launch HUMAN GATE:
    mail still goes out and a warning is logged once per process."""
    global _postal_warning_logged
    try:
        from utils.config import ComplianceConfig
        name = ComplianceConfig.COMPANY_LEGAL_NAME or APP_NAME
        address = ComplianceConfig.COMPANY_POSTAL_ADDRESS
    except Exception:  # pragma: no cover - defensive
        name, address = APP_NAME, ""
    if not address and not _postal_warning_logged:
        _postal_warning_logged = True
        logger.warning(
            "[email:compliance] COMPANY_POSTAL_ADDRESS is not set. CAN-SPAM requires a "
            "valid postal address in every commercial email; set it before launch.")
    return name, address


def _unsubscribe_mailto(list_id: str, token: str) -> str:
    try:
        from utils.config import ComplianceConfig
        box = ComplianceConfig.EMAIL_UNSUBSCRIBE_MAILTO
    except Exception:  # pragma: no cover - defensive
        box = ""
    if not box or "@" not in box:
        return ""
    return f"mailto:{box}?subject=unsubscribe%20{list_id}%20{token}"


def list_unsubscribe_headers(list_id: str, token: str) -> dict:
    """The two RFC 8058 headers. The HTTPS URI comes first because RFC 8058
    section 3.1 requires one; the mailto is an optional fallback for clients
    that only speak RFC 2369."""
    from services import privacy_service
    parts = [f"<{privacy_service.one_click_url(token)}>"]
    mailto = _unsubscribe_mailto(list_id, token)
    if mailto:
        parts.append(f"<{mailto}>")
    return {
        "List-Unsubscribe": ", ".join(parts),
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def compliance_footer(list_id: str, token: str) -> Tuple[str, str]:
    """(html, text) footer for list mail: why you got it, a one-step
    unsubscribe that needs no sign-in, and the sender's legal name and address."""
    import html as _html
    from services import privacy_service
    label = privacy_service.LIST_LABELS.get(list_id, "these emails")
    page = privacy_service.unsubscribe_page_url(token)
    name, address = _company_identity()
    identity = " · ".join(p for p in (name, address) if p)
    html_footer = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'max-width:560px;margin:24px auto 0;color:#888;font-size:12px;line-height:1.6">'
        f'<p style="margin:0 0 6px">You are receiving this email because you are subscribed '
        f'to {_html.escape(label)} from {APP_NAME}. '
        f'<a href="{_html.escape(page, quote=True)}" style="color:#4f46e5">Unsubscribe from '
        f'{_html.escape(label)}</a>. No sign-in is needed.</p>'
        f'<p style="margin:0">{_html.escape(identity)}</p>'
        '</div>'
    )
    text_footer = (
        f"You are receiving this email because you are subscribed to {label} from {APP_NAME}.\n"
        f"Unsubscribe from {label} (no sign-in needed): {page}\n"
        f"{identity}"
    )
    return html_footer, text_footer


def _prepare_list_mail(to_email: str, list_id: str, html_body: str, text_body: str,
                       headers: Optional[dict]):
    """Gate + decorate list mail. Returns (html, text, headers), or None when
    the message must not be sent (left the list, or no opt-out link possible)."""
    try:
        from services import privacy_service
        from services.notifications import service as notify
        if list_id not in notify.EMAIL_LISTS:
            logger.error("[email:list] unknown list_id=%s; not sending", list_id)
            return None
        if not notify.list_allows(to_email, list_id):
            logger.info("[email:list-optout] recipient left list=%s; not sending", list_id)
            return None
        token = privacy_service.make_unsubscribe_token(to_email, list_id)
    except Exception as e:
        logger.warning("[email:list] gate failed for list=%s (%s); not sending",
                       list_id, type(e).__name__)
        return None
    if not token:
        logger.warning("[email:list] no unsubscribe token for list=%s (no account); not sending",
                       list_id)
        return None
    footer_html, footer_text = compliance_footer(list_id, token)
    merged = dict(headers or {})
    merged.update(list_unsubscribe_headers(list_id, token))
    return f"{html_body}{footer_html}", f"{text_body}\n\n{footer_text}", merged


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

def _refuse_real_send_under_test() -> None:
    """Under pytest, a real transport must never run. `utils.config` loads the
    repo-local `.env`, which carries the production ZeptoMail token, so a test
    that reached here would email a real inbox. Tests that observe sends patch
    `_send_via_zeptomail` / `_send_via_ses` (or `send_email`) instead."""
    if "pytest" in sys.modules:
        raise RuntimeError("real email transport refused while pytest is loaded")


def _zepto_payload(
    to_email: str, subject: str, html_body: str, text_body: str, from_addr: str,
    *, reply_to: Optional[str] = None, headers: Optional[dict] = None,
) -> dict:
    """The ZeptoMail `POST /v1.1/email` body. Pure, so tests can check it."""
    from_name, from_email = parseaddr(from_addr)
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
        # and drops the send). Used for the X-JP-Forwarded outbound loop marker
        # and the RFC 8058 List-Unsubscribe / List-Unsubscribe-Post pair.
        # Docs (https://www.zoho.com/zeptomail/help/api/email-sending.html):
        # "mime_headers: Custom MIME headers as a flat JSON object of header name
        # to string value"; reserved headers (Content-Type, From, To, Subject,
        # Date) cannot be overridden. The page does not list List-Unsubscribe
        # either way, so a real send must be checked ("Show original") before
        # launch. Best-effort: only string values.
        payload["mime_headers"] = {str(k): str(v) for k, v in headers.items()}
    return payload


def _send_via_zeptomail(
    to_email: str, subject: str, html_body: str, text_body: str, from_addr: str,
    *, reply_to: Optional[str] = None, headers: Optional[dict] = None,
) -> bool:
    """Send via the ZeptoMail transactional API (HTTPS, token auth). Raises on
    any HTTP/transport error so the caller's fallback logs the content."""
    _refuse_real_send_under_test()
    import requests

    token = _zepto_token()
    # The header value is `Zoho-enczapikey <token>`. Accept either a bare token
    # or a value the user already prefixed, so paste-in mistakes still work.
    auth = token if token.lower().startswith("zoho-enczapikey") else f"Zoho-enczapikey {token}"
    payload = _zepto_payload(to_email, subject, html_body, text_body, from_addr,
                             reply_to=reply_to, headers=headers)

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
    best-effort belt-and-suspenders only. The same applies to List-Unsubscribe:
    list mail that fails over to SES keeps its footer unsubscribe link (the
    CAN-SPAM opt-out) but loses the RFC 8058 one-click headers."""
    _refuse_real_send_under_test()
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
    signup on the result.

    List mail (``product_updates``): it introduces the product rather than
    completing a transaction, so it carries the unsubscribe footer and headers."""
    app_url = (app_url or "https://aspirely.me").rstrip("/")
    subject = f"Welcome to {APP_NAME}"
    html_body = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:480px;margin:auto;color:#0f172a">
  <h2 style="margin:0 0 12px">Welcome to {APP_NAME}</h2>
  <p style="color:#444;line-height:1.6">Your account is ready. {APP_NAME} helps you tailor your
  resume to each job, track applications, and prepare for interviews. It is built to be honest
  about your real experience and never to fabricate it.</p>
  <p style="color:#444;line-height:1.6;margin:14px 0 0">To get started:</p>
  <ol style="color:#444;line-height:1.7;padding-left:20px;margin:6px 0 0">
    <li>Upload your resume</li>
    <li>See your best-matched jobs</li>
    <li>Tailor and apply</li>
  </ol>
  <p style="margin:24px 0">
    <a href="{app_url}" style="background:#4f46e5;color:#fff;padding:12px 20px;border-radius:8px;
       text-decoration:none;display:inline-block;font-weight:600">Open {APP_NAME}</a>
  </p>
  <p style="color:#888;font-size:13px;line-height:1.6">Questions? Reply to this email and a real person will answer.</p>
</div>"""
    text_body = (
        f"Welcome to {APP_NAME}\n\n"
        f"Your account is ready. {APP_NAME} helps you tailor your resume to each job, track "
        f"applications, and prepare for interviews.\n\n"
        f"Get started:\n  1. Upload your resume\n  2. See your best-matched jobs\n  3. Tailor and apply\n\n"
        f"Open {APP_NAME}: {app_url}\n\n"
        f"Questions? Reply to this email."
    )
    return send_email(to_email, subject, html_body, text_body, list_id="product_updates")


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()
