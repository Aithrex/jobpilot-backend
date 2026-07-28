"""AUTOAPPLY_REPAIR_SPEC §3/§4 — the in-app approve fix + the resume-after-answer
loop + the unsupported-ATS terminal.

Three behaviours this file pins down (all previously broken):

  1. IN-APP APPROVE ACTUALLY SUBMITS. `POST /api/apply/auto-submit/approve` used
     to write ONLY the System-A `auto_submit_approvals` record, leaving the
     canonical ledger row at `ready_for_review` forever (request_immediate_submit
     needs approved/queued_for_execution; the hourly sweep redrives `approved`) —
     so the application was never submitted. It must now advance the ledger AND
     charge the auto_submit slot EXACTLY ONCE (the ledger reserves it under
     AUTO_APPLY_RESERVE; the route must not charge a second time).

  2. `needs_input` IS NO LONGER A DEAD END. `POST /api/apply/needs-input/answer`
     merges answers, banks them in the answer library, re-resolves, shrinks the
     queue or advances to `ready_for_review`, and honours the autonomy mode.

  3. AN UNSUPPORTED VENDOR IS HONEST. A permanent "we can't drive this system"
     report lands `unsupported` with a user-facing message — never conflated with
     a TRANSIENT executor drain (which stays a retryable `failed`).

Runs fully offline (mongomock via conftest/_vg_support; no Bedrock, no network,
no live application is ever submitted).
"""
import pytest

from _vg_support import current_db, load_service_app

import services.application_ledger_service as als
import services.auto_apply_service as aas
import services.auto_submit_service as ass

USER = "alice@aspirely.me"

_COLLECTIONS = (
    "applications", "auto_submit_approvals", "auto_submit_audit",
    "auto_submit_settings", "auto_apply_answers", "auto_apply_freezes",
    "application_answer_library", "feature_usage", "quota_counters",
    "quota_ledger", "resume_jobs", "user_preferences", "resume_assets",
)


@pytest.fixture()
def db():
    d = current_db()
    for c in _COLLECTIONS:
        d[c].delete_many({})
    yield d
    # Tear down as well as set up: the resume loop now DECLARES answers
    # (`answer_library_service.remember_many(..., declare=True)` writes flat
    # `user_preferences[field_id]` keys), and a leaked declared profile silently
    # changes what OTHER suites resolve. Cheap, and it kills a class of
    # cross-suite flake.
    for c in _COLLECTIONS:
        d[c].delete_many({})


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT_ENABLED", "true")
    monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
    return load_service_app("jobs")


def _client(flask_app, identity=USER):
    from flask_jwt_extended import create_access_token
    with flask_app.app_context():
        tok = create_access_token(identity=identity)
    c = flask_app.test_client()

    def call(method, path, json=None):
        return getattr(c, method)(
            path, headers={"Authorization": f"Bearer {tok}"}, json=json)

    return call


def _row(user=USER, *, status="ready_for_review", needs=None, title="Cloud Engineer"):
    """A canonical ledger row driven to `status` through the legal edges."""
    view, _ = als.create_application(
        user, company="Acme", title=title,
        jd_text="Build AWS infra with Terraform and Kubernetes.",
        source="greenhouse", ats_family="greenhouse",
        apply_url="https://job-boards.greenhouse.io/acme/jobs/999")
    aid = view["application_id"]
    als.mark_gathering(user, aid)
    if status == "needs_input":
        als.mark_needs_input(user, aid, needs or [], autonomy="ask",
                             confidence_score=0.7)
    elif status != "gathering":
        als.mark_ready_for_review(user, aid, autonomy="auto", confidence_score=0.95)
    return aid


def _no_dispatch(monkeypatch):
    """Capture submit dispatches instead of spawning the real async job (which
    would start a background thread against mongomock)."""
    calls = []
    monkeypatch.setattr(aas, "_enqueue_submit",
                        lambda aid, email: calls.append((aid, email)) or "job-x")
    return calls


