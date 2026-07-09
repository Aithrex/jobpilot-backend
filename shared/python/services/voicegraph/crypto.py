"""Fernet encryption for writing samples at rest (§9.2).

Mirrors the token-encryption idiom in linkedin_connect_service / gmail_service so
the whole platform derives its symmetric key the same way. The Fernet import is
lazy so importing this module never pulls `cryptography` at cold-start / CI import.

Key source (via utils.config._get_config_value → SSM in prod, env locally):
  INTEGRATION_TOKEN_ENCRYPTION_KEY  (preferred)  →  GMAIL_TOKEN_ENCRYPTION_KEY  (fallback)

A raw 44-char urlsafe-base64 value is used verbatim as a Fernet key; any other
string is SHA-256'd into 32 bytes and base64-urlsafe encoded (deterministic, so
ciphertext stays decryptable across restarts).
"""
import base64
import hashlib
import logging

from utils.config import _get_config_value

logger = logging.getLogger(__name__)


def _encryption_key() -> bytes:
    raw = (
        _get_config_value("INTEGRATION_TOKEN_ENCRYPTION_KEY", "")
        or _get_config_value("GMAIL_TOKEN_ENCRYPTION_KEY", "")
    )
    if not raw:
        raise RuntimeError(
            "INTEGRATION_TOKEN_ENCRYPTION_KEY (or GMAIL_TOKEN_ENCRYPTION_KEY) "
            "is not configured — cannot encrypt writing samples."
        )
    if len(raw) == 44 and raw.endswith("="):
        return raw.encode("utf-8")
    # Not a proper Fernet key. We still derive one deterministically so existing
    # ciphertext stays readable, but a passphrase-style value has weak entropy at
    # rest — the key MUST be a Fernet.generate_key() output in production.
    logger.warning(
        "voicegraph crypto: encryption key is not a 44-char Fernet key; deriving "
        "via SHA-256. Set a Fernet.generate_key() value for at-rest strength."
    )
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet():
    from cryptography.fernet import Fernet  # lazy — keeps cold start / CI import lean
    return Fernet(_encryption_key())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt((plaintext or "").encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
