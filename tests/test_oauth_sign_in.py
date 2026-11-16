"""Provider sign-in: who gets linked to which account, and whose browser may
finish a flow (`services/auth/services/oauth_service.py`).

There were no tests here before 2026-11-15, and the code linked a provider to
an existing account by email even when the provider had not verified that
email. Microsoft rarely verifies it, so every Microsoft sign-in would have
taken that path: type the victim's address into a tenant you control, sign in,
own their account.

Providers are mocked at the two functions that talk to them
(`exchange_code`, `fetch_identity`); everything else is the real app on
mongomock. No network.
"""
from urllib.parse import parse_qs, urlparse

import pytest

from _vg_support import load_service_app

from utils import db_connect

EMAIL = "riya@example.com"
PASSWORD = "Str0ng!Pass1"

COLLECTIONS = ("users", "user_preferences", "oauth_login_states",
               "password_reset_tokens", "rate_limit_buckets")


@pytest.fixture()
def db():
    from utils.security import get_rate_limiter
    database = db_connect.DBConnect().get_db()
    for c in COLLECTIONS:
        database[c].delete_many({})
    get_rate_limiter()._requests.clear()
    yield database
    get_rate_limiter()._requests.clear()


@pytest.fixture()
def app(db, monkeypatch):
    monkeypatch.setenv("GOOGLE_SIGNIN_CLIENT_ID", "client-id.apps.example")
    monkeypatch.setenv("GOOGLE_SIGNIN_CLIENT_SECRET", "not-a-real-secret")
    monkeypatch.setenv("FRONTEND_URL", "https://app.test")
    import services.email_service as es
    monkeypatch.setattr(es, "send_email", lambda *a, **k: True)
    return load_service_app("auth")


@pytest.fixture()
def oauth(app):
    import services.oauth_service as module
    return module


@pytest.fixture()
def provider(oauth, monkeypatch):
    """Make the mocked provider answer with `identity` on the next callback."""
    seen = {}

    def answer(**identity):
        identity.setdefault("subject", "sub-1")
        identity.setdefault("email", EMAIL)
        identity.setdefault("email_verified", True)
        identity.setdefault("name", "Riya Sharma")

        def exchange(prov, code, code_verifier=None):
            seen.update(code=code, code_verifier=code_verifier)
            return {"access_token": "at", "id_token": "it"}

        monkeypatch.setattr(oauth, "exchange_code", exchange)
        monkeypatch.setattr(oauth, "fetch_identity", lambda prov, tokens: dict(identity))
        return seen

    return answer


def _start(client, provider_name="google"):
    resp = client.get(f"/api/auth/oauth/{provider_name}/authorize")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    query = parse_qs(urlparse(resp.get_json()["authorize_url"]).query)
    return query["state"][0], query


