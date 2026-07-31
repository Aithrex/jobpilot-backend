"""CHARACTERIZATION — asserts CURRENT behavior to guard the E/F/G refactor;
update deliberately when that behavior is intentionally changed.

Scope: the READ paths that aggregate `tailoring_records.application.status` (and
the applications-ledger `needs_input` rows) into the surfaces the user sees —
the Applications board funnel + the Today Inbox. The canonical-applications
refactor changes how status is stored/derived; these golden snapshots make any
drift in the READER OUTPUT (counts / grouping / merged shape) fail here instead
of in production.

Two readers are exercised directly at the service layer (no route/JWT wiring
needed — both are pure, deterministic, LLM-free Mongo + arithmetic):

  * pipeline_intel_service.funnel() — the documented "Applications board"
    aggregation: cumulative stage counts + conversion rates over
    application.status. (pipeline_intel's own docstring names tailoring_records
    "the Applications board".)
  * today_inbox_service.get_today_inbox() — the unified pending-decisions feed;
    we pin the ledger needs_input source + the did_you_apply source merge shape.

Runs fully offline (mongomock; no network / Bedrock). The heavier
`job_intelligence_service.get_funnel_analytics` per-status counter is a THIRD
board reader but it calls the LLM for a narrative insight and lives in the
resume service's local package (not the shared layer), so it is left to a
route/service test the E-team owns — see the gap note in the handoff.
"""
from datetime import datetime, timezone

import pytest

from _vg_support import current_db

import services.pipeline_intel_service as pintel
import services.today_inbox_service as inbox
import services.application_ledger_service as als

USER = "alice@aspirely.me"


@pytest.fixture()
def board_db(monkeypatch):
    # Keep the auto_submit + gmail today-inbox sources cleanly OFF so the feed is
    # deterministic (matches tests/test_today_inbox.py).
    monkeypatch.delenv("AUTO_SUBMIT_ENABLED", raising=False)
    d = current_db()
    for c in ("tailoring_records", "applications"):
        d[c].delete_many({})
    return d


def _seed_status(db, record_id, status, *, applied_at=None, interview_dates=None,
                 company="Acme", title="Engineer"):
    app = {"status": status}
    if applied_at is not None:
        app["applied_at"] = applied_at
    if interview_dates is not None:
        app["interview_dates"] = interview_dates
    db.tailoring_records.insert_one({
        "record_id": record_id,
        "user_email": USER,
        "jd_analysis": {"company": company, "job_title": title},
        "application": app,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    })


# ---------------------------------------------------------------------------
# Applications board funnel (pipeline_intel_service.funnel)
# ---------------------------------------------------------------------------
class TestBoardFunnel:
    def test_cumulative_stage_counts_and_conversions_golden(self, board_db):
        # One record at each stage the board recognizes. Stages are CUMULATIVE
        # ("reached at least"): offer also counts toward applied + interview.
        _seed_status(board_db, "r-draft", "draft")           # stage 0
        _seed_status(board_db, "r-applied", "applied")       # stage 1
        _seed_status(board_db, "r-interview", "interviewing")  # stage 2
        _seed_status(board_db, "r-offer", "offer")           # stage 3
        _seed_status(board_db, "r-rejected", "rejected")     # stage 1 (was applied)

        out = pintel.funnel(USER)

        # applied>=1: applied, interviewing, offer, rejected = 4
        # interview>=2: interviewing, offer = 2
        # offer>=3: offer = 1  ;  tracked = all 5 records (draft included)
        assert out["stages"] == [
            {"key": "applied", "label": "Applied", "count": 4},
            {"key": "interview", "label": "Interview", "count": 2},
            {"key": "offer", "label": "Offer", "count": 1},
        ]
        assert out["conversion"] == {
            "applied_to_interview": 0.5,   # 2/4
            "interview_to_offer": 0.5,     # 1/2
            "applied_to_offer": 0.25,      # 1/4
        }
        assert out["totals"] == {
            "tracked": 5, "applied": 4, "interview": 2, "offer": 1}
        # Untracked stages are NAMED, never faked, in the payload. GOLDEN.
        assert out["untracked_stages"] == ["viewed", "online_assessment"]

    def test_valid_user_no_records_yields_zero_count_stages(self, board_db):
        # A valid user with an empty board still returns the full stage
        # scaffold with 0 counts + None conversions (NOT the blank shape — that
        # is reserved for a blank/invalid email, see below). GOLDEN.
        assert pintel.funnel(USER) == {
            "stages": [
                {"key": "applied", "label": "Applied", "count": 0},
                {"key": "interview", "label": "Interview", "count": 0},
                {"key": "offer", "label": "Offer", "count": 0},
            ],
            "conversion": {
                "applied_to_interview": None,
                "interview_to_offer": None,
                "applied_to_offer": None,
            },
            "totals": {"tracked": 0, "applied": 0, "interview": 0, "offer": 0},
            "untracked_stages": ["viewed", "online_assessment"],
        }

    def test_blank_user_yields_the_empty_shape(self, board_db):
        assert pintel.funnel("") == {
            "stages": [], "conversion": {}, "totals": {}, "untracked_stages": []}

    def test_conversion_is_none_not_zero_when_no_denominator(self, board_db):
        # A single draft record: nobody has "applied", so applied_to_interview
        # has a 0 denominator -> None ("no data" is honestly NOT "0%").
        _seed_status(board_db, "r-draft", "draft")
        out = pintel.funnel(USER)
        assert out["conversion"]["applied_to_interview"] is None
        assert out["totals"]["applied"] == 0

    def test_terminal_negative_status_implies_submission(self, board_db):
        # rejected/withdrawn/ghosted imply the app WAS submitted (stage>=1) even
        # with no applied_at; interview stage only if an interview was recorded.
        _seed_status(board_db, "r-ghosted", "ghosted")
        _seed_status(board_db, "r-rej-iv", "rejected",
                     interview_dates=[datetime(2026, 2, 1, tzinfo=timezone.utc)])
        out = pintel.funnel(USER)
        assert out["totals"]["applied"] == 2      # both count as applied
        assert out["totals"]["interview"] == 1    # only the one with an interview


