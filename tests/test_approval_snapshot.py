"""Offline tests for DEFECT F — approval only from ``ready_for_review`` + a
frozen, re-validated approval snapshot.

The bug being fixed: the pre-F ``approve_to_ready`` accepted queued/gathering/
needs_input rows (an approval could race a still-gathering row) and froze NO
evidence of WHAT was approved — so a form / résumé / answer change between
approval and submission would submit a stale application. F splits the overloaded
``ready`` into ``ready_for_review`` → ``approved`` → ``queued_for_execution`` and:

  * approval is tightened to ONLY ``ready_for_review`` (tolerating a legacy
    ``ready``) → ``approved``;
  * approval FREEZES an immutable snapshot on the row (application_hash /
    resume_version_id / answers_fingerprint / policy_version / consent_version /
    approved_at / approved_by);
  * before the executor submits, the snapshot is RE-VALIDATED — a drift
    invalidates the approval back to ``ready_for_review`` (never submit stale);
  * autopilot (`auto`) and manual approvals converge on the same approved+snapshot
    state (approved_by ``system`` vs ``user``);
  * duplicate/replayed approvals are idempotent (no double transition, no
    re-freeze — belt-and-suspenders against a double-charge once G lands).

Runs fully offline (mongomock via conftest/_vg_support; no Bedrock / live Mongo /
network). The executor is injected via ``set_executor`` so submit runs end-to-end.
"""
import pytest

from _vg_support import current_db

import services.application_ledger_service as ledger
import services.auto_apply_service as aas
from services.auto_submit_service import _answers_fingerprint
from services.executor_adapter import StubExecutor, SUBMIT_SUCCESS

USER = "alice@aspirely.me"
_JD = "We are hiring a backend engineer to build distributed systems in Python."

_SNAP_COLLECTIONS = (
    "applications", "auto_submit_audit", "auto_apply_answers",
    "auto_apply_freezes",
)

_SNAPSHOT_FIELDS = {
    "application_hash", "resume_version_id", "answers_fingerprint",
    "policy_version", "consent_version", "approved_at", "approved_by",
}


@pytest.fixture()
def led_db():
    d = current_db()
    for c in _SNAP_COLLECTIONS:
        d[c].delete_many({})
    d.applications.create_index(
        [("user_email", 1), ("job_identity", 1)], unique=True,
        name="user_email_job_identity_unique")
    d.applications.create_index(
        [("application_id", 1)], unique=True, name="application_id_unique")
    return d


@pytest.fixture(autouse=True)
def _reset_executor():
    yield
    aas.set_executor(None)


class _SpyExecutor(StubExecutor):
    """Available executor that records whether submit() was actually called."""
    name = "spy"

    def __init__(self):
        self.submit_called = False

    def is_available(self):
        return True

    def submit(self, application, answers, resume_pdf):
        self.submit_called = True
        return {"status": SUBMIT_SUCCESS, "confirmation": "OK-1"}


def _create(company="Acme", title="Software Engineer"):
    view, _ = ledger.create_application(
        USER, company=company, title=title, jd_text=_JD, source="greenhouse",
        ats_family="greenhouse", apply_url="https://boards.greenhouse.io/x/jobs/1")
    return view["application_id"]


def _seat_ready_for_review(aid, *, answers=None, recon_fp="recon-1",
                           resume_version="rv-1", autonomy="ask"):
    """Drive a queued row to ready_for_review with the persisted answer/recon/
    résumé signals the approval snapshot keys off (the state gather/prepare leave
    a row in before approval)."""
    ledger.mark_gathering(USER, aid)
    aas._persist_answers(aid, USER, answers if answers is not None else {"email": "a@b.co"},
                         1.0, False, autonomy=autonomy, confidence_score=0.4,
                         recon_fingerprint=recon_fp, resume_version_id=resume_version)
    ledger.mark_ready_for_review(USER, aid, autonomy=autonomy, confidence_score=0.4)


