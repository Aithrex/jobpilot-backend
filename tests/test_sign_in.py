"""Sign-in, sign-up and the emailed one-time code (`/api/auth`).

What these hold, in the order the 2026-11-15 review raised them:

  * no account exists, and no password works, for an address nobody has
    proven they own (the register-first takeover);
  * a code is six digits, stored hashed, lives ten minutes, works once and
    dies after five wrong tries;
  * the limits are shared across processes and every 429 has one shape;
  * `/validate-password` is gone and `/reset-password` is limited;
  * closed accounts stay closed;
  * no sign-in failure is ever a 401 (the frontend logs out on any 401).

Runs against the REAL auth app on mongomock. No mail leaves the process:
`mail` records what would have been sent.
"""
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import load_service_app, sign_up

from utils import db_connect

EMAIL = "riya@example.com"
PASSWORD = "Str0ng!Pass1"
OTHER_PASSWORD = "An0ther!Pass2"

COLLECTIONS = ("users", "user_preferences", "password_reset_tokens",
               "auth_email_codes", "rate_limit_buckets", "email_suppressions")


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
def mail(monkeypatch):
    """Every code that would have been mailed, newest last."""
    import services.email_service as es
    sent = []
    monkeypatch.setattr(es, "send_login_code",
                        lambda to, code, minutes=10: sent.append((to, code)) or True)
    monkeypatch.setattr(es, "send_email", lambda *a, **k: True)
    return sent


@pytest.fixture()
def client(db, mail):
    return load_service_app("auth").test_client()


def _legacy_password_account(db, email=EMAIL, password=PASSWORD, **extra):
    """The shape `/register` wrote before 2026-11: a working password on an
    address nobody had verified."""
    from models.user import User
    doc = {"email": email, "password_hash": User.hash_password(password),
           "auth_providers": ["password"], "email_verified": False,
           "plan": "free", "status": "active", "login_attempts": 0}
    doc.update(extra)
    db.users.insert_one(doc)


def _skip_cooldown(db, email=EMAIL):
    db.auth_email_codes.update_many(
        {"email": email}, {"$set": {"created_at": datetime.now(timezone.utc) - timedelta(minutes=2)}})


def _request(client, email=EMAIL):
    return client.post("/api/auth/otp/request", json={"email": email})


def _verify(client, code, email=EMAIL, **extra):
    return client.post("/api/auth/otp/verify", json={"email": email, "code": code, **extra})


# ---------------------------------------------------------------------------
# The code itself
# ---------------------------------------------------------------------------
def test_request_mails_six_digits_and_stores_only_a_hash(client, db, mail):
    resp = _request(client)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True and body["resend_after"] == 60 and body["expires_in"] == 600

    (to, code), = mail
    assert to == EMAIL and len(code) == 6 and code.isdigit()
    row = db.auth_email_codes.find_one({"email": EMAIL})
    assert code not in str(row)                      # hashed, never the digits
    assert row["used"] is False and row["attempts"] == 0
    assert db.users.find_one({"email": EMAIL}) is None  # asking creates nothing


def test_request_answers_the_same_for_a_known_and_an_unknown_address(client, db, mail):
    sign_up(client, "known@example.com", PASSWORD)
    _skip_cooldown(db, "known@example.com")
    known = _request(client, "known@example.com")
    unknown = _request(client, "nobody@example.com")
    assert known.status_code == unknown.status_code == 200
    assert known.get_json() == unknown.get_json()


def test_a_new_address_needs_consent_and_the_code_survives_the_refusal(client, db, mail):
    _request(client)
    code = mail[-1][1]

    resp = _verify(client, code)
    assert resp.status_code == 400 and resp.get_json()["code"] == "consent_required"
    assert db.users.find_one({"email": EMAIL}) is None
    assert db.auth_email_codes.find_one({"email": EMAIL})["attempts"] == 0   # not a wasted try

    resp = _verify(client, code, age_confirmed=True, terms_version="2026-11-11")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["email"] == EMAIL and body["access_token"] and body["created"] is True
    user = db.users.find_one({"email": EMAIL})
    assert user["email_verified"] is True
    assert user["auth_providers"] == ["email_code"] and "password_hash" not in user
    assert user["terms"]["version"] == "2026-11-11" and user["age_confirmed"] is True
    assert db.user_preferences.find_one({"user_email": EMAIL})["onboarding_complete"] is False