# ---------------------------------------------------------------------------
# Today Inbox aggregation (today_inbox_service.get_today_inbox)
# ---------------------------------------------------------------------------
def _seed_needs_input(db, aid, created_at, *, questions=None,
                      company="Acme", title="Engineer"):
    db.applications.insert_one({
        "application_id": aid,
        "user_email": USER,
        "status": als.STATUS_NEEDS_INPUT,
        "company": company,
        "title": title,
        "needs_input": questions if questions is not None else [
            {"question": "Desired salary?", "kind": "value", "field_id": "f1"}],
        "created_at": created_at,
    })


def _seed_did_you_apply(db, rid, created_at):
    db.tailoring_records.insert_one({
        "record_id": rid,
        "user_email": USER,
        "application": {"batch_confirmation_pending": True},
        "jd_analysis": {"company": "Globex", "job_title": "Data Scientist"},
        "created_at": created_at,
    })


class TestTodayInbox:
    def test_merged_shape_and_newest_first_golden(self, board_db):
        older = datetime(2026, 1, 1, tzinfo=timezone.utc)
        newer = datetime(2026, 6, 1, tzinfo=timezone.utc)
        _seed_needs_input(board_db, "app-1", older)
        _seed_did_you_apply(board_db, "rec-1", newer)
        out = inbox.get_today_inbox(USER)
        assert out["count"] == 2
        assert [i["kind"] for i in out["items"]] == ["did_you_apply", "needs_input"]
        # Uniform typed item shape across every source. GOLDEN.
        for it in out["items"]:
            assert set(it) == {"id", "kind", "title", "detail", "deep_link",
                               "action_label", "created_at"}

    def test_needs_input_item_is_kind_namespaced_and_detailed(self, board_db):
        _seed_needs_input(board_db, "app-1",
                          datetime(2026, 1, 1, tzinfo=timezone.utc))
        out = inbox.get_today_inbox(USER)
        item = out["items"][0]
        assert item["id"] == "needs_input:app-1"       # id namespaced by kind
        assert item["kind"] == "needs_input"
        assert item["deep_link"] == "/app/apply"
        assert item["action_label"] == "Review & answer"
        # single question -> its text becomes the detail line.
        assert item["detail"] == "Desired salary?"

    def test_multiple_questions_summarize_count(self, board_db):
        _seed_needs_input(
            board_db, "app-2", datetime(2026, 1, 1, tzinfo=timezone.utc),
            questions=[
                {"question": "Salary?", "kind": "value", "field_id": "f1"},
                {"question": "Notice period?", "kind": "value", "field_id": "f2"}])
        out = inbox.get_today_inbox(USER)
        assert out["items"][0]["detail"] == (
            "2 questions need your answer to continue applying.")

    def test_empty_and_blank_user_are_safe(self, board_db):
        assert inbox.get_today_inbox(USER) == {"count": 0, "items": []}
        assert inbox.get_today_inbox("") == {"count": 0, "items": []}
