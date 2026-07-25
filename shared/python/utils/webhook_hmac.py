"""Constant-time HMAC-SHA256 signing/verification for machine webhooks.

Shared, dependency-free helper for the JWT-exempt machine routes that
authenticate with a pre-shared secret instead of a bearer token (e.g. the
Cloudflare inbound-mail Worker -> ``/api/resume/mail-inbound/*``,
managed-mailbox-v1 §4). The Worker and the backend each compute
``HMAC-SHA256(secret, canonical)`` over an agreed canonical string and compare
hex digests; a mismatch is a 403 (never a 401 — invariant #1: a machine caller
has no session to expire).

Security notes:
  - ``verify`` uses ``hmac.compare_digest`` so comparison time does not leak how
    many leading bytes matched (timing-attack defense).
  - The canonical string is built by the CALLER from already-untrusted fields;
    this module never parses it. Both sides must agree on field order/newlines.
  - An empty secret makes ``verify`` fail closed (returns False) — a missing
    ``MAIL_INBOUND_HMAC_SECRET`` must never accidentally accept every request.
"""

from __future__ import annotations

import hashlib
import hmac


def sign(secret: str, canonical: str) -> str:
    """Return the lowercase hex HMAC-SHA256 of ``canonical`` under ``secret``.

    Returns ``""`` for an empty secret so a misconfiguration can never produce a
    signature that ``verify`` would then accept."""
    if not secret:
        return ""
    return hmac.new(
        secret.encode("utf-8"),
        (canonical or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify(secret: str, canonical: str, signature: str) -> bool:
    """Constant-time check that ``signature`` is the valid HMAC of ``canonical``.

    Fail-closed: an empty secret or an empty/garbage signature returns False."""
    if not secret or not signature:
        return False
    expected = sign(secret, canonical)
    if not expected:
        return False
    return hmac.compare_digest(expected, str(signature).strip().lower())
