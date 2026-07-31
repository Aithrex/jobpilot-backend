"""Every KMS encryption-context value must be a NON-EMPTY string.

This is a production incident caught live. The one-Workday-password-per-user
change keys its canonical row with an EMPTY tenant. `_context` bound
`{"tenant": ""}` straight through, and KMS rejects an empty VALUE in an
EncryptionContext:

    ClientError ValidationException: Invalid EncryptionContext

`kms_cipher.encrypt` turns any ClientError into the deliberately-opaque
"Credential encryption is unavailable.", so the real cause was invisible: no
credential could ever be stored, `ats_credentials` stayed empty, and every
Workday application parked on the account gate telling the user to go create an
account by hand — the exact thing the feature exists to avoid.

Verified against real KMS before the fix:
    tenant="acme" -> OK
    tenant=""     -> ValidationException: Invalid EncryptionContext

The context is ALSO the GCM associated data, so this map is load-bearing for
decryption, not just for the API call.
"""
import pytest

import services.ats_credential_service as creds


class TestNoEmptyValues:
    """KMS's actual constraint."""

    @pytest.mark.parametrize("tenant", ["", None])
    def test_an_empty_tenant_becomes_a_sentinel(self, tenant):
        ctx = creds._context("a@b.co", "workday", tenant)
        assert ctx["tenant"] == creds._SHARED_TENANT_SENTINEL
        assert ctx["tenant"] != ""

    def test_no_context_value_is_ever_empty(self):
        for tenant in ("", None, "acme", "wf1"):
            ctx = creds._context("a@b.co", "workday", tenant)
            empties = [k for k, v in ctx.items() if not str(v).strip()]
            assert not empties, f"KMS rejects empty context values: {empties}"

    def test_every_value_is_a_string(self):
        ctx = creds._context("a@b.co", "workday", "")
        assert all(isinstance(v, str) for v in ctx.values())


class TestBindingStrength:
    def test_a_real_tenant_is_untouched(self):
        assert creds._context("a@b.co", "workday", "acme")["tenant"] == "acme"

    def test_the_shared_row_keeps_the_same_context_shape(self):
        """Dropping the key instead of substituting would make the shared row's
        context a strict SUBSET of a per-tenant row's — a weaker binding."""
        shared = creds._context("a@b.co", "workday", "")
        tenant = creds._context("a@b.co", "workday", "acme")
        assert shared.keys() == tenant.keys()

    def test_shared_and_per_tenant_contexts_stay_distinct(self):
        """The binding must not collapse: a blob sealed for the shared row must
        not be decryptable as a per-tenant one."""
        assert creds._context("a@b.co", "workday", "") != \
            creds._context("a@b.co", "workday", "acme")

    def test_the_sentinel_cannot_collide_with_a_real_tenant(self):
        """Workday tenants are hostname-safe (alphanumeric/hyphen), so a value
        containing underscores is unreachable as a genuine tenant."""
        assert "_" in creds._SHARED_TENANT_SENTINEL
        assert creds._SHARED_TENANT_SENTINEL != ""

    def test_context_still_binds_the_owner(self):
        """The whole point of the AAD: another user's blob must not decrypt."""
        assert creds._context("a@b.co", "workday", "") != \
            creds._context("mallory@evil.co", "workday", "")