def _approve_user(aid):
    return ledger.approve(
        USER, [aid], approved_by="user",
        snapshot_provider=lambda a: aas.build_approval_snapshot(USER, a))


# ---------------------------------------------------------------------------
# 1. Approval is tightened to ready_for_review; snapshot frozen with all fields
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("start", [
    ledger.STATUS_QUEUED, ledger.STATUS_GATHERING, ledger.STATUS_PREPARING,
])
def test_approve_rejected_from_non_review_states(led_db, start):
    aid = _create()
    led_db.applications.update_one(
        {"application_id": aid, "user_email": USER}, {"$set": {"status": start}})
    assert _approve_user(aid) == 0
    assert ledger.get_application(USER, aid)["status"] == start
    assert ledger.get_approval_snapshot(USER, aid) is None


def test_approve_from_ready_for_review_freezes_full_snapshot(led_db):
    aid = _create()
    answers = {"email": "a@b.co", "phone": "+1-555-0100"}
    _seat_ready_for_review(aid, answers=answers, recon_fp="recon-1",
                           resume_version="rv-1")

    assert _approve_user(aid) == 1
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_APPROVED

    snap = ledger.get_approval_snapshot(USER, aid)
    assert set(snap) == _SNAPSHOT_FIELDS
    assert snap["application_hash"] == "recon-1"
    assert snap["resume_version_id"] == "rv-1"
    assert snap["answers_fingerprint"] == _answers_fingerprint(answers)
    assert snap["policy_version"] == ledger._APPROVAL_POLICY_VERSION
    assert snap["consent_version"] == ledger._APPROVAL_CONSENT_VERSION
    assert snap["approved_by"] == "user"
    assert snap["approved_at"] is not None


def test_legacy_ready_is_tolerated_as_review(led_db):
    # A legacy `ready` row (pre-split) is approved as its canonical peer.
    aid = _create()
    _seat_ready_for_review(aid)
    led_db.applications.update_one(
        {"application_id": aid, "user_email": USER},
        {"$set": {"status": ledger.STATUS_READY}})
    assert _approve_user(aid) == 1
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_APPROVED


# ---------------------------------------------------------------------------
# 2. Re-validation before submit — drift invalidates, unchanged submits
# ---------------------------------------------------------------------------
def test_unchanged_snapshot_submits(led_db):
    aid = _create()
    _seat_ready_for_review(aid)
    _approve_user(aid)

    spy = _SpyExecutor()
    aas.set_executor(spy)
    out = aas.submit(aid, USER)

    assert spy.submit_called is True
    assert out["status"] == ledger.STATUS_SUBMITTED
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_SUBMITTED


def _mutate_answers_field(aid, field, value):
    current_db().auto_apply_answers.update_one(
        {"application_id": aid, "user_email": USER}, {"$set": {field: value}})


@pytest.mark.parametrize("field,value", [
    ("recon_fingerprint", "recon-CHANGED"),   # the form changed
    ("resume_version_id", "rv-CHANGED"),       # the pinned résumé changed
    ("answers", {"email": "DIFFERENT@b.co"}),  # the answers changed
])
def test_drifted_snapshot_invalidates_and_never_submits(led_db, field, value):
    aid = _create()
    _seat_ready_for_review(aid)
    _approve_user(aid)

    # Something drifts AFTER approval was frozen.
    _mutate_answers_field(aid, field, value)

    spy = _SpyExecutor()
    aas.set_executor(spy)
    out = aas.submit(aid, USER)

    assert spy.submit_called is False           # stale approval never submits
    assert out["status"] == "approval_invalidated"
    row = ledger.get_application(USER, aid)
    assert row["status"] == ledger.STATUS_READY_FOR_REVIEW
    # Invalidation voids the frozen snapshot; a fresh approval must re-freeze.
    assert ledger.get_approval_snapshot(USER, aid) is None


