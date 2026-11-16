"""Gmail sync as the user meets it: one press runs to completion, a dead
connection says Reconnect, an empty tracker says so, and an automatic move
can be put back. Offline: mongomock, no Google, no network."""
from datetime import datetime, timedelta, timezone

import pytest

from _vg_support import current_db
from services import gmail_service as gs

USER = "gmail-runs@example.test"
NOW = datetime.now(timezone.utc)


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)
        self.headers = {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Fernet:
    def encrypt(self, value):
        return b"enc:" + value

    def decrypt(self, value):
        if not value.startswith(b"enc:"):
            raise ValueError("not ours")
        return value[4:]


@pytest.fixture
def db(monkeypatch):
    db = current_db()
    for name in ("gmail_connections", "gmail_suggestions", "gmail_oauth_states",
                 "tailoring_records", "users", "applications"):
        db[name].delete_many({})
    monkeypatch.setattr(gs, "_fernet", lambda: _Fernet())
    monkeypatch.setattr(gs, "gmail_configured", lambda: True)
    monkeypatch.setattr(gs, "gmail_oauth_enabled", lambda: True)
    monkeypatch.setattr(gs, "classify_record_thread", lambda *_: None)
    monkeypatch.setattr(gs, "revoke_refresh_token", lambda *_: None)
    return db


def connect(db, **fields):
    db.gmail_connections.insert_one({
        "user_email": USER, "gmail_address": "me@gmail.test",
        "refresh_token_enc": "enc:refresh", "linked_at": NOW, **fields})


def track(db, record_id="r1", company="Acme", title="Platform Engineer", status="applied", **fields):
    db.tailoring_records.insert_one({
        "user_email": USER, "record_id": record_id, "created_at": NOW - timedelta(days=20),
        "jd_analysis": {"company": company, "job_title": title},
        "application": {"status": status}, **fields})


def message(mid, subject, body, when_ms=None, sender="Acme Talent <jobs@acme.test>"):
    import base64
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {"id": mid, "threadId": f"t-{mid}",
            "internalDate": str(when_ms or int(NOW.timestamp() * 1000)),
            "snippet": body[:80],
            "payload": {"mimeType": "text/plain", "body": {"data": data},
                        "headers": [{"name": "From", "value": sender},
                                    {"name": "Subject", "value": subject}]}}


def noise(mid):
    return message(mid, "Your weekly digest", "Nothing about jobs here.",
                   sender="Newsletter <news@letters.test>")


REJECTION = message(
    "m-reject", "Your application to Acme for Platform Engineer",
    "Thank you for applying to Acme for the Platform Engineer role. Unfortunately, we have "
    "decided to move forward with other candidates.")


def mailbox(monkeypatch, pages):
    """`pages` is a list of lists of full messages; each list is one batch."""
    monkeypatch.setattr(gs, "_get_access_token", lambda user: ("access", gs.get_connection(user)))
    monkeypatch.setattr(gs, "MAX_MESSAGES_PER_SYNC", 1)
    flat = [m for page in pages for m in page]
    by_id = {m["id"]: m for m in flat}
    calls = {"list": 0, "get": 0}

    def gmail(path, access, params=None, raise_on_error=False, **_):
        if path.endswith("/messages"):
            calls["list"] += 1
            start = int((params or {}).get("pageToken") or 0)
            body = {"messages": [{"id": m["id"]} for m in flat[start:start + 1]]}
            if start + 1 < len(flat):
                body["nextPageToken"] = str(start + 1)
            return body
        calls["get"] += 1
        return by_id[path.rsplit("/", 1)[-1]]

    monkeypatch.setattr(gs, "_gmail_get", gmail)
    return calls


# --- one press runs to completion -------------------------------------------

def test_one_run_reads_every_batch_and_reports_totals(db, monkeypatch):
    connect(db)
    track(db)
    mailbox(monkeypatch, [[m] for m in [noise("n0"), noise("n1"), noise("n2"), REJECTION]])

    result = gs.run_slice(USER, gs.start_run(USER)["run_id"])

    assert result["continue"] is False and result["has_more"] is False
    assert result["messages_scanned"] == 4 and result["batches"] == 4
    assert result["auto_applied"] == 1 and result["outcome"] == "updated"
    conn = gs.get_connection(USER)
    assert conn["sync_run"]["state"] == "completed"
    assert conn["last_synced_at"] and "scan_next_page_token" not in conn
    assert conn["last_sync_summary"]["messages_scanned"] == 4
    assert db.tailoring_records.find_one({"record_id": "r1"})["application"]["status"] == "rejected"
    status = gs.status_payload(USER)
    assert status["state"] == "synced" and status["sync"]["state"] == "completed"


def test_a_slice_that_runs_out_of_time_asks_to_continue_and_keeps_its_place(db, monkeypatch):
    connect(db)
    track(db)
    mailbox(monkeypatch, [[noise(f"n{i}")] for i in range(3)])
    run_id = gs.start_run(USER)["run_id"]

    first = gs.run_slice(USER, run_id, budget_seconds=0)
    assert first["continue"] is True and first["messages_scanned"] == 1
    status = gs.status_payload(USER, light=True)
    assert status["state"] == "syncing" and status["sync"]["messages_scanned"] == 1
    assert status["pending_suggestions"] is None

    gs.pause_run(USER, run_id)
    assert gs.status_payload(USER)["state"] == "paused"

    # Continuing never re-reads what was already checked.
    rest = gs.run_slice(USER, gs.start_run(USER)["run_id"])
    assert rest["continue"] is False and rest["messages_scanned"] == 2


def test_a_second_press_joins_the_running_scan_instead_of_starting_another(db):
    connect(db)
    first = gs.start_run(USER)
    second = gs.start_run(USER)
    assert not first["already_running"]
    assert second == {"run_id": first["run_id"], "already_running": True}


def test_a_run_whose_worker_died_can_be_taken_over(db):
    connect(db)
    first = gs.start_run(USER)
    db.gmail_connections.update_one({"user_email": USER}, {"$set": {
        "sync_run.updated_at": NOW - timedelta(seconds=gs.RUN_STALE_SECONDS + 5)}})
    assert gs.status_payload(USER)["state"] == "paused"
    second = gs.start_run(USER)
    assert not second["already_running"] and second["run_id"] != first["run_id"]


# --- an empty tracker is said out loud --------------------------------------

def test_an_empty_tracker_reads_no_mail_and_says_why(db, monkeypatch):
    connect(db)
    monkeypatch.setattr(gs, "fetch_recent_messages",
                        lambda *_: pytest.fail("No mail may be read without tracked applications"))

    result = gs.run_slice(USER, gs.start_run(USER)["run_id"])

    assert result["outcome"] == "no_applications" and result["messages_scanned"] == 0
    status = gs.status_payload(USER)
    assert status["state"] == "synced" and status["tracked_applications"] == 0
    assert status["last_sync_summary"]["outcome"] == "no_applications"
    # The first real scan must still cover the whole window.
    assert not gs.get_connection(USER).get("last_synced_at")


def test_outcomes_explain_a_scan_that_changed_nothing(db, monkeypatch):
    connect(db)
    track(db)
    mailbox(monkeypatch, [[]])
    assert gs.run_slice(USER, gs.start_run(USER)["run_id"])["outcome"] == "no_mail"

    db.gmail_connections.update_one({"user_email": USER}, {"$set": {"last_synced_at": None}})
    mailbox(monkeypatch, [[noise("n1")]])
    result = gs.run_slice(USER, gs.start_run(USER)["run_id"])
    assert result["outcome"] == "no_matches" and result["window_days"] == 60


# --- expired or revoked access ----------------------------------------------

@pytest.mark.parametrize("status_code", [400, 401])
def test_a_refused_refresh_token_is_a_typed_reconnect_error(monkeypatch, status_code):
    monkeypatch.setattr(gs.requests, "post", lambda *_, **__: _Response(
        status_code, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}))
    with pytest.raises(gs.GmailReconnectRequired) as caught:
        gs.refresh_access_token("stale")
    assert caught.value.code == "gmail_reconnect_required"
    assert "expired or revoked" not in str(caught.value)  # never Google's text


