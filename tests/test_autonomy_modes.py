"""Offline tests for the explicit autonomy MODES (Copilot / Approval-first /
Autopilot) — auto-apply-defects-plan.md "## Autonomy MODES", layered on defect F.

LOCKED decisions built to EXACTLY:
  * no implicit default — onboarding forces the pick; a mode-unset row REFUSES to
    auto-act (fail-safe);
  * `mode` on `auto_submit_settings.mode` is the SINGLE source of truth;
    managed_mailboxes.mode reads THROUGH to it;
  * copilot = prepare-only (no automation): a would-be-`auto` row lands in
    ready_for_review flagged manual_apply + an answer sheet, never system-approved,
    never dispatched;
  * approval = every application waits: a raw `auto` is capped to `ask`;
  * autopilot honors `auto` ONLY past the graduation gate (N=10 user-approved
    confirmations); below it `auto` -> `ask`;
  * hard never-fabricate rules (unconfirmed legal attestation -> ask) win in ALL
    modes, INCLUDING autopilot.

The mode gate is dark-launched behind AUTO_APPLY_MODES (default OFF) so every
existing gather/prepare/approval test stays byte-identical; these tests flip it
ON. Runs fully offline (mongomock via conftest/_vg_support; no Bedrock/network).
"""
import secrets

import pytest

from _vg_support import current_db, load_service_app

import services.autonomy_service as auto
import services.auto_apply_service as aas
import services.application_ledger_service as ledger
import services.managed_mailbox_service as mmb
import services.resume_service as rs
from services.executor_adapter import StubExecutor

USER = "alice@aspirely.me"
USER_B = "bob@aspirely.me"

_MODE_COLLECTIONS = (
    "applications", "auto_submit_audit", "auto_apply_answers",
    "auto_apply_freezes", "auto_submit_settings", "resume_jobs",
    "managed_mailboxes", "mail_aliases", "feature_usage",
)


@pytest.fixture()
def mdb():
    d = current_db()
    for c in _MODE_COLLECTIONS:
        d[c].delete_many({})
    return d


@pytest.fixture(autouse=True)
def _reset_executor():
    yield
    aas._executor = None


@pytest.fixture(autouse=True)
def _modes_on(monkeypatch):
    """Flip the dark-launch flag ON for the gather-path mode tests (default OFF)."""
    monkeypatch.setenv("AUTO_APPLY_MODES", "true")
    yield


class _ReconExecutor(StubExecutor):
    """Recon returns a plain resolvable required question; NOT available for
    submit (StubExecutor default) so any dispatched submit no-ops. Gather's
    answer/decision are monkeypatched, so the questions are just a non-empty form."""
    name = "recon"

    def recon_form(self, apply_url, ats_family):
        return {"questions": [
            {"id": "q_email", "text": "What is your email address?",
             "type": "text", "required": True, "is_legal_attestation": False},
        ], "needs_account": False}


def _no_gaps_auto(monkeypatch, autonomy="auto", confidence=0.95):
    """Force a clean, gap-free decision so ONLY the mode gate decides the landing."""
    monkeypatch.setattr(aas, "_resolve_answers",
                        lambda ue, qs: ({"q_email": "a@b.co"}, [],
                                        {"coverage": 1.0,
                                         "has_novel_free_text": False,
                                         "has_unconfirmed_legal_attestation": False}))
    monkeypatch.setattr(aas, "_decide_autonomy",
                        lambda *a, **k: (autonomy, confidence))


@pytest.fixture()
def dispatches(monkeypatch):
    """Capture async dispatches instead of spawning daemon-thread workers, so
    'not dispatched' is asserted deterministically."""
    seen = []
    monkeypatch.setattr(rs.ResumeService, "invoke_async",
                        staticmethod(lambda job_id, job_type, payload: seen.append(
                            (job_type, payload.get("application_id")))))
    return seen


def _create():
    view, _ = ledger.create_application(
        USER, company="Acme", title="Software Engineer",
        jd_text="Build distributed systems in Python.", source="greenhouse",
        ats_family="greenhouse", apply_url="https://boards.greenhouse.io/x/jobs/1")
    return view["application_id"]