def _auto_submit_slots(db, user=USER):
    """(atomic-counter units, ledger entries) charged for auto_submit."""
    counted = sum(int(c.get("count") or 0) for c in db.quota_counters.find(
        {"_id": {"$regex": "^auto_submit:"}}))
    entries = db.quota_ledger.count_documents(
        {"user_email": user, "feature": "auto_submit"})
    return counted, entries


# ===========================================================================
# 1. THE #1 BUG — an in-app approve must advance the canonical ledger row
# ===========================================================================
class TestInAppApproveAdvancesLedger:
    def test_approve_moves_row_to_approved_and_enqueues_submit(
            self, db, app, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "true")
        ass.set_settings(USER, True)
        calls = _no_dispatch(monkeypatch)
        aid = _row()

        resp = _client(app)("post", "/api/apply/auto-submit/approve",
                            {"application_id": aid, "answers": {"q1": "yes"}})
        assert resp.status_code == 201, resp.get_json()
        body = resp.get_json()
        assert body["ledger_approved"] is True

        # The canonical row actually moved — this is what makes it submit.
        assert als.get_application(USER, aid)["status"] == als.STATUS_APPROVED
        # ...with the F snapshot frozen, exactly like the magic-link path.
        assert als.get_approval_snapshot(USER, aid) is not None
        # ...and the submit job was fired immediately (not left for the sweep).
        assert calls == [(aid, USER)]

    def test_approve_reserves_the_auto_submit_slot_exactly_once(
            self, db, app, monkeypatch):
        """Regression: the route ALSO metered auto_submit, so with
        AUTO_APPLY_RESERVE on (prod) an in-app approve was billed TWICE."""
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "true")
        ass.set_settings(USER, True)
        _no_dispatch(monkeypatch)
        aid = _row()

        resp = _client(app)("post", "/api/apply/auto-submit/approve",
                            {"application_id": aid, "answers": {"q1": "yes"}})
        assert resp.status_code == 201

        counted, entries = _auto_submit_slots(db)
        assert (counted, entries) == (1, 1)
        # And the legacy meter was NOT also bumped.
        assert db.feature_usage.count_documents(
            {"user_email": USER, "feature": "auto_submit"}) == 0

    def test_reapprove_never_double_charges(self, db, app, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "true")
        ass.set_settings(USER, True)
        _no_dispatch(monkeypatch)
        aid = _row()
        call = _client(app)
        first = call("post", "/api/apply/auto-submit/approve",
                     {"application_id": aid, "answers": {"q1": "yes"}})
        assert first.status_code == 201
        second = call("post", "/api/apply/auto-submit/approve",
                      {"application_id": aid, "answers": {"q1": "yes"}})
        assert second.status_code == 200          # idempotent no-op
        assert second.get_json()["created"] is False
        assert _auto_submit_slots(db) == (1, 1)

    def test_reserve_off_keeps_the_single_legacy_charge(
            self, db, app, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "false")
        ass.set_settings(USER, True)
        _no_dispatch(monkeypatch)
        aid = _row()

        resp = _client(app)("post", "/api/apply/auto-submit/approve",
                            {"application_id": aid, "answers": {"q1": "yes"}})
        assert resp.status_code == 201
        assert resp.get_json()["ledger_approved"] is True
        assert als.get_application(USER, aid)["status"] == als.STATUS_APPROVED
        # Legacy metering is the ONE charge when the reservation ledger is off.
        usage = db.feature_usage.find_one(
            {"user_email": USER, "feature": "auto_submit"})
        assert usage and usage["count"] == 1
        assert _auto_submit_slots(db)[1] == 0

    def test_approve_without_a_ledger_row_is_unchanged(self, db, app, monkeypatch):
        """A pure extension approval (no canonical row) keeps the legacy path."""
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "true")
        ass.set_settings(USER, True)
        resp = _client(app)("post", "/api/apply/auto-submit/approve",
                            {"application_id": "job-1", "answers": {"q": "a"}})
        assert resp.status_code == 201
        assert resp.get_json()["ledger_approved"] is False
        assert _auto_submit_slots(db) == (1, 1)   # charged by the route, once

    def test_over_quota_approve_is_429_and_does_not_move_the_row(
            self, db, app, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "true")
        ass.set_settings(USER, True)
        _no_dispatch(monkeypatch)
        aid = _row()
        # No auto_submit slots left for this plan/period.
        monkeypatch.setattr("services.entitlements_service.quota",
                            lambda email, feature: 0 if feature == "auto_submit" else 5)

        resp = _client(app)("post", "/api/apply/auto-submit/approve",
                            {"application_id": aid, "answers": {"q1": "yes"}})
        assert resp.status_code == 429           # never 401
        assert als.get_application(USER, aid)["status"] == als.STATUS_READY_FOR_REVIEW

    def test_over_quota_with_reserve_OFF_also_never_approves_the_row(
            self, db, app, monkeypatch):
        """AUTO_APPLY_RESERVE=false is the DOCUMENTED ROLLBACK LEVER, so this
        branch has to be correct — and it was the broken one.

        The route advanced the ledger row to `approved` BEFORE the legacy quota
        charge, so an exhausted quota answered 429 while leaving a REAL approved
        application that `apply_scheduler` redrives into `auto_apply_submit` on the
        next hourly tick: the user was told it did not happen and it happened.
        """
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "false")
        ass.set_settings(USER, True)
        _no_dispatch(monkeypatch)
        aid = _row()
        monkeypatch.setattr("services.entitlements_service.quota",
                            lambda email, feature: 0 if feature == "auto_submit" else 5)

        resp = _client(app)("post", "/api/apply/auto-submit/approve",
                            {"application_id": aid, "answers": {"q1": "yes"}})

        assert resp.status_code == 429           # never 401
        row = als.get_application(USER, aid)
        assert row["status"] == als.STATUS_READY_FOR_REVIEW, \
            "a 429 must never leave an approved (submittable) row behind"
        # Nothing was charged, and no System-A approval record was written either.
        assert db.feature_usage.count_documents(
            {"user_email": USER, "feature": "auto_submit"}) == 0
        assert db.auto_submit_approvals.count_documents(
            {"user_email": USER, "application_id": aid}) == 0
        # And the row was never approved even TRANSIENTLY: the quota gate is a
        # non-incrementing PRE-check, so there is nothing to roll back. (A
        # rollback would leave an approve + an invalidation in the audit trail.)
        assert db.auto_submit_audit.count_documents(
            {"application_id": aid, "action": "application_approved"}) == 0

    def test_a_charge_that_loses_a_race_rolls_the_approval_back(
            self, db, app, monkeypatch):
        """The residual window: the non-incrementing pre-check passes, then the
        ATOMIC charge is denied by a concurrent approval. The row must be rolled
        back out of `approved` rather than left for the hourly sweep to submit."""
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "false")
        ass.set_settings(USER, True)
        _no_dispatch(monkeypatch)
        aid = _row()

        import importlib
        route = importlib.import_module("blueprints.apply")
        monkeypatch.setattr(route, "_charge_feature",
                            lambda email, feature, **kw: (False, {"limit": 25,
                                                                  "used": 25,
                                                                  "remaining": 0}))

        resp = _client(app)("post", "/api/apply/auto-submit/approve",
                            {"application_id": aid, "answers": {"q1": "yes"}})

        assert resp.status_code == 429
        assert als.get_application(USER, aid)["status"] == als.STATUS_READY_FOR_REVIEW
        # The rollback also voids the frozen snapshot, so a later approval re-freezes.
        assert als.get_approval_snapshot(USER, aid) is None

    def test_an_already_approved_row_is_never_charged_a_second_time(
            self, db, app, monkeypatch):
        """FINDING 4 — live in prod. The emailed magic link approves through
        `ledger.approve`, which RESERVES the slot and freezes the reservation on
        the row. A later in-app approve of that row misses the status-guarded CAS
        (`approved` is not approvable), so `ledger_advanced` is False — and the
        route then reserved the SAME approval again under its own key (1 -> 2).
        """
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "true")
        ass.set_settings(USER, True)
        _no_dispatch(monkeypatch)
        aid = _row()

        # The magic-link edge: approve via the ledger (reserves + freezes).
        assert als.approve(
            USER, [aid], approved_by="user",
            snapshot_provider=lambda a: aas.build_approval_snapshot(USER, a)) == 1
        assert _auto_submit_slots(db) == (1, 1)

        resp = _client(app)("post", "/api/apply/auto-submit/approve",
                            {"application_id": aid, "answers": {"q1": "yes"}})

        assert resp.status_code == 201, resp.get_json()
        assert resp.get_json()["ledger_approved"] is False   # CAS legitimately missed
        assert als.get_application(USER, aid)["status"] == als.STATUS_APPROVED
        # STILL exactly one charge for this approval.
        assert _auto_submit_slots(db) == (1, 1)
        assert db.feature_usage.count_documents(
            {"user_email": USER, "feature": "auto_submit"}) == 0