def test_a_google_outage_is_not_mistaken_for_a_dead_connection(monkeypatch):
    monkeypatch.setattr(gs.requests, "post", lambda *_, **__: _Response(503, ValueError("html")))
    with pytest.raises(gs.GmailSyncError) as caught:
        gs.refresh_access_token("fine")
    assert not isinstance(caught.value, gs.GmailReconnectRequired)
    assert caught.value.code == "gmail_error"


def test_revoked_access_marks_the_connection_and_fails_the_run(db, monkeypatch):
    connect(db, last_synced_at=NOW - timedelta(days=1))
    track(db)
    monkeypatch.setattr(gs.requests, "post", lambda *_, **__: _Response(400, {"error": "invalid_grant"}))

    with pytest.raises(gs.GmailReconnectRequired):
        gs.run_slice(USER, gs.start_run(USER)["run_id"])

    status = gs.status_payload(USER)
    assert status["state"] == "needs_reconnect" and status["needs_reconnect"] is True
    assert status["reconnect_reason"] == "expired_or_revoked"
    assert status["sync"]["state"] == "failed"
    assert status["sync"]["error_code"] == "gmail_reconnect_required"
    assert status["connected"] is True  # still theirs to reconnect or disconnect
    # The finished-scan watermark survives, so a reconnect resumes from it.
    assert gs.get_connection(USER)["last_synced_at"]