def _seed_confirmed(d, n, *, approved_by="user", user=USER, with_snapshot=True):
    docs = []
    for _ in range(n):
        doc = {
            "application_id": "seed-" + secrets.token_hex(8),
            "user_email": user,
            "job_identity": secrets.token_hex(32),   # 64-hex, unique
            "status": "confirmed",
        }
        if with_snapshot:
            doc["approval_snapshot"] = {"approved_by": approved_by}
        docs.append(doc)
    if docs:
        d.applications.insert_many(docs)


# ===========================================================================
# copilot — prepare-only: would-be-auto row NEVER auto-approves / dispatches;
# lands ready_for_review + manual_apply + answer sheet.
# ===========================================================================
def test_copilot_would_be_auto_lands_manual_review_never_dispatches(mdb, monkeypatch, dispatches):
    auto.set_mode(USER, auto.MODE_COPILOT)
    aas.set_executor(_ReconExecutor())
    _no_gaps_auto(monkeypatch)

    aid = _create()
    res = aas.gather(aid, USER)

    # Capped: `auto` -> `ask`; the row WAITS in review (never execution).
    assert res["mode"] == "copilot"
    assert res["manual_apply"] is True
    assert res["autonomy"] == "ask"
    assert res["status"] == ledger.STATUS_READY_FOR_REVIEW

    row = ledger.get_application(USER, aid)
    assert row["status"] == ledger.STATUS_READY_FOR_REVIEW
    # NOT system-approved (a system-approval freezes a snapshot).
    assert ledger.get_approval_snapshot(USER, aid) is None
    # NOT dispatched to the executor / submit.
    assert dispatches == []

    # The answer sheet is present for the manual apply.
    sheet = aas.answer_sheet(USER, aid)
    assert sheet["manual_apply"] is True
    assert sheet["mode"] == "copilot"
    assert sheet["answers"] == {"q_email": "a@b.co"}


# ===========================================================================
# approval — would-be-auto row downgraded to ask (waits for manual approve);
# never system-approves.
# ===========================================================================
def test_approval_downgrades_auto_to_ask_and_waits(mdb, monkeypatch, dispatches):
    auto.set_mode(USER, auto.MODE_APPROVAL)
    aas.set_executor(_ReconExecutor())
    _no_gaps_auto(monkeypatch)

    aid = _create()
    res = aas.gather(aid, USER)

    assert res["mode"] == "approval"
    assert res["autonomy"] == "ask"
    assert res["manual_apply"] is False
    assert res["status"] == ledger.STATUS_READY_FOR_REVIEW

    row = ledger.get_application(USER, aid)
    assert row["status"] == ledger.STATUS_READY_FOR_REVIEW
    assert ledger.get_approval_snapshot(USER, aid) is None   # never system-approved
    assert dispatches == []


# ===========================================================================
# autopilot — honors `auto` ONLY past the graduation gate.
# ===========================================================================
def test_autopilot_below_gate_downgrades_auto_to_ask(mdb, monkeypatch, dispatches):
    auto.set_mode(USER, auto.MODE_AUTOPILOT)
    _seed_confirmed(mdb, 9, approved_by="user")     # one short of N=10
    assert auto.autopilot_unlocked(USER) is False
    aas.set_executor(_ReconExecutor())
    _no_gaps_auto(monkeypatch)

    aid = _create()
    res = aas.gather(aid, USER)

    assert res["mode"] == "autopilot"
    assert res["autonomy"] == "ask"                 # not yet graduated
    assert res["status"] == ledger.STATUS_READY_FOR_REVIEW
    assert ledger.get_approval_snapshot(USER, aid) is None


def test_autopilot_at_gate_honors_auto_and_system_approves(mdb, monkeypatch, dispatches):
    auto.set_mode(USER, auto.MODE_AUTOPILOT)
    _seed_confirmed(mdb, 10, approved_by="user")    # exactly N=10 -> unlocked
    assert auto.autopilot_unlocked(USER) is True
    aas.set_executor(_ReconExecutor())
    _no_gaps_auto(monkeypatch)

    aid = _create()
    res = aas.gather(aid, USER)

    assert res["mode"] == "autopilot"
    assert res["autonomy"] == "auto"                # graduated -> honored
    assert res["status"] == ledger.STATUS_APPROVED

    row = ledger.get_application(USER, aid)
    assert row["status"] == ledger.STATUS_APPROVED
    snap = ledger.get_approval_snapshot(USER, aid)
    assert snap is not None
    assert snap["approved_by"] == "system"          # autopilot, not a user click


