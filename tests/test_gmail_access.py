"""Who may connect Gmail while Google limits the app to listed test accounts.

In Testing, a Google account that is not on the project's test-user list is
stopped on Google's own page. The app therefore asks first: a person requests
access, the owner adds the address at Google and records it here, and only then
is Connect offered. Offline: mongomock, no Google, no email sent."""
import pytest

from _vg_support import current_db, load_service_app
from services import gmail_access_service as access
from services import gmail_service as gs

USER = "asks@example.test"
ADMIN = "owner@example.test"
ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "client-id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
    "GOOGLE_OAUTH_REDIRECT_URI": "https://app.test/oauth/gmail/callback",
    "GMAIL_TOKEN_ENCRYPTION_KEY": "test-key",
    "GMAIL_OAUTH_ENABLED": "true",
}


@pytest.fixture
def db(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    for key in ("GMAIL_ACCESS_GATE", "GOOGLE_OAUTH_CONSENT_MODE", "GMAIL_ACCESS_NOTIFY_EMAIL"):
        monkeypatch.delenv(key, raising=False)
    db = current_db()
    for name in ("gmail_access", "gmail_connections", "gmail_oauth_states", "users", "tailoring_records"):
        db[name].delete_many({})
    db.users.insert_one({"email": ADMIN, "is_admin": True})
    from utils.security import get_rate_limiter
    get_rate_limiter()._requests.clear()
    sent = []
    from services import email_service
    monkeypatch.setattr(email_service, "send_email",
                        lambda to, subject, *_, **__: sent.append((to, subject)) or True)
    db.sent = sent
    return db


@pytest.fixture
def resume(db):
    return load_service_app("resume")


def auth(app, email=USER):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {"Authorization": f"Bearer {create_access_token(identity=email)}"}


def status(app, email=USER):
    return app.test_client().get("/api/resume/gmail/status", headers=auth(app, email)).get_json()


# --- the gate ---------------------------------------------------------------

def test_the_gate_follows_the_consent_screen_and_can_be_overridden(db, monkeypatch):
    assert access.gate() == "invite"
    monkeypatch.setenv("GOOGLE_OAUTH_CONSENT_MODE", "production")
    assert access.gate() == "open"
    monkeypatch.setenv("GMAIL_ACCESS_GATE", "invite")
    assert access.gate() == "invite"


def test_nobody_is_sent_to_google_before_their_address_is_added(resume):
    client = resume.test_client()
    assert status(resume)["access"] == {
        "mode": "testing", "expires_at": None, "gate": "invite", "state": "none",
        "google_email": None, "requested_at": None, "decided_at": None}
    refused = client.get("/api/resume/gmail/auth-url", headers=auth(resume))
    assert refused.status_code == 403, "an access answer is never a 401: that logs the person out"
    assert refused.get_json()["code"] == "gmail_access_required"
    assert current_db().gmail_oauth_states.count_documents({}) == 0


def test_asking_is_recorded_once_and_tells_the_owner(resume, db, monkeypatch):
    monkeypatch.setenv("GMAIL_ACCESS_NOTIFY_EMAIL", "owner@example.test")
    client = resume.test_client()
    first = client.post("/api/resume/gmail/access-request", headers=auth(resume),
                        json={"google_email": "  Priya.Sharma@Gmail.test "})
    assert first.status_code == 200
    assert first.get_json()["access"]["state"] == "requested"
    assert first.get_json()["access"]["google_email"] == "priya.sharma@gmail.test"
    again = client.post("/api/resume/gmail/access-request", headers=auth(resume),
                        json={"google_email": "priya.sharma@gmail.test"})
    assert again.get_json()["access"]["state"] == "requested"
    assert db.gmail_access.count_documents({}) == 1
    assert db.sent == [("owner@example.test", "Gmail access request: priya.sharma@gmail.test")]
    assert status(resume)["access"]["state"] == "requested"
    assert client.get("/api/resume/gmail/auth-url", headers=auth(resume)).status_code == 403


def test_the_account_address_is_used_when_none_is_given(resume, db):
    body = resume.test_client().post("/api/resume/gmail/access-request", headers=auth(resume)).get_json()
    assert body["access"]["google_email"] == USER


@pytest.mark.parametrize("bad", ["not an address", "a@b", {"$ne": ""}, 42])
def test_a_bad_address_is_a_400_with_a_sentence_and_is_never_stored(resume, db, bad):
    response = resume.test_client().post("/api/resume/gmail/access-request", headers=auth(resume),
                                         json={"google_email": bad})
    assert response.status_code == 400 and response.get_json()["code"] == "gmail_address_invalid"
    assert db.gmail_access.count_documents({}) == 0


def test_asking_over_and_over_is_a_429_not_a_logout(resume):
    client = resume.test_client()
    codes = [client.post("/api/resume/gmail/access-request", headers=auth(resume),
                         json={"google_email": f"p{i}@gmail.test"}).status_code for i in range(8)]
    assert codes[:6] == [200] * 6 and codes[-1] == 429


# --- the owner approves -----------------------------------------------------

def test_approval_opens_connect_preselects_the_account_and_emails_the_person(resume, db):
    access.request_access(USER, "priya.sharma@gmail.test")
    row = access.approve(ADMIN, user_email=USER)
    assert row["state"] == "approved" and row["notified"] is True
    assert db.sent[-1] == (USER, "Gmail updates are ready on Aspirely")
    assert status(resume)["access"]["state"] == "approved"
    response = resume.test_client().get("/api/resume/gmail/auth-url", headers=auth(resume))
    assert response.status_code == 200
    assert "login_hint=priya.sharma%40gmail.test" in response.get_json()["auth_url"]


def test_an_address_recorded_ahead_of_time_lets_its_owner_straight_in(resume, db):
    access.approve(ADMIN, google_email="Asks@Example.test")
    assert status(resume)["access"]["state"] == "approved"
    assert resume.test_client().get("/api/resume/gmail/auth-url", headers=auth(resume)).status_code == 200


def test_a_person_who_asks_for_an_address_already_added_does_not_wait(db):
    access.approve(ADMIN, google_email="early@gmail.test")
    assert access.request_access(USER, "early@gmail.test")["state"] == "approved"
    assert db.gmail_access.count_documents({}) == 1


def test_someone_already_connected_can_always_reconnect(resume, db):
    db.gmail_connections.insert_one({"user_email": USER, "gmail_address": "me@gmail.test",
                                     "refresh_token_enc": "x", "needs_reconnect": True})
    assert status(resume)["access"]["state"] == "approved"
    assert resume.test_client().get("/api/resume/gmail/auth-url", headers=auth(resume)).status_code == 200


def test_changing_the_address_goes_back_to_waiting(db):
    access.request_access(USER, "first@gmail.test")
    access.approve(ADMIN, user_email=USER)
    assert access.request_access(USER, "second@gmail.test")["state"] == "requested"
    assert access.may_connect(USER) is False


def test_once_google_lists_the_app_publicly_everyone_is_let_in(resume, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CONSENT_MODE", "production")
    assert status(resume)["access"]["gate"] == "open"
    assert status(resume)["access"]["state"] == "approved"
    response = resume.test_client().get("/api/resume/gmail/auth-url", headers=auth(resume))
    assert response.status_code == 200 and "login_hint" not in response.get_json()["auth_url"]


# --- the owner's list -------------------------------------------------------

def test_the_owner_sees_who_is_waiting_and_how_many_places_are_used(db):
    auth_app = load_service_app("auth")
    client = auth_app.test_client()
    access.request_access(USER, "priya.sharma@gmail.test")
    access.approve(ADMIN, google_email="early@gmail.test")
    db.gmail_connections.insert_one({"user_email": "old@example.test", "gmail_address": "old@gmail.test"})

    assert client.get("/api/admin/gmail-access", headers=auth(auth_app, USER)).status_code == 403
    overview = client.get("/api/admin/gmail-access", headers=auth(auth_app, ADMIN)).get_json()
    assert overview["gate"] == "invite" and overview["limit"] == 100
    assert [r["google_email"] for r in overview["pending"]] == ["priya.sharma@gmail.test"]
    assert overview["used"] == 2, "one recorded approval plus one mailbox connected before the list"
    assert overview["connected_without_a_record"] == ["old@gmail.test"]

    approved = client.post("/api/admin/gmail-access/approve", headers=auth(auth_app, ADMIN),
                           json={"user_email": USER}).get_json()
    assert approved["notified"] is True and approved["pending"] == [] and approved["used"] == 3

    removed = client.post("/api/admin/gmail-access/remove", headers=auth(auth_app, ADMIN),
                          json={"user_email": USER}).get_json()
    assert removed["removed"] is True and removed["used"] == 2
    assert access.access_for(USER)["state"] == "none"


@pytest.mark.parametrize("body", [{"google_email": "nope"}, {"user_email": "nobody@example.test"}, {}, []])
def test_owner_mistakes_are_400s_with_a_sentence(db, body):
    auth_app = load_service_app("auth")
    response = auth_app.test_client().post("/api/admin/gmail-access/approve",
                                           headers=auth(auth_app, ADMIN), json=body)
    assert response.status_code == 400 and response.get_json()["error"]
