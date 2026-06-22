"""Transactional email via AWS SES, with a dev console fallback.

Used for password-reset links today; structured so notifications (interview
reminders, event nudges, weekly digests) can reuse `send_email` later.

If SES isn't configured (no SES_FROM_ADDRESS, or boto3/SES errors), we DON'T
fail the caller — we log the full message (including any reset link) to the
console so local development works without an email provider. Callers should
treat send as best-effort.
"""
from __future__ import annotations

import logging
from typing import Optional

from utils.config import _get_config_value

logger = logging.getLogger(__name__)

APP_NAME = "JobPilot"


def _ses_region() -> str:
    return _get_config_value("SES_REGION", _get_config_value("AWS_REGION", "us-east-1"))


def _from_address() -> str:
    return _get_config_value("SES_FROM_ADDRESS", "")


def email_configured() -> bool:
    return bool(_from_address())


def send_email(to_email: str, subject: str, html_body: str, text_body: Optional[str] = None) -> bool:
    """Send an email. Returns True if dispatched via SES, False if it fell back
    to console logging (dev) or failed. Never raises."""
    from_addr = _from_address()
    text_body = text_body or _strip_html(html_body)

    if not from_addr:
        logger.info(
            "[email:dev-fallback] (SES not configured) To=%s | Subject=%s\n%s",
            to_email, subject, text_body,
        )
        return False

    try:
        import boto3  # bundled in the Lambda runtime
        client = boto3.client("ses", region_name=_ses_region())
        client.send_email(
            Source=from_addr,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                },
            },
        )
        return True
    except Exception as e:
        # Log the content so a dev/ops can still recover the link if SES is down.
        logger.warning(
            "[email:send-failed] %s — falling back to log. To=%s | Subject=%s\n%s",
            e, to_email, subject, text_body,
        )
        return False


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


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()