# ===========================================================================
# autopilot + unconfirmed legal attestation — the hard never-fabricate rule
# wins even at full autonomy.
# ===========================================================================
def test_autopilot_unconfirmed_legal_still_asks(mdb, monkeypatch, dispatches):
    auto.set_mode(USER, auto.MODE_AUTOPILOT)
    _seed_confirmed(mdb, 10, approved_by="user")    # autopilot COULD auto
    assert auto.autopilot_unlocked(USER) is True
    aas.set_executor(_ReconExecutor())

    # A required, undeclared legal attestation is unresolved -> the REAL
    # _decide_autonomy returns `ask` (hard rule), which the mode gate can never
    # upgrade. Left as a gap, so the row parks in needs_input.
    monkeypatch.setattr(
        aas, "_resolve_answers",
        lambda ue, qs: ({}, [{"question": "Authorized to work?",
                              "kind": "legal_attestation", "field_id": "q_auth"}],
                        {"coverage": 0.0, "has_novel_free_text": False,
                         "has_unconfirmed_legal_attestation": True}))

    aid = _create()
    res = aas.gather(aid, USER)

    assert res["autonomy"] == "ask"                 # never auto, never fabricated
    assert res["status"] == ledger.STATUS_NEEDS_INPUT
    assert ledger.get_approval_snapshot(USER, aid) is None
    assert dispatches == []


# ===========================================================================
# mode unset — the fail-safe: gather REFUSES and nothing auto-proceeds.
# ===========================================================================
def test_mode_unset_refuses_and_nothing_proceeds(mdb, monkeypatch, dispatches):
    # No set_mode call -> auto_submit_settings.mode is unset.
    assert auto.get_mode(USER) is None
    aas.set_executor(_ReconExecutor())
    _no_gaps_auto(monkeypatch)

    aid = _create()
    res = aas.gather(aid, USER)

    assert res["status"] == "mode_required"
    assert res["needs_onboarding"] is True
    assert res["mode"] is None

    # Nothing auto-proceeded: no approval, no dispatch, row parked (not approved).
    row = ledger.get_application(USER, aid)
    assert row["status"] not in (ledger.STATUS_APPROVED, ledger.STATUS_SUBMITTED)
    assert ledger.get_approval_snapshot(USER, aid) is None
    assert dispatches == []

    # The resolved answers were still persisted as an answer sheet for later.
    sheet = aas.answer_sheet(USER, aid)
    assert sheet["answers"] == {"q_email": "a@b.co"}
    assert sheet["mode"] is None


def test_modes_flag_off_keeps_todays_behavior(mdb, monkeypatch, dispatches):
    # Flag OFF -> the mode gate is inert: a would-be-auto row auto-approves today
    # exactly as before, even with NO mode set (no refusal).
    monkeypatch.setenv("AUTO_APPLY_MODES", "false")
    aas.set_executor(_ReconExecutor())
    _no_gaps_auto(monkeypatch)

    aid = _create()
    res = aas.gather(aid, USER)

    assert res["mode"] is None
    assert res["autonomy"] == "auto"
    assert res["status"] == ledger.STATUS_APPROVED


# ===========================================================================
# Graduation count — ONLY confirmed + approved_by=="user" count (anti-bootstrap).
# ===========================================================================
def test_graduation_counts_only_user_approved_confirmed(mdb):
    # 10 user-approved confirms -> unlocked.
    _seed_confirmed(mdb, 10, approved_by="user")
    assert auto.autopilot_unlocked(USER) is True

    mdb.applications.delete_many({})

    # 9 user-approved -> still locked (boundary).
    _seed_confirmed(mdb, 9, approved_by="user")
    assert auto.autopilot_unlocked(USER) is False


