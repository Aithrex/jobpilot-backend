"""ATS credential vault — ONE Workday password per user, safely stored.

Covers the product decision ("one Workday password across every Workday
employer, revealable on demand") AND the security invariants it must not cost
us: server-generated only, KMS-envelope at rest, a single audited plaintext
egress with the audit write REQUIRED BEFORE the decrypt, and a sharing boundary
that Greenhouse/Lever/Ashby can never silently cross.
"""
import pytest

from services import ats_credential_service as creds


USER = "owner@example.com"
OTHER = "someone.else@example.com"


class _FakeCipher:
    """Stand-in for KmsEnvelopeCipher: keeps the encryption-context binding
    (the property the real GCM AAD gives us) so a blob-swap still fails, with
    no AWS in the loop."""

    def __init__(self):
        self.encrypt_calls = 0
        self.decrypt_calls = 0

    def encrypt(self, plaintext, context=None):
        self.encrypt_calls += 1
        return {"v": 1, "ct": plaintext, "ctx": dict(context or {})}

    def decrypt(self, blob, context=None):
        self.decrypt_calls += 1
        if blob.get("ctx") != dict(context or {}):
            from services.kms_cipher import KmsCipherError
            raise KmsCipherError("context mismatch")
        return blob["ct"]


@pytest.fixture()
def vault(monkeypatch):
    from utils.db_connect import DBConnect
    db = DBConnect().get_db()
    db.ats_credentials.delete_many({})
    db.ats_credential_audit.delete_many({})
    cipher = _FakeCipher()
    monkeypatch.setattr(creds, "_cipher", cipher)
    monkeypatch.setattr(creds, "_indexes_ensured", False)
    yield cipher
    db.ats_credentials.delete_many({})
    db.ats_credential_audit.delete_many({})


def _audits(action=None):
    from utils.db_connect import DBConnect
    flt = {"action": action} if action else {}
    return list(DBConnect().get_db().ats_credential_audit.find(flt))


# ---------------------------------------------------------------------------
# The product ask: one Workday password
# ---------------------------------------------------------------------------
class TestSharedWorkdayPassword:
    def test_every_workday_tenant_reveals_the_same_password(self, vault):
        creds.resolve_login(USER, "workday", "acme", username="a@alias.test")
        creds.resolve_login(USER, "workday", "globex", username="a@alias.test")

        first = creds.reveal(USER, "workday", "acme")
        second = creds.reveal(USER, "workday", "globex")

        assert first["password"] == second["password"]
        assert len(first["password"]) == 24

    def test_canonical_row_owns_the_only_blob(self, vault):
        creds.resolve_login(USER, "workday", "acme")
        creds.resolve_login(USER, "workday", "globex")

        from utils.db_connect import DBConnect
        rows = list(DBConnect().get_db().ats_credentials.find(
            {"user_email": USER, "ats_family": "workday"}))
        with_blob = [r for r in rows if r.get("password_blob")]
        assert [r["tenant"] for r in with_blob] == [creds.SHARED_TENANT]
        pointers = [r for r in rows if r.get("shared_credential")]
        assert sorted(r["tenant"] for r in pointers) == ["acme", "globex"]

    def test_resolve_is_idempotent_and_never_rotates(self, vault):
        creds.resolve_login(USER, "workday", "acme")
        before = creds.reveal(USER, "workday", "acme")["password"]
        for _ in range(3):
            creds.resolve_login(USER, "workday", "acme")
        assert creds.reveal(USER, "workday", "acme")["password"] == before

    def test_the_canonical_row_is_audited_as_stored_exactly_once(self, vault):
        creds.resolve_login(USER, "workday", "acme")
        creds.resolve_login(USER, "workday", "globex")
        creds.resolve_login(USER, "workday", "acme")
        stored = [a for a in _audits("stored") if a["ats_family"] == "workday"]
        assert len(stored) == 1
        assert stored[0]["tenant"] == creds.SHARED_TENANT
        assert stored[0]["meta"]["scope"] == "shared"

    def test_a_blobless_row_at_the_target_key_is_repaired(self, vault):
        """Defensive: a half-written row must not leave reveal 404-ing forever."""
        from utils.db_connect import DBConnect
        DBConnect().get_db().ats_credentials.insert_one({
            "user_email": USER, "ats_family": "workday",
            "tenant": creds.SHARED_TENANT, "username": "u@alias.test",
        })
        creds.resolve_login(USER, "workday", "acme")
        assert creds.reveal(USER, "workday", "acme")["password"]

    def test_store_or_rotate_cannot_refragment_workday(self, vault):
        """The user-facing PUT route must not re-create per-tenant Workday
        passwords behind resolve_login's back."""
        creds.resolve_login(USER, "workday", "acme")
        creds.store_or_rotate(USER, "workday", "globex")

        from utils.db_connect import DBConnect
        with_blob = list(DBConnect().get_db().ats_credentials.find(
            {"user_email": USER, "ats_family": "workday",
             "password_blob": {"$exists": True}}))
        assert [r["tenant"] for r in with_blob] == [creds.SHARED_TENANT]
        assert (creds.reveal(USER, "workday", "acme")["password"]
                == creds.reveal(USER, "workday", "globex")["password"])


