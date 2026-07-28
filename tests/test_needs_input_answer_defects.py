"""Design-review defects in the needs_input answer loop (AUTOAPPLY_REPAIR_SPEC §3).

Three BACKEND defects, one regression class each. (The fourth — a 429 rendered as
a failure — is frontend-only and pinned by
`jobpilot-frontend/src/components/autoSubmit/__tests__/needs-input-quota.test.tsx`.)

  1. THE PERMANENT LOOP. `apply_forms.normalize_recon_questions` already produces
     `type` + `options` per question; `_needs_item` / `_needs_view` threw both
     away and the ledger's `_sanitize_needs_input` dropped them again, so the FE
     had no choices to render and fell back to a free-text box
     (`needsInput.inferControl`). For an enumerated select (`how_did_you_hear`)
     the user typed a value no option matched -> `fill_driver` reported
     `no_option_match` -> the row re-parked with the placeholder
     "We need your answer to continue (how_did_you_hear)." and STILL no options
     -> forever. The typed value was also banked in the answer library, so it
     came back as `user_store`/1.0 for every future application.

  2. IN-FLIGHT ROWS VANISHED. The approval fold covered eight statuses; `queued`,
     `gathering` and `running` — all with live writers, and `running` lasting
     minutes — were absent. The user approved, the card disappeared, and only
     came back if the run FAILED.

  3. "NEVER ASKED TWICE" WAS UNFULFILLED. `remember()` was called positionally
     with no `field_id=`, so the memo stored `field_id: None` and
     `_lookup_user_store`'s strongest key never matched; `declare=True`'s write
     to `user_preferences[field_id]` never happened (provenance never upgraded
     from `user_store` to `declared`); and an answer for a field_id that was not
     in the current queue banked NOTHING at all, silently.

Runs fully offline (mongomock via conftest/_vg_support; no Bedrock, no network,
no live application is ever submitted).
"""
import pytest

from _vg_support import current_db, load_service_app

import services.application_ledger_service as als
import services.application_question_bank as qb
import services.auto_apply_service as aas
import services.auto_submit_service as ass
import services.apply_forms as apply_forms

USER = "alice@aspirely.me"

_COLLECTIONS = (
    "applications", "auto_submit_approvals", "auto_submit_audit",
    "auto_submit_settings", "auto_apply_answers", "auto_apply_freezes",
    "application_answer_library", "feature_usage", "quota_counters",
    "quota_ledger", "resume_jobs", "user_preferences", "resume_assets",
)

# The catalog select at the heart of defect 1.
_HEAR = "how_did_you_hear"


@pytest.fixture()
def db():
    d = current_db()
    for c in _COLLECTIONS:
        d[c].delete_many({})
    yield d
    # Teardown too: several suites seed `user_preferences` / `resume_assets` and
    # leave them behind, which makes a full-suite run flaky. Ours never leaks.
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


def _no_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(aas, "_enqueue_submit",
                        lambda aid, email: calls.append((aid, email)) or "job-x")
    return calls


def _row(user=USER, *, status="ready_for_review", needs=None):
    """A canonical ledger row driven to `status` through the legal edges."""
    view, _ = als.create_application(
        user, company="Acme", title="Cloud Engineer",
        jd_text="Build AWS infra with Terraform and Kubernetes.",
        source="greenhouse", ats_family="greenhouse",
        apply_url="https://job-boards.greenhouse.io/acme/jobs/999")
    aid = view["application_id"]
    if status == "queued":
        return aid
    als.mark_gathering(user, aid)
    if status == "gathering":
        return aid
    if status == "needs_input":
        als.mark_needs_input(user, aid, needs or [], autonomy="ask",
                             confidence_score=0.7)
        return aid
    als.mark_ready_for_review(user, aid, autonomy="auto", confidence_score=0.95)
    if status == "ready_for_review":
        return aid
    als.approve(user, [aid], approved_by="user")
    if status == "approved":
        return aid
    als.transition(user, aid, als.STATUS_QUEUED_FOR_EXECUTION)
    if status == "queued_for_execution":
        return aid
    als.transition(user, aid, als.STATUS_RUNNING)
    return aid