def test_graduation_excludes_system_approved(mdb):
    # 10 SYSTEM-approved confirms must NOT unlock — avoids a bootstrap loop where
    # autopilot graduates itself with zero human review.
    _seed_confirmed(mdb, 10, approved_by="system")
    assert auto.autopilot_unlocked(USER) is False


def test_graduation_excludes_unconfirmed_and_snapshotless(mdb):
    # confirmed-but-no-snapshot, and approved-but-not-confirmed, both excluded.
    _seed_confirmed(mdb, 10, approved_by="user", with_snapshot=False)
    mdb.applications.insert_many([
        {"application_id": "ap-" + secrets.token_hex(6), "user_email": USER,
         "job_identity": secrets.token_hex(32), "status": "approved",
         "approval_snapshot": {"approved_by": "user"}} for _ in range(10)])
    assert auto.autopilot_unlocked(USER) is False


def test_graduation_is_owner_scoped(mdb):
    # Another user's confirmations never count toward THIS user's unlock.
    _seed_confirmed(mdb, 10, approved_by="user", user=USER_B)
    assert auto.autopilot_unlocked(USER) is False
    assert auto.autopilot_unlocked(USER_B) is True


# ===========================================================================
# get_mode / set_mode — single source, enum-validated, no default written.
# ===========================================================================
def test_get_mode_none_until_set_then_round_trips(mdb):
    assert auto.get_mode(USER) is None            # no implicit default written
    assert auto.set_mode(USER, auto.MODE_AUTOPILOT) == "autopilot"
    assert auto.get_mode(USER) == "autopilot"


def test_set_mode_rejects_invalid_enum(mdb):
    with pytest.raises(ValueError):
        auto.set_mode(USER, "banana")
    assert auto.get_mode(USER) is None            # nothing persisted on reject


def test_stored_bad_mode_normalizes_to_none(mdb):
    # A hand-corrupted value is never silently trusted.
    mdb.auto_submit_settings.update_one(
        {"user_email": USER}, {"$set": {"user_email": USER, "mode": "bogus"}},
        upsert=True)
    assert auto.get_mode(USER) is None


# ===========================================================================
# Settings route PUT/GET round-trip; managed_mailbox reads through.
# ===========================================================================
@pytest.fixture()
def jobs_client(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT_ENABLED", "true")
    flask_app = load_service_app("jobs")

    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        tok = create_access_token(identity=USER)
    c = flask_app.test_client()

    def call(method, path, json=None):
        return getattr(c, method)(
            path, headers={"Authorization": f"Bearer {tok}"}, json=json)

    return call


def test_settings_put_get_round_trips_mode(mdb, jobs_client):
    put = jobs_client("put", "/api/apply/auto-submit/settings",
                      json={"auto_submit_enabled": True, "mode": "autopilot"})
    assert put.status_code == 200
    assert put.get_json()["mode"] == "autopilot"

    got = jobs_client("get", "/api/apply/auto-submit/settings")
    assert got.status_code == 200
    body = got.get_json()
    assert body["mode"] == "autopilot"
    assert body["auto_submit_enabled"] is True


def test_settings_get_mode_null_until_set(mdb, jobs_client):
    got = jobs_client("get", "/api/apply/auto-submit/settings")
    assert got.status_code == 200
    assert got.get_json()["mode"] is None


def test_settings_put_invalid_mode_is_400_never_401(mdb, jobs_client):
    resp = jobs_client("put", "/api/apply/auto-submit/settings",
                       json={"auto_submit_enabled": True, "mode": "banana"})
    assert resp.status_code == 400               # business error, NEVER 401
    assert resp.get_json()["error"] == "invalid_mode"


def test_managed_mailbox_reads_through_to_settings_mode(mdb):
    mmb.provision(USER, "Alice Example")
    # Legacy default until the single source is set.
    assert mmb.get_mailbox(USER)["mode"] == "approval"
    # Setting the SINGLE source flips the mailbox read-through.
    auto.set_mode(USER, auto.MODE_AUTOPILOT)
    assert mmb.get_mailbox(USER)["mode"] == "autopilot"
