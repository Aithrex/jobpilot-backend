"""Notification channel abstraction + digests (Compass §6.4 / §2.5).

A single ``service.send(user_email, event, payload)`` routes a message to the
user's preferred channel (WhatsApp -> SMS -> Push -> Email) subject to channel
availability, per-event opt-in, quiet hours and a proactive-message-per-day cap.
EMAIL is the always-available fallback (it degrades to a console log in dev via
the existing SES path), so the digest ships today while WhatsApp/Twilio/web-push
sit behind human-gated feature flags (``WHATSAPP_ENABLED`` / ``TWILIO_ENABLED``
/ ``WEBPUSH_ENABLED``).

Import-safe by construction (mirrors ``services/voicegraph``): nothing here
touches Mongo / SES / Twilio / Meta / web-push / ``requests`` at import time —
every side effect happens lazily inside a function so ``import app; create_app()``
smoke tests run with no network/credentials. Sub-package under the ``services``
PEP-420 namespace (same idiom as ``services/llm_providers`` and
``services/voicegraph``, which also carry an ``__init__.py``).
"""

# Consent scopes for the double-opt-in ledger (channels that dial a real
# phone number and therefore require recorded consent before first send).
WHATSAPP_CONSENT_SCOPE = "notify_whatsapp"
SMS_CONSENT_SCOPE = "notify_sms"

# Bump when the opt-in copy materially changes; stored per grant for audit.
CONSENT_TEXT_VERSION = "notify_v1"