# ===========================================================================
# DEFECT 1 — the enumerated select must reach the user AS a select
# ===========================================================================
class TestControlMetadataSurvivesTheWholeLoop:
    def test_needs_item_echoes_the_forms_type_options_and_required(self):
        """`normalize_recon_questions` produces them (apply_forms) — the queue
        item must carry them, or the FE renders a text box for a select."""
        questions = apply_forms.normalize_recon_questions([{
            "id": "q_src", "label": "How did you hear about us?",
            "type": "select", "required": True,
            "options": ["Company website", "LinkedIn", "Indeed"],
        }])
        assert questions and questions[0]["field_id"] == _HEAR

        item = aas._needs_item(questions[0], kind="screening")

        assert item["field_id"] == _HEAR
        assert item["value_type"] == "select"
        assert item["options"] == ["Company website", "LinkedIn", "Indeed"]
        assert item["required"] is True

    def test_needs_item_falls_back_to_the_catalog_when_recon_had_no_options(self):
        """An item parked without recon metadata still renders a real control."""
        item = aas._needs_item({"field_id": _HEAR, "label": "Where'd you hear?",
                                "required": True}, kind="screening")
        assert item["value_type"] == "select"
        assert item["options"] == qb.get_field(_HEAR)["options"]

    def test_a_yes_no_field_gets_boolean_not_a_yes_no_option_list(self):
        """`value_type: boolean` is what makes the FE render (and serialize) real
        Yes/No pills; a redundant option list would turn them into strings."""
        assert qb.get_field("relatives_employed")["value_type"] == "boolean"
        item = aas._needs_item({"field_id": "relatives_employed",
                                "label": "Any relatives employed here?"},
                               kind="screening")
        assert item["value_type"] == "boolean"
        assert "options" not in item

    def test_the_ledger_persists_the_control_metadata(self, db):
        """`_sanitize_needs_input` dropped every unknown key, so even an enriched
        item lost its options on the way into Mongo — and the FOLD reads the
        stored row, not the answer response."""
        aid = _row(status="gathering")
        als.mark_needs_input(USER, aid, [{
            "question": "How did you hear about us?", "kind": "screening",
            "field_id": _HEAR, "value_type": "select", "required": True,
            "options": ["Company website", "LinkedIn"],
        }])
        stored = als.get_application(USER, aid)["needs_input"][0]
        assert stored["value_type"] == "select"
        assert stored["options"] == ["Company website", "LinkedIn"]
        assert stored["required"] is True

    def test_the_fold_hands_the_options_to_the_frontend(self, db, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        aid = _row(status="gathering")
        als.mark_needs_input(USER, aid, [{
            "question": "How did you hear about us?", "kind": "screening",
            "field_id": _HEAR, "value_type": "select",
            "options": ["Company website", "LinkedIn"],
        }])
        rec = next(r for r in ass.list_canonical_approvals(USER)
                   if r["application_id"] == aid)
        item = rec["needs_input"][0]
        assert item["value_type"] == "select"
        assert item["options"] == ["Company website", "LinkedIn"]

    def test_needs_view_echoes_the_metadata_for_remaining(self):
        """`remaining` is the other half of the contract the FE reads."""
        view = aas._needs_view({
            "question": "How did you hear about us?", "kind": "screening",
            "field_id": _HEAR, "value_type": "select", "required": True,
            "options": ["Company website", "LinkedIn"]})
        assert view["value_type"] == "select"
        assert view["options"] == ["Company website", "LinkedIn"]
        assert view["required"] is True

    def test_a_still_open_question_keeps_its_options_in_remaining(
            self, db, app, monkeypatch):
        """End-to-end: answer ONE of two questions; the one still open must come
        back with its choices intact (a re-park must never degrade a select)."""
        _no_dispatch(monkeypatch)
        aid = _row(status="needs_input", needs=[
            {"question": "Are you a former Acme employee?", "kind": "screening",
             "field_id": "previous_employee"},
            {"question": "How did you hear about us?", "kind": "screening",
             "field_id": _HEAR, "value_type": "select",
             "options": ["Company website", "LinkedIn", "Indeed"]},
        ])

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"previous_employee": "No"}})
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        left = [r for r in body["remaining"] if r["field_id"] == _HEAR]
        assert left, body["remaining"]
        assert left[0]["options"] == ["Company website", "LinkedIn", "Indeed"]
        assert left[0]["value_type"] == "select"
        # And the SHRUNKEN queue persisted on the row keeps them too.
        row_item = next(i for i in als.get_application(USER, aid)["needs_input"]
                        if i["field_id"] == _HEAR)
        assert row_item["options"] == ["Company website", "LinkedIn", "Indeed"]

    def test_the_executor_repark_keeps_a_real_question_and_its_options(
            self, db, monkeypatch):
        """The loop's last link: `finalize_submission` overwrote the label with
        "We need your answer to continue (how_did_you_hear)." and shipped no
        options, so round-trip #2 was a nameless text box."""
        aid = _row(status="running")

        aas.finalize_submission(aid, USER, status="needs_input",
                                field=_HEAR, reason="no_option_match")

        item = als.get_application(USER, aid)["needs_input"][0]
        assert item["field_id"] == _HEAR
        assert item["kind"] == "screening"
        assert "We need your answer to continue" not in item["question"]
        assert item["question"] == qb.get_field(_HEAR)["label"]
        assert item["options"] == qb.get_field(_HEAR)["options"]
        assert aas._answerable(item) is True

    def test_an_unknown_field_still_gets_the_placeholder(self, db, monkeypatch):
        """No catalog entry, no prior label -> the placeholder is the honest
        fallback. It must not disappear."""
        aid = _row(status="running")
        aas.finalize_submission(aid, USER, status="needs_input",
                               field="acme_custom_widget_42",
                               reason="unresolved")
        item = als.get_application(USER, aid)["needs_input"][0]
        assert "We need your answer to continue" in item["question"]
        assert item["field_id"] == "acme_custom_widget_42"


