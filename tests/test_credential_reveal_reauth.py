"""Step-up re-auth for the ATS credential reveal — the OAuth-only account fix.

The live bug: Google is the primary signup path, those users have NO
``password_hash`` at all, and the reveal gate demanded one — so every employer
password JobPilot generated on their behalf was unreachable forever
(403 ``reauth_unavailable``, honest but terminal).

What must be true now:

  * an OAuth-only account CAN reveal, via a one-time code mailed to the address
    the account is bound to;
  * a password account still works, unchanged and not weakened, and may use the
    emailed code as a fallback;
  * the code is single-use, short-lived, attempt-capped, send-rate-limited, and
    only its bcrypt hash is stored — the plaintext never appears in a response;
  * every pre-existing guarantee survives: audit BEFORE decrypt, the 5-per-5-min
    reveal budget, owner scoping, and NEVER a 401 for a business failure.

Fully offline: mongomock via conftest/_vg_support, a fake KMS cipher, and a
fake mail transport. No AWS, no network, no real email.
"""
import pytest

from _vg_support import load_service_app, current_db

import services.ats_credential_service as creds
import services.reveal_reauth_service as reauth
from models.user import User

OAUTH_USER = "google.only@example.com"
PW_USER = "haspassword@example.com"
OTHER = "someone.else@example.com"
ACCOUNT_PASSWORD = "Correct-Horse-9!"

_COLLECTIONS = ("ats_credentials", "ats_credential_audit",
                "credential_reveal_challenges", "users")


class _FakeCipher:
    """Keeps the encryption-context binding (what the real GCM AAD gives us) so
    a blob-swap still fails, with no AWS in the loop. Counts decrypts so a test
    can prove the audit write happens FIRST."""

    def __init__(self):
        self.decrypt_calls = 0

    def encrypt(self, plaintext, context=None):
        return {"v": 1, "ct": plaintext, "ctx": dict(context or {})}

    def decrypt(self, blob, context=None):
        self.decrypt_calls += 1
        if blob.get("ctx") != dict(context or {}):
            from services.kms_cipher import KmsCipherError
            raise KmsCipherError("context mismatch")
        return blob["ct"]


