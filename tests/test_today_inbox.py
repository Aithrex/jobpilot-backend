"""Offline tests for the Today Inbox aggregator (read-only pending-decisions feed).

Runs fully offline (mongomock; no network). The load-bearing property is
per-source ERROR ISOLATION: a single failing/disabled source must contribute an
empty list and NEVER 500 the whole feed. We also pin the merged shape and the
newest-first ordering.

The auto_submit and gmail sources are OFF by default here (master flags unset /
no connected inbox), so they cleanly contribute nothing — exactly the graceful
degradation the feed promises.
"""
from datetime import datetime, timezone

import pytest

from _vg_support import current_db

import services.today_inbox_service as inbox
import services.application_ledger_service as als

USER = "alice@aspirely.me"


@pytest.fixture()
def inbox_db(monkeypatch):
    monkeypatch.delenv("AUTO_SUBMIT_ENABLED", raising=False)  # source 2 off
    d = current_db()
    for c in ("applications", "tailoring_records"):
        d[c].delete_many({})
    return d


def _insert_needs_input_row(db, aid, created_at, *, company="Acme", title="Engineer"):
    db.applications.insert_one({
        "application_id": aid,
        "user_email": USER,
        "status": als.STATUS_NEEDS_INPUT,
        "company": company,
        "title": title,
        "needs_input": [{"question": "Salary?", "kind": "value", "field_id": "f1"}],
        "created_at": created_at,
    })


def _insert_did_you_apply(db, rid, created_at):
    db.tailoring_records.insert_one({
        "record_id": rid,
        "user_email": USER,
        "application": {"batch_confirmation_pending": True},
        "jd_analysis": {"company": "Globex", "job_title": "Data Scientist"},
        "created_at": created_at,
    })


# ---------------------------------------------------------------------------
# Aggregation + shape
# ---------------------------------------------------------------------------
class TestAggregation:
    def test_merges_sources_newest_first(self, inbox_db):
        older = datetime(2026, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2026, 6, 1, tzinfo=timezone.utc)
        _insert_needs_input_row(inbox_db, "app-1", older)
        _insert_did_you_apply(inbox_db, "rec-1", newer)
        out = inbox.get_today_inbox(USER)
        assert out["count"] == 2
        kinds = [i["kind"] for i in out["items"]]
        assert kinds == ["did_you_apply", "needs_input"]  # newest first
        # Every item carries the uniform typed shape.
        for it in out["items"]:
            assert set(it) >= {"id", "kind", "title", "detail", "deep_link",
                               "action_label", "created_at"}

    def test_needs_input_item_ids_are_kind_namespaced(self, inbox_db):
        _insert_needs_input_row(inbox_db, "app-1", datetime(2026, 1, 1, tzinfo=timezone.utc))
        out = inbox.get_today_inbox(USER)
        assert out["items"][0]["id"] == "needs_input:app-1"

    def test_empty_when_nothing_pending(self, inbox_db):
        out = inbox.get_today_inbox(USER)
        assert out == {"count": 0, "items": []}

    def test_blank_user_is_safe_noop(self, inbox_db):
        assert inbox.get_today_inbox("") == {"count": 0, "items": []}


# ---------------------------------------------------------------------------
# Per-source error isolation — the load-bearing guarantee
# ---------------------------------------------------------------------------
class TestErrorIsolation:
    def test_failing_source_yields_empty_not_500(self, inbox_db, monkeypatch):
        # Force the ledger source to blow up mid-collect.
        import services.application_ledger_service as als
        monkeypatch.setattr(
            als, "list_applications",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
        # A healthy source still contributes.
        _insert_did_you_apply(inbox_db, "rec-1", datetime(2026, 6, 1, tzinfo=timezone.utc))
        out = inbox.get_today_inbox(USER)  # must not raise
        assert out["count"] == 1
        assert out["items"][0]["kind"] == "did_you_apply"

    def test_all_sources_failing_returns_empty_feed(self, inbox_db, monkeypatch):
        import services.application_ledger_service as als
        monkeypatch.setattr(
            als, "list_applications",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        # tailoring_records query will simply find nothing; feed stays empty, no raise.
        out = inbox.get_today_inbox(USER)
        assert out == {"count": 0, "items": []}
