"""KMS envelope encryption for the ATS credential vault (Tranche 1 §4 — SECURITY P0).

Why not the existing Fernet seam (voicegraph/crypto.py, gmail_service)?
The Fernet key is CO-LOCATED with the app (SSM param the Lambda role can read
for every request path). Credentials/passwords get a stronger boundary:

  * The data key is generated per-encryption by AWS KMS (`GenerateDataKey`)
    under a dedicated CMK (`infra/terraform/kms_credentials.tf`).
  * Only the ENCRYPTED data key is stored next to the ciphertext; recovering
    a plaintext requires a live, IAM-authorized `kms:Decrypt` call — every
    reveal is therefore CloudTrail-visible and revocable by key policy.
  * AES-256-GCM (AEAD) with an encryption-context binding means a blob copied
    from user B's document cannot be opened under user A's context — a
    structural defense against blob-swap / cross-tenant confusion attacks.

Interface is the same shape as the platform's crypto seam
(`encrypt(plaintext) -> blob`, `decrypt(blob) -> plaintext`) so it is a
swap-in behind callers; the blob here is a Mongo-storable dict
`{v, encrypted_dek, iv, ciphertext, tag}` (all bytes fields base64-encoded)
instead of a Fernet token string.

Config (via utils.config._get_config_value → SSM/env in prod, .env locally):
  CREDENTIALS_KMS_KEY_ID — CMK key id / ARN / alias (NOT a secret; a plain
                           Lambda env var wired from Terraform is fine).

Imports of boto3/cryptography are lazy — importing this module never pulls
heavy deps at cold start / CI import (mirrors voicegraph/crypto.py).
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any, Dict, Optional

from utils.config import _get_config_value

logger = logging.getLogger(__name__)

# 96-bit IV is the NIST-recommended GCM nonce size; 128-bit tag is GCM default.
_IV_BYTES = 12
_TAG_BYTES = 16
_BLOB_VERSION = 1

# Reuse the KMS client across warm Lambda invocations (repo convention:
# module-scope client caching, created lazily).
_kms_client = None


def _kms():
    global _kms_client
    if _kms_client is None:
        import boto3  # lazy — bundled in the Lambda runtime
        region = (
            os.getenv("AWS_REGION_NAME")
            or os.getenv("AWS_REGION")
            or "us-east-1"
        )
        _kms_client = boto3.client("kms", region_name=region)
    return _kms_client


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), validate=True)


class KmsCipherError(Exception):
    """Raised on any encrypt/decrypt failure. The message is deliberately
    generic — never echo key material, plaintext, or raw blob contents."""


class KmsEnvelopeCipher:
    """Envelope encryption: KMS GenerateDataKey → AES-256-GCM → discard DEK.

    encrypt(plaintext, context=...) -> blob dict
    decrypt(blob, context=...)      -> plaintext str

    ``context`` (optional dict[str, str]) is used BOTH as the KMS encryption
    context (must match on Decrypt — enforced server-side by KMS) AND as the
    AES-GCM associated data. Callers storing per-user secrets MUST pass a
    stable identity-binding context (e.g. {"user_email": ...}) so ciphertext
    cannot be replayed under another user's document.
    """

    def __init__(self, key_id: Optional[str] = None):
        # Resolved lazily so constructing the cipher in an unconfigured env
        # (local dev without the vault) doesn't raise until first use.
        self._key_id = key_id

    # -- config ------------------------------------------------------------
    def _resolve_key_id(self) -> str:
        key_id = self._key_id or _get_config_value("CREDENTIALS_KMS_KEY_ID", "")
        if not key_id:
            raise KmsCipherError(
                "CREDENTIALS_KMS_KEY_ID is not configured — the credential "
                "vault is unavailable in this environment."
            )
        return key_id

    @staticmethod
    def _normalize_context(context: Optional[Dict[str, Any]]) -> Dict[str, str]:
        """KMS encryption context must be a flat str->str map; normalize and
        sort-serialize deterministically for the GCM associated data."""
        if not context:
            return {}
        return {str(k): str(v) for k, v in context.items()}

    @staticmethod
    def _aad(context: Dict[str, str]) -> Optional[bytes]:
        if not context:
            return None
        # Deterministic: sorted key=value joined — same bytes on both sides.
        return "&".join(f"{k}={context[k]}" for k in sorted(context)).encode("utf-8")

    # -- encrypt -----------------------------------------------------------
    def encrypt(self, plaintext: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """Encrypt ``plaintext`` and return the envelope blob
        {v, encrypted_dek, iv, ciphertext, tag} (base64 string fields)."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # lazy

        ctx = self._normalize_context(context)
        try:
            resp = _kms().generate_data_key(
                KeyId=self._resolve_key_id(),
                KeySpec="AES_256",
                **({"EncryptionContext": ctx} if ctx else {}),
            )
        except KmsCipherError:
            raise
        except Exception as e:
            # Log the exception class only — botocore errors can echo request
            # parameters; never let key material or plaintext near a log line.
            logger.error("KMS GenerateDataKey failed: %s", type(e).__name__)
            raise KmsCipherError("Credential encryption is unavailable.") from None

        dek = resp["Plaintext"]
        try:
            iv = os.urandom(_IV_BYTES)
            sealed = AESGCM(dek).encrypt(
                iv, (plaintext or "").encode("utf-8"), self._aad(ctx)
            )
        finally:
            # Best-effort: drop our reference to the plaintext DEK immediately.
            # (CPython can't guarantee memory zeroization; the KMS envelope
            # model — DEK never persisted, short-lived in memory — is the
            # actual control.)
            del dek

        ciphertext, tag = sealed[:-_TAG_BYTES], sealed[-_TAG_BYTES:]
        return {
            "v": _BLOB_VERSION,
            "encrypted_dek": _b64e(resp["CiphertextBlob"]),
            "iv": _b64e(iv),
            "ciphertext": _b64e(ciphertext),
            "tag": _b64e(tag),
        }

    # -- decrypt -----------------------------------------------------------
    def decrypt(self, blob: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> str:
        """Open an envelope blob produced by :meth:`encrypt`. ``context`` must
        match the encryption-time context or KMS/GCM will refuse."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # lazy

        if not isinstance(blob, dict):
            raise KmsCipherError("Malformed credential blob.")
        try:
            encrypted_dek = _b64d(blob["encrypted_dek"])
            iv = _b64d(blob["iv"])
            sealed = _b64d(blob["ciphertext"]) + _b64d(blob["tag"])
        except (KeyError, TypeError, ValueError):
            raise KmsCipherError("Malformed credential blob.") from None

        ctx = self._normalize_context(context)
        try:
            resp = _kms().decrypt(
                CiphertextBlob=encrypted_dek,
                # KeyId pin: refuse DEKs wrapped under any other CMK, even if
                # the role could decrypt them (defense against blob injection).
                KeyId=self._resolve_key_id(),
                **({"EncryptionContext": ctx} if ctx else {}),
            )
        except KmsCipherError:
            raise
        except Exception as e:
            logger.error("KMS Decrypt failed: %s", type(e).__name__)
            raise KmsCipherError("Credential decryption failed.") from None

        dek = resp["Plaintext"]
        try:
            opened = AESGCM(dek).decrypt(iv, sealed, self._aad(ctx))
        except Exception:
            # Tag mismatch / wrong AAD — tampered or cross-context blob.
            raise KmsCipherError("Credential decryption failed.") from None
        finally:
            del dek

        return opened.decode("utf-8")