class _Mailbox:
    """Stand-in mail transport. Records every send so a test can read the code
    the user would have received — and assert it is nowhere else."""

    def __init__(self):
        self.sent = []
        self.deliver = True

    def send_email(self, to_email, subject, html_body, text_body=None, **kw):
        self.sent.append({"to": to_email, "subject": subject,
                          "html": html_body, "text": text_body or ""})
        return self.deliver

    def code_for(self, to_email):
        import re
        for msg in reversed(self.sent):
            if msg["to"] == to_email:
                m = re.search(r"\b(\d{6})\b", msg["text"] or msg["html"])
                if m:
                    return m.group(1)
        return None


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Reset BOTH limiters around every test in this file.

    ``tests/_vg_support.py`` defines exactly this as an autouse fixture — but
    its name starts with an underscore, and ``from _vg_support import *`` (how
    tests/conftest.py pulls the support module in) never re-exports underscore
    names, so that fixture is inert and rate-limit counters currently leak
    between tests in one pytest process. These tests assert on rate limits, so
    they cannot inherit someone else's counter or hand theirs to the next file.
    """
    def _clear():
        from utils.security import get_rate_limiter
        get_rate_limiter()._requests.clear()
        try:
            current_db()["rate_limit_buckets"].delete_many({})
        except Exception:
            pass

    _clear()
    yield
    _clear()


@pytest.fixture()
def db():
    d = current_db()
    for c in _COLLECTIONS:
        d[c].delete_many({})
    # The account the bug was reported on: Google signup, no password of any kind.
    d.users.insert_one({
        "email": OAUTH_USER,
        "auth_providers": ["google"],
        "oauth_identities": {"google": {"subject": "1234"}},
    })
    d.users.insert_one({
        "email": PW_USER,
        "auth_providers": ["password"],
        "password_hash": User.hash_password(ACCOUNT_PASSWORD),
    })
    d.users.insert_one({"email": OTHER, "auth_providers": ["google"]})
    yield d
    for c in _COLLECTIONS:
        d[c].delete_many({})


@pytest.fixture()
def cipher(monkeypatch):
    c = _FakeCipher()
    monkeypatch.setattr(creds, "_cipher", c)
    monkeypatch.setattr(creds, "_indexes_ensured", False)
    monkeypatch.setattr(reauth, "_indexes_ensured", False)
    return c


@pytest.fixture()
def mailbox(monkeypatch):
    import services.email_service as email_service
    box = _Mailbox()
    monkeypatch.setattr(email_service, "send_email", box.send_email)
    monkeypatch.setattr(email_service, "email_configured", lambda: True)
    return box


@pytest.fixture()
def app():
    return load_service_app("jobs")


def _client(flask_app, identity):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        tok = create_access_token(identity=identity)
    c = flask_app.test_client()

    def call(method, path, json=None):
        kwargs = {"headers": {"Authorization": f"Bearer {tok}"}}
        if json is not None:
            kwargs["json"] = json
        return getattr(c, method)(path, **kwargs)

    return call


def _seed_vault(user=OAUTH_USER, tenant="acme"):
    creds.resolve_login(user, "workday", tenant, username=f"{user}.alias")


# ---------------------------------------------------------------------------
# 1. The reported bug: an OAuth-only account can finally get its password back
# ---------------------------------------------------------------------------
class TestOAuthOnlyAccountCanReveal:
    def test_methods_offer_email_code_and_not_password(self, db, app):
        call = _client(app, OAUTH_USER)
        resp = call("get", "/api/apply/credentials/reauth-methods")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["methods"] == ["email_code"]
        assert "password" not in body["methods"]
        # Enough to recognize your own inbox, not enough to disclose an address.
        assert body["email_hint"].endswith("@example.com")
        assert OAUTH_USER.split("@")[0] not in body["email_hint"]
        assert body["code_ttl_seconds"] == 600

    def test_emailed_code_reveals_the_stored_password(self, db, app, cipher, mailbox):
        _seed_vault()
        call = _client(app, OAUTH_USER)

        chal = call("post", "/api/apply/credentials/reveal/challenge", {})
        assert chal.status_code == 200
        assert chal.get_json()["sent"] is True

        code = mailbox.code_for(OAUTH_USER)
        assert code and len(code) == 6

        resp = call("post", "/api/apply/credentials/reveal",
                    {"ats_family": "workday", "tenant": "acme", "code": code})
        assert resp.status_code == 200
        secret = resp.get_json()
        assert len(secret["password"]) == 24
        assert secret["username"] == f"{OAUTH_USER}.alias"

    def test_the_password_route_still_says_why_it_cannot_help(self, db, app, cipher):
        """A password submitted by an OAuth-only account is still a distinct
        403 — never 401, and never a misleading "wrong password"."""
        _seed_vault()
        resp = _client(app, OAUTH_USER)("post", "/api/apply/credentials/reveal",
                                        {"ats_family": "workday", "tenant": "acme",
                                         "password": "anything-at-all"})
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "reauth_unavailable"

    def test_the_challenge_response_never_carries_the_code(self, db, app, mailbox):
        call = _client(app, OAUTH_USER)
        resp = call("post", "/api/apply/credentials/reveal/challenge", {})
        code = mailbox.code_for(OAUTH_USER)
        assert code not in resp.get_data(as_text=True)
        assert "code" not in resp.get_json()

    def test_only_the_bcrypt_hash_is_stored(self, db, app, mailbox):
        _client(app, OAUTH_USER)("post", "/api/apply/credentials/reveal/challenge", {})
        code = mailbox.code_for(OAUTH_USER)
        row = db.credential_reveal_challenges.find_one({"user_email": OAUTH_USER})
        assert code not in str(row)
        assert row["code_hash"].startswith("$2")
        assert User.verify_password(code, row["code_hash"])

    def test_the_code_goes_only_to_the_registered_address(self, db, app, mailbox):
        """The destination is the JWT identity — a body field cannot redirect it."""
        resp = _client(app, OAUTH_USER)(
            "post", "/api/apply/credentials/reveal/challenge",
            {"email": "attacker@evil.test", "to": "attacker@evil.test"})
        assert resp.status_code == 200, resp.get_json()
        assert [m["to"] for m in mailbox.sent] == [OAUTH_USER]


# ---------------------------------------------------------------------------
# 2. The code is a real second factor, not a formality
# ---------------------------------------------------------------------------
class TestCodeIsSingleUseAndShortLived:
    def test_a_used_code_cannot_be_replayed(self, db, app, cipher, mailbox):
        _seed_vault()
        call = _client(app, OAUTH_USER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        code = mailbox.code_for(OAUTH_USER)
        body = {"ats_family": "workday", "tenant": "acme", "code": code}

        assert call("post", "/api/apply/credentials/reveal", body).status_code == 200
        replay = call("post", "/api/apply/credentials/reveal", body)
        assert replay.status_code == 403
        assert replay.get_json()["error"] == "code_expired"

    def test_the_claim_itself_is_single_use(self, db, mailbox):
        """Service-level, so it cannot pass on the route's post-reveal cleanup:
        verifying a code CONSUMES it, full stop."""
        reauth.send_email_code(OAUTH_USER)
        code = mailbox.code_for(OAUTH_USER)
        assert reauth.verify(OAUTH_USER, code=code) == "email_code"
        with pytest.raises(reauth.ReauthError) as e:
            reauth.verify(OAUTH_USER, code=code)
        assert e.value.error == "code_expired"

    def test_a_code_is_burned_even_when_the_reveal_fails(self, db, app, cipher, mailbox):
        """The proof is spent when it is checked, not when the reveal succeeds —
        otherwise a 404/409 hands the code back for another try."""
        _seed_vault()
        call = _client(app, OAUTH_USER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        code = mailbox.code_for(OAUTH_USER)

        missing = call("post", "/api/apply/credentials/reveal",
                       {"ats_family": "greenhouse", "tenant": "nobody", "code": code})
        assert missing.status_code == 404

        retry = call("post", "/api/apply/credentials/reveal",
                     {"ats_family": "workday", "tenant": "acme", "code": code})
        assert retry.status_code == 403
        assert retry.get_json()["error"] == "code_expired"

    def test_a_wrong_code_is_refused_with_403(self, db, app, cipher, mailbox):
        _seed_vault()
        call = _client(app, OAUTH_USER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        real = mailbox.code_for(OAUTH_USER)
        wrong = f"{(int(real) + 1) % 10 ** 6:06d}"

        resp = call("post", "/api/apply/credentials/reveal",
                    {"ats_family": "workday", "tenant": "acme", "code": wrong})
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "code_invalid"
        assert cipher.decrypt_calls == 0

    def test_an_expired_code_is_refused(self, db, app, cipher, mailbox, monkeypatch):
        from datetime import datetime, timedelta, timezone
        _seed_vault()
        call = _client(app, OAUTH_USER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        code = mailbox.code_for(OAUTH_USER)

        # Walk the SERVICE's clock past the TTL instead of back-dating the row:
        # mongomock emulates the TTL index, so an edited row simply disappears
        # and the test would pass even with the expiry filter deleted.
        later = datetime.now(timezone.utc) + timedelta(
            seconds=reauth._CODE_TTL_SECONDS + 60)
        monkeypatch.setattr(reauth, "_now", lambda: later)
        assert db.credential_reveal_challenges.count_documents(
            {"user_email": OAUTH_USER}) == 1        # the row is still there

        resp = call("post", "/api/apply/credentials/reveal",
                    {"ats_family": "workday", "tenant": "acme", "code": code})
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "code_expired"
        assert cipher.decrypt_calls == 0

    def test_a_new_code_invalidates_the_previous_one(self, db, app, cipher, mailbox):
        """Codes must not pool — only the newest one is live."""
        _seed_vault()
        call = _client(app, OAUTH_USER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        first = mailbox.code_for(OAUTH_USER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        second = mailbox.code_for(OAUTH_USER)
        assert first != second

        stale = call("post", "/api/apply/credentials/reveal",
                     {"ats_family": "workday", "tenant": "acme", "code": first})
        assert stale.status_code == 403
        assert stale.get_json()["error"] == "code_invalid"
        assert db.credential_reveal_challenges.count_documents(
            {"user_email": OAUTH_USER}) == 1

    def test_guessing_burns_the_challenge_after_five_tries(self, db, mailbox):
        """Service-level (the route's own 5/5min budget would mask this)."""
        reauth.send_email_code(OAUTH_USER)
        real = mailbox.code_for(OAUTH_USER)
        wrong = f"{(int(real) + 1) % 10 ** 6:06d}"

        for _ in range(5):
            with pytest.raises(reauth.ReauthError) as e:
                reauth.verify(OAUTH_USER, code=wrong)
            assert e.value.error == "code_invalid"

        with pytest.raises(reauth.ReauthError) as e:
            reauth.verify(OAUTH_USER, code=wrong)
        assert e.value.error == "code_attempts_exceeded"
        assert e.value.status == 429
        # Burned: even the RIGHT code is dead now.
        with pytest.raises(reauth.ReauthError) as e:
            reauth.verify(OAUTH_USER, code=real)
        assert e.value.error == "code_expired"

    def test_a_code_is_bound_to_the_account_that_asked_for_it(self, db, mailbox):
        reauth.send_email_code(OAUTH_USER)
        code = mailbox.code_for(OAUTH_USER)
        with pytest.raises(reauth.ReauthError) as e:
            reauth.verify(OTHER, code=code)
        assert e.value.error == "code_expired"   # OTHER has no live challenge

    def test_reveal_without_any_factor_is_400(self, db, app, cipher):
        _seed_vault()
        resp = _client(app, OAUTH_USER)("post", "/api/apply/credentials/reveal",
                                        {"ats_family": "workday", "tenant": "acme"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "reauth_required"


# ---------------------------------------------------------------------------
# 3. Rate limits hold on both the send and the reveal
# ---------------------------------------------------------------------------
class TestRateLimits:
    def test_code_sends_are_capped(self, db, app, mailbox):
        call = _client(app, OAUTH_USER)
        for _ in range(3):
            assert call("post", "/api/apply/credentials/reveal/challenge", {}).status_code == 200
        resp = call("post", "/api/apply/credentials/reveal/challenge", {})
        assert resp.status_code == 429
        assert resp.get_json()["error"] == "code_send_rate_limited"
        assert len(mailbox.sent) == 3        # the 4th was never mailed

    def test_reveal_keeps_its_five_per_five_minute_budget(self, db, app, cipher):
        _seed_vault()
        call = _client(app, OAUTH_USER)
        body = {"ats_family": "workday", "tenant": "acme", "code": "000000"}
        for _ in range(5):
            assert call("post", "/api/apply/credentials/reveal", body).status_code == 403
        resp = call("post", "/api/apply/credentials/reveal", body)
        assert resp.status_code == 429
        assert resp.get_json()["error"] == "rate_limited"


# ---------------------------------------------------------------------------
# 4. The password path is untouched
# ---------------------------------------------------------------------------
class TestPasswordAccountStillWorks:
    def test_methods_list_password_first_with_email_fallback(self, db, app):
        body = _client(app, PW_USER)("get", "/api/apply/credentials/reauth-methods").get_json()
        assert body["methods"] == ["password", "email_code"]

    def test_the_account_password_still_reveals(self, db, app, cipher):
        _seed_vault(PW_USER)
        resp = _client(app, PW_USER)("post", "/api/apply/credentials/reveal",
                                     {"ats_family": "workday", "tenant": "acme",
                                      "password": ACCOUNT_PASSWORD})
        assert resp.status_code == 200
        assert len(resp.get_json()["password"]) == 24

    def test_a_wrong_account_password_is_403_never_401(self, db, app, cipher):
        _seed_vault(PW_USER)
        resp = _client(app, PW_USER)("post", "/api/apply/credentials/reveal",
                                     {"ats_family": "workday", "tenant": "acme",
                                      "password": "Not-The-Password-1!"})
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "reauth_failed"
        assert cipher.decrypt_calls == 0

    def test_a_password_user_may_also_use_an_emailed_code(self, db, app, cipher, mailbox):
        _seed_vault(PW_USER)
        call = _client(app, PW_USER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        resp = call("post", "/api/apply/credentials/reveal",
                    {"ats_family": "workday", "tenant": "acme",
                     "code": mailbox.code_for(PW_USER)})
        assert resp.status_code == 200

    def test_a_code_cannot_stand_in_for_a_password_the_user_never_completed(
            self, db, app, cipher):
        """No challenge outstanding ⇒ a made-up code proves nothing."""
        _seed_vault(PW_USER)
        resp = _client(app, PW_USER)("post", "/api/apply/credentials/reveal",
                                     {"ats_family": "workday", "tenant": "acme",
                                      "code": "123456"})
        assert resp.status_code == 403
        assert cipher.decrypt_calls == 0


# ---------------------------------------------------------------------------
# 5. Everything the surface already guaranteed
# ---------------------------------------------------------------------------
class TestExistingGuaranteesSurvive:
    def test_the_audit_row_is_written_before_the_decrypt(self, db, app, cipher, mailbox,
                                                         monkeypatch):
        """If the reveal cannot be attributed, it does not happen."""
        _seed_vault()
        call = _client(app, OAUTH_USER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        code = mailbox.code_for(OAUTH_USER)

        def _boom(*a, **kw):
            raise RuntimeError("audit store down")

        monkeypatch.setattr(creds, "_audit", _boom)
        resp = call("post", "/api/apply/credentials/reveal",
                    {"ats_family": "workday", "tenant": "acme", "code": code})
        assert resp.status_code == 503
        assert resp.get_json()["error"] == "audit_unavailable"
        assert cipher.decrypt_calls == 0

    def test_a_successful_reveal_leaves_exactly_one_audit_row(self, db, app, cipher,
                                                              mailbox):
        _seed_vault()
        call = _client(app, OAUTH_USER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        call("post", "/api/apply/credentials/reveal",
             {"ats_family": "workday", "tenant": "acme",
              "code": mailbox.code_for(OAUTH_USER)})
        rows = list(db.ats_credential_audit.find({"action": "revealed"}))
        assert len(rows) == 1
        assert rows[0]["user_email"] == OAUTH_USER
        # PII-light: no password material, and no code material either.
        assert "password" not in str(rows[0]).lower()

    @pytest.mark.parametrize("family", ["workday", "greenhouse"])
    def test_another_user_cannot_reveal_your_credential(self, db, app, cipher,
                                                        mailbox, family):
        """Both shapes: a SHARED (Workday pointer → canonical) row and a plain
        per-tenant row. A perfectly valid code of your own must not reach
        someone else's vault — it is 404, never a decrypt attempt."""
        creds.resolve_login(OAUTH_USER, family, "acme", username="a@alias.test")
        call = _client(app, OTHER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        resp = call("post", "/api/apply/credentials/reveal",
                    {"ats_family": family, "tenant": "acme",
                     "code": mailbox.code_for(OTHER)})
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "not_found"
        assert cipher.decrypt_calls == 0

    def test_a_consumed_challenge_is_cleared_after_the_reveal(self, db, app, cipher,
                                                              mailbox):
        _seed_vault()
        call = _client(app, OAUTH_USER)
        call("post", "/api/apply/credentials/reveal/challenge", {})
        call("post", "/api/apply/credentials/reveal",
             {"ats_family": "workday", "tenant": "acme",
              "code": mailbox.code_for(OAUTH_USER)})
        assert db.credential_reveal_challenges.count_documents(
            {"user_email": OAUTH_USER}) == 0

    def test_every_reveal_failure_avoids_401(self, db, app, cipher, mailbox):
        """401 is reserved for a missing/invalid JWT — the FE force-logs-out on
        any 401, so a business failure must never produce one."""
        _seed_vault()
        call = _client(app, OAUTH_USER)
        for body in (
            {"ats_family": "workday", "tenant": "acme"},                      # no factor
            {"ats_family": "workday", "tenant": "acme", "code": "000000"},    # no challenge
            {"ats_family": "workday", "tenant": "acme", "password": "x"},     # no password
            {"ats_family": "greenhouse", "tenant": "nope", "code": "000000"},  # not found
        ):
            assert call("post", "/api/apply/credentials/reveal", body).status_code != 401
        assert call("get", "/api/apply/credentials/reauth-methods").status_code == 200
        # And a MISSING JWT still is a 401 — the contract's other half.
        assert app.test_client().post(
            "/api/apply/credentials/reveal", json={}).status_code == 401
        assert app.test_client().post(
            "/api/apply/credentials/reveal/challenge", json={}).status_code == 401
        assert app.test_client().get(
            "/api/apply/credentials/reauth-methods").status_code == 401


# ---------------------------------------------------------------------------
# 6. A code that could not be delivered must not stay valid
# ---------------------------------------------------------------------------
class TestUndeliverableCode:
    def test_a_failed_send_invalidates_the_challenge(self, db, app, mailbox):
        """email_service logs the body when a provider send fails — a code sitting
        in CloudWatch must not still open the vault."""
        mailbox.deliver = False
        resp = _client(app, OAUTH_USER)("post", "/api/apply/credentials/reveal/challenge", {})
        assert resp.status_code == 503
        assert resp.get_json()["error"] == "email_unavailable"
        assert db.credential_reveal_challenges.count_documents(
            {"user_email": OAUTH_USER}) == 0

    def test_an_unconfigured_mailer_never_mints_a_code(self, db, app, monkeypatch,
                                                       mailbox):
        import services.email_service as email_service
        monkeypatch.setattr(email_service, "email_configured", lambda: False)
        resp = _client(app, OAUTH_USER)("post", "/api/apply/credentials/reveal/challenge", {})
        assert resp.status_code == 503
        assert mailbox.sent == []
        assert db.credential_reveal_challenges.count_documents({}) == 0