def test_gmail_refusing_the_token_or_the_scope_also_means_reconnect(db, monkeypatch):
    connect(db)
    track(db)
    monkeypatch.setattr(gs, "_get_access_token", lambda user: ("access", gs.get_connection(user)))
    scope_error = ('{"error": {"code": 403, "errors": [{"reason": "insufficientPermissions"}]}}')

    def refuse(*_, **__):
        raise gs.GmailAPIError(403, scope_error)

    monkeypatch.setattr(gs, "_gmail_get", refuse)
    with pytest.raises(gs.GmailReconnectRequired):
        gs.sync_user(USER)
    assert gs.status_payload(USER)["reconnect_reason"] == "permission_missing"


def test_a_wrong_server_key_is_our_problem_and_never_marks_connections_dead(db):
    """A changed GMAIL_TOKEN_ENCRYPTION_KEY makes every stored token unreadable
    at once. Putting the key back must be all it takes to recover."""
    connect(db, refresh_token_enc="written-with-another-key")
    track(db)
    with pytest.raises(gs.GmailSyncError) as caught:
        gs.sync_user(USER)
    assert caught.value.code == "gmail_unavailable"
    assert not isinstance(caught.value, gs.GmailReconnectRequired)
    assert "needs_reconnect" not in gs.get_connection(USER)


