"""Notification channels — one small class per delivery medium (Compass §2.5).

Each channel implements the same two-method contract:

    available() -> bool          # is this channel usable right now?
    send(to, event, payload)     # deliver; returns True on dispatch, never raises

The router in ``service.py`` walks the user's preferred channels in order and
uses the first ``available()`` one, always falling back to :class:`EmailChannel`
(which is ALWAYS available — it reuses the existing SES path and degrades to a
console log in dev, so a digest can never be silently un-deliverable).

External-account channels are gated behind feature flags and, until the human
gate clears (see MANUAL_STEPS), their ``send`` LOGS the exact upstream request
it *would* have made and returns False — the real request shape is present in
code so flipping the flag is the only remaining step:

    * WhatsAppChannel  -> Meta WhatsApp Cloud API   (WHATSAPP_ENABLED)
    * SmsChannel       -> Twilio Messages API        (TWILIO_ENABLED)
    * PushChannel      -> Web Push / VAPID            (WEBPUSH_ENABLED)

Import-safe: ``requests`` / ``boto3`` / the SES ``email_service`` are imported
lazily inside ``send`` so importing this module never touches the network.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from utils.config import _get_config_value

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def _flag_enabled(name: str) -> bool:
    """Feature flag, default OFF (external channels need an account/approval)."""
    return (_get_config_value(name, "") or "").strip().lower() in _TRUE


def _cfg(name: str, default: str = "") -> str:
    return (_get_config_value(name, default) or "").strip()


# ---------------------------------------------------------------------------
# Channel interface
# ---------------------------------------------------------------------------
class Channel:
    """Delivery-medium interface. Concrete channels are cheap to construct
    (no I/O in ``__init__``); all work happens in ``available``/``send``."""

    key: str = "channel"

    def available(self) -> bool:
        raise NotImplementedError

    def send(self, to: Any, event: str, payload: Dict[str, Any]) -> bool:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Email — the always-available fallback (reuses the existing SES path)
# ---------------------------------------------------------------------------
class EmailChannel(Channel):
    """Reuses ``services.email_service.send_email`` (AWS SES) — NOT reimplemented.

    ``email_service`` is the auth service's local module (``services/auth/
    services/email_service.py``) merged into the ``services`` PEP-420 namespace.
    It is imported lazily so this file stays import-safe in Lambdas that don't
    carry it; if it can't be imported we log and report failure rather than
    raising (the router treats a False as "try the next channel").

    ``available()`` is always True: even with SES unconfigured, ``send_email``
    logs the message to the console and returns False, so email is the reliable
    last resort the router can always fall back to.
    """

    key = "email"

    def available(self) -> bool:
        return True

    def send(self, to: Any, event: str, payload: Dict[str, Any]) -> bool:
        to_email = str(to or "").strip()
        if not to_email:
            return False
        block = payload.get("email") if isinstance(payload.get("email"), dict) else {}
        subject = str(block.get("subject") or payload.get("title") or "JobPilot update")
        html_body = str(block.get("html") or payload.get("html") or _fallback_html(payload))
        text_body = block.get("text") or payload.get("text") or None
        try:
            # Lazy: the SES module lives in the auth service's local `services/`
            # (namespace-merged). Reuse it — do not reimplement SES here.
            from services import email_service  # type: ignore
            return bool(email_service.send_email(to_email, subject, html_body, text_body))
        except Exception as e:  # ImportError in a Lambda without the auth-local module, or SES error
            logger.warning("[notify:email] send failed for event=%s: %s", event, e)
            return False


def _fallback_html(payload: Dict[str, Any]) -> str:
    title = str(payload.get("title") or "JobPilot")
    line = str(payload.get("summary_line") or payload.get("text") or "")
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:520px;margin:auto">'
        f'<h2 style="margin:0 0 12px">{title}</h2>'
        f'<p style="color:#444;line-height:1.5">{line}</p>'
        "</div>"
    )


# ---------------------------------------------------------------------------
# WhatsApp — Meta WhatsApp Cloud API (template messages), flag-gated
# ---------------------------------------------------------------------------
class WhatsAppChannel(Channel):
    """Meta WhatsApp Cloud API. Business-initiated (proactive) messages MUST use
    a pre-approved template, so the payload carries a ``whatsapp`` block:

        payload["whatsapp"] = {
            "template": "daily_digest",       # approved template name
            "language": "en_US",
            "params":   ["3", "Stripe, Ramp, Databricks"],  # body {{1}},{{2}}...
        }

    Gated by ``WHATSAPP_ENABLED`` + credentials. Until Meta Business
    verification + template approval land (MANUAL_STEPS), ``send`` logs the real
    Graph request and returns False.
    """

    key = "whatsapp"
    API_VERSION = "v20.0"

    def _phone_number_id(self) -> str:
        return _cfg("WHATSAPP_PHONE_NUMBER_ID")

    def _access_token(self) -> str:
        return _cfg("WHATSAPP_ACCESS_TOKEN")

    def available(self) -> bool:
        return bool(_flag_enabled("WHATSAPP_ENABLED") and self._phone_number_id() and self._access_token())

    def send(self, to: Any, event: str, payload: Dict[str, Any]) -> bool:
        to_number = _digits(to)
        if not to_number:
            return False
        wa = payload.get("whatsapp") if isinstance(payload.get("whatsapp"), dict) else {}
        template = str(wa.get("template") or event)
        language = str(wa.get("language") or "en_US")
        params = [str(p) for p in (wa.get("params") or [])]

        # The exact upstream request (ready the moment the flag flips).
        url = f"https://graph.facebook.com/{self.API_VERSION}/{self._phone_number_id()}/messages"
        body = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": language},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
                ] if params else [],
            },
        }

        if not self.available():
            logger.info("[notify:whatsapp:stub] flag/creds off — would POST %s template=%s to=%s",
                        url, template, _mask(to_number))
            return False
        try:
            import requests  # lazy; in the shared layer
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {self._access_token()}",
                         "Content-Type": "application/json"},
                json=body, timeout=10,
            )
            ok = resp.status_code < 300
            if not ok:
                logger.warning("[notify:whatsapp] %s -> %s %s", template, resp.status_code, resp.text[:300])
            return ok
        except Exception as e:
            logger.warning("[notify:whatsapp] send failed for event=%s: %s", event, e)
            return False


# ---------------------------------------------------------------------------
# SMS — Twilio Messages API, flag-gated
# ---------------------------------------------------------------------------
class SmsChannel(Channel):
    """Twilio programmable SMS. Gated by ``TWILIO_ENABLED`` + account creds.
    Payload carries an ``sms`` block ``{"body": "..."}`` (or falls back to
    ``summary_line``). Stub logs the Twilio request until the account lands."""

    key = "sms"

    def _sid(self) -> str:
        return _cfg("TWILIO_ACCOUNT_SID")

    def _token(self) -> str:
        return _cfg("TWILIO_AUTH_TOKEN")

    def _from(self) -> str:
        # Prefer a Messaging Service SID if configured, else a from-number.
        return _cfg("TWILIO_MESSAGING_SERVICE_SID") or _cfg("TWILIO_FROM_NUMBER")

    def available(self) -> bool:
        return bool(_flag_enabled("TWILIO_ENABLED") and self._sid() and self._token() and self._from())

    def send(self, to: Any, event: str, payload: Dict[str, Any]) -> bool:
        to_number = _e164(to)
        if not to_number:
            return False
        sms = payload.get("sms") if isinstance(payload.get("sms"), dict) else {}
        body = str(sms.get("body") or payload.get("summary_line") or payload.get("title") or "").strip()
        if not body:
            return False
        body = body[:640]  # keep well under Twilio's concatenated-segment limit

        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._sid()}/Messages.json"
        frm = self._from()
        form = {"To": to_number, "Body": body}
        if frm.startswith("MG"):  # Messaging Service SID
            form["MessagingServiceSid"] = frm
        else:
            form["From"] = frm

        if not self.available():
            logger.info("[notify:sms:stub] flag/creds off — would POST %s to=%s", url, _mask(to_number))
            return False
        try:
            import requests  # lazy
            resp = requests.post(url, data=form, auth=(self._sid(), self._token()), timeout=10)
            ok = resp.status_code < 300
            if not ok:
                logger.warning("[notify:sms] -> %s %s", resp.status_code, resp.text[:300])
            return ok
        except Exception as e:
            logger.warning("[notify:sms] send failed for event=%s: %s", event, e)
            return False


# ---------------------------------------------------------------------------
# Push — Web Push (VAPID), flag-gated
# ---------------------------------------------------------------------------
class PushChannel(Channel):
    """Browser Web Push via VAPID. Gated by ``WEBPUSH_ENABLED`` + VAPID keys.
    ``to`` is a stored PushSubscription dict (endpoint + p256dh/auth keys).
    Payload carries a ``push`` block ``{"title","body","url"}``. Uses
    ``pywebpush`` when present; otherwise logs the intended push and returns
    False so the router falls through to email."""

    key = "push"

    def _vapid_private(self) -> str:
        return _cfg("VAPID_PRIVATE_KEY")

    def _vapid_subject(self) -> str:
        return _cfg("VAPID_SUBJECT", "mailto:no-reply@jobpilot.app")

    def available(self) -> bool:
        return bool(_flag_enabled("WEBPUSH_ENABLED") and self._vapid_private())

    def send(self, to: Any, event: str, payload: Dict[str, Any]) -> bool:
        subscription = to if isinstance(to, dict) else None
        if not subscription or not subscription.get("endpoint"):
            return False
        push = payload.get("push") if isinstance(payload.get("push"), dict) else {}
        import json
        message = json.dumps({
            "title": str(push.get("title") or payload.get("title") or "JobPilot"),
            "body": str(push.get("body") or payload.get("summary_line") or ""),
            "url": str(push.get("url") or payload.get("cta_url") or ""),
        })

        if not self.available():
            logger.info("[notify:push:stub] flag/keys off — would web-push to endpoint=%s",
                        _mask(str(subscription.get("endpoint"))))
            return False
        try:
            from pywebpush import webpush  # lazy; optional dep (see MANUAL_STEPS)
            webpush(
                subscription_info=subscription,
                data=message,
                vapid_private_key=self._vapid_private(),
                vapid_claims={"sub": self._vapid_subject()},
                timeout=10,
            )
            return True
        except Exception as e:
            logger.warning("[notify:push] send failed for event=%s: %s", event, e)
            return False


# ---------------------------------------------------------------------------
# Registry / helpers
# ---------------------------------------------------------------------------
_CHANNEL_CLASSES = {
    EmailChannel.key: EmailChannel,
    WhatsAppChannel.key: WhatsAppChannel,
    SmsChannel.key: SmsChannel,
    PushChannel.key: PushChannel,
}

# Router default order: richest opt-in channels first, email always last.
DEFAULT_PRIORITY = ["whatsapp", "sms", "push", "email"]

# Channels that dial a real phone number -> require recorded double opt-in.
CONSENT_REQUIRED = {"whatsapp", "sms"}


def get_channel(key: str) -> Optional[Channel]:
    cls = _CHANNEL_CLASSES.get((key or "").strip().lower())
    return cls() if cls else None


def known_channel(key: str) -> bool:
    return (key or "").strip().lower() in _CHANNEL_CLASSES


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _e164(value: Any) -> str:
    raw = str(value or "").strip()
    digits = _digits(raw)
    if not digits:
        return ""
    return ("+" + digits) if raw.startswith("+") else ("+" + digits)


def _mask(value: str) -> str:
    s = str(value or "")
    return (s[:4] + "…" + s[-2:]) if len(s) > 8 else "…"