def test_under_age_is_refused(client, db, mail):
    _request(client)
    resp = _verify(client, mail[-1][1], age_confirmed=False)
    assert resp.status_code == 400 and resp.get_json()["error"] == "age_requirement"
    assert db.users.find_one({"email": EMAIL}) is None


def test_a_code_signs_in_an_existing_account(client, db, mail):
    sign_up(client, EMAIL, PASSWORD, role="Data Scientist")
    _skip_cooldown(db)
    _request(client)
    resp = _verify(client, mail[-1][1])
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["created"] is False and body["role"] == "Data Scientist"
    assert "password_removed" not in body
    assert db.users.find_one({"email": EMAIL})["password_hash"]


def test_a_code_works_once(client, db, mail):
    sign_up(client, EMAIL, PASSWORD)
    _skip_cooldown(db)
    _request(client)
    code = mail[-1][1]
    assert _verify(client, code).status_code == 200
    again = _verify(client, code)
    assert again.status_code == 400 and again.get_json()["code"] == "code_expired"


def test_wrong_codes_count_down_then_kill_the_code(client, db, mail):
    sign_up(client, EMAIL, PASSWORD)
    _skip_cooldown(db)
    _request(client)
    right = mail[-1][1]
    wrong = "000000" if right != "000000" else "111111"

    for left in (4, 3, 2, 1):
        resp = _verify(client, wrong)
        assert resp.status_code == 400
        assert resp.get_json()["code"] == "code_invalid"
        assert resp.get_json()["attempts_left"] == left
    resp = _verify(client, wrong)
    assert resp.status_code == 400 and resp.get_json()["code"] == "code_locked"
    # The right code is dead too: five tries is the whole allowance.
    resp = _verify(client, right)
    assert resp.status_code == 400 and resp.get_json()["code"] == "code_expired"