# ---------------------------------------------------------------------------
# The boundary: sharing is Workday-ONLY
# ---------------------------------------------------------------------------
class TestSharingBoundary:
    def test_only_workday_shares(self):
        assert creds.SHARED_PASSWORD_FAMILIES == frozenset({"workday"})
        assert creds.is_shared_family("workday") is True
        for other in ("greenhouse", "lever", "ashby", "icims", "taleo", "",
                      None, {"$ne": None}):
            assert creds.is_shared_family(other) is False

    def test_other_vendors_get_a_password_per_tenant(self, vault):
        creds.resolve_login(USER, "greenhouse", "acme")
        creds.resolve_login(USER, "greenhouse", "globex")
        assert (creds.reveal(USER, "greenhouse", "acme")["password"]
                != creds.reveal(USER, "greenhouse", "globex")["password"])

    def test_a_non_workday_vendor_never_reads_the_workday_secret(self, vault):
        creds.resolve_login(USER, "workday", "acme")
        # Same tenant name, different vendor: must NOT resolve to the Workday
        # canonical row, and must not exist at all until asked for.
        with pytest.raises(creds.CredentialError) as e:
            creds.reveal(USER, "lever", "acme")
        assert e.value.status == 404

    def test_shared_lookup_is_scoped_to_the_owner(self, vault):
        creds.resolve_login(USER, "workday", "acme")
        with pytest.raises(creds.CredentialError) as e:
            creds.reveal(OTHER, "workday", "acme")
        assert e.value.status == 404


# ---------------------------------------------------------------------------
# Migration: never show a password the employer does not have
# ---------------------------------------------------------------------------
class TestLegacyPerTenantRows:
    def test_pre_existing_per_tenant_password_is_kept(self, vault, monkeypatch):
        # Reproduce the pre-change world exactly: with no shared family
        # configured, store_or_rotate writes a per-tenant Workday password —
        # which is what a real employer account was then created with.
        monkeypatch.setattr(creds, "SHARED_PASSWORD_FAMILIES", frozenset())
        creds.store_or_rotate(USER, "workday", "legacyco")
        monkeypatch.undo()
        monkeypatch.setattr(creds, "_cipher", vault)
        legacy_pw = creds.reveal(USER, "workday", "legacyco")["password"]

        out = creds.resolve_login(USER, "workday", "legacyco")
        assert out["source_tenant"] == "legacyco"
        assert out["shared"] is False
        assert out["credential"]["legacy_per_tenant"] is True
        # Untouched — the employer's side still has exactly this password.
        assert creds.reveal(USER, "workday", "legacyco")["password"] == legacy_pw

        # A NEW employer still gets the shared canonical secret.
        creds.resolve_login(USER, "workday", "newco")
        assert creds.reveal(USER, "workday", "newco")["password"] != legacy_pw


