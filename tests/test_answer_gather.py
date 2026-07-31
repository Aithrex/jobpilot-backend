"""Offline tests for auto-apply answer gathering (Defect B).

The bug being fixed: the orchestrator handed the answer library DICT questions
while the library typed ``questions: List[str]`` and keyed results by question
TEXT — so every question was silently dropped and coverage was reported a false
1.0 with ZERO resolved answers. The fix is one normalized ``field_id`` schema
end-to-end: recon → `apply_forms.normalize_recon_questions` → field-id-keyed
`answer_library_service.resolve` → `auto_apply_service` looks answers up by the
same id.

Runs fully offline (mongomock; no network, no LLM). Covers, per the plan:
  * a dict question with a known field_id resolves BY field_id (not dropped);
  * an unknown required field → needs_input;
  * an unresolved legal attestation → needs_input + has_unconfirmed_legal;
  * a `declared`-source legal hit is ACCEPTED (never forced to needs_input);
  * coverage reflects ONLY required fields;
  * a fully-resolved set → coverage 1.0 WITH real answers (not the old false 1.0).
"""
import pytest

from _vg_support import current_db

import services.answer_library_service as lib
import services.apply_forms as apply_forms
import services.auto_apply_service as aas
import services.application_ledger_service as ledger
from services.executor_adapter import StubExecutor

USER = "alice@aspirely.me"

_WORK_AUTH_Q = "Are you legally authorized to work in the United States?"
_EMAIL_Q = "What is your email address?"
_PHONE_Q = "What is your phone number?"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def gather_db():
    d = current_db()
    for c in ("application_answer_library", "user_resumes", "resume_assets",
              "users", "user_preferences", "visa_profiles", "application_answers",
              "applications", "auto_submit_audit", "auto_apply_answers",
              "auto_apply_freezes", "auto_submit_settings"):
        d[c].delete_many({})
    return d


@pytest.fixture(autouse=True)
def _reset_executor():
    yield
    aas._executor = None


def _seed_resume(d, structured):
    d.resume_assets.insert_one({
        "resume_id": "asset-gather", "owner_type": "user",
        "user_email": USER, "structured": structured,
        "parse_status": "parsed", "parsed_at": "2026-01-01T00:00:00+00:00",
    })


def _nq(field_id, label, *, required=False, is_legal=False, type="text"):
    return {"field_id": field_id, "label": label, "type": type,
            "required": required, "is_legal_attestation": is_legal}


class _FakeExecutor(StubExecutor):
    """Recon returns real questions (no unavailable status) so the gather path
    runs end-to-end without a browser."""
    name = "fake"

    def __init__(self, questions, needs_account=False):
        self._questions = questions
        self._needs_account = needs_account

    def recon_form(self, apply_url, ats_family):
        return {"questions": self._questions, "needs_account": self._needs_account}


def _make_app(ats_family="greenhouse", apply_url="https://boards.greenhouse.io/x"):
    view, outcome = ledger.create_application(
        USER, company="Acme", title="Engineer", jd_text="Build things.",
        source="test", apply_url=apply_url, ats_family=ats_family)
    return view["application_id"]


def _recon_q(id, text, *, required=False, is_legal=False, type="text"):
    return {"id": id, "text": text, "type": type,
            "required": required, "is_legal_attestation": is_legal}


