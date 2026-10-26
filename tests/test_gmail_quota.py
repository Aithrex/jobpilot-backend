"""Gmail quota failures must back off without losing a scan continuation."""
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from _vg_support import current_db
from services import gmail_service as gs

USER = "quota-test@example.test"


def response(status, reason=None, *, message=None, headers=None, payload=None):
    data = payload if payload is not None else {"error": {
        "code": status,
        "message": message or reason or "provider failure",
        "errors": [{"reason": reason}] if reason else [],
    }}
    return SimpleNamespace(status_code=status, text=json.dumps(data), headers=headers or {}, json=lambda: data)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": 0.0, "sleeps": []}
    monkeypatch.setattr(gs.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(gs.secrets, "randbelow", lambda _: 0)

    def sleep(seconds):
        state["sleeps"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(gs.time, "sleep", sleep)
    return state


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
def test_403_quota_retries_same_request_then_recovers(monkeypatch, clock, reason):
    calls = []
    responses = iter([response(403, reason), response(200, payload={"messages": []})])

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(gs.requests, "get", get)
    assert gs._gmail_get("/users/me/messages", "test-access", raise_on_error=True) == {"messages": []}
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0]
    assert clock["sleeps"] == [1.0]


def test_confirmed_query_cost_message_is_retryable_without_legacy_reason(monkeypatch, clock):
    responses = iter([
        response(403, message="Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute per user'."),
        response(200, payload={"id": "fixture-message"}),
    ])
    monkeypatch.setattr(gs.requests, "get", lambda *_, **__: next(responses))
    assert gs._gmail_get("/users/me/messages/fixture-message", "test-access", raise_on_error=True)["id"] == "fixture-message"


def test_retry_after_is_honored(monkeypatch, clock):
    responses = iter([response(429, headers={"Retry-After": "12"}), response(200, payload={})])
    monkeypatch.setattr(gs.requests, "get", lambda *_, **__: next(responses))
    gs._gmail_get("/users/me/messages", "test-access", raise_on_error=True)
    assert clock["sleeps"] == [12.0]


def test_retry_after_http_date_is_honored(monkeypatch, clock):
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    responses = iter([response(403, "userRateLimitExceeded", headers={"Retry-After": format_datetime(future)}), response(200, payload={})])
    monkeypatch.setattr(gs.requests, "get", lambda *_, **__: next(responses))
    gs._gmail_get("/users/me/messages", "test-access", raise_on_error=True)
    assert 28 <= sum(clock["sleeps"]) <= 30


def test_long_retry_after_defers_instead_of_retrying_early(monkeypatch, clock):
    calls = []
    monkeypatch.setattr(gs.requests, "get", lambda *_, **__: calls.append(1) or response(429, headers={"Retry-After": "600"}))
    with pytest.raises(gs.GmailAPIError) as exc:
        gs._gmail_get("/users/me/messages", "test-access", raise_on_error=True)
    assert len(calls) == 1
    assert not clock["sleeps"]
    assert exc.value.retry_after_seconds == 600


@pytest.mark.parametrize("reason", ["accessNotConfigured", "insufficientPermissions", "domainPolicy", "dailyLimitExceeded"])
def test_non_transient_403_does_not_retry(monkeypatch, clock, reason):
    calls = []
    monkeypatch.setattr(gs.requests, "get", lambda *_, **__: calls.append(1) or response(403, reason))
    with pytest.raises(gs.GmailAPIError) as exc:
        gs._gmail_get("/users/me/messages", "test-access", raise_on_error=True)
    assert len(calls) == 1
    assert not clock["sleeps"]
    assert exc.value.reason == reason
    assert not exc.value.retryable


def test_persistent_quota_has_bounded_retry_attempts(monkeypatch, clock):
    calls = []
    monkeypatch.setattr(gs.requests, "get", lambda *_, **__: calls.append(1) or response(403, "rateLimitExceeded"))
    with pytest.raises(gs.GmailAPIError):
        gs._gmail_get("/users/me/messages", "test-access", raise_on_error=True)
    assert 1 < len(calls) <= 7
    assert 1 <= sum(clock["sleeps"]) < 180


def test_fetch_paces_requests_and_bounds_batch(monkeypatch, clock):
    call_times = []
    list_sizes = []
    monkeypatch.setattr(gs, "_get_access_token", lambda _: ("test-access", {}))

    def get(url, **kwargs):
        call_times.append(clock["now"])
        if url.endswith("/messages"):
            list_sizes.append(kwargs["params"]["maxResults"])
            return response(200, payload={"messages": [{"id": "one"}, {"id": "two"}], "nextPageToken": "next"})
        return response(200, payload={"internalDate": "1000", "payload": {}})

    monkeypatch.setattr(gs.requests, "get", get)
    monkeypatch.setattr(gs, "MAX_MESSAGES_PER_SYNC", 2)
    batch = gs.fetch_recent_messages(USER, None)
    assert len(batch) == 2
    assert batch.next_page_token == "next"
    assert list_sizes == [2]
    assert all(b - a >= 1 for a, b in zip(call_times, call_times[1:]))


def test_fetch_deadline_is_shared_across_requests(monkeypatch, clock):
    monkeypatch.setattr(gs, "_get_access_token", lambda _: ("test-access", {}))
    call_times = []

    def get(url, **kwargs):
        call_times.append(clock["now"])
        assert 0 < kwargs["timeout"] <= min(15, 180 - clock["now"])
        clock["now"] += 10
        if url.endswith("/messages"):
            return response(200, payload={"messages": [{"id": str(i)} for i in range(30)]})
        return response(200, payload={"internalDate": "1000", "payload": {}})

    monkeypatch.setattr(gs.requests, "get", get)
    with pytest.raises(RuntimeError, match="Try syncing again"):
        gs.fetch_recent_messages(USER, None)
    assert len(call_times) < 30
    assert max(call_times) < 180


@pytest.mark.parametrize("fail_on_message", [False, True])
def test_quota_failure_preserves_connection_cursor_and_watermark(monkeypatch, clock, fail_on_message):
    db = current_db()
    for name in ("gmail_connections", "gmail_suggestions", "tailoring_records"):
        db[name].delete_many({})
    last = datetime(2026, 10, 1, tzinfo=timezone.utc)
    before = datetime(2026, 10, 25, tzinfo=timezone.utc)
    db.gmail_connections.insert_one({"user_email": USER, "last_synced_at": last,
        "scan_next_page_token": "saved-page", "scan_query": "saved-query", "scan_before": before})
    db.tailoring_records.insert_one({"user_email": USER, "record_id": "r1",
        "jd_analysis": {"company": "Acme", "job_title": "Engineer"}, "application": {"status": "applied"}})
    monkeypatch.setattr(gs, "_get_access_token", lambda _: ("test-access", gs.get_connection(USER)))

    def get(url, **kwargs):
        if fail_on_message and url.endswith("/messages"):
            return response(200, payload={"messages": [{"id": "first"}, {"id": "failed"}], "nextPageToken": "future-page"})
        if fail_on_message and url.endswith("/first"):
            return response(200, payload={"internalDate": "1000", "payload": {}})
        return response(403, "userRateLimitExceeded")

    monkeypatch.setattr(gs.requests, "get", get)

    with pytest.raises(RuntimeError, match="request limit") as exc:
        gs.sync_user(USER)
    assert "connection is still active" in str(exc.value)
    assert "enabled" not in str(exc.value)
    connection = gs.get_connection(USER)
    assert connection["last_synced_at"].replace(tzinfo=timezone.utc) == last
    assert connection["scan_before"].replace(tzinfo=timezone.utc) == before
    assert connection["scan_query"] == "saved-query"
    assert connection["scan_next_page_token"] == "saved-page"
    assert "sync_lock_id" not in connection
    assert "last_sync_summary" not in connection


def test_safe_error_logging_never_includes_provider_body_or_message_id(monkeypatch, clock, caplog):
    monkeypatch.setattr(gs.requests, "get", lambda *_, **__: response(
        403, "insufficientPermissions", message="untrusted@example.test secret-provider-detail"))
    with pytest.raises(gs.GmailAPIError) as exc:
        gs._gmail_get("/users/me/messages/private-message-id", "test-access", raise_on_error=True)
    assert "read permission" in str(exc.value)
    for forbidden in ("untrusted@example.test", "secret-provider-detail", "private-message-id", "test-access"):
        assert forbidden not in caplog.text
        assert forbidden not in str(exc.value)


@pytest.mark.parametrize("payload", ["<html>temporarily unavailable</html>", "{}", '{"error": "unknown"}'])
def test_unstructured_provider_errors_do_not_masquerade_as_quota(payload):
    error = gs.GmailAPIError(403, payload)
    assert not error.retryable
    assert "request limit" not in str(error)


def test_google_error_info_quota_reason_is_retryable():
    error = gs.GmailAPIError(403, json.dumps({"error": {
        "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED"}],
    }}))
    assert error.retryable
    assert error.rate_limited