# ===========================================================================
# DEFECT 2 — an in-flight row must stay visible for the whole run
# ===========================================================================
class TestInFlightRowsAreFolded:
    @pytest.mark.parametrize("status", ["queued", "gathering", "running"])
    def test_in_flight_rows_are_visible_read_only_and_not_eligible(
            self, status, db, monkeypatch):
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        aid = _row(status=status)
        assert als.get_application(USER, aid)["status"] == status

        rec = next((r for r in ass.list_canonical_approvals(USER)
                    if r["application_id"] == aid), None)

        assert rec is not None, f"a {status} row vanished from the Auto-Apply tab"
        assert rec["status"] == status
        # Read-only: the FE must never offer a second Approve for a live run.
        assert rec["already_approved"] is True
        # ...but nothing is cleared for the extension to submit.
        assert rec["eligible"] is False

    def test_a_running_row_gets_no_cancel_countdown(self, db, monkeypatch):
        """`undo_until` is a CANCEL affordance; `cancel_ledger` refuses a running
        row, so stamping one would render a button that cannot work."""
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        aid = _row(status="running")
        rec = next(r for r in ass.list_canonical_approvals(USER)
                   if r["application_id"] == aid)
        assert rec.get("undo_until") is None

    def test_an_approved_row_still_gets_its_undo_window(self, db, monkeypatch):
        """The guard above must not cost the approved row its cancel buffer."""
        monkeypatch.setenv("AUTO_APPLY_APPROVAL_FOLD", "true")
        aid = _row(status="approved")
        rec = next(r for r in ass.list_canonical_approvals(USER)
                   if r["application_id"] == aid)
        assert rec["undo_until"] is not None


