"""E2 (defect E) — fold `auto_submit_approvals` INTO the applications ledger.

The pending queue becomes a THIN ADAPTER over the ledger's F approval states
(ready_for_review / approved / queued_for_execution) + the frozen
approval_snapshot, LABELED with `status` + an `already_approved` flag so defect
H's FE renders an already-approved row read-only. Everything is dark by default
(AUTO_APPLY_APPROVAL_FOLD, OFF) so the live queue is unchanged until flipped —
tests/test_auto_submit.py (flag off) is therefore untouched and still green.

Runs fully offline (mongomock; no Bedrock/network).
"""
import pytest

from _vg_support import current_db, load_service_app

import services.auto_submit_service as ass
import services.application_ledger_service as als

USER = "alice@aspirely.me"


@pytest.fixture()
def db():
    d = current_db()
    for c in ("applications", "auto_submit_approvals", "auto_submit_audit",
              "auto_submit_settings", "feature_usage"):
        d[c].delete_many({})
    return d


def _ready_for_review(user=USER, *, apply_url="https://job-boards/acme/999"):
    """Drive a canonical row through the F path to `ready_for_review`."""
    view, _ = als.create_application(
        user, company="Acme", title="Cloud Engineer",
        jd_text="Build AWS infra with Terraform and Kubernetes.",
        source="greenhouse", ats_family="greenhouse", apply_url=apply_url)
    aid = view["application_id"]
    als.mark_gathering(user, aid)
    als.mark_ready_for_review(user, aid, autonomy="auto", confidence_score=0.95)
    return aid


# ---------------------------------------------------------------------------
# Dark by default — the fold contributes NOTHING until the flag is on
# ---------------------------------------------------------------------------
def test_fold_is_dark_by_default(db, monkeypatch):
    monkeypatch.delenv("AUTO_APPLY_APPROVAL_FOLD", raising=False)
    _ready_for_review()
    assert ass.approval_fold_enabled() is False
    assert ass.list_canonical_approvals(USER) == []


# ---------------------------------------------------------------------------
# The fold surfaces canonical rows LABELED with status + already_approved
# ---------------------------------------------------------------------------
class TestCanonicalFold:
    def test_ready_for_review_is_awaiting_not_yet_approved(self, db, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        aid = _ready_for_review()
        rows = ass.list_canonical_approvals(USER)
        rec = next((r for r in rows if r["application_id"] == aid), None)
        assert rec is not None
        assert rec["status"] == "ready_for_review"
        assert rec["already_approved"] is False
        assert rec["eligible"] is False        # awaits the user's go-ahead
        assert rec["source"] == "ledger"

    def test_ledger_approve_drives_already_approved(self, db, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        aid = _ready_for_review()

        # `ledger.approve()` is what the fold reports on — approve then re-read.
        moved = als.approve(USER, [aid], approved_by="user")
        assert moved == 1

        rows = ass.list_canonical_approvals(USER)
        rec = next((r for r in rows if r["application_id"] == aid), None)
        assert rec is not None
        assert rec["status"] == "approved"
        assert rec["already_approved"] is True   # H renders this read-only
        assert rec["eligible"] is True           # cleared to submit
        # The F snapshot was frozen at approval.
        assert als.get_approval_snapshot(USER, aid) is not None

    def test_approve_ledger_adapter_delegates(self, db, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        aid = _ready_for_review()
        assert ass.approve_ledger(USER, [aid], approved_by="system") == 1
        assert als.get_application(USER, aid)["status"] == als.STATUS_APPROVED

    def test_needs_input_surfaces_in_attention_payload(self, db, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        view, _ = als.create_application(
            USER, company="EnergyHub", title="SWE",
            jd_text="Build support tooling.",
            source="greenhouse", ats_family="greenhouse",
            apply_url="https://job-boards.greenhouse.io/energyhub/jobs/1")
        aid = view["application_id"]
        als.mark_gathering(USER, aid)
        gaps = [{"question": "Alarm.com dealer?", "kind": "screening",
                 "field_id": "q1"}]
        als.mark_needs_input(USER, aid, gaps, autonomy="ask", confidence_score=0.7)
        rows = ass.list_canonical_approvals(USER)
        rec = next((r for r in rows if r["application_id"] == aid), None)
        assert rec is not None
        assert rec["status"] == "needs_input"
        assert rec["already_approved"] is False
        assert rec["eligible"] is False
        assert rec["needs_input"][0]["question"] == "Alarm.com dealer?"

    def test_account_gate_gets_synthetic_needs_input(self, db, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        view, _ = als.create_application(
            USER, company="Wells Fargo", title="SWE",
            jd_text="Java full stack.",
            source="workday", ats_family="workday",
            apply_url="https://wf.wd1.myworkdayjobs.com/job/1")
        aid = view["application_id"]
        als.mark_gathering(USER, aid)
        als.mark_needs_input(USER, aid, [], autonomy="hold", confidence_score=0.65)
        # Gather persisted needs_account on the answer sheet with an empty gap list.
        db.auto_apply_answers.insert_one({
            "application_id": aid, "user_email": USER,
            "needs_account": True, "answers": {}, "coverage": 1.0,
        })
        rows = ass.list_canonical_approvals(USER)
        rec = next((r for r in rows if r["application_id"] == aid), None)
        assert rec is not None
        assert rec["status"] == "needs_input"
        assert rec["needs_input"]
        assert "account" in rec["needs_input"][0]["question"].lower()


# ---------------------------------------------------------------------------
# Route: /auto-submit/pending prefers the fold when the flag is on
# ---------------------------------------------------------------------------
def test_route_pending_labels_approved_rows(db, monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT_ENABLED", "true")
    monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
    aid = _ready_for_review()
    als.approve(USER, [aid], approved_by="user")

    flask_app = load_service_app("jobs")
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        token = create_access_token(identity=USER)
    resp = flask_app.test_client().get(
        "/api/apply/auto-submit/pending",
        headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    pending = resp.get_json()["pending"]
    rec = next((p for p in pending if p.get("application_id") == aid), None)
    assert rec is not None
    assert rec["already_approved"] is True
    assert rec["status"] == "approved"
    assert rec["source"] == "ledger"
