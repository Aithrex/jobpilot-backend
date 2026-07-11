"""Offline tests for the signed magic-link approval tokens (Tranche 1 §2).

Runs fully offline (mongomock; no network). Verifies the security contract of
the auth-less-but-signature-verified approve-link channel:

  * mint → verify round-trip returns the exact signed claim;
  * single-use (a second claim of the same jti is rejected 409);
  * forged signature + tampered payload are rejected 403;
  * an expired link is rejected 403 (clock check, before the single-use claim);
  * NONE of the failure statuses is ever 401 (401 is reserved for @jwt_required;
    this channel is intentionally token-authenticated, never JWT).

APPROVAL_LINK_SECRET is pinned to a deterministic test value so signatures are
stable across the process (the dev fallback is a per-process random secret,
which would still round-trip but makes the forgery/expiry assertions clearer).
"""
import json

import pytest

from _vg_support import current_db

import services.approval_token_service as ats

USER_A = "alice@aspirely.me"

# Every typed error this channel may raise — the route maps each to an HTML
# page. The contract: all are business statuses, NEVER 401.
_ALLOWED_STATUSES = {400, 403, 409, 503}


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("APPROVAL_LINK_SECRET", "deterministic-test-approval-secret")
    monkeypatch.setenv("ENVIRONMENT", "development")


@pytest.fixture()
def tok_db():
    d = current_db()
    d.approval_tokens.delete_many({})
    d.approval_tokens.create_index([("jti", 1)], unique=True, name="jti_unique")
    return d


# ---------------------------------------------------------------------------
# Round-trip + single-use
# ---------------------------------------------------------------------------
class TestRoundTrip:
    def test_mint_then_verify_returns_signed_claim(self, tok_db):
        token = ats.make_token("approve_all", USER_A, ["app-1", "app-2"])
        claim = ats.verify_and_claim(token)
        assert claim["user_email"] == USER_A
        assert claim["action"] == "approve_all"
        assert claim["application_ids"] == ["app-1", "app-2"]
        assert isinstance(claim["exp"], int)

    def test_token_record_is_persisted_before_return(self, tok_db):
        token = ats.make_token("skip", USER_A, ["app-1"])
        payload_b64 = token.split(".")[0]
        payload = json.loads(ats._b64d(payload_b64).decode("utf-8"))
        rec = tok_db.approval_tokens.find_one({"jti": payload["jti"]})
        assert rec is not None
        assert rec["used"] is False

    def test_second_claim_is_rejected_already_used(self, tok_db):
        token = ats.make_token("approve_one", USER_A, ["app-1"])
        ats.verify_and_claim(token)  # first use wins
        with pytest.raises(ats.TokenAlreadyUsed) as ei:
            ats.verify_and_claim(token)
        assert ei.value.status == 409
        # The verified payload rides along so the route can answer idempotently.
        assert ei.value.payload["user_email"] == USER_A

    def test_used_flag_flips_exactly_once(self, tok_db):
        token = ats.make_token("review", USER_A, [])
        ats.verify_and_claim(token)
        payload = json.loads(ats._b64d(token.split(".")[0]).decode("utf-8"))
        rec = tok_db.approval_tokens.find_one({"jti": payload["jti"]})
        assert rec["used"] is True
        assert rec["used_at"] is not None


# ---------------------------------------------------------------------------
# Forgery + tampering
# ---------------------------------------------------------------------------
class TestForgery:
    def test_forged_signature_rejected(self, tok_db):
        token = ats.make_token("approve_all", USER_A, ["app-1"])
        payload_b64, _sig = token.split(".")
        forged = f"{payload_b64}.{ats._b64e(b'not-the-real-signature')}"
        with pytest.raises(ats.TokenInvalid) as ei:
            ats.verify_and_claim(forged)
        assert ei.value.status == 403

    def test_tampered_payload_breaks_signature(self, tok_db):
        # Attacker mints for themselves then swaps the email to a victim.
        token = ats.make_token("approve_all", USER_A, ["app-1"])
        _payload_b64, sig_b64 = token.split(".")
        evil = {"jti": "x", "user_email": "victim@aspirely.me",
                "action": "approve_all", "application_ids": ["app-1"], "exp": 9999999999}
        evil_b64 = ats._b64e(json.dumps(evil, sort_keys=True, separators=(",", ":")).encode())
        with pytest.raises(ats.TokenInvalid) as ei:
            ats.verify_and_claim(f"{evil_b64}.{sig_b64}")
        assert ei.value.status == 403

    def test_malformed_token_rejected(self, tok_db):
        for bad in ("", "no-dot", "a.b.c", "onlyone."):
            with pytest.raises(ats.TokenInvalid):
                ats.verify_and_claim(bad)

    def test_unknown_jti_rejected_as_invalid(self, tok_db):
        # Correctly signed but never persisted (or TTL-reaped) → never honored.
        token = ats.make_token("approve_all", USER_A, ["app-1"])
        tok_db.approval_tokens.delete_many({})
        with pytest.raises(ats.TokenInvalid) as ei:
            ats.verify_and_claim(token)
        assert ei.value.status == 403


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
class TestExpiry:
    def test_expired_link_rejected(self, tok_db, monkeypatch):
        token = ats.make_token("approve_all", USER_A, ["app-1"])
        # Advance the service clock past the token's exp (3-day TTL).
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(days=4)
        monkeypatch.setattr(ats, "_now", lambda: future)
        with pytest.raises(ats.TokenExpired) as ei:
            ats.verify_and_claim(token)
        assert ei.value.status == 403

    def test_expiry_checked_before_single_use_claim(self, tok_db, monkeypatch):
        token = ats.make_token("approve_all", USER_A, ["app-1"])
        payload = json.loads(ats._b64d(token.split(".")[0]).decode("utf-8"))
        from datetime import datetime, timedelta, timezone
        future = datetime.now(timezone.utc) + timedelta(days=4)
        monkeypatch.setattr(ats, "_now", lambda: future)
        with pytest.raises(ats.TokenExpired):
            ats.verify_and_claim(token)
        # The record must NOT have been consumed by an expired click.
        rec = tok_db.approval_tokens.find_one({"jti": payload["jti"]})
        assert rec["used"] is False


# ---------------------------------------------------------------------------
# The never-401 contract
# ---------------------------------------------------------------------------
class TestNever401:
    def test_all_error_statuses_are_business_not_401(self):
        for exc in (ats.ApprovalTokenError, ats.TokenInvalid, ats.TokenExpired,
                    ats.TokenAlreadyUsed, ats.TokenServiceNotConfigured):
            assert exc.status != 401
            assert exc.status in _ALLOWED_STATUSES

    def test_service_fails_closed_in_prod_without_secret(self, monkeypatch, tok_db):
        # Production + no configured secret → 503 fail-closed, still never 401.
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("APPROVAL_LINK_SECRET", raising=False)
        with pytest.raises(ats.TokenServiceNotConfigured) as ei:
            ats.make_token("approve_all", USER_A, ["app-1"])
        assert ei.value.status == 503