# ---------------------------------------------------------------------------
# Security invariants that must survive the change
# ---------------------------------------------------------------------------
class TestSecurityInvariants:
    def test_plaintext_never_leaves_store_or_list(self, vault):
        result = creds.resolve_login(USER, "workday", "acme")
        listed = creds.list_credentials(USER)
        blob = str(result) + str(listed)
        pw = creds.reveal(USER, "workday", "acme")["password"]
        assert pw not in blob
        assert all("password_blob" not in row for row in listed)
        assert all("password" not in row for row in listed)

    def test_pointer_row_reports_has_password_and_shared_scope(self, vault):
        creds.resolve_login(USER, "workday", "acme", company="Acme Corp",
                            login_url="https://acme.wd1.myworkdayjobs.com/x?t=1")
        by_tenant = {r["tenant"]: r for r in creds.list_credentials(USER)}
        acme = by_tenant["acme"]
        assert acme["has_password"] is True
        assert acme["scope"] == "shared"
        assert acme["company"] == "Acme Corp"
        # query string (can carry a one-use token) is stripped
        assert acme["login_url"] == "https://acme.wd1.myworkdayjobs.com/x"

    def test_reveal_refuses_when_the_audit_write_fails(self, vault, monkeypatch):
        creds.resolve_login(USER, "workday", "acme")
        before = vault.decrypt_calls

        def _boom(*a, **kw):
            raise RuntimeError("audit down")

        monkeypatch.setattr(creds, "_audit", _boom)
        with pytest.raises(creds.CredentialError) as e:
            creds.reveal(USER, "workday", "acme")
        assert e.value.status == 503
        assert e.value.error == "audit_unavailable"
        # The hard rule: no decrypt happened.
        assert vault.decrypt_calls == before

    def test_reveal_audits_the_requested_tenant_and_its_source(self, vault):
        creds.resolve_login(USER, "workday", "acme")
        creds.reveal(USER, "workday", "acme")
        rows = _audits("revealed")
        assert len(rows) == 1
        assert rows[0]["tenant"] == "acme"
        assert rows[0]["meta"]["source_tenant"] == creds.SHARED_TENANT
        assert rows[0]["meta"]["shared"] is True
        # PII-light: no password material anywhere in the audit row.
        assert "password" not in str(rows[0])

    def test_blob_is_bound_to_the_user(self, vault):
        creds.resolve_login(USER, "workday", "acme")
        from utils.db_connect import DBConnect
        db = DBConnect().get_db()
        stolen = db.ats_credentials.find_one(
            {"user_email": USER, "tenant": creds.SHARED_TENANT})["password_blob"]
        db.ats_credentials.insert_one({
            "user_email": OTHER, "ats_family": "workday",
            "tenant": creds.SHARED_TENANT, "username": "x",
            "password_blob": stolen, "rotation_count": 1, "reset_count": 0,
        })
        with pytest.raises(creds.CredentialError) as e:
            creds.reveal(OTHER, "workday", creds.SHARED_TENANT)
        assert e.value.error == "decrypt_failed"

    def test_injection_shaped_identifiers_are_rejected(self, vault):
        for bad in ({"$ne": None}, ["a"], "acme; drop", "$where"):
            with pytest.raises(creds.CredentialError) as e:
                creds.resolve_login(USER, "workday", bad)
            assert e.value.status == 400


class TestReset:
    def test_reset_through_a_pointer_rotates_the_shared_secret(self, vault):
        creds.resolve_login(USER, "workday", "acme")
        creds.resolve_login(USER, "workday", "globex")
        old = creds.reveal(USER, "workday", "acme")["password"]

        out = creds.request_reset(USER, "workday", "acme")
        assert out["shared"] is True

        new_acme = creds.reveal(USER, "workday", "acme")["password"]
        new_globex = creds.reveal(USER, "workday", "globex")["password"]
        assert new_acme != old
        assert new_acme == new_globex

        # Every remote account still holds the OLD password → all flagged.
        from utils.db_connect import DBConnect
        pending = list(DBConnect().get_db().ats_credentials.find(
            {"user_email": USER, "ats_family": "workday",
             "pending_ats_reset": True}))
        assert sorted(r["tenant"] for r in pending) == [
            creds.SHARED_TENANT, "acme", "globex"]

    def test_reset_on_a_non_shared_vendor_touches_only_that_tenant(self, vault):
        creds.resolve_login(USER, "greenhouse", "acme")
        creds.resolve_login(USER, "greenhouse", "globex")
        other = creds.reveal(USER, "greenhouse", "globex")["password"]

        out = creds.request_reset(USER, "greenhouse", "acme")
        assert out["shared"] is False
        assert creds.reveal(USER, "greenhouse", "globex")["password"] == other
