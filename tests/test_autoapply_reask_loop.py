"""Regression suite for the reported production loop: "some of them are not
going through and asking the same thing more times".

WHAT HAPPENED (prod, 2026-07-28)
--------------------------------
Two rows re-parked on the SAME field over and over — Adyen's `country` six
times, EnergyHub's four. The contradiction in the data was that the field was
already RESOLVED::

    applications.needs_input   [{kind: 'screening', field_id: 'country', ...}]
    auto_apply_answers.answers {'country': 'United States of America'}
    auto_apply_answers.coverage 1.0

Those two facts come from DIFFERENT phases and never met. `coverage`/`answers`
are written by the RESOLUTION phase (gather / resume-after-answer); the queue
entry was written by the FILL phase, from the executor's terminal report::

    browser submit BLOCKED pre-submit: 2 required gap(s),
        first=country reason=field_not_on_page (filled=6)

`field_not_on_page` is emitted by `browser_executor._presubmit_gaps` ONLY for a
field we HAD a vetted value for and could not place. `finalize_submission` then
re-parked it as a plain `screening` question — i.e. it asked the user for the
answer it was already holding. That closes an unbreakable cycle:

    park(question) -> user answers -> resume_after_answers finds nothing open
      -> ready_for_review -> approved -> submit -> the same fill fails
      -> park(question) -> ...

which is exactly six round trips of "it comes back".

WHAT IS PINNED HERE
-------------------
1. A field whose answer we STILL HOLD re-parks as a HAND-OFF, not a question:
   outside `_ANSWERABLE_KINDS`, carrying `action: complete_in_browser` + the
   apply URL, with copy that does not ask for what we already have.
2. The cycle is therefore broken — answering it again cannot advance the row
   back into the identical failure.
3. The narrowness: a field we genuinely have NO answer for (and a held answer
   that is empty/None, which is what EnergyHub's row carried) is still a real
   question, still answerable, still resolvable in one round trip.

Runs fully offline (mongomock via conftest/_vg_support); no live application is
ever submitted.
"""
import pytest

from _vg_support import current_db, load_service_app

import services.application_ledger_service as als
import services.auto_apply_service as aas

USER = "alice@aspirely.me"
_APPLY_URL = "https://job-boards.greenhouse.io/acme/jobs/999"

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
    for c in _COLLECTIONS:
        d[c].delete_many({})


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT_ENABLED", "true")
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


def _running_row(db, answers=None, needs=None):
    """A row driven to `running` (i.e. the executor is mid-drive), with the
    answer sheet the resolution phase would have left behind."""
    view, _ = als.create_application(
        USER, company="Adyen", title="Cloud Engineer",
        jd_text="Build AWS infra with Terraform and Kubernetes.",
        source="greenhouse", ats_family="greenhouse", apply_url=_APPLY_URL)
    aid = view["application_id"]
    als.mark_gathering(USER, aid)
    if needs is not None:
        als.mark_needs_input(USER, aid, needs, autonomy="ask", confidence_score=0.7)
        als.transition(USER, aid, als.STATUS_READY_FOR_REVIEW)
    else:
        als.mark_ready_for_review(USER, aid, autonomy="auto", confidence_score=0.95)
    als.transition(USER, aid, als.STATUS_APPROVED)
    als.transition(USER, aid, als.STATUS_QUEUED_FOR_EXECUTION)
    als.transition(USER, aid, als.STATUS_RUNNING)
    if answers is not None:
        db.auto_apply_answers.update_one(
            {"application_id": aid, "user_email": USER},
            {"$set": {"answers": answers, "coverage": 1.0}}, upsert=True)
    return aid


def _queue(aid):
    return als.get_application(USER, aid)["needs_input"]


def _no_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(aas, "_enqueue_submit",
                        lambda a, e: calls.append((a, e)) or "job-x")
    return calls


# ===========================================================================
# 1. The bug — a held answer must never be re-asked
# ===========================================================================
class TestAHeldAnswerIsNeverReAsked:
    def test_the_exact_production_report_parks_as_a_handoff(self, db):
        """The Adyen row, byte for byte: answers hold the country, the executor
        reports it could not place it."""
        aid = _running_row(db, answers={"country": "United States of America"})

        out = aas.finalize_submission(aid, USER, status="needs_input",
                                      field="country",
                                      reason="field_not_on_page")

        assert out["answer_held"] is True
        item = _queue(aid)[0]
        assert item["field_id"] == "country"
        assert item["kind"] == "fill_failed"
        assert item["kind"] not in aas._ANSWERABLE_KINDS
        assert aas._answerable(item) is False
        # A hand-off has somewhere to go.
        assert item["action"] == "complete_in_browser"
        assert item["apply_url"] == _APPLY_URL

    def test_the_copy_does_not_ask_for_what_we_already_have(self, db):
        aid = _running_row(db, answers={"country": "United States of America"})
        aas.finalize_submission(aid, USER, status="needs_input",
                                field="country", reason="field_not_on_page")

        question = _queue(aid)[0]["question"]
        assert "We need your answer" not in question
        assert "have your answer" in question.lower()
        # The form's own label survives so the user knows WHICH field.
        assert "Country" in question

    def test_a_handoff_sentence_is_never_read_back_as_a_field_label(self, db):
        """A hand-off item's `question` is a SENTENCE of user copy, not a field
        label — feeding it to the next park nests it ("We have your answer for
        “We have your answer for “Country”…”"). The catalog label is right there.

        The queue is written directly because today's only re-park path clears it
        first (needs_input -> ready_for_review empties `needs_input`), so this
        pins the branch against a future edge that keeps the queue — the same
        shape `_park_verify`'s `update_needs_input` rewrite already uses."""
        aid = _running_row(db, answers={"country": "United States of America"})
        aas.finalize_submission(aid, USER, status="needs_input",
                                field="country", reason="field_not_on_page")
        first = _queue(aid)[0]
        assert first["question"].count("We have your answer") == 1

        db.applications.update_one(
            {"application_id": aid, "user_email": USER},
            {"$set": {"status": als.STATUS_RUNNING, "needs_input": [first]}})
        aas.finalize_submission(aid, USER, status="needs_input",
                                field="country", reason="field_not_on_page")

        assert _queue(aid)[0]["question"] == first["question"]

    def test_the_answer_sheet_is_left_intact(self, db):
        """Parking is not a reason to forget what the user told us — the value
        is still there for the retry / for the next employer."""
        aid = _running_row(db, answers={"country": "United States of America"})
        aas.finalize_submission(aid, USER, status="needs_input",
                                field="country", reason="field_not_on_page")

        stored = db.auto_apply_answers.find_one({"application_id": aid})
        assert stored["answers"]["country"] == "United States of America"