# ===========================================================================
# DEFECT 3 — the answer is banked under its catalog field_id AND declared
# ===========================================================================
class TestAnswersAreBankedProperly:
    def test_the_memo_carries_the_field_id_and_upgrades_to_declared(
            self, db, app, monkeypatch):
        """Positional `remember(email, question, value)` stored `field_id: None`
        (so `_lookup_user_store`'s strongest key never matched) and never wrote
        the declared store (so provenance never left `user_store`)."""
        _no_dispatch(monkeypatch)
        aid = _row(status="needs_input", needs=[
            {"question": "Are you a former Acme employee?", "kind": "screening",
             "field_id": "previous_employee"}])

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"previous_employee": "No"}})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["remembered"] == 1

        memo = db.application_answer_library.find_one(
            {"user_email": USER, "field_id": "previous_employee"})
        assert memo is not None, "the memo did not carry its catalog field_id"
        assert memo["answer"] == "No"
        # declare=True -> the flat declared store `_load_canonical` reads first.
        # (`previous_employee` is a catalog SELECT over ["Yes", "No"], so the
        # validated declared value is the option string, not a bool.)
        prefs = db.user_preferences.find_one({"user_email": USER}) or {}
        assert prefs.get("previous_employee") == "No"
        # ...and the resolution now reports it as DECLARED, not user_store.
        from services import answer_library_service
        hit = answer_library_service.resolve(USER, [{
            "field_id": "previous_employee",
            "label": "Have you previously worked for Acme?",
            "required": True}])["resolved"].get("previous_employee")
        assert hit is not None
        assert hit["source"] == "declared"

    def test_an_answer_for_a_field_outside_the_queue_is_still_banked(
            self, db, app, monkeypatch):
        """`by_field.get(fid)` was None -> empty question -> `remember()` bailed
        and NOTHING was banked, silently."""
        _no_dispatch(monkeypatch)
        aid = _row(status="needs_input", needs=[
            {"question": "Are you a former Acme employee?", "kind": "screening",
             "field_id": "previous_employee"}])

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"previous_employee": "No",
                                         _HEAR: "LinkedIn"}})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["remembered"] == 2
        memo = db.application_answer_library.find_one(
            {"user_email": USER, "field_id": _HEAR})
        assert memo is not None, "an off-queue answer was silently dropped"
        assert memo["answer"] == "LinkedIn"
        prefs = db.user_preferences.find_one({"user_email": USER}) or {}
        assert prefs.get(_HEAR) == "LinkedIn"

    def test_a_value_the_catalog_rejects_is_not_declared(
            self, db, app, monkeypatch):
        """The other half of the poisoning story: a free-typed value that is not
        one of the select's options must NOT become a declared fact."""
        _no_dispatch(monkeypatch)
        aid = _row(status="needs_input", needs=[
            {"question": "How did you hear about us?", "kind": "screening",
             "field_id": _HEAR, "value_type": "select",
             "options": qb.get_field(_HEAR)["options"]}])

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {_HEAR: "Google"}})
        assert resp.status_code == 200, resp.get_json()
        prefs = db.user_preferences.find_one({"user_email": USER}) or {}
        assert _HEAR not in prefs

    def test_a_wall_answer_is_never_banked(self, db, app, monkeypatch):
        """A captcha/verify wall is not a question; banking its typed "answer"
        would poison every future application."""
        _no_dispatch(monkeypatch)
        aid = _row(status="needs_input", needs=[
            {"question": "Verification required (verify) to finish",
             "kind": "oracle_verify", "field_id": "oracle_verify",
             "action": "complete_in_browser"}])

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"oracle_verify": "done"}})
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["remembered"] == 0
        assert db.application_answer_library.count_documents(
            {"user_email": USER}) == 0