def test_reapproval_refreezes_snapshot(led_db):
    aid = _create()
    _seat_ready_for_review(aid, recon_fp="recon-1")
    _approve_user(aid)
    _mutate_answers_field(aid, "recon_fingerprint", "recon-2")

    # First submit invalidates (form drifted) → back to ready_for_review.
    aas.set_executor(_SpyExecutor())
    assert aas.submit(aid, USER)["status"] == "approval_invalidated"

    # Re-approve → a fresh snapshot pins the NEW form fingerprint.
    assert _approve_user(aid) == 1
    snap2 = ledger.get_approval_snapshot(USER, aid)
    assert snap2["application_hash"] == "recon-2"

    # Now the snapshot matches current state → submit proceeds.
    spy2 = _SpyExecutor()
    aas.set_executor(spy2)
    assert aas.submit(aid, USER)["status"] == ledger.STATUS_SUBMITTED
    assert spy2.submit_called is True


# ---------------------------------------------------------------------------
# 3. Duplicate / concurrent approve is idempotent (no double transition/freeze)
# ---------------------------------------------------------------------------
def test_duplicate_approve_is_idempotent(led_db):
    aid = _create()
    _seat_ready_for_review(aid)

    assert _approve_user(aid) == 1
    snap1 = ledger.get_approval_snapshot(USER, aid)

    # A replayed link / duplicate click → no second transition, no re-freeze.
    assert _approve_user(aid) == 0
    snap2 = ledger.get_approval_snapshot(USER, aid)
    assert snap2["approved_at"] == snap1["approved_at"]
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_APPROVED


# ---------------------------------------------------------------------------
# 4. Autopilot converges on approved+system snapshot; `ask` waits in review
# ---------------------------------------------------------------------------
class _ReconExecutor(StubExecutor):
    """Recon returns a plain (resolvable) required question so gather runs end to
    end without a browser or answer library."""
    name = "recon"

    def recon_form(self, apply_url, ats_family):
        return {"questions": [
            {"id": "q_email", "text": "What is your email address?",
             "type": "text", "required": True, "is_legal_attestation": False},
        ], "needs_account": False}


def test_autopilot_auto_reaches_approved_with_system_snapshot(led_db, monkeypatch):
    aas.set_executor(_ReconExecutor())
    # Force a clean auto decision with no gaps.
    monkeypatch.setattr(aas, "_resolve_answers",
                        lambda ue, qs, **_kw: ({"email": "a@b.co"}, [],
                                        {"coverage": 1.0, "has_novel_free_text": False,
                                         "has_unconfirmed_legal_attestation": False}))
    monkeypatch.setattr(aas, "_decide_autonomy",
                        lambda *a, **k: ("auto", 0.95))

    aid = _create()
    res = aas.gather(aid, USER)

    assert res["status"] == ledger.STATUS_APPROVED
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_APPROVED
    snap = ledger.get_approval_snapshot(USER, aid)
    assert snap is not None
    assert snap["approved_by"] == "system"          # autopilot, not a user click


def test_ask_waits_in_ready_for_review(led_db, monkeypatch):
    aas.set_executor(_ReconExecutor())
    monkeypatch.setattr(aas, "_resolve_answers",
                        lambda ue, qs, **_kw: ({"email": "a@b.co"}, [],
                                        {"coverage": 1.0, "has_novel_free_text": False,
                                         "has_unconfirmed_legal_attestation": False}))
    monkeypatch.setattr(aas, "_decide_autonomy",
                        lambda *a, **k: ("ask", 0.4))

    aid = _create()
    res = aas.gather(aid, USER)

    # `ask` (no gaps) waits for a MANUAL approval — never auto-approved.
    assert res["status"] == ledger.STATUS_READY_FOR_REVIEW
    assert ledger.get_application(USER, aid)["status"] == ledger.STATUS_READY_FOR_REVIEW
    assert ledger.get_approval_snapshot(USER, aid) is None