# ===========================================================================
# 2. The cycle itself — the mutation guard
# ===========================================================================
class TestTheCycleIsBroken:
    def test_answering_it_again_cannot_re_drive_the_same_failure(
            self, db, app, monkeypatch):
        """THE regression. Before the fix this returned `remaining: []` and put
        the row back on the ready -> approved -> submit path, straight into the
        identical fill failure — six times on the live Adyen row."""
        dispatched = _no_dispatch(monkeypatch)
        aid = _running_row(db, answers={"country": "United States of America"})
        aas.finalize_submission(aid, USER, status="needs_input",
                                field="country", reason="field_not_on_page")

        resp = _client(app)("post", "/api/apply/needs-input/answer",
                            {"application_id": aid,
                             "answers": {"country": "United States of America"}})

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["application_status"] == als.STATUS_NEEDS_INPUT
        assert [r["field_id"] for r in body["remaining"]] == ["country"]
        assert als.get_application(USER, aid)["status"] == als.STATUS_NEEDS_INPUT
        assert dispatched == [], "re-drove the drive that just failed"

    def test_a_second_drive_never_produces_a_second_ask(self, db):
        """The production sequence, end to end: the row is parked with a real
        question the FIRST time (no answer yet), the user answers, the fill still
        cannot place it — and that second report is a hand-off, so there is no
        third ask."""
        first = [{"question": "Country", "kind": "screening",
                  "field_id": "country", "value_type": "text", "required": True}]
        aid = _running_row(db, answers={}, needs=first)
        aas.finalize_submission(aid, USER, status="needs_input",
                                field="country", reason="unresolved")
        assert aas._answerable(_queue(aid)[0]) is True      # a real question

        # The user answers; the next drive still cannot drive the control.
        db.auto_apply_answers.update_one(
            {"application_id": aid, "user_email": USER},
            {"$set": {"answers": {"country": "United States of America"}}})
        als.transition(USER, aid, als.STATUS_READY_FOR_REVIEW)
        als.transition(USER, aid, als.STATUS_APPROVED)
        als.transition(USER, aid, als.STATUS_QUEUED_FOR_EXECUTION)
        als.transition(USER, aid, als.STATUS_RUNNING)
        aas.finalize_submission(aid, USER, status="needs_input",
                                field="country", reason="field_not_on_page")

        assert aas._answerable(_queue(aid)[0]) is False


# ===========================================================================
# 3. Narrowness — a field we really do not know is still a question
# ===========================================================================
class TestStillAsksWhenItReallyDoesNotKnow:
    def test_no_answer_sheet_at_all_stays_answerable(self, db):
        aid = _running_row(db, answers=None)

        aas.finalize_submission(aid, USER, status="needs_input",
                                field="years_experience", reason="unresolved")

        item = _queue(aid)[0]
        assert item["kind"] in aas._ANSWERABLE_KINDS
        assert aas._answerable(item) is True
        assert "action" not in item

    @pytest.mark.parametrize("held", [None, "", [], {}])
    def test_an_empty_held_value_is_not_an_answer(self, db, held):
        """EnergyHub's row carried `country: None`. A null is the ABSENCE of an
        answer — asking for it is correct, and must stay correct."""
        aid = _running_row(db, answers={"country": held})

        out = aas.finalize_submission(aid, USER, status="needs_input",
                                      field="country", reason="unresolved")

        assert out["answer_held"] is False
        assert aas._answerable(_queue(aid)[0]) is True

    def test_a_report_naming_no_field_is_still_a_wall(self, db):
        aid = _running_row(db, answers={"country": "United States of America"})

        aas.finalize_submission(aid, USER, status="needs_input", reason="captcha")

        item = _queue(aid)[0]
        assert item["kind"] == "captcha"
        assert aas._answerable(item) is False

    def test_an_executor_supplied_artifact_stays_an_attachment_problem(self, db):
        """`resume` is OUR handle, not the user's — even with a value on the
        sheet it must keep reading as an attachment failure, not as a field the
        user should go and set."""
        aid = _running_row(db, answers={"resume": "resume.pdf"})

        aas.finalize_submission(aid, USER, status="needs_input",
                                field="resume", reason="fill_failed")

        assert _queue(aid)[0]["kind"] == "attachment_failed"