# ---------------------------------------------------------------------------
# resolve(): field-id keying + coverage
# ---------------------------------------------------------------------------
class TestResolveByFieldId:
    def test_known_field_id_resolved_not_dropped(self, gather_db):
        _seed_resume(gather_db, {"contact": {"email": "alice@aspirely.me"}})
        out = lib.resolve(USER, [_nq("email", _EMAIL_Q, required=True)])
        # keyed by field_id, carries the real value + a source.
        assert "email" in out["resolved"]
        assert out["resolved"]["email"]["value"] == "alice@aspirely.me"
        assert out["resolved"]["email"]["source"] == "profile"
        assert out["coverage"] == 1.0
        assert out["unresolved"] == []

    def test_unknown_required_field_is_unresolved(self, gather_db):
        out = lib.resolve(USER, [_nq("pet_riddle", "Zorp glimber wux plonk?",
                                     required=True)])
        assert out["resolved"] == {}
        assert "pet_riddle" in out["unresolved"]
        assert out["coverage"] == 0.0
        assert out["has_novel_free_text"] is True

    def test_unresolved_legal_needs_input_and_flag(self, gather_db):
        out = lib.resolve(USER, [_nq("work_authorized_us", _WORK_AUTH_Q,
                                     required=True, is_legal=True)])
        assert out["resolved"] == {}
        assert "work_authorized_us" in out["unresolved"]
        assert out["has_unconfirmed_legal_attestation"] is True

    def test_declared_source_legal_is_accepted(self, gather_db):
        gather_db.user_preferences.insert_one(
            {"user_email": USER, "work_authorized_us": True})
        out = lib.resolve(USER, [_nq("work_authorized_us", _WORK_AUTH_Q,
                                     required=True, is_legal=True)])
        hit = out["resolved"]["work_authorized_us"]
        assert hit["value"] is True
        assert hit["source"] == "declared"
        assert out["has_unconfirmed_legal_attestation"] is False
        assert out["coverage"] == 1.0

    def test_coverage_counts_only_required_fields(self, gather_db):
        _seed_resume(gather_db, {"contact": {"email": "alice@aspirely.me"}})
        out = lib.resolve(USER, [
            _nq("email", _EMAIL_Q, required=True),                       # resolved
            _nq("work_authorized_us", _WORK_AUTH_Q, required=True,       # unresolved
                is_legal=True),
            _nq("some_optional", "Zorp glimber wux plonk?", required=False),  # optional
        ])
        # 1 of 2 REQUIRED fields resolved; the optional gap does not count.
        assert out["coverage"] == pytest.approx(0.5)

    def test_fully_resolved_set_is_coverage_one_with_real_answers(self, gather_db):
        _seed_resume(gather_db, {"contact": {
            "email": "alice@aspirely.me", "phone": "+1-555-0100"}})
        out = lib.resolve(USER, [
            _nq("email", _EMAIL_Q, required=True),
            _nq("phone", _PHONE_Q, required=True),
        ])
        assert out["coverage"] == 1.0
        # The regression guard: coverage 1.0 must come WITH the actual answers,
        # not the old false 1.0-with-zero-resolved.
        assert len(out["resolved"]) == 2
        assert out["resolved"]["email"]["value"] == "alice@aspirely.me"
        assert out["resolved"]["phone"]["value"] == "+1-555-0100"


# ---------------------------------------------------------------------------
# normalize_recon_questions(): stable field_id from a recon label
# ---------------------------------------------------------------------------
class TestNormalizeRecon:
    def test_recon_label_maps_to_stable_field_id(self, gather_db):
        norm = apply_forms.normalize_recon_questions(
            [_recon_q("q_7fa3", _EMAIL_Q, required=True)])
        assert len(norm) == 1
        assert norm[0]["field_id"] == "email"
        assert norm[0]["recon_id"] == "q_7fa3"
        assert norm[0]["required"] is True

    def test_recon_legal_flag_or_classifier_survives(self, gather_db):
        # Even if recon forgot the flag, the classifier tags a legal category.
        norm = apply_forms.normalize_recon_questions(
            [_recon_q("q1", _WORK_AUTH_Q, is_legal=False)])
        assert norm[0]["field_id"] == "work_authorized_us"
        assert norm[0]["is_legal_attestation"] is True

    def test_one_control_reported_twice_is_merged(self, gather_db):
        """Same DOM handle = one input seen twice. Merge, keeping the stricter
        `required`."""
        norm = apply_forms.normalize_recon_questions([
            {"id": "q_years", "text": "Years of experience",
             "type": "text", "required": False},
            {"id": "q_years",
             "text": "How many years of professional experience do you have?",
             "type": "text", "required": True},
        ])
        assert len(norm) == 1
        assert norm[0]["field_id"] == "years_experience"
        assert norm[0]["required"] is True

    def test_distinct_controls_sharing_a_field_id_are_both_kept(self, gather_db):
        """THE production defect (EnergyHub, 2026-07-29).

        A required immigration attestation whose label mentions "…a written
        submission to your school…" classified onto `school` and collided with
        the form's real School control. The de-dupe dropped it: its recon_id was
        discarded so nothing filled it, while its `required` was OR-merged onto
        the survivor — so `_presubmit_gaps` saw a satisfied field, reported ZERO
        gaps, and the executor clicked Submit on an incomplete form. Only the
        ATS's own validation stopped it.

        Both controls must survive, each keeping its own DOM handle, and the
        survivor must NOT inherit the other's `required`."""
        norm = apply_forms.normalize_recon_questions([
            {"id": "school--0", "text": "School", "type": "select",
             "required": False},
            {"id": "question_999", "type": "select", "required": True,
             "text": "Do you require a written submission to your school to "
                     "maintain your employment eligibility?"},
        ])
        assert len(norm) == 2, "a distinct required control must never vanish"
        assert {q["recon_id"] for q in norm} == {"school--0", "question_999"}
        school = next(q for q in norm if q["recon_id"] == "school--0")
        assert school["required"] is False, "must not inherit the other's flag"

    def test_a_collision_key_is_stable_across_recons(self, gather_db):
        """A re-recon must yield the same keys, or a remembered answer would
        stop matching the question it was stored for."""
        recon = [
            {"id": "a", "text": "School", "type": "select", "required": False},
            {"id": "b", "text": "Which school did your sibling attend?",
             "type": "text", "required": True},
        ]
        first = apply_forms.normalize_recon_questions(recon)
        second = apply_forms.normalize_recon_questions(recon)
        assert [q["field_id"] for q in first] == [q["field_id"] for q in second]
        assert len({q["field_id"] for q in first}) == 2


