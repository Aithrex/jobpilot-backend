"""Job-alert idempotency, deleted searches, unsubscribe."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from _vg_support import current_db
from services import tech_feed_alerts as alerts
from services import tech_feed_saved_searches as tfss

USER = "seeker@example.com"


@pytest.fixture(autouse=True)
def clean():
    db = current_db()
    db[tfss.SAVED_SEARCHES].delete_many({})
    db[alerts.DELIVERIES].delete_many({})
    db[alerts.FAILURES].delete_many({})
    db[alerts.METRICS].delete_many({})
    yield


def test_idempotency_key_is_stable():
    a = alerts.idempotency_key(USER, "s1", "job-1")
    b = alerts.idempotency_key(USER, "s1", "job-1")
    c = alerts.idempotency_key(USER, "s1", "job-2")
    assert a == b and a != c


def test_claim_delivery_is_once():
    alerts.ensure_indexes()
    assert alerts._claim_delivery(USER, "s1", "job-1") is True
    assert alerts._claim_delivery(USER, "s1", "job-1") is False


def test_deleted_search_does_not_notify():
    tfss.save(USER, client_id="gone", name="X",
              filters={"query": "rust"}, alerts={"enabled": True})
    tfss.delete(USER, "gone")
    out = alerts.evaluate_search({
        "user_email": USER, "client_id": "gone",
        "filters": {}, "alerts": {"enabled": True}, "name": "X",
    })
    assert out["status"] == "deleted"


def test_unsubscribed_is_suppressed(monkeypatch):
    tfss.save(USER, client_id="s1", name="X",
              filters={"query": "rust"}, alerts={"enabled": True})

    class FakeNotify:
        @staticmethod
        def get_prefs(_email):
            return {"unsubscribed": True, "channels": {"email": {"enabled": True}},
                    "events": {}}

        @staticmethod
        def send(*a, **k):
            raise AssertionError("must not send")

    import services.notifications.service as notify
    monkeypatch.setattr("services.notifications.service.get_prefs",
                        FakeNotify.get_prefs)
    monkeypatch.setattr("services.notifications.service.send", FakeNotify.send)
    out = alerts.evaluate_search({
        "user_email": USER, "client_id": "s1",
        "filters": {"query": "rust"}, "alerts": {"enabled": True}, "name": "X",
    })
    assert out["status"] == "unsubscribed"


def test_save_round_trips_alert_settings():
    tfss.save(USER, client_id="s1", name="SRE",
              filters={"query": "kubernetes"},
              alerts={"enabled": True, "timezone": "America/Los_Angeles"})
    row = tfss.list_for(USER)["searches"][0]
    assert row["alerts"]["enabled"] is True
    assert row["alerts"]["frequency"] == "daily"
    assert row["alerts"]["timezone"] == "America/Los_Angeles"
