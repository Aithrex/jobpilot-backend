"""POST /api/auth/set-password — the in-session password add/change route.

The case that motivates it: an account created with "Continue with Google" has
no ``password_hash``, so ``/login`` refuses it and the only way to get password
sign-in was the reset email. When mail delivery is broken that user is stuck on
social sign-in forever. A valid JWT already proves control of the account, so
this route lets them set a password directly.

Runs against the REAL auth service app (mongomock DB, no network).
"""
import pytest

from _vg_support import load_service_app

from utils import db_connect

PASSWORD = "Str0ng!Pass1"
NEW_PASSWORD = "Even5tr0nger!"

COLLECTIONS = ("users", "user_preferences", "password_reset_tokens", "rate_limit_buckets")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)


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
def auth_app(db):
    return load_service_app("auth")


def _auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def _register(client, email, password=PASSWORD):
    resp = client.post("/api/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()["access_token"]


def _make_social_only(db, client, email):
    """Create the shape the Google OAuth callback produces: a user row with no
    password_hash, then mint a session for it the way the callback does."""
    db.users.insert_one({
        "email": email,
        "auth_providers": ["google"],
        "google_subject": "sub-123",
        "plan": "free",
    })
    with client.application.app_context():
        from flask_jwt_extended import create_access_token
        return create_access_token(identity=email)


# ---------------------------------------------------------------------------
# Social-only accounts: the primary case
# ---------------------------------------------------------------------------

def test_social_only_account_sets_password_without_current_password(auth_app, db):
    email = "google.user@example.com"
    client = auth_app.test_client()
    token = _make_social_only(db, client, email)

    # Password login is refused before a password exists, and says why.
    pre = client.post("/api/auth/login", json={"email": email, "password": NEW_PASSWORD})
    assert pre.status_code == 400
    assert pre.get_json().get("social_only") is True

    resp = client.post(
        "/api/auth/set-password",
        json={"new_password": NEW_PASSWORD},
        headers=_auth_header(token),
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is True
    assert "password" in body["auth_providers"]
    assert "google" in body["auth_providers"]

    # The whole point: email + password sign-in now works.
    login = client.post("/api/auth/login", json={"email": email, "password": NEW_PASSWORD})
    assert login.status_code == 200, login.get_data(as_text=True)
    assert login.get_json()["email"] == email


def test_set_password_requires_a_session(auth_app, db):
    client = auth_app.test_client()
    resp = client.post("/api/auth/set-password", json={"new_password": NEW_PASSWORD})
    # No token at all IS a genuine 401 (jwt_required), unlike an auth *failure*.
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Accounts that already have a password
# ---------------------------------------------------------------------------

def test_change_password_requires_correct_current_password(auth_app, db):
    email = "pw.user@example.com"
    client = auth_app.test_client()
    token = _register(client, email)

    missing = client.post(
        "/api/auth/set-password",
        json={"new_password": NEW_PASSWORD},
        headers=_auth_header(token),
    )
    assert missing.status_code == 400
    assert "current password" in missing.get_json()["error"].lower()

    wrong = client.post(
        "/api/auth/set-password",
        json={"new_password": NEW_PASSWORD, "current_password": "Wr0ng!Pass9"},
        headers=_auth_header(token),
    )
    assert wrong.status_code == 400

    # The old password still works — nothing was changed by the failed attempts.
    assert client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    ).status_code == 200


def test_change_password_succeeds_and_old_password_stops_working(auth_app, db):
    email = "pw.rotate@example.com"
    client = auth_app.test_client()
    token = _register(client, email)

    resp = client.post(
        "/api/auth/set-password",
        json={"new_password": NEW_PASSWORD, "current_password": PASSWORD},
        headers=_auth_header(token),
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)

    assert client.post(
        "/api/auth/login", json={"email": email, "password": NEW_PASSWORD}
    ).status_code == 200
    assert client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    ).status_code == 400


def test_reusing_the_same_password_is_rejected(auth_app, db):
    email = "pw.same@example.com"
    client = auth_app.test_client()
    token = _register(client, email)

    resp = client.post(
        "/api/auth/set-password",
        json={"new_password": PASSWORD, "current_password": PASSWORD},
        headers=_auth_header(token),
    )
    assert resp.status_code == 400


def test_weak_password_is_rejected(auth_app, db):
    email = "pw.weak@example.com"
    client = auth_app.test_client()
    token = _register(client, email)

    resp = client.post(
        "/api/auth/set-password",
        json={"new_password": "short", "current_password": PASSWORD},
        headers=_auth_header(token),
    )
    assert resp.status_code == 400
    # Unchanged.
    assert client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    ).status_code == 200


# ---------------------------------------------------------------------------
# Interaction with the emailed reset flow
# ---------------------------------------------------------------------------

def test_setting_a_password_burns_pending_reset_tokens(auth_app, db):
    """A reset link already sitting in an inbox must not be able to overwrite
    the password the user just chose in-session."""
    email = "pw.pending@example.com"
    client = auth_app.test_client()
    token = _register(client, email)

    assert client.post(
        "/api/auth/request-password-reset", json={"email": email}
    ).status_code == 200
    assert db.password_reset_tokens.count_documents({"user_email": email, "used": False}) == 1

    assert client.post(
        "/api/auth/set-password",
        json={"new_password": NEW_PASSWORD, "current_password": PASSWORD},
        headers=_auth_header(token),
    ).status_code == 200

    assert db.password_reset_tokens.count_documents({"user_email": email, "used": False}) == 0