# ===========================================================================
# 2. RESUME AFTER ANSWER (spec §3)
# ===========================================================================
_GAP = {"question": "Are you a former Acme employee?", "kind": "screening",
        "field_id": "previous_employee"}
_GAP2 = {"question": "How many years of Kubernetes do you have?",
         "kind": "screening", "field_id": "years_kubernetes"}


class TestResumeAfterAnswer:
    def test_answering_the_last_gap_advances_to_review_and_submits(
            self, db, app, monkeypatch):
        """Aggressive mode: the answer clears the row and it goes straight
        through approve + submit — the user is NEVER told to apply manually."""
        monkeypatch.setenv("AUTO_APPLY_MODES", "true")
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "true")
        from services import autonomy_service
        autonomy_service.set_mode(USER, autonomy_service.MODE_AGGRESSIVE)
        calls = _no_dispatch(monkeypatch)
        aid = _row(status="needs_input", needs=[_GAP])

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"previous_employee": "No"}})
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["status"] == "resumed"
        assert body["application_status"] == als.STATUS_APPROVED
        assert body["remaining"] == []
        assert als.get_application(USER, aid)["status"] == als.STATUS_APPROVED
        assert calls == [(aid, USER)]
        # The answer landed on the sheet the executor fills from.
        sheet = db.auto_apply_answers.find_one({"application_id": aid})
        assert sheet["answers"]["previous_employee"] == "No"

    def test_answer_is_remembered_so_it_is_never_asked_twice(
            self, db, app, monkeypatch):
        _no_dispatch(monkeypatch)
        aid = _row(status="needs_input", needs=[_GAP])
        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"previous_employee": "No"}})
        assert resp.status_code == 200
        assert resp.get_json()["remembered"] == 1
        banked = db.application_answer_library.find_one({"user_email": USER})
        assert banked is not None
        assert banked["answer"] == "No"

    def test_partial_answer_stays_needs_input_with_a_shrunken_queue(
            self, db, app, monkeypatch):
        _no_dispatch(monkeypatch)
        aid = _row(status="needs_input", needs=[_GAP, _GAP2])

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"previous_employee": "No"}})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["application_status"] == als.STATUS_NEEDS_INPUT
        remaining_ids = [r["field_id"] for r in body["remaining"]]
        assert remaining_ids == ["years_kubernetes"]
        # The answered question is GONE from the row's queue — never re-asked.
        row = als.get_application(USER, aid)
        assert [i["field_id"] for i in row["needs_input"]] == ["years_kubernetes"]

    def test_an_oracle_wall_is_never_cleared_by_typing_an_answer(
            self, db, app, monkeypatch):
        """A verify/CAPTCHA wall is not answerable — the row must stay parked."""
        _no_dispatch(monkeypatch)
        wall = {"question": "Verification required (verify) to finish",
                "kind": "oracle_verify", "field_id": "oracle_verify",
                "action": "complete_in_browser"}
        aid = _row(status="needs_input", needs=[_GAP, wall])

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"previous_employee": "No",
                                         "oracle_verify": "done"}})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["application_status"] == als.STATUS_NEEDS_INPUT
        assert [r["field_id"] for r in body["remaining"]] == ["oracle_verify"]

    def test_an_account_gate_is_never_cleared_by_an_answer(
            self, db, app, monkeypatch):
        """A Workday login wall parks the row with an EMPTY question list — an
        unrelated answer must not make it look 'clear' and resume it."""
        _no_dispatch(monkeypatch)
        aid = _row(status="needs_input", needs=[])
        db.auto_apply_answers.insert_one({
            "application_id": aid, "user_email": USER,
            "needs_account": True, "answers": {}, "coverage": 1.0})

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"previous_employee": "No"}})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["application_status"] == als.STATUS_NEEDS_INPUT
        assert [r["field_id"] for r in body["remaining"]] == ["ats_account"]
        assert als.get_application(USER, aid)["status"] == als.STATUS_NEEDS_INPUT

    @pytest.mark.parametrize("kind", ["validation_failed", "captcha",
                                      "sensitive_unconfirmed", "", "whatever_v2"])
    def test_an_unrecognised_kind_is_a_WALL_not_an_answerable_question(
            self, kind, db, app, monkeypatch):
        """`kind` is an OPEN vocabulary — `finalize_submission` writes it from a
        free-form executor/fill-driver reason. Under the old DENY-list, anything
        unlisted was answerable BY DEFAULT: the user typed anything, `remaining`
        emptied, the row marched ready_for_review -> approve -> submit, and the
        executor hit the very same wall again. Unrecognised must read as a wall."""
        _no_dispatch(monkeypatch)
        wall = {"question": "Something only the site can settle",
                "kind": kind, "field_id": "blocked_field"}
        aid = _row(status="needs_input", needs=[_GAP, wall])

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"previous_employee": "No",
                                         "blocked_field": "anything at all"}})

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["application_status"] == als.STATUS_NEEDS_INPUT
        assert [r["field_id"] for r in body["remaining"]] == ["blocked_field"]
        assert als.get_application(USER, aid)["status"] == als.STATUS_NEEDS_INPUT

    def test_an_executor_field_request_stays_answerable(self, db, app, monkeypatch):
        """The other side of the inversion: a fill-driver report that NAMES a
        field is a genuine question, so it must be written with an allow-listed
        kind — otherwise the one-tap resume loop would park forever."""
        _no_dispatch(monkeypatch)
        aid = _row(status="ready_for_review")
        als.transition(USER, aid, als.STATUS_APPROVED)
        als.transition(USER, aid, als.STATUS_QUEUED_FOR_EXECUTION)
        als.transition(USER, aid, als.STATUS_RUNNING)

        aas.finalize_submission(aid, USER, status="needs_input",
                                field="years_experience",
                                reason="no_option_match")

        item = als.get_application(USER, aid)["needs_input"][0]
        assert item["field_id"] == "years_experience"
        assert item["kind"] in aas._ANSWERABLE_KINDS
        assert aas._answerable(item) is True

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"years_experience": "6"}})
        assert resp.status_code == 200
        assert resp.get_json()["remaining"] == []

    def test_an_executor_report_with_no_field_stays_a_wall(self, db, app, monkeypatch):
        """No field named = not a question. It keeps its raw reason (so the real
        cause survives) and no typed answer can clear it."""
        _no_dispatch(monkeypatch)
        aid = _row(status="ready_for_review")
        als.transition(USER, aid, als.STATUS_APPROVED)
        als.transition(USER, aid, als.STATUS_QUEUED_FOR_EXECUTION)
        als.transition(USER, aid, als.STATUS_RUNNING)

        aas.finalize_submission(aid, USER, status="needs_input", reason="captcha")

        item = als.get_application(USER, aid)["needs_input"][0]
        assert item["kind"] == "captcha"
        assert aas._answerable(item) is False

    def test_row_not_awaiting_input_is_409_never_401(self, db, app, monkeypatch):
        aid = _row(status="ready_for_review")
        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid, "answers": {"a": "b"}})
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "not_in_needs_input"

    def test_unknown_application_is_404(self, db, app):
        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": "nope", "answers": {"a": "b"}})
        assert resp.status_code == 404

    def test_empty_or_malformed_answers_are_400(self, db, app):
        aid = _row(status="needs_input", needs=[_GAP])
        call = _client(app)
        assert call("post", "/api/apply/needs-input/answer",
                    {"application_id": aid, "answers": []}).status_code == 400
        assert call("post", "/api/apply/needs-input/answer",
                    {"application_id": aid, "answers": {}}).status_code == 400
        assert call("post", "/api/apply/needs-input/answer",
                    {"answers": {"a": "b"}}).status_code == 400

    def test_operator_shaped_keys_never_reach_the_answer_sheet(
            self, db, app, monkeypatch):
        _no_dispatch(monkeypatch)
        aid = _row(status="needs_input", needs=[_GAP])
        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"$where": "1", "a.b": "x",
                                         "previous_employee": "No"}})
        assert resp.status_code == 200
        stored = db.auto_apply_answers.find_one({"application_id": aid})["answers"]
        assert set(stored) == {"previous_employee"}

    def test_cross_user_row_is_invisible(self, db, app, monkeypatch):
        _no_dispatch(monkeypatch)
        aid = _row(user="bob@aspirely.me", status="needs_input", needs=[_GAP])
        resp = _client(app, USER)("post", "/api/apply/needs-input/answer",
                                  {"application_id": aid,
                                   "answers": {"previous_employee": "No"}})
        assert resp.status_code == 404


