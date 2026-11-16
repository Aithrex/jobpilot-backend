"""Gmail connect, sync start, job slices and undo, through the real routes.

Every failure must answer with a sentence for the user plus a stable `code`,
never exception text, and never a 401 for a Google problem (the frontend logs
the user out on any 401). Offline: mongomock, no Google."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from _vg_support import current_db, load_service_app
from services import gmail_service as gs

USER = "gmail-connect@example.test"
OTHER = "someone-else@example.test"
ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "client-id",
    "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
    "GOOGLE_OAUTH_REDIRECT_URI": "https://app.test/oauth/gmail/callback",
    "GMAIL_TOKEN_ENCRYPTION_KEY": "test-key",
    "GMAIL_OAUTH_ENABLED": "true",
}


class _Response:
    def __init__(self, status_code, body):
        self.status_code, self._body, self.text, self.headers = status_code, body, str(body), {}

    def json(self):
        return self._body


@pytest.fixture
def app(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    db = current_db()
    for name in ("gmail_connections", "gmail_suggestions", "gmail_oauth_states",
                 "tailoring_records", "resume_jobs"):
        db[name].delete_many({})
    monkeypatch.setattr(gs, "revoke_refresh_token", lambda *_: None)
    monkeypatch.setattr(gs, "_userinfo", lambda _: {"email": "me@gmail.test"})
    from utils.security import get_rate_limiter
    get_rate_limiter()._requests.clear()
    return load_service_app("resume")


def auth(app, email=USER):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {"Authorization": f"Bearer {create_access_token(identity=email)}"}


def google(monkeypatch, status=200, body=None):
    body = body if body is not None else {
        "access_token": "access", "refresh_token": "refresh", "scope": " ".join(gs.GMAIL_SCOPES)}
    monkeypatch.setattr(gs.requests, "post", lambda *_, **__: _Response(status, body))


def issue_state(app):
    response = app.test_client().get("/api/resume/gmail/auth-url", headers=auth(app))
    assert response.status_code == 200
    return parse_qs(urlparse(response.get_json()["auth_url"]).query)


# --- auth-url ----------------------------------------------------------------

def test_auth_url_asks_google_for_read_only_offline_access_with_a_bound_state(app):
    query = issue_state(app)
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.readonly openid email"]
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["redirect_uri"] == [ENV["GOOGLE_OAUTH_REDIRECT_URI"]]
    stored = current_db().gmail_oauth_states.find_one({"state": query["state"][0]})
    assert stored["user_email"] == USER and stored["expires_at"] > stored["created_at"]


def test_auth_url_needs_a_signed_in_user(app):
    assert app.test_client().get("/api/resume/gmail/auth-url").status_code == 401


@pytest.mark.parametrize("missing", ["GMAIL_TOKEN_ENCRYPTION_KEY", "GOOGLE_OAUTH_CLIENT_SECRET",
                                     "GOOGLE_OAUTH_REDIRECT_URI"])
def test_connect_is_not_offered_when_it_could_not_finish(app, monkeypatch, missing):
    monkeypatch.delenv(missing)
    client = app.test_client()
    status = client.get("/api/resume/gmail/status", headers=auth(app)).get_json()
    assert status["configured"] is False and status["state"] == "unavailable"
    response = client.get("/api/resume/gmail/auth-url", headers=auth(app))
    assert response.status_code == 503 and response.get_json()["code"] == "gmail_unavailable"
    assert current_db().gmail_oauth_states.count_documents({}) == 0


def test_the_kill_switch_hides_connect_and_refuses_the_callback(app, monkeypatch):
    state = issue_state(app)["state"][0]
    monkeypatch.setenv("GMAIL_OAUTH_ENABLED", "false")
    client = app.test_client()
    assert client.get("/api/resume/gmail/status", headers=auth(app)).get_json()["state"] == "unavailable"
    response = client.post("/api/resume/gmail/callback", headers=auth(app),
                           json={"code": "c", "state": state})
    assert response.status_code == 503


# --- callback ----------------------------------------------------------------

def test_callback_stores_an_encrypted_token_and_reports_the_mailbox(app, monkeypatch):
    google(monkeypatch)
    state = issue_state(app)["state"][0]
    response = app.test_client().post("/api/resume/gmail/callback", headers=auth(app),
                                      json={"code": "good-code", "state": state})
    assert response.status_code == 200
    assert response.get_json()["gmail_address"] == "me@gmail.test"
    conn = current_db().gmail_connections.find_one({"user_email": USER})
    assert "refresh" not in conn["refresh_token_enc"]
    assert gs._fernet().decrypt(conn["refresh_token_enc"].encode()).decode() == "refresh"
    status = app.test_client().get("/api/resume/gmail/status", headers=auth(app)).get_json()
    assert status["state"] == "never_synced" and status["connected"] is True
    assert status["access"]["mode"] == "testing" and status["access"]["expires_at"]


@pytest.mark.parametrize("body", [{}, {"code": "c"}, {"state": "s"}, {"code": 5, "state": ["s"]}, ["c", "s"]])
def test_callback_without_code_and_state_is_a_clear_400(app, body):
    response = app.test_client().post("/api/resume/gmail/callback", headers=auth(app), json=body)
    assert response.status_code == 400 and response.get_json()["code"] == "missing_params"


def test_a_state_is_single_use(app, monkeypatch):
    google(monkeypatch)
    state = issue_state(app)["state"][0]
    client = app.test_client()
    assert client.post("/api/resume/gmail/callback", headers=auth(app),
                       json={"code": "c", "state": state}).status_code == 200
    again = client.post("/api/resume/gmail/callback", headers=auth(app), json={"code": "c", "state": state})
    assert again.status_code == 400 and again.get_json()["code"] == "state_invalid"


def test_a_state_issued_to_one_account_cannot_link_a_mailbox_to_another(app, monkeypatch):
    """The consent-link attack: a link made by OTHER, completed by USER, must
    not attach USER's mailbox to anyone."""
    google(monkeypatch)
    query = parse_qs(urlparse(app.test_client().get(
        "/api/resume/gmail/auth-url", headers=auth(app, OTHER)).get_json()["auth_url"]).query)
    response = app.test_client().post("/api/resume/gmail/callback", headers=auth(app, USER),
                                      json={"code": "victims-code", "state": query["state"][0]})
    assert response.status_code == 400 and response.get_json()["code"] == "state_invalid"
    assert current_db().gmail_connections.count_documents({}) == 0


