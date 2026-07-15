"""Offline tests for the OPT-IN auto-submit backend (guardrailed apply feature).

Runs fully offline (mongomock via conftest/_vg_support; no Bedrock, no live
Mongo, no network). Two layers are exercised:

  * ROUTE layer (the jobs Lambda's apply blueprint) — master-flag gating,
    opt-in requirement, and cross-user isolation, driven through real Flask
    dispatch + JWT so the auth contract (never-401) is verified.
  * SERVICE layer (auto_submit_service) — the time-sensitive state machine
    (undo window, expiry, single-use, idempotency, audit) using injectable
    undo/ttl seconds + direct timestamp control instead of real sleeps.
"""
import sys

import pytest

from _vg_support import load_service_app, current_db

import services.auto_submit_service as ass
import services.application_ledger_service as als


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
_AUTO_SUBMIT_COLLECTIONS = (
    "auto_submit_settings",
    "auto_submit_approvals",
    "auto_submit_audit",
    "feature_usage",
    # The /auto-submit/pending endpoint now also bridges auto-apply LEDGER rows
    # in `ready` (extension submit path). Clear it too so ledger rows left by
    # other test files (shared mongomock) don't leak into these assertions.
    "applications",
)

USER_A = "alice@aspirely.me"
USER_B = "bob@aspirely.me"


@pytest.fixture()
def db():
    """Clean auto-submit collections for one test (mongomock, shared with the app)."""
    d = current_db()
    for c in _AUTO_SUBMIT_COLLECTIONS:
        d[c].delete_many({})
    return d


@pytest.fixture()
def app(monkeypatch):
    # Master flag ON for the happy-path tests; individual tests override it.
    monkeypatch.setenv("AUTO_SUBMIT_ENABLED", "true")
    return load_service_app("jobs")


def _token(flask_app, identity):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        return create_access_token(identity=identity)


def _client(flask_app, identity):
    tok = _token(flask_app, identity)
    c = flask_app.test_client()

    def call(method, path, json=None):
        return getattr(c, method)(
            path, headers={"Authorization": f"Bearer {tok}"}, json=json
        )

    return call


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------
def test_master_flag_off_disables_every_endpoint(db, monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT_ENABLED", "false")
    flask_app = load_service_app("jobs")
    call = _client(flask_app, USER_A)

    for method, path, body in [
        ("get", "/api/apply/auto-submit/settings", None),
        ("put", "/api/apply/auto-submit/settings", {"auto_submit_enabled": True}),
        ("post", "/api/apply/auto-submit/approve",
         {"application_id": "job-1", "answers": {"q": "a"}}),
        ("post", "/api/apply/auto-submit/cancel", {"application_id": "job-1"}),
        ("get", "/api/apply/auto-submit/pending", None),
        ("post", "/api/apply/auto-submit/mark-submitted", {"application_id": "job-1"}),
    ]:
        resp = call(method, path, body)
        assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}"
        assert resp.get_json().get("error") == "not_found"


