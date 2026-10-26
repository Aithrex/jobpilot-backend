"""Bounded Gmail scans must continue without losing an inbox backlog."""
from datetime import datetime, timezone

import pytest

from _vg_support import current_db
from services import gmail_service as gs

USER = "gmail-batch@example.test"


@pytest.fixture
def db(monkeypatch):
    db = current_db()
    for name in ("gmail_connections", "gmail_suggestions", "tailoring_records"):
        db[name].delete_many({})
    db.gmail_connections.insert_one({"user_email": USER})
    db.tailoring_records.insert_one({"user_email": USER, "record_id": "r1",
        "jd_analysis": {"company": "Acme", "job_title": "Platform Engineer"},
        "application": {"status": "applied"}})
    monkeypatch.setattr(gs, "_get_access_token", lambda user: ("test-access", gs.get_connection(user)))
    monkeypatch.setattr(gs, "MAX_MESSAGES_PER_SYNC", 1)
    monkeypatch.setattr(gs, "classify_record_thread", lambda *_: None)
    return db


def test_scan_continuation_keeps_watermark_until_last_page(db, monkeypatch):
    list_params = []

    def gmail(path, access, params=None, raise_on_error=False, **_):
        if path.endswith("/messages"):
            list_params.append(dict(params))
            if params.get("pageToken") == "next-page":
                return {"messages": [{"id": "older"}]}
            return {"messages": [{"id": "newer"}], "nextPageToken": "next-page"}
        return {"threadId": "t1", "internalDate": "1000", "payload": {"headers": []}}

    monkeypatch.setattr(gs, "_gmail_get", gmail)
    first = gs.sync_user(USER)
    assert first["has_more"]
    assert not gs.get_connection(USER).get("last_synced_at")
    second = gs.sync_user(USER)
    assert not second["has_more"]
    assert gs.get_connection(USER).get("last_synced_at")
    assert list_params[1]["pageToken"] == "next-page"
    assert list_params[0]["q"] == list_params[1]["q"]
    assert "before:" in list_params[0]["q"]


def test_message_fetch_failure_does_not_advance_watermark(db, monkeypatch):
    last = datetime(2026, 10, 1, tzinfo=timezone.utc)
    db.gmail_connections.update_one({"user_email": USER}, {"$set": {"last_synced_at": last}})

    def gmail(path, access, params=None, raise_on_error=False, **_):
        if path.endswith("/messages"):
            return {"messages": [{"id": "failed"}]}
        assert raise_on_error
        raise gs.GmailAPIError(503, "temporarily unavailable")

    monkeypatch.setattr(gs, "_gmail_get", gmail)
    with pytest.raises(RuntimeError):
        gs.sync_user(USER)
    assert gs.get_connection(USER)["last_synced_at"].replace(tzinfo=timezone.utc) == last


def test_relink_clears_previous_mailbox_cursor_and_lease(db, monkeypatch):
    db.gmail_connections.update_one({"user_email": USER}, {"$set": {
        "scan_next_page_token": "old-mailbox-page", "scan_query": "old-query",
        "scan_before": datetime.now(timezone.utc), "sync_lock_id": "old-worker",
        "sync_lock_until": datetime.now(timezone.utc), "last_sync_summary": {"has_more": True}}})
    monkeypatch.setattr(gs, "_userinfo", lambda _: {"email": "new-mailbox@example.test"})
    class FakeFernet:
        def encrypt(self, value):
            return b"test-encrypted-token"
    monkeypatch.setattr(gs, "_fernet", lambda: FakeFernet())
    gs.store_connection(USER, {"refresh_token": "test-refresh", "access_token": "test-access"})
    conn = gs.get_connection(USER)
    assert conn["gmail_address"] == "new-mailbox@example.test"
    for field in ("scan_next_page_token", "scan_query", "scan_before", "sync_lock_id", "sync_lock_until", "last_sync_summary"):
        assert field not in conn


def test_old_worker_cannot_write_cursor_to_replacement_connection(db, monkeypatch):
    def replace_during_fetch(*_):
        db.gmail_connections.delete_one({"user_email": USER})
        db.gmail_connections.insert_one({"user_email": USER, "gmail_address": "new-mailbox@example.test"})
        return []
    monkeypatch.setattr(gs, "fetch_recent_messages", replace_during_fetch)
    with pytest.raises(RuntimeError, match="connection changed"):
        gs.sync_user(USER)
    conn = gs.get_connection(USER)
    assert "last_synced_at" not in conn
    assert "last_sync_summary" not in conn