# ---------------------------------------------------------------------------
# auto_apply gather: the end-to-end coverage fix + the legal guard
# ---------------------------------------------------------------------------
class TestGatherPipeline:
    def test_gather_resolves_answers_and_true_coverage(self, gather_db):
        _seed_resume(gather_db, {"contact": {
            "email": "alice@aspirely.me", "phone": "+1-555-0100"}})
        aas.set_executor(_FakeExecutor([
            _recon_q("q_email", _EMAIL_Q, required=True),
            _recon_q("q_phone", _PHONE_Q, required=True),
        ]))
        app_id = _make_app()
        res = aas.gather(app_id, USER)

        assert res["coverage"] == 1.0
        assert res["resolved"] == 2                 # real answers, not a false 1.0
        assert res["autonomy"] == "auto"            # greenhouse + full coverage
        # F: clean auto → ready_for_review → system-approved → `approved`.
        assert res["status"] == ledger.STATUS_APPROVED
        # answers actually persisted, keyed by the stable field_id.
        stored = aas._load_answers(app_id, USER)
        assert stored["email"] == "alice@aspirely.me"
        assert stored["phone"] == "+1-555-0100"

    def test_gather_unresolved_legal_forces_needs_input(self, gather_db):
        _seed_resume(gather_db, {"contact": {"email": "alice@aspirely.me"}})
        aas.set_executor(_FakeExecutor([
            _recon_q("q_email", _EMAIL_Q, required=True),
            _recon_q("q_auth", _WORK_AUTH_Q, required=True, is_legal=True),
        ]))
        app_id = _make_app()
        res = aas.gather(app_id, USER)

        assert res["status"] == ledger.STATUS_NEEDS_INPUT
        assert res["autonomy"] == "ask"             # never auto on unconfirmed legal
        assert res["needs_input"] >= 1
        # the legal field is NOT in the resolved answers (never guessed).
        assert "work_authorized_us" not in aas._load_answers(app_id, USER)

    def test_gather_declared_legal_is_accepted(self, gather_db):
        _seed_resume(gather_db, {"contact": {"email": "alice@aspirely.me"}})
        gather_db.user_preferences.insert_one(
            {"user_email": USER, "work_authorized_us": True})
        aas.set_executor(_FakeExecutor([
            _recon_q("q_email", _EMAIL_Q, required=True),
            _recon_q("q_auth", _WORK_AUTH_Q, required=True, is_legal=True),
        ]))
        app_id = _make_app()
        res = aas.gather(app_id, USER)

        # declared work-auth resolves → no legal gate → clean auto → approved (F).
        assert res["status"] == ledger.STATUS_APPROVED
        assert res["autonomy"] == "auto"
        assert aas._load_answers(app_id, USER)["work_authorized_us"] is True