def test_an_expired_code_is_refused(client, db, mail):
    sign_up(client, EMAIL, PASSWORD)
    _skip_cooldown(db)
    _request(client)
    db.auth_email_codes.update_many(
        {"email": EMAIL, "used": False},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    resp = _verify(client, mail[-1][1])
    assert resp.status_code == 400 and resp.get_json()["code"] == "code_expired"


def test_a_pasted_code_with_a_space_works(client, db, mail):
    sign_up(client, EMAIL, PASSWORD)
    _skip_cooldown(db)
    _request(client)
    code = mail[-1][1]
    assert _verify(client, f"{code[:3]} {code[3:]}").status_code == 200


def test_resend_waits_out_the_cooldown_then_replaces_the_code(client, db, mail):
    assert _request(client).status_code == 200
    first = mail[-1][1]

    resp = _request(client)
    assert resp.status_code == 429
    body = resp.get_json()
    assert body["code"] == "code_cooldown" and 1 <= body["retry_after"] <= 60
    assert resp.headers["Retry-After"] == str(body["retry_after"])
    assert len(mail) == 1

    _skip_cooldown(db)
    assert _request(client).status_code == 200
    second = mail[-1][1]
    assert db.auth_email_codes.count_documents({"email": EMAIL, "used": False}) == 1
    if first != second:
        resp = _verify(client, first, age_confirmed=True)
        assert resp.status_code == 400 and resp.get_json()["code"] == "code_invalid"
    assert _verify(client, second, age_confirmed=True).status_code == 200


def test_one_address_gets_five_codes_an_hour(client, db, mail):
    for _ in range(5):
        assert _request(client).status_code == 200
        _skip_cooldown(db)
    resp = _request(client)
    assert resp.status_code == 429
    assert resp.get_json()["code"] == "rate_limited" and resp.headers["Retry-After"]
    assert len(mail) == 5


def test_a_mail_failure_is_told_to_the_person(client, db, monkeypatch):
    """Production mail has one provider and swallows its errors. A code that
    was never sent must not look like one that was."""
    import services.email_service as es
    monkeypatch.setattr(es, "email_configured", lambda: True)
    monkeypatch.setattr(es, "send_login_code", lambda *a, **k: False)

    resp = _request(client)
    assert resp.status_code == 503 and resp.get_json()["code"] == "email_send_failed"
    assert db.auth_email_codes.count_documents({"email": EMAIL}) == 0   # nothing to wait for

    monkeypatch.setattr(es, "send_login_code", lambda *a, **k: True)
    assert _request(client).status_code == 200                          # and no cooldown


def test_malformed_input_is_a_400_not_a_crash(client, db):
    for body in ({"email": {"$gt": ""}}, {"email": "not-an-email"}, {}):
        resp = client.post("/api/auth/otp/request", json=body)
        assert resp.status_code == 400 and resp.get_json()["code"] == "invalid_email"
    for body in ({"email": EMAIL, "code": {"$ne": ""}}, {"email": EMAIL, "code": "12345"},
                 {"email": {"$gt": ""}, "code": "123456"}):
        resp = client.post("/api/auth/otp/verify", json=body)
        assert resp.status_code == 400 and resp.get_json()["code"] == "code_invalid"


# ---------------------------------------------------------------------------
# Sign-up with a password
# ---------------------------------------------------------------------------
def test_register_stores_nothing_until_the_code_is_entered(client, db, mail):
    resp = client.post("/api/auth/register", json={
        "email": EMAIL, "password": PASSWORD, "age_confirmed": True, "terms_version": "2026-11-11"})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["verification_required"] is True and body["email"] == EMAIL
    assert "access_token" not in body
    assert db.users.find_one({"email": EMAIL}) is None

    # Nothing to sign in to yet, and the answer does not say why.
    login = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 400 and login.get_json()["code"] == "invalid_credentials"

    done = _verify(client, mail[-1][1], password=PASSWORD, age_confirmed=True,
                   terms_version="2026-11-11")
    assert done.status_code == 200 and done.get_json()["created"] is True
    user = db.users.find_one({"email": EMAIL})
    assert user["email_verified"] is True and user["auth_providers"] == ["password"]

    login = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200
    assert set(login.get_json()) == {"access_token", "email"}       # the flat payload


def test_register_checks_the_password_before_mailing(client, db, mail):
    resp = client.post("/api/auth/register", json={"email": EMAIL, "password": "short"})
    assert resp.status_code == 400 and resp.get_json()["code"] == "weak_password"
    assert mail == []

    _request(client)
    resp = _verify(client, mail[-1][1], password="short", age_confirmed=True)
    assert resp.status_code == 400 and resp.get_json()["code"] == "weak_password"
    assert db.users.find_one({"email": EMAIL}) is None


def test_register_on_a_taken_address_says_how_that_account_signs_in(client, db, mail):
    sign_up(client, EMAIL, PASSWORD)
    resp = client.post("/api/auth/register", json={"email": EMAIL, "password": OTHER_PASSWORD})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["code"] == "account_exists" and body["methods"] == ["password"]

    db.users.insert_one({"email": "g@example.com", "auth_providers": ["google"],
                         "oauth_identities": {"google": {"subject": "s1"}}})
    resp = client.post("/api/auth/register", json={"email": "g@example.com", "password": PASSWORD})
    assert resp.status_code == 409 and resp.get_json()["methods"] == ["google"]


# ---------------------------------------------------------------------------
# The register-first takeover
# ---------------------------------------------------------------------------
def test_an_unverified_password_account_cannot_sign_in_until_it_proves_the_email(client, db, mail):
    _legacy_password_account(db)

    wrong = client.post("/api/auth/login", json={"email": EMAIL, "password": OTHER_PASSWORD})
    assert wrong.status_code == 400 and wrong.get_json()["code"] == "invalid_credentials"

    resp = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "email_unverified" and "access_token" not in resp.get_json()

    # The owner finishes with a code and the same password: nothing is lost.
    _request(client)
    done = _verify(client, mail[-1][1], password=PASSWORD)
    assert done.status_code == 200 and "password_removed" not in done.get_json()
    assert db.users.find_one({"email": EMAIL})["email_verified"] is True
    assert client.post("/api/auth/login",
                       json={"email": EMAIL, "password": PASSWORD}).status_code == 200


def test_a_squatters_password_dies_when_the_real_owner_proves_the_email(client, db, mail):
    """Someone registered the victim's address first. The victim arrives and
    signs in with a code. The squatter's password must stop working."""
    _legacy_password_account(db, password=OTHER_PASSWORD)

    _request(client)
    done = _verify(client, mail[-1][1])
    assert done.status_code == 200 and done.get_json()["password_removed"] is True

    user = db.users.find_one({"email": EMAIL})
    assert "password_hash" not in user and "password" not in user["auth_providers"]
    assert user["email_verified"] is True
    resp = client.post("/api/auth/login", json={"email": EMAIL, "password": OTHER_PASSWORD})
    assert resp.status_code == 400 and resp.get_json()["code"] == "no_password"


def test_a_wrong_password_with_the_code_does_not_keep_the_old_one(client, db, mail):
    _legacy_password_account(db, password=OTHER_PASSWORD)
    _request(client)
    done = _verify(client, mail[-1][1], password=PASSWORD)
    assert done.status_code == 200 and done.get_json()["password_removed"] is True
    assert "password_hash" not in db.users.find_one({"email": EMAIL})


def test_a_reset_link_proves_the_email(client, db, mail, monkeypatch):
    import services.email_service as es
    _legacy_password_account(db)
    links = []
    monkeypatch.setattr(es, "send_password_reset", lambda to, link: links.append(link) or True)

    assert client.post("/api/auth/request-password-reset", json={"email": EMAIL}).status_code == 200
    token = links[-1].split("token=")[1]
    resp = client.post("/api/auth/reset-password", json={"token": token, "new_password": OTHER_PASSWORD})
    assert resp.status_code == 200
    assert db.users.find_one({"email": EMAIL})["email_verified"] is True
    assert client.post("/api/auth/login",
                       json={"email": EMAIL, "password": OTHER_PASSWORD}).status_code == 200
    # one link, one use
    again = client.post("/api/auth/reset-password", json={"token": token, "new_password": PASSWORD})
    assert again.status_code == 400


# ---------------------------------------------------------------------------
# Password sign-in
# ---------------------------------------------------------------------------
def test_wrong_password_and_unknown_address_get_one_answer(client, db, mail):
    sign_up(client, EMAIL, PASSWORD)
    wrong = client.post("/api/auth/login", json={"email": EMAIL, "password": OTHER_PASSWORD})
    unknown = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 400
    assert wrong.get_json() == unknown.get_json()
    assert wrong.get_json()["message"] == "Invalid email or password"


def test_eight_wrong_passwords_lock_the_account_and_a_code_unlocks_it(client, db, mail):
    sign_up(client, EMAIL, PASSWORD)
    for _ in range(8):
        assert client.post("/api/auth/login",
                           json={"email": EMAIL, "password": OTHER_PASSWORD}).status_code == 400

    resp = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 429
    body = resp.get_json()
    assert body["code"] == "account_locked" and 1 <= body["retry_after"] <= 15 * 60
    assert resp.headers["Retry-After"] == str(body["retry_after"])

    _skip_cooldown(db)
    _request(client)
    assert _verify(client, mail[-1][1]).status_code == 200
    user = db.users.find_one({"email": EMAIL})
    assert "lockout_until" not in user and user["login_attempts"] == 0
    assert client.post("/api/auth/login",
                       json={"email": EMAIL, "password": PASSWORD}).status_code == 200


def test_an_account_with_no_password_is_pointed_at_what_works(client, db):
    db.users.insert_one({"email": EMAIL, "auth_providers": ["google"], "email_verified": True,
                         "oauth_identities": {"google": {"subject": "s1", "email": EMAIL}}})
    resp = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["code"] == "no_password" and body["social_only"] is True
    assert body["auth_providers"] == ["google"] and "Google" in body["message"]


def test_a_closed_account_stays_closed_on_every_door(client, db, mail):
    sign_up(client, EMAIL, PASSWORD)
    db.users.update_one({"email": EMAIL}, {"$set": {"is_deleted": True}})

    for path, body in (("/api/auth/login", {"email": EMAIL, "password": PASSWORD}),
                       ("/api/auth/otp/request", {"email": EMAIL}),
                       ("/api/auth/otp/verify", {"email": EMAIL, "code": "123456"}),
                       ("/api/auth/register", {"email": EMAIL, "password": PASSWORD})):
        resp = client.post(path, json=body)
        assert resp.status_code == 403, path
        assert resp.get_json()["code"] == "account_closed", path
    assert client.post("/api/auth/check-email", json={"email": EMAIL}).get_json()["closed"] is True
    # and no reset mail goes to it
    assert client.post("/api/auth/request-password-reset", json={"email": EMAIL}).status_code == 200
    assert db.password_reset_tokens.count_documents({}) == 0


def test_check_email_says_how_an_account_signs_in(client, db, mail):
    assert client.post("/api/auth/check-email", json={"email": EMAIL}).get_json() == {"exists": False}
    sign_up(client, EMAIL, PASSWORD)
    assert client.post("/api/auth/check-email", json={"email": EMAIL}).get_json() == {
        "exists": True, "has_password": True, "methods": ["password"]}


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------
def test_validate_password_is_gone(client, db):
    """It tested passwords for any address without touching the lockout."""
    resp = client.post("/api/auth/validate-password", json={"email": EMAIL, "password": PASSWORD})
    assert resp.status_code in (404, 405)


def test_the_login_limit_is_shared_across_processes(client, db, mail):
    """A second worker has an empty in-memory limiter. The count that decides
    is the one in Mongo, so emptying the local one must change nothing."""
    from utils.security import get_rate_limiter
    for _ in range(30):
        client.post("/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
        get_rate_limiter()._requests.clear()          # "another process"
    resp = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert resp.status_code == 429
    assert db.rate_limit_buckets.count_documents({"bucket": "auth:login"}) == 1


@pytest.mark.parametrize("path,body,limit", [
    ("/api/auth/login", {"email": EMAIL, "password": "x"}, 30),
    ("/api/auth/register", {"email": EMAIL, "password": "x"}, 15),
    ("/api/auth/reset-password", {"token": "nope", "new_password": PASSWORD}, 10),
    ("/api/auth/request-password-reset", {"email": EMAIL}, 10),
    ("/api/auth/check-email", {"email": EMAIL}, 30),
    ("/api/auth/otp/verify", {"email": EMAIL, "code": "123456"}, 40),
])
def test_every_429_has_one_shape(client, db, path, body, limit):
    for _ in range(limit):
        assert client.post(path, json=body).status_code != 429
    resp = client.post(path, json=body)
    assert resp.status_code == 429
    payload = resp.get_json()
    assert payload["error"] == "rate_limited" and payload["code"] == "rate_limited"
    assert isinstance(payload["retry_after"], int) and payload["retry_after"] >= 1
    assert payload["message"]
    assert resp.headers["Retry-After"] == str(payload["retry_after"])


def test_no_sign_in_failure_is_ever_a_401(client, db, mail):
    _legacy_password_account(db, email="legacy@example.com")
    sign_up(client, EMAIL, PASSWORD)
    attempts = [
        ("/api/auth/login", {"email": EMAIL, "password": OTHER_PASSWORD}),
        ("/api/auth/login", {"email": "nobody@example.com", "password": PASSWORD}),
        ("/api/auth/login", {"email": "legacy@example.com", "password": PASSWORD}),
        ("/api/auth/login", {}),
        ("/api/auth/register", {"email": EMAIL, "password": PASSWORD}),
        ("/api/auth/otp/verify", {"email": EMAIL, "code": "000000"}),
        ("/api/auth/otp/verify", {"email": "new@example.com", "code": "000000"}),
        ("/api/auth/otp/request", {"email": "bad"}),
        ("/api/auth/reset-password", {"token": "nope", "new_password": PASSWORD}),
    ]
    for path, body in attempts:
        resp = client.post(path, json=body)
        assert resp.status_code in (400, 403, 409, 429), (path, resp.status_code)
        assert resp.get_json().get("error"), path