def test_the_callback_still_needs_the_session(app, monkeypatch):
    google(monkeypatch)
    state = issue_state(app)["state"][0]
    response = app.test_client().post("/api/resume/gmail/callback", json={"code": "c", "state": state})
    assert response.status_code == 401
    # Not burned: signing in again within the 10 minutes can still finish it.
    assert current_db().gmail_oauth_states.count_documents({"state": state}) == 1


def test_an_expired_state_is_refused(app, monkeypatch):
    google(monkeypatch)
    state = issue_state(app)["state"][0]
    current_db().gmail_oauth_states.update_one({"state": state}, {"$set": {
        "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1)}})
    response = app.test_client().post("/api/resume/gmail/callback", headers=auth(app),
                                      json={"code": "c", "state": state})
    assert response.status_code == 400 and response.get_json()["code"] == "state_invalid"


def test_a_code_google_refuses_is_a_400_with_a_next_step(app, monkeypatch):
    google(monkeypatch, 400, {"error": "invalid_grant", "error_description": "Bad Request: internal detail"})
    response = app.test_client().post("/api/resume/gmail/callback", headers=auth(app),
                                      json={"code": "old", "state": issue_state(app)["state"][0]})
    data = response.get_json()
    assert response.status_code == 400 and data["code"] == "code_rejected"
    assert "Connect Gmail again" in data["error"] and "internal detail" not in data["error"]


def test_unticking_gmail_on_googles_screen_does_not_store_a_useless_connection(app, monkeypatch):
    google(monkeypatch, body={"access_token": "a", "refresh_token": "r", "scope": "openid email"})
    response = app.test_client().post("/api/resume/gmail/callback", headers=auth(app),
                                      json={"code": "c", "state": issue_state(app)["state"][0]})
    assert response.status_code == 400 and response.get_json()["code"] == "permission_missing"
    assert current_db().gmail_connections.count_documents({}) == 0


def test_no_refresh_token_is_explained(app, monkeypatch):
    google(monkeypatch, body={"access_token": "a", "scope": " ".join(gs.GMAIL_SCOPES)})
    response = app.test_client().post("/api/resume/gmail/callback", headers=auth(app),
                                      json={"code": "c", "state": issue_state(app)["state"][0]})
    assert response.status_code == 400 and response.get_json()["code"] == "no_refresh_token"


def test_the_callback_never_shows_a_raw_exception(app, monkeypatch):
    def explode(*_, **__):
        raise RuntimeError("pymongo.errors.OperationFailure: mongodb+srv://user:hunter2@cluster")
    monkeypatch.setattr(gs, "exchange_code_for_tokens", explode)
    response = app.test_client().post("/api/resume/gmail/callback", headers=auth(app),
                                      json={"code": "c", "state": issue_state(app)["state"][0]})
    text = response.get_data(as_text=True)
    assert response.status_code == 500 and response.get_json()["code"] == "gmail_error"
    assert "hunter2" not in text and "pymongo" not in text


# --- disconnect --------------------------------------------------------------

def test_disconnect_revokes_removes_the_token_and_drops_pending_reviews(app, monkeypatch):
    google(monkeypatch)
    client = app.test_client()
    client.post("/api/resume/gmail/callback", headers=auth(app),
                json={"code": "c", "state": issue_state(app)["state"][0]})
    db = current_db()
    db.gmail_suggestions.insert_many([
        {"user_email": USER, "suggestion_id": "pending", "applied": False, "dismissed": False},
        {"user_email": USER, "suggestion_id": "done", "applied": True, "dismissed": False},
        {"user_email": OTHER, "suggestion_id": "theirs", "applied": False, "dismissed": False}])
    revoked = []
    monkeypatch.setattr(gs, "revoke_refresh_token", revoked.append)

    assert client.post("/api/resume/gmail/disconnect", headers=auth(app)).status_code == 200

    assert revoked == ["refresh"]
    assert db.gmail_connections.count_documents({"user_email": USER}) == 0
    assert {s["suggestion_id"] for s in db.gmail_suggestions.find({})} == {"done", "theirs"}
    status = client.get("/api/resume/gmail/status", headers=auth(app)).get_json()
    assert status["state"] == "not_connected" and status["connected"] is False
    # Disconnecting twice is not an error.
    assert client.post("/api/resume/gmail/disconnect", headers=auth(app)).status_code == 200


# --- starting a sync ---------------------------------------------------------

@pytest.fixture
def dispatched(monkeypatch):
    from services.resume_service import ResumeService
    calls = []
    monkeypatch.setattr(ResumeService, "invoke_async",
                        staticmethod(lambda job_id, job_type, payload: calls.append((job_id, job_type, payload))))
    return calls


def connected(**fields):
    current_db().gmail_connections.insert_one({
        "user_email": USER, "gmail_address": "me@gmail.test",
        "refresh_token_enc": gs._fernet().encrypt(b"refresh").decode(),
        "linked_at": datetime.now(timezone.utc), **fields})


def test_sync_starts_one_run_and_a_second_press_joins_it(app, dispatched):
    connected()
    client = app.test_client()
    first = client.post("/api/resume/gmail/sync", headers=auth(app))
    data = first.get_json()
    assert first.status_code == 202 and data["status"] == "started" and data["run_id"]
    assert dispatched == [(data["job_id"], "gmail_sync",
                           {"user_email": USER, "run_id": data["run_id"], "trigger": "manual"})]
    status = client.get("/api/resume/gmail/status?light=1", headers=auth(app)).get_json()
    assert status["state"] == "syncing" and status["sync"]["run_id"] == data["run_id"]
    assert status["pending_suggestions"] is None

    second = client.post("/api/resume/gmail/sync", headers=auth(app)).get_json()
    assert second["already_running"] is True and second["run_id"] == data["run_id"]
    assert len(dispatched) == 1


def test_sync_without_a_connection_is_a_400_not_a_logout(app, dispatched):
    response = app.test_client().post("/api/resume/gmail/sync", headers=auth(app))
    assert response.status_code == 400 and response.get_json()["code"] == "gmail_not_connected"


def test_sync_on_a_dead_connection_says_reconnect_and_reads_nothing(app, dispatched):
    connected(needs_reconnect=True, reconnect_reason="expired_or_revoked")
    response = app.test_client().post("/api/resume/gmail/sync", headers=auth(app))
    data = response.get_json()
    assert response.status_code == 409 and data["code"] == "gmail_reconnect_required"
    assert "Reconnect Gmail" in data["error"] and dispatched == []


def test_pressing_sync_too_often_is_a_429_with_a_wait_not_a_logout(app, dispatched):
    connected()
    client = app.test_client()
    responses = [client.post("/api/resume/gmail/sync", headers=auth(app)) for _ in range(7)]
    assert [r.status_code for r in responses[:6]] == [202] * 6
    data = responses[6].get_json()
    assert responses[6].status_code == 429 and data["code"] == "rate_limited"
    assert data["retry_after_seconds"] == 300 and "5 minutes" in data["error"]
    assert len(dispatched) == 1  # the other five joined the same run


def test_a_run_that_cannot_be_dispatched_is_not_left_looking_busy(app, monkeypatch):
    from services.resume_service import ResumeService
    connected()

    def closed(*_):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(ResumeService, "invoke_async", staticmethod(closed))
    response = app.test_client().post("/api/resume/gmail/sync", headers=auth(app))
    assert response.status_code == 500 and "futures" not in response.get_data(as_text=True)
    assert gs.status_payload(USER)["state"] == "paused"


# --- the job handler ---------------------------------------------------------

def run_job(payload):
    from services import resume_service
    svc = resume_service.get_resume_service()
    job_id = svc.create_job("gmail_sync", payload, user_email=USER)
    resume_service._process_job(job_id, "gmail_sync", payload)
    return svc.get_job(job_id, user_email=USER)


def test_a_slice_with_mail_left_starts_the_next_slice(app, dispatched, monkeypatch):
    seen = []

    def slice_(email, run_id=None, **kwargs):
        seen.append((email, run_id, kwargs.get("trigger")))
        return {"run_id": "run-1", "messages_scanned": 100, "has_more": True, "continue": True}

    monkeypatch.setattr(gs, "run_slice", slice_)
    job = run_job({"user_email": USER, "run_id": "run-1", "trigger": "manual"})
    assert seen == [(USER, "run-1", "manual")]
    assert job["status"] == "completed" and job["result"]["continue"] is True
    assert len(dispatched) == 1 and dispatched[0][1] == "gmail_sync"
    assert dispatched[0][2]["run_id"] == "run-1"
    assert job["result"]["next_job_id"] == dispatched[0][0]


def test_the_last_slice_completes_without_starting_another(app, dispatched, monkeypatch):
    monkeypatch.setattr(gs, "run_slice", lambda *_, **__: {
        "run_id": "run-1", "messages_scanned": 3, "auto_applied": 1, "continue": False})
    job = run_job({"user_email": USER, "run_id": "run-1"})
    assert job["status"] == "completed" and job["result"]["auto_applied"] == 1
    assert dispatched == []


def test_a_deploy_in_the_middle_pauses_the_run_for_the_scheduler(app, monkeypatch):
    from services.resume_service import ResumeService
    paused = []
    monkeypatch.setattr(gs, "run_slice", lambda *_, **__: {"run_id": "run-1", "continue": True})
    monkeypatch.setattr(gs, "pause_run", lambda email, run_id: paused.append((email, run_id)))

    def closed(*_):
        raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    monkeypatch.setattr(ResumeService, "invoke_async", staticmethod(closed))
    job = run_job({"user_email": USER, "run_id": "run-1"})
    assert job["status"] == "completed" and paused == [(USER, "run-1")]


def test_a_failed_slice_fails_the_job_with_its_code_and_safe_text(app, dispatched, monkeypatch):
    def refuse(*_, **__):
        raise gs.GmailReconnectRequired("expired_or_revoked")

    monkeypatch.setattr(gs, "run_slice", refuse)
    job = run_job({"user_email": USER, "run_id": "run-1"})
    assert job["status"] == "failed" and job["error_code"] == "gmail_reconnect_required"
    assert "Reconnect Gmail" in job["error"] and dispatched == []


def test_the_whole_path_route_job_status_with_a_revoked_token(app, dispatched, monkeypatch):
    """Press Sync with a token Google has revoked: the job fails, the status
    says reconnect, and the next press is refused before anything is read."""
    from services import resume_service
    connected()
    current_db().tailoring_records.insert_one({
        "user_email": USER, "record_id": "r1",
        "jd_analysis": {"company": "Acme", "job_title": "Engineer"}, "application": {"status": "applied"}})
    google(monkeypatch, 400, {"error": "invalid_grant"})
    client = app.test_client()
    started = client.post("/api/resume/gmail/sync", headers=auth(app)).get_json()
    job_id, job_type, payload = dispatched[0]
    resume_service._process_job(job_id, job_type, payload)

    job = client.get(f"/api/resume/job/{started['job_id']}", headers=auth(app)).get_json()
    assert job["status"] == "failed" and job["error_code"] == "gmail_reconnect_required"
    status = client.get("/api/resume/gmail/status", headers=auth(app)).get_json()
    assert status["state"] == "needs_reconnect" and status["connected"] is True
    assert client.post("/api/resume/gmail/sync", headers=auth(app)).status_code == 409


# --- what changed, and undo ---------------------------------------------------

def test_activity_and_undo_routes(app, monkeypatch):
    seen = []
    monkeypatch.setattr(gs, "list_activity", lambda user, limit=20: seen.append((user, limit)) or [
        {"suggestion_id": "s1", "undoable": True}])
    client = app.test_client()
    response = client.get("/api/resume/gmail/activity?limit=5", headers=auth(app))
    assert response.get_json() == {"changes": [{"suggestion_id": "s1", "undoable": True}]}
    assert seen == [(USER, 5)]
    assert client.get("/api/resume/gmail/activity?limit=many", headers=auth(app)).status_code == 400

    monkeypatch.setattr(gs, "undo_auto_apply", lambda user, sid: {
        "suggestion": {"suggestion_id": sid}, "restored_status": "applied"})
    undone = client.post("/api/resume/gmail/suggestions/s1/undo", headers=auth(app))
    assert undone.status_code == 200 and undone.get_json()["restored_status"] == "applied"


@pytest.mark.parametrize("error, status, code", [
    (LookupError("x"), 404, None),
    (gs.GmailUndoError("status_changed"), 409, "status_changed"),
    (gs.GmailUndoError("not_undoable"), 409, "not_undoable"),
    (RuntimeError("secret detail"), 500, "gmail_error"),
])
def test_undo_failures_are_plain_and_never_401(app, monkeypatch, error, status, code):
    def fail(*_):
        raise error
    monkeypatch.setattr(gs, "undo_auto_apply", fail)
    response = app.test_client().post("/api/resume/gmail/suggestions/s1/undo", headers=auth(app))
    assert response.status_code == status and response.get_json().get("code") == code
    assert "secret detail" not in response.get_data(as_text=True)
