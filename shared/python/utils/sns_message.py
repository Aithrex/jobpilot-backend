"""Amazon SNS HTTP(S) message signature verification.

The SES bounce/complaint webhook (`/api/auth/ses-notifications`) is PUBLIC and
unauthenticated. Without cryptographic verification, anyone could POST a forged
"bounce" for a victim's address and get it suppressed — silently blocking that
victim's password-reset (and every other) email. So we verify the SNS signature
against the AWS-published signing certificate before trusting a single field.

Two independent guards:
  1. SSRF guard — the SigningCertURL must be https AND its host must match
     `^sns\\.[a-z0-9-]+\\.amazonaws\\.com$`, so we only ever fetch a real AWS
     SNS cert and never an attacker-controlled URL. (The webhook applies the
     same guard to SubscribeURL before confirming a subscription.)
  2. Signature check — canonicalize the message per the AWS spec, then verify
     the RSA (PKCS1v15) signature with the fetched public key.

Import-safe: no network I/O at import time; certs are fetched lazily and cached.
"""
from __future__ import annotations

import base64
import logging
import re

from utils.config import _get_config_value

logger = logging.getLogger(__name__)

# Only ever fetch a genuine AWS SNS signing certificate. Applied to both the
# SigningCertURL here and the SubscribeURL in the webhook handler.
SNS_HOST_RE = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")

# Canonical key ordering per SNS message type (AWS spec). Subject is included
# for a Notification ONLY when present in the message.
_NOTIFICATION_KEYS = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
_SUBSCRIPTION_KEYS = (
    "Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type",
)

# Cache PEM certs by URL to avoid refetching on every notification.
_CERT_CACHE: dict[str, bytes] = {}


def expected_topic_arn() -> str:
    """The SNS topic ARN this backend accepts SES notifications from ("" if unset)."""
    return _get_config_value("SES_SNS_TOPIC_ARN", "") or ""


def _host_is_aws_sns(url: str) -> bool:
    """True iff `url` is https with an sns.<region>.amazonaws.com host."""
    try:
        from urllib.parse import urlparse
        parts = urlparse(url or "")
        if parts.scheme != "https":
            return False
        host = (parts.hostname or "").lower()
        return bool(SNS_HOST_RE.match(host))
    except Exception:
        return False


def _fetch_cert(url: str) -> bytes:
    """Fetch (and cache) the PEM certificate at `url`. Caller must SSRF-check first."""
    cached = _CERT_CACHE.get(url)
    if cached is not None:
        return cached
    import requests
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    pem = resp.content
    _CERT_CACHE[url] = pem
    return pem


def _canonical_string(msg: dict) -> str | None:
    """Build the string-to-sign for the message, or None if the type is unknown."""
    msg_type = msg.get("Type")
    if msg_type == "Notification":
        keys = _NOTIFICATION_KEYS
    elif msg_type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        keys = _SUBSCRIPTION_KEYS
    else:
        return None

    parts = []
    for key in keys:
        # Subject is included only when actually present in a Notification.
        if key == "Subject" and "Subject" not in msg:
            continue
        if key not in msg:
            # A required key is missing -> cannot build a valid canonical string.
            return None
        parts.append(f"{key}\n{msg[key]}\n")
    return "".join(parts)


def verify_sns_message(msg: dict) -> bool:
    """Return True only if `msg` is an authentic, signature-valid SNS message."""
    try:
        if not isinstance(msg, dict):
            return False

        cert_url = msg.get("SigningCertURL", "")
        # SSRF guard: never fetch an arbitrary URL.
        if not _host_is_aws_sns(cert_url):
            logger.warning("[sns] rejecting message: bad SigningCertURL host")
            return False

        # Pick the hash from SignatureVersion (1 => SHA1, 2 => SHA256).
        from cryptography.hazmat.primitives import hashes
        sig_version = str(msg.get("SignatureVersion", ""))
        if sig_version == "1":
            chosen_hash = hashes.SHA1()
        elif sig_version == "2":
            chosen_hash = hashes.SHA256()
        else:
            logger.warning("[sns] rejecting message: unknown SignatureVersion %r", sig_version)
            return False

        canonical = _canonical_string(msg)
        if canonical is None:
            logger.warning("[sns] rejecting message: unknown/incomplete type")
            return False

        signature = base64.b64decode(msg.get("Signature", ""))

        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import padding
        pem = _fetch_cert(cert_url)
        cert = x509.load_pem_x509_certificate(pem)
        cert.public_key().verify(
            signature,
            canonical.encode("utf-8"),
            padding.PKCS1v15(),
            chosen_hash,
        )
        return True
    except Exception as exc:
        logger.warning("[sns] signature verification failed: %s", exc)
        return False