def _finish(client, state, provider_name="google", **params):
    params.setdefault("code", "auth-code")
    resp = client.get(f"/api/auth/oauth/{provider_name}/callback",
                      query_string={"state": state, **params})
    assert resp.status_code == 302
    target = urlparse(resp.headers["Location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == "https://app.test/oauth/callback"
    return {k: v[0] for k, v in parse_qs(target.fragment).items()}


def _password_account(db, verified, email=EMAIL):
    from models.user import User
    db.users.insert_one({"email": email, "password_hash": User.hash_password(PASSWORD),
                         "auth_providers": ["password"], "email_verified": verified, "plan": "free"})


# ---------------------------------------------------------------------------
# The flow, and whose browser may finish it
# ---------------------------------------------------------------------------
def test_a_first_sign_in_creates_a_verified_account(app, db, provider):
    seen = provider()
    client = app.test_client()
    state, query = _start(client)
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"][0]

    result = _finish(client, state)
    assert result.get("token") and "error" not in result
    assert seen["code_verifier"]                        # PKCE verifier reached the exchange
    user = db.users.find_one({"email": EMAIL})
    assert user["email_verified"] is True and user["auth_providers"] == ["google"]
    assert user["oauth_identities"]["google"]["subject"] == "sub-1"
    assert "password_hash" not in user


def test_the_state_cookie_is_httponly_and_scoped(app, db, provider):
    provider()
    resp = app.test_client().get("/api/auth/oauth/google/authorize")
    cookie = resp.headers["Set-Cookie"]
    assert cookie.startswith("aspirely_oauth=")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Path=/api/auth/oauth" in cookie


def test_redirect_mode_sends_the_browser_straight_to_the_provider(app, db):
    resp = app.test_client().get("/api/auth/oauth/google/authorize?redirect=1")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("https://accounts.google.com/")
    assert "aspirely_oauth=" in resp.headers["Set-Cookie"]

    resp = app.test_client().get("/api/auth/oauth/microsoft/authorize?redirect=1")
    assert resp.status_code == 302                      # not configured: back to the app, with why
    assert resp.headers["Location"].startswith("https://app.test/oauth/callback#error=provider_not_configured")


def test_another_browser_cannot_finish_the_flow(app, db, provider):
    """Login CSRF: the attacker starts a flow and gives the victim the
    callback link. The victim's browser never held the cookie."""
    provider(email="attacker@example.com", subject="attacker-sub")
    attacker = app.test_client()
    state, _ = _start(attacker)

    victim = app.test_client()
    result = _finish(victim, state)
    assert result == {"error": "invalid_state", "provider": "google"}
    assert db.users.count_documents({}) == 0


def test_a_state_works_once(app, db, provider):
    provider()
    client = app.test_client()
    state, _ = _start(client)
    assert _finish(client, state).get("token")
    assert _finish(client, state)["error"] == "invalid_state"


def test_two_tabs_in_one_browser_both_finish(app, db, provider):
    provider()
    client = app.test_client()
    first, _ = _start(client)
    second, _ = _start(client)
    assert _finish(client, first).get("token")
    assert _finish(client, second).get("token")


def test_a_provider_error_reaches_the_app_as_a_safe_token(app, db):
    client = app.test_client()
    state, _ = _start(client)
    assert _finish(client, state, error="access_denied")["error"] == "access_denied"
    state, _ = _start(client)
    result = _finish(client, state, error="<script>alert(1)</script>&token=forged")
    assert result["error"] == "scriptalert1scripttokenforged" and "token" not in result


# ---------------------------------------------------------------------------
# Linking rules
# ---------------------------------------------------------------------------
def test_an_unverified_provider_email_never_links_to_an_existing_account(app, db, provider):
    """The takeover: the address is the victim's, the provider account is not."""
    _password_account(db, verified=True)
    provider(subject="attacker-sub", email_verified=False)
    client = app.test_client()
    state, _ = _start(client)

    result = _finish(client, state)
    assert result == {"error": "email_unverified", "provider": "google"}
    user = db.users.find_one({"email": EMAIL})
    assert "oauth_identities" not in user and user["auth_providers"] == ["password"]


def test_an_unverified_provider_email_never_creates_an_account(app, db, provider):
    """An account on an unproven address would be waiting for its real owner
    to be linked into it."""
    provider(email_verified=False)
    client = app.test_client()
    state, _ = _start(client)
    assert _finish(client, state)["error"] == "email_unverified"
    assert db.users.count_documents({}) == 0


def test_a_verified_email_links_and_keeps_a_proven_password(app, db, provider):
    _password_account(db, verified=True)
    provider()
    client = app.test_client()
    state, _ = _start(client)
    assert _finish(client, state).get("token")

    user = db.users.find_one({"email": EMAIL})
    assert set(user["auth_providers"]) == {"password", "google"}
    assert user["password_hash"]
    assert db.users.count_documents({}) == 1


def test_a_verified_email_links_and_removes_an_unproven_password(app, db, provider):
    """Someone registered this address before its owner. The provider now
    vouches for the owner, so the squatter's password stops working."""
    _password_account(db, verified=False)
    provider()
    client = app.test_client()
    state, _ = _start(client)
    assert _finish(client, state).get("token")

    user = db.users.find_one({"email": EMAIL})
    assert "password_hash" not in user and user["auth_providers"] == ["google"]
    assert user["email_verified"] is True
    resp = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 400 and resp.get_json()["code"] == "no_password"


def test_a_linked_subject_signs_in_even_if_the_provider_email_changed(app, db, provider):
    provider()
    client = app.test_client()
    state, _ = _start(client)
    _finish(client, state)

    provider(email="renamed@example.com", email_verified=False)
    state, _ = _start(client)
    assert _finish(client, state).get("token")
    assert db.users.count_documents({}) == 1 and db.users.find_one({"email": EMAIL})


def test_no_email_from_the_provider_is_refused(app, db, provider):
    provider(email=None, email_verified=False)
    client = app.test_client()
    state, _ = _start(client)
    assert _finish(client, state)["error"] == "no_email"


def test_a_closed_account_cannot_come_back_through_a_provider(app, db, provider):
    # by linked subject
    db.users.insert_one({"email": EMAIL, "is_deleted": True, "auth_providers": ["google"],
                         "oauth_identities": {"google": {"subject": "sub-1", "email": EMAIL}}})
    provider()
    client = app.test_client()
    state, _ = _start(client)
    assert _finish(client, state)["error"] == "account_closed"

    # by email, with a provider account the closed one never saw
    provider(subject="another-sub")
    state, _ = _start(client)
    assert _finish(client, state)["error"] == "account_closed"
    assert db.users.count_documents({}) == 1


def test_apple_never_links_by_email(app, db, oauth):
    """Apple may hand out a private relay address; it is never merged."""
    _password_account(db, verified=True)
    with pytest.raises(oauth.SignInRefused) as refused:
        oauth.upsert_user("apple", {"subject": "apple-sub", "email": EMAIL,
                                    "email_verified": True, "name": None})
    assert refused.value.code == "link_not_allowed"
    assert "oauth_identities" not in db.users.find_one({"email": EMAIL})


def test_microsoft_vouches_only_with_xms_edov(oauth):
    """A work account's `email` claim is whatever its tenant admin typed."""
    info = {"email": EMAIL, "email_verified": True}
    assert oauth.email_is_vouched("microsoft", info, {}) is False
    assert oauth.email_is_vouched("microsoft", info, {"xms_edov": False}) is False
    assert oauth.email_is_vouched("microsoft", info, {"xms_edov": True}) is True
    assert oauth.email_is_vouched("microsoft", info, {"xms_edov": "true"}) is True
    assert oauth.email_is_vouched("google", {"email_verified": True}, {}) is True
    assert oauth.email_is_vouched("google", {"email_verified": "false"}, {}) is False
    assert oauth.email_is_vouched("google", {}, {"xms_edov": True}) is False


def test_identity_reads_the_id_token_when_userinfo_is_silent(oauth, monkeypatch):
    class Silent:
        status_code = 500

    monkeypatch.setattr(oauth.requests, "get", lambda *a, **k: Silent())
    monkeypatch.setattr(oauth, "_decode_id_token", lambda token: {
        "sub": 12345, "email": "Riya@Example.com", "email_verified": "true", "name": "Riya"})
    identity = oauth.fetch_identity("google", {"access_token": "at", "id_token": "it"})
    assert identity == {"subject": "12345", "email": EMAIL, "email_verified": True, "name": "Riya"}