def test_reconnecting_the_same_mailbox_clears_the_flag_and_keeps_the_watermark(db, monkeypatch):
    watermark = NOW - timedelta(days=2)
    connect(db, needs_reconnect=True, reconnect_reason="expired_or_revoked",
            last_synced_at=watermark, sync_run={"run_id": "old", "state": "failed"})
    monkeypatch.setattr(gs, "_userinfo", lambda _: {"email": "me@gmail.test"})

    gs.store_connection(USER, {"refresh_token": "fresh", "access_token": "a",
                               "scope": " ".join(gs.GMAIL_SCOPES)})

    conn = gs.get_connection(USER)
    assert "needs_reconnect" not in conn and "sync_run" not in conn
    assert conn["last_synced_at"].replace(tzinfo=timezone.utc) == watermark.replace(microsecond=watermark.microsecond // 1000 * 1000)
    assert gs.status_payload(USER)["state"] == "synced"


def test_connecting_a_different_mailbox_starts_from_nothing(db, monkeypatch):
    connect(db, last_synced_at=NOW)
    monkeypatch.setattr(gs, "_userinfo", lambda _: {"email": "other@gmail.test"})
    gs.store_connection(USER, {"refresh_token": "fresh", "access_token": "a"})
    assert gs.get_connection(USER)["last_synced_at"] is None


def test_other_gmail_failures_keep_their_own_codes(db, monkeypatch):
    connect(db)
    track(db)
    monkeypatch.setattr(gs, "_get_access_token", lambda user: ("access", gs.get_connection(user)))

    def limited(*_, **__):
        raise gs.GmailAPIError(429, "{}", retry_after_seconds=30)

    monkeypatch.setattr(gs, "_gmail_get", limited)
    with pytest.raises(gs.GmailSyncError) as caught:
        gs.run_slice(USER, gs.start_run(USER)["run_id"])
    assert caught.value.code == "gmail_rate_limited"
    status = gs.status_payload(USER)
    assert status["needs_reconnect"] is False
    assert status["sync"]["error_code"] == "gmail_rate_limited" and "30 seconds" in status["sync"]["error"]


def test_an_unexpected_crash_never_reaches_the_user_as_exception_text(db, monkeypatch):
    connect(db)
    track(db)

    def crash(_):
        raise KeyError("mongodb+srv://user:secret@cluster")

    monkeypatch.setattr(gs, "sync_user", crash)
    with pytest.raises(gs.GmailSyncError) as caught:
        gs.run_slice(USER, gs.start_run(USER)["run_id"])
    assert "secret" not in str(caught.value)
    assert "secret" not in str(gs.status_payload(USER)["sync"])


# --- testing-mode honesty ----------------------------------------------------

def test_status_says_when_google_will_end_a_testing_mode_connection(db, monkeypatch):
    connect(db)
    monkeypatch.setattr(gs, "consent_mode", lambda: "testing")
    access = gs.status_payload(USER)["access"]
    assert access["mode"] == "testing"
    assert datetime.fromisoformat(access["expires_at"]) - NOW.replace(microsecond=0) <= timedelta(days=7, seconds=1)
    monkeypatch.setattr(gs, "consent_mode", lambda: "production")
    published = gs.status_payload(USER)["access"]
    assert (published["mode"], published["expires_at"]) == ("production", None)
    # The block also says who may connect (tests/test_gmail_access.py).
    assert published["gate"] == "open" and published["state"] == "approved"


def test_consent_mode_is_testing_until_the_owner_says_otherwise(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CONSENT_MODE", raising=False)
    assert gs.consent_mode() == "testing"
    monkeypatch.setenv("GOOGLE_OAUTH_CONSENT_MODE", "Production")
    assert gs.consent_mode() == "production"


# --- newly tracked applications ---------------------------------------------

def test_an_application_added_after_the_last_scan_gets_its_earlier_mail_read(db, monkeypatch):
    watermark = NOW - timedelta(days=1)
    # Already scanned under the current rules, so this is an ordinary top-up.
    connect(db, last_synced_at=watermark, scan_version=gs.SCAN_VERSION)
    track(db, record_id="old", created_at=NOW - timedelta(days=30))
    seen = []
    monkeypatch.setattr(gs, "fetch_recent_messages", lambda _, since, *__: seen.append(since) or [])

    gs.sync_user(USER)
    assert seen[-1].replace(tzinfo=timezone.utc) - watermark < timedelta(seconds=1)

    db.gmail_connections.update_one({"user_email": USER}, {"$set": {"last_synced_at": watermark}})
    track(db, record_id="new", company="Globex", created_at=NOW,
          application={"status": "applied", "applied_date": (NOW - timedelta(days=5)).date().isoformat()})
    gs.sync_user(USER)
    assert watermark - seen[-1] > timedelta(days=4)
    assert NOW - seen[-1] <= timedelta(days=gs.INITIAL_LOOKBACK_DAYS)


# --- what changed, and undo --------------------------------------------------

def synced_rejection(db, monkeypatch):
    connect(db)
    track(db)
    mailbox(monkeypatch, [[REJECTION]])
    gs.run_slice(USER, gs.start_run(USER)["run_id"])
    return gs.list_activity(USER)


def test_activity_says_which_application_moved_and_which_email_did_it(db, monkeypatch):
    changes = synced_rejection(db, monkeypatch)
    assert len(changes) == 1
    change = changes[0]
    assert (change["company"], change["title"]) == ("Acme", "Platform Engineer")
    assert (change["from_status"], change["to_status"]) == ("applied", "rejected")
    assert change["subject"] == "Your application to Acme for Platform Engineer"
    assert change["undoable"] is True and change["undone"] is False
    assert change["sync_run_id"] == gs.get_connection(USER)["sync_run"]["run_id"]


def test_undo_restores_the_status_and_the_email_never_moves_it_again(db, monkeypatch):
    change = synced_rejection(db, monkeypatch)[0]

    result = gs.undo_auto_apply(USER, change["suggestion_id"])

    assert result["restored_status"] == "applied"
    assert db.tailoring_records.find_one({"record_id": "r1"})["application"]["status"] == "applied"
    after = gs.list_activity(USER)[0]
    assert after["undone"] is True and after["undoable"] is False
    assert gs.list_suggestions(USER) == []  # not handed back as a review item

    # The same mail read again (a rescan) must not redo the move.
    db.gmail_connections.update_one({"user_email": USER}, {"$set": {"last_synced_at": None}})
    gs.run_slice(USER, gs.start_run(USER)["run_id"])
    assert db.tailoring_records.find_one({"record_id": "r1"})["application"]["status"] == "applied"

    with pytest.raises(gs.GmailUndoError) as again:
        gs.undo_auto_apply(USER, change["suggestion_id"])
    assert again.value.code == "not_undoable"


def test_undo_refuses_once_the_user_has_moved_the_application_on(db, monkeypatch):
    change = synced_rejection(db, monkeypatch)[0]
    db.tailoring_records.update_one({"record_id": "r1"}, {"$set": {"application.status": "withdrawn"}})
    assert gs.list_activity(USER)[0]["undoable"] is False
    with pytest.raises(gs.GmailUndoError) as caught:
        gs.undo_auto_apply(USER, change["suggestion_id"])
    assert caught.value.code == "status_changed"
    assert db.tailoring_records.find_one({"record_id": "r1"})["application"]["status"] == "withdrawn"


def test_undo_is_owner_scoped_and_only_for_automatic_moves(db, monkeypatch):
    change = synced_rejection(db, monkeypatch)[0]
    with pytest.raises(LookupError):
        gs.undo_auto_apply("someone-else@example.test", change["suggestion_id"])
    db.gmail_suggestions.update_one({"suggestion_id": change["suggestion_id"]},
                                    {"$set": {"auto_applied": False}})
    with pytest.raises(gs.GmailUndoError):
        gs.undo_auto_apply(USER, change["suggestion_id"])


def test_undoing_a_receipt_also_removes_the_applied_date_it_set(db, monkeypatch):
    connect(db)
    track(db, status="draft")
    receipt = message("m-receipt", "Thank you for applying to Acme",
                      "Thank you for applying to Acme for the Platform Engineer role. "
                      "We received your application.")
    mailbox(monkeypatch, [[receipt]])
    gs.run_slice(USER, gs.start_run(USER)["run_id"])
    changes = gs.list_activity(USER)
    assert changes and changes[0]["to_status"] == "applied"
    assert db.tailoring_records.find_one({"record_id": "r1"})["application"].get("applied_at")
    gs.undo_auto_apply(USER, changes[0]["suggestion_id"])
    application = db.tailoring_records.find_one({"record_id": "r1"})["application"]
    assert application["status"] == "draft" and "applied_at" not in application


# --- scheduled background sync ----------------------------------------------

def test_scheduled_sync_picks_only_connections_that_are_due(db, monkeypatch):
    ran = []
    monkeypatch.setattr(gs, "run_slice", lambda email, run_id=None, **kw: ran.append(
        (email, kw.get("trigger"))) or {"run_id": "x", "continue": False})
    monkeypatch.setattr(gs, "auto_sync_enabled", lambda: True)

    def add(email, tracked=True, last_login=NOW, **conn):
        db.gmail_connections.insert_one({"user_email": email, **conn})
        db.users.insert_one({"email": email, "last_login": last_login})
        if tracked:
            db.tailoring_records.insert_one({"user_email": email, "record_id": email})

    add("stale@example.test", last_synced_at=NOW - timedelta(hours=7))
    add("fresh@example.test", last_synced_at=NOW - timedelta(hours=1))
    add("unfinished@example.test", last_synced_at=NOW - timedelta(hours=1), scan_next_page_token="p2")
    add("never@example.test")
    add("dead@example.test", needs_reconnect=True, last_synced_at=NOW - timedelta(days=3))
    add("empty@example.test", tracked=False, last_synced_at=NOW - timedelta(days=3))
    add("gone@example.test", last_login=NOW - timedelta(days=90), last_synced_at=NOW - timedelta(days=3))
    add("running@example.test", last_synced_at=NOW - timedelta(days=3),
        # A fresh heartbeat: NOW is fixed at import, which in a full run is
        # long enough ago to read as a worker that died.
        sync_run={"run_id": "live", "state": "running",
                  "updated_at": datetime.now(timezone.utc)})

    result = gs.sync_due(budget_seconds=60)

    assert [email for email, _ in ran] == [
        "unfinished@example.test", "never@example.test", "stale@example.test"]
    assert {trigger for _, trigger in ran} == {"scheduled"}
    assert result["due"] == 3 and result["finished"] == 3 and result["skipped"] == 2


def _two_due_users(db, monkeypatch):
    monkeypatch.setattr(gs, "auto_sync_enabled", lambda: True)
    for email in ("a@example.test", "b@example.test"):
        db.gmail_connections.insert_one({"user_email": email})
        db.tailoring_records.insert_one({"user_email": email, "record_id": email})


def test_scheduled_sync_survives_one_users_failure(db, monkeypatch):
    _two_due_users(db, monkeypatch)

    def slice_(email, run_id=None, **_):
        if email == "a@example.test":
            raise gs.GmailReconnectRequired()
        return {"run_id": "run-b", "continue": False}

    monkeypatch.setattr(gs, "run_slice", slice_)
    result = gs.sync_due(budget_seconds=60)
    assert result["needs_reconnect"] == 1 and result["finished"] == 1 and result["synced"] == 2


def test_scheduled_sync_pauses_what_its_budget_cannot_finish(db, monkeypatch):
    _two_due_users(db, monkeypatch)
    paused = []
    monkeypatch.setattr(gs, "run_slice", lambda email, run_id=None, **_: {
        "run_id": f"run-{email[0]}", "continue": True})
    monkeypatch.setattr(gs, "pause_run", lambda email, run_id: paused.append((email, run_id)))
    result = gs.sync_due(budget_seconds=0)
    # One mailbox always gets a turn; the rest wait for the next tick.
    assert result["synced"] == 1 and result["behind"] == 2
    assert paused == [("a@example.test", "run-a")]


def test_scheduled_sync_is_off_with_the_feature(db, monkeypatch):
    monkeypatch.setattr(gs, "gmail_oauth_enabled", lambda: False)
    monkeypatch.setattr(gs, "run_slice", lambda *_, **__: pytest.fail("must not read mail"))
    db.gmail_connections.insert_one({"user_email": USER})
    assert gs.sync_due() == {"enabled": False}


def test_the_worker_schedules_gmail_sync():
    from services.scheduler import runner
    from services import scheduled_jobs
    assert runner.EVERY["gmail_sync"] == 30
    seen = {}

    def due(budget_seconds):
        seen["budget"] = budget_seconds
        return {"enabled": True}

    original = gs.sync_due
    gs.sync_due = due
    try:
        out = scheduled_jobs.dispatch_scheduled("gmail_sync")
    finally:
        gs.sync_due = original
    assert out == {"job": "gmail_sync", "result": {"enabled": True}}
    assert seen["budget"] < runner.EVERY["gmail_sync"] * 60


# --- housekeeping ------------------------------------------------------------

def test_abandoned_consent_attempts_expire(db, monkeypatch):
    monkeypatch.setattr(gs, "_INDEXES_READY", False)
    monkeypatch.setattr(gs, "_client_id", lambda: "client")
    monkeypatch.setattr(gs, "_redirect_uri", lambda: "https://app.test/oauth/gmail/callback")
    gs.build_auth_url(USER)
    indexes = db.gmail_oauth_states.index_information()
    assert indexes["ttl_expires"]["expireAfterSeconds"] == 0
    assert indexes["ttl_expires"]["key"] == [("expires_at", 1)]
    assert indexes["state_unique"]["unique"] is True


def test_the_index_migration_declares_the_same_ttl():
    import importlib.util
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "scripts" / "migrate_indexes.py").read_text()
    block = source[source.index('("gmail_oauth_states", [("expires_at"'):][:200]
    assert '"expireAfterSeconds": 0' in block and importlib.util is not None


def test_the_owner_health_report_names_settings_without_their_values(db, monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN_ENCRYPTION_KEY", "super-secret-key")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "super-secret-client")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "https://aspirely.me/oauth/gmail/callback")
    connect(db, needs_reconnect=True, linked_at=NOW - timedelta(days=9))
    report = gs.health_report()
    assert report["settings_present"]["GMAIL_TOKEN_ENCRYPTION_KEY"] is True
    assert report["redirect_uri"] == "https://aspirely.me/oauth/gmail/callback"
    assert report["connections"] == {"total": 1, "needs_reconnect": 1, "never_finished_a_scan": 1,
                                     "scan_unfinished": 0, "linked_over_7_days_ago": 1}
    assert "super-secret" not in str(report)