# ===========================================================================
# 3. UNSUPPORTED ATS (spec §4) — honest, and NOT conflated with a drain
# ===========================================================================
class _Executor:
    """Minimal executor stub returning one canned submit result."""

    def __init__(self, result):
        self._result = result

    def is_available(self):
        return True

    def recon_form(self, apply_url, ats_family):
        return {"questions": [], "needs_account": False}

    def submit(self, application, answers, resume_pdf):
        return self._result

    def create_account(self, *a, **kw):
        return {"status": "executor_unavailable"}

    def verify_state(self, *a, **kw):
        return {"status": "executor_unavailable", "resolved": False}


def _approved_row(monkeypatch, user=USER):
    aid = _row(user=user)
    als.approve(user, [aid], approved_by="user")
    return aid


class TestUnsupportedVendor:
    def test_executor_unsupported_lands_the_unsupported_terminal(
            self, db, monkeypatch):
        aid = _approved_row(monkeypatch)
        aas.set_executor(_Executor({
            "status": "executor_unsupported",
            "user_message": als.UNSUPPORTED_MESSAGE,
            "reason": "unsupported_family:icims", "retryable": False}))
        try:
            out = aas.submit(aid, USER)
        finally:
            aas.set_executor(None)

        assert out["status"] == als.STATUS_UNSUPPORTED
        row = als.get_application(USER, aid)
        assert row["status"] == als.STATUS_UNSUPPORTED
        # The row carries the USER-FACING sentence, not an opaque code.
        assert row["error"] == als.UNSUPPORTED_MESSAGE
        assert "apply manually" in row["error"]

    def test_transient_drain_is_still_a_retryable_failure(self, db, monkeypatch):
        """Do NOT conflate: an unavailable fleet is retryable, an unsupported
        vendor is permanent."""
        aid = _approved_row(monkeypatch)
        aas.set_executor(_Executor({"status": "executor_unavailable"}))
        try:
            aas.submit(aid, USER)
        finally:
            aas.set_executor(None)
        row = als.get_application(USER, aid)
        assert row["status"] == als.STATUS_FAILED
        assert row["error"] == "executor_unavailable_midflight"

    def test_prepared_is_a_human_handoff_not_a_failure(self, db, monkeypatch):
        aid = _approved_row(monkeypatch)
        aas.set_executor(_Executor({"status": "prepared", "submitted": False,
                                    "screenshot_ref": "s3://shot.png"}))
        try:
            out = aas.submit(aid, USER)
        finally:
            aas.set_executor(None)
        assert out["status"] == als.STATUS_WAITING_HUMAN
        assert out["prepared"] is True
        row = als.get_application(USER, aid)
        assert row["status"] == als.STATUS_WAITING_HUMAN
        assert row["error"]                      # the "review and send" message

    def test_waiting_human_from_the_fill_driver_parks_not_fails(
            self, db, monkeypatch):
        aid = _approved_row(monkeypatch)
        aas.set_executor(_Executor({"status": "waiting_human",
                                    "reason": "anti_bot"}))
        try:
            out = aas.submit(aid, USER)
        finally:
            aas.set_executor(None)
        assert out["status"] == als.STATUS_WAITING_HUMAN
        assert als.get_application(USER, aid)["status"] == als.STATUS_WAITING_HUMAN

    def test_finalize_submission_maps_the_same_vocabulary(self, db, monkeypatch):
        aid = _approved_row(monkeypatch)
        als.transition(USER, aid, als.STATUS_QUEUED_FOR_EXECUTION)
        als.transition(USER, aid, als.STATUS_RUNNING)
        out = aas.finalize_submission(aid, USER, status="executor_unsupported",
                                      reason="unsupported_family:taleo")
        assert out["status"] == als.STATUS_UNSUPPORTED
        assert als.get_application(USER, aid)["error"] == als.UNSUPPORTED_MESSAGE

    def test_unsupported_row_surfaces_in_the_fold_with_its_message(
            self, db, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        aid = _approved_row(monkeypatch)
        als.transition(USER, aid, als.STATUS_QUEUED_FOR_EXECUTION)
        als.mark_unsupported(USER, aid)
        rows = ass.list_canonical_approvals(USER)
        rec = next((r for r in rows if r["application_id"] == aid), None)
        assert rec is not None
        assert rec["status"] == "unsupported"
        assert rec["already_approved"] is False
        assert "apply manually" in rec["reason"]


# ===========================================================================
# 4. FOLD PAYLOAD — answers_snapshot + undo_until, and no double render
# ===========================================================================
class TestFoldPayload:
    def test_fold_carries_answers_snapshot_and_undo_window(
            self, db, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        aid = _row()
        db.auto_apply_answers.insert_one({
            "application_id": aid, "user_email": USER, "coverage": 1.0,
            "answers": {"first_name": "Alice", "previous_employee": "No"}})
        als.approve(USER, [aid], approved_by="user")

        rec = next(r for r in ass.list_canonical_approvals(USER)
                   if r["application_id"] == aid)
        # Without these the review card renders "No application questions to
        # review" and the approved card shows no cancel window.
        assert rec["answers_snapshot"] == {"first_name": "Alice",
                                           "previous_employee": "No"}
        assert rec["answers_count"] == 2
        assert rec["undo_until"] is not None and rec["approved_at"] is not None
        assert rec["undo_until"] > rec["approved_at"]

    def test_row_without_an_answer_sheet_gets_an_empty_snapshot(
            self, db, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        aid = _row()
        rec = next(r for r in ass.list_canonical_approvals(USER)
                   if r["application_id"] == aid)
        assert rec["answers_snapshot"] == {}
        assert rec.get("undo_until") is None      # not approved yet

    def test_pending_renders_an_approved_row_once_during_the_undo_window(
            self, db, app, monkeypatch):
        """The in-app approve mints a System-A record (in `waiting` for 60s) AND
        the fold reports the canonical row — deduping only against `pending`
        rendered it twice for the whole undo window."""
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "true")
        ass.set_settings(USER, True)
        _no_dispatch(monkeypatch)
        aid = _row()
        call = _client(app)
        assert call("post", "/api/apply/auto-submit/approve",
                    {"application_id": aid,
                     "answers": {"q1": "yes"}}).status_code == 201

        body = call("get", "/api/apply/auto-submit/pending", None).get_json()
        ids = [r.get("application_id") for r in body["pending"]]
        ids += [r.get("application_id") for r in body["waiting"]]
        assert ids.count(aid) == 1

    def test_cancel_undoes_the_canonical_row_too(self, db, app, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_RESERVE", "true")
        ass.set_settings(USER, True)
        _no_dispatch(monkeypatch)
        aid = _row()
        call = _client(app)
        call("post", "/api/apply/auto-submit/approve",
             {"application_id": aid, "answers": {"q1": "yes"}})

        resp = call("post", "/api/apply/auto-submit/cancel",
                    {"application_id": aid})
        assert resp.status_code == 200
        assert resp.get_json()["ledger_cancelled"] is True
        # The ledger row must NOT still be marching toward submission.
        assert als.get_application(USER, aid)["status"] == als.STATUS_CANCELLED