def test_service_create_raises_when_flag_off(db, monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT_ENABLED", "false")
    ass.set_settings(USER_A, True)  # opted in, but master flag off
    with pytest.raises(ass.AutoSubmitDisabled):
        ass.create_approval(USER_A, "job-1", {"q": "a"})


# ---------------------------------------------------------------------------
# Opt-in is required (separate from the master flag)
# ---------------------------------------------------------------------------
def test_opt_in_required_to_approve(db, app):
    call = _client(app, USER_A)
    # Not opted in yet → 403 (never 401).
    resp = call("post", "/api/apply/auto-submit/approve",
                {"application_id": "job-1", "answers": {"q": "a"}})
    assert resp.status_code == 403
    assert resp.get_json().get("error") == "auto_submit_opt_in_required"


def test_settings_roundtrip_and_opt_in_default_false(db, app):
    call = _client(app, USER_A)
    resp = call("get", "/api/apply/auto-submit/settings", None)
    assert resp.status_code == 200
    assert resp.get_json()["auto_submit_enabled"] is False

    resp = call("put", "/api/apply/auto-submit/settings", {"auto_submit_enabled": True})
    assert resp.status_code == 200
    assert resp.get_json()["auto_submit_enabled"] is True

    resp = call("get", "/api/apply/auto-submit/settings", None)
    assert resp.get_json()["auto_submit_enabled"] is True


def test_settings_put_rejects_non_bool(db, app):
    call = _client(app, USER_A)
    resp = call("put", "/api/apply/auto-submit/settings", {"auto_submit_enabled": "yes"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Approve creates a record with an undo window
# ---------------------------------------------------------------------------
def test_approve_creates_record_with_undo_window(db, app):
    call = _client(app, USER_A)
    call("put", "/api/apply/auto-submit/settings", {"auto_submit_enabled": True})

    resp = call("post", "/api/apply/auto-submit/approve",
                {"application_id": "job-1", "answers": {"q1": "grounded answer"}})
    assert resp.status_code == 201
    rec = resp.get_json()["approval"]
    assert rec["status"] == "approved"
    assert rec["source"] == "in_app"
    assert rec["undo_until"] is not None
    assert rec["expires_at"] is not None
    assert rec["eligible"] is False  # still inside the undo window
    # Exactly one approval persisted.
    assert db.auto_submit_approvals.count_documents({"user_email": USER_A}) == 1


def test_reapprove_is_idempotent_noop(db, app):
    call = _client(app, USER_A)
    call("put", "/api/apply/auto-submit/settings", {"auto_submit_enabled": True})
    r1 = call("post", "/api/apply/auto-submit/approve",
              {"application_id": "job-1", "answers": {"q": "a"}})
    assert r1.status_code == 201
    r2 = call("post", "/api/apply/auto-submit/approve",
              {"application_id": "job-1", "answers": {"q": "different"}})
    assert r2.status_code == 200
    assert r2.get_json()["created"] is False
    # Still exactly one record; the snapshot is the ORIGINAL approval.
    assert db.auto_submit_approvals.count_documents({"user_email": USER_A}) == 1
    assert r2.get_json()["approval"]["approval_id"] == r1.get_json()["approval"]["approval_id"]


# ---------------------------------------------------------------------------
# Cannot submit before the undo window elapses
# ---------------------------------------------------------------------------
def test_cannot_submit_before_undo_until(db, app):
    call = _client(app, USER_A)
    call("put", "/api/apply/auto-submit/settings", {"auto_submit_enabled": True})
    call("post", "/api/apply/auto-submit/approve",
         {"application_id": "job-1", "answers": {"q": "a"}})

    # Default undo window is 60s → immediate submit is not yet eligible.
    resp = call("post", "/api/apply/auto-submit/mark-submitted", {"application_id": "job-1"})
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "not_eligible"

    # And it is NOT in the eligible pending queue (it is in `waiting`).
    pending = call("get", "/api/apply/auto-submit/pending", None).get_json()
    assert pending["pending"] == []
    assert len(pending["waiting"]) == 1


# ---------------------------------------------------------------------------
# Eligible after undo, then submit; double-submit is a 409
# ---------------------------------------------------------------------------
def test_eligible_after_undo_then_submit_then_double_submit_409(db):
    ass.set_settings(USER_A, True)
    # undo=0 → eligible immediately; ttl generous.
    rec, created = ass.create_approval(USER_A, "job-1", {"q": "a"},
                                       undo_seconds=0, ttl_seconds=900)
    assert created is True

    pending = ass.list_pending_eligible(USER_A)
    assert len(pending["pending"]) == 1
    assert pending["pending"][0]["eligible"] is True

    submitted = ass.mark_submitted(USER_A, "job-1")
    assert submitted["status"] == "submitted"
    assert submitted["submitted_at"] is not None

    # Second submit is an idempotent conflict, never a second real submission.
    with pytest.raises(ass.AlreadyFinal):
        ass.mark_submitted(USER_A, "job-1")


# ---------------------------------------------------------------------------
# Cancel within the undo window
# ---------------------------------------------------------------------------
def test_cancel_within_window_then_cannot_submit(db, app):
    call = _client(app, USER_A)
    call("put", "/api/apply/auto-submit/settings", {"auto_submit_enabled": True})
    call("post", "/api/apply/auto-submit/approve",
         {"application_id": "job-1", "answers": {"q": "a"}})

    resp = call("post", "/api/apply/auto-submit/cancel", {"application_id": "job-1"})
    assert resp.status_code == 200
    assert resp.get_json()["approval"]["status"] == "cancelled"

    # A cancelled approval cannot be submitted.
    resp = call("post", "/api/apply/auto-submit/mark-submitted", {"application_id": "job-1"})
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "already_final"


# ---------------------------------------------------------------------------
# Expired approval can't submit
# ---------------------------------------------------------------------------
def test_expired_approval_cannot_submit(db):
    ass.set_settings(USER_A, True)
    rec, _ = ass.create_approval(USER_A, "job-1", {"q": "a"},
                                 undo_seconds=0, ttl_seconds=900)
    # Force expiry by moving expires_at into the past.
    from datetime import datetime, timezone, timedelta
    db_conn = current_db()
    db_conn.auto_submit_approvals.update_one(
        {"approval_id": rec["approval_id"]},
        {"$set": {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}},
    )

    with pytest.raises(ass.NotEligible):
        ass.mark_submitted(USER_A, "job-1")

    # It transitioned to `expired` and is not in the eligible queue.
    doc = db_conn.auto_submit_approvals.find_one({"approval_id": rec["approval_id"]})
    assert doc["status"] == "expired"
    assert ass.list_pending_eligible(USER_A)["pending"] == []


# ---------------------------------------------------------------------------
# Audit log records every transition, redacted of answer text
# ---------------------------------------------------------------------------
def test_audit_log_records_transitions_and_redacts_pii(db):
    secret_answer = "my SSN is 078-05-1120 and I earn $200k"
    ass.set_settings(USER_A, True)
    ass.create_approval(USER_A, "job-1", {"q": secret_answer},
                        undo_seconds=0, ttl_seconds=900)
    ass.mark_submitted(USER_A, "job-1")

    audit = current_db().auto_submit_audit
    actions = [d["action"] for d in audit.find({"user_email": USER_A})]
    assert "settings_enabled" in actions
    assert "approved" in actions
    assert "submitted" in actions

    # No audit row may contain the raw answer text anywhere in its serialized form.
    import json
    for d in audit.find({"user_email": USER_A}):
        d.pop("_id", None)
        blob = json.dumps(d, default=str)
        assert secret_answer not in blob
        assert "078-05-1120" not in blob


# ---------------------------------------------------------------------------
# Cross-user isolation — B can never touch A's application
# ---------------------------------------------------------------------------
def test_cross_user_isolation_service(db):
    ass.set_settings(USER_A, True)
    ass.set_settings(USER_B, True)
    ass.create_approval(USER_A, "job-shared", {"q": "a-answer"},
                        undo_seconds=0, ttl_seconds=900)

    # B has no approval for A's application id → not found on submit/cancel.
    with pytest.raises(ass.ApprovalNotFound):
        ass.mark_submitted(USER_B, "job-shared")
    with pytest.raises(ass.ApprovalNotFound):
        ass.cancel_approval(USER_B, "job-shared")

    # B's pending queue is empty; A's still has the record.
    assert ass.list_pending_eligible(USER_B)["pending"] == []
    assert len(ass.list_pending_eligible(USER_A)["pending"]) == 1

    # B approving the SAME application id creates a SEPARATE record under B, and
    # A's record is untouched.
    ass.create_approval(USER_B, "job-shared", {"q": "b-answer"},
                        undo_seconds=0, ttl_seconds=900)
    d = current_db().auto_submit_approvals
    assert d.count_documents({"application_id": "job-shared"}) == 2
    a_doc = d.find_one({"user_email": USER_A, "application_id": "job-shared"})
    assert a_doc["status"] == "approved"  # not clobbered by B


def test_cross_user_isolation_route(db, app):
    # A approves through the route.
    call_a = _client(app, USER_A)
    call_a("put", "/api/apply/auto-submit/settings", {"auto_submit_enabled": True})
    call_a("post", "/api/apply/auto-submit/approve",
           {"application_id": "job-x", "answers": {"q": "a"}})

    # B (opted in) tries to submit A's application → 404 approval_not_found.
    call_b = _client(app, USER_B)
    call_b("put", "/api/apply/auto-submit/settings", {"auto_submit_enabled": True})
    resp = call_b("post", "/api/apply/auto-submit/mark-submitted", {"application_id": "job-x"})
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "approval_not_found"


# ---------------------------------------------------------------------------
# WhatsApp channel plumbing (source=whatsapp), same guardrails
# ---------------------------------------------------------------------------
def test_whatsapp_hook_creates_approved_record(db):
    ass.set_settings(USER_A, True)
    rec = ass.create_whatsapp_approval(USER_A, "job-1", {"q": "a"})
    assert rec["source"] == "whatsapp"
    assert rec["status"] == "approved"


def test_whatsapp_hook_requires_opt_in(db):
    # Not opted in → even the WhatsApp hook refuses (both gates always required).
    with pytest.raises(ass.OptInRequired):
        ass.create_whatsapp_approval(USER_A, "job-1", {"q": "a"})


if __name__ == "__main__":  # allow `python tests/test_auto_submit.py`
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# Extension ↔ auto-apply LEDGER bridge (the wired submit path)
# ---------------------------------------------------------------------------
def _ready_ledger_app(user=USER_A):
    view, _ = als.create_application(
        user, company="Acme", title="Cloud Engineer",
        jd_text="Build AWS infra with Terraform and Kubernetes.",
        source="greenhouse", ats_family="greenhouse",
        apply_url="https://job-boards.greenhouse.io/acme/jobs/999")
    aid = view["application_id"]
    als.mark_gathering(user, aid)
    als.mark_ready(user, aid, autonomy="auto", confidence_score=0.95, tailoring_record_id=None)
    return aid


def test_pending_surfaces_ledger_ready_app(db, app):
    aid = _ready_ledger_app()
    call = _client(app, USER_A)
    pending = call("get", "/api/apply/auto-submit/pending", None).get_json()
    rec = next((p for p in pending["pending"] if p.get("application_id") == aid), None)
    assert rec is not None
    assert rec["status"] == "approved" and rec["eligible"] is True
    assert rec["source"] == "ledger"
    assert rec["apply_url"] == "https://job-boards.greenhouse.io/acme/jobs/999"


def test_mark_submitted_bridges_to_ledger(db, app):
    aid = _ready_ledger_app()
    call = _client(app, USER_A)
    resp = call("post", "/api/apply/auto-submit/mark-submitted", {"application_id": aid})
    assert resp.status_code == 200
    assert resp.get_json()["approval"]["source"] == "ledger"
    row = als.get_application(USER_A, aid)
    assert row["status"] == als.STATUS_SUBMITTED
