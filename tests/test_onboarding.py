"""Offline tests for auto-apply ONBOARDING (QT2) + the J stale-deep-link fix.

Runs fully offline (mongomock; no network, no LLM, no AWS). Proves the
``docs/specs/auto-apply-fill-engine.md`` "## Onboarding profile" contract:

  * GET returns the CANONICAL question bank grouped by category, each field with
    required / sensitivity / default / current_value / missing, plus a top-level
    ``ready_for_autoapply`` (false while a required field is missing) and ``mode``.
  * PUT validates each field_id + value against the catalog and persists to the
    SAME declared store the answer library resolves from — proven by a ROUND-TRIP:
    a declared work-auth + an EEO "decline" written at onboarding are returned by
    ``answer_library_service.resolve`` as source "declared".
  * unknown field_id -> 400 (never 401); mode is set through autonomy_service.
  * the READY gate flips true once contact + work-auth + sponsorship are present.
  * protected/legal fields DEFAULT to decline and are only ever DECLARED — an
    undeclared EEO field is never inferred (resolve leaves it unresolved).
  * J: the applications deep-link builders return `/next`-based URLs (no bare
    `/applications`); onboarding business failures are 400 — never 401.
"""
import pytest

from _vg_support import current_db, load_service_app

import services.apply_onboarding_service as onb
import services.answer_library_service as lib
import services.autonomy_service as autonomy
import services.application_question_bank as qb

USER = "alice@aspirely.me"
OTHER = "bob@aspirely.me"

_WORK_AUTH_Q = "Are you legally authorized to work in the United States?"
_GENDER_Q = "What is your gender?"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def onb_db():
    d = current_db()
    for c in ("user_preferences", "user_resumes", "resume_assets", "users",
              "visa_profiles", "application_answer_library", "auto_submit_settings"):
        d[c].delete_many({})
    return d


def _seed_contact(d, email=USER, name="Alice Smith",
                  contact_email="alice@aspirely.me", phone="+1-555-0100"):
    d.resume_assets.insert_one({
        "resume_id": f"asset-{email}", "owner_type": "user",
        "user_email": email,
        "structured": {"contact": {"name": name, "email": contact_email,
                                   "phone": phone}},
        "parse_status": "parsed", "parsed_at": "2026-01-01T00:00:00+00:00",
    })


def _nq(field_id, label, *, required=False, is_legal=False, type="text"):
    return {"field_id": field_id, "label": label, "type": type,
            "required": required, "is_legal_attestation": is_legal}


def _find_field(payload, field_id):
    for group in payload["groups"]:
        for f in group["fields"]:
            if f["field_id"] == field_id:
                return f, group["category"]
    return None, None


# ---------------------------------------------------------------------------
# GET — the grouped catalog
# ---------------------------------------------------------------------------
class TestQuestionsPayload:
    def test_grouped_by_category_with_flags_and_ready_false(self, onb_db):
        payload = onb.build_questions_payload(USER)

        # grouped by category, in the documented order, no free-text essays.
        cats = [g["category"] for g in payload["groups"]]
        assert cats == [c for c in onb._CATEGORY_ORDER if c in cats]
        assert qb.CAT_FREE_TEXT not in cats

        # a required work-auth field carries the full shape.
        wa, cat = _find_field(payload, "work_authorized_us")
        assert cat == qb.CAT_WORK_AUTH
        assert wa["required"] is True                 # default_policy == required_onboarding
        assert wa["sensitivity"] == qb.SENS_LEGAL
        assert wa["current_value"] is None
        assert wa["missing"] is True                  # required && no value
        assert wa["default"] is None                  # factual — never a guessed default

        # nothing declared/derived yet -> not ready.
        assert payload["ready_for_autoapply"] is False
        assert payload["mode"] is None
        assert set(payload["required_fields"]) == set(onb.READY_REQUIRED_FIELDS)

    def test_identity_current_value_from_resume_profile(self, onb_db):
        _seed_contact(onb_db)
        payload = onb.build_questions_payload(USER)
        email_f, _ = _find_field(payload, "email")
        name_f, _ = _find_field(payload, "full_name")
        assert email_f["current_value"] == "alice@aspirely.me"
        assert name_f["current_value"] == "Alice Smith"

    def test_protected_and_legal_default_to_decline(self, onb_db):
        payload = onb.build_questions_payload(USER)
        gender, cat = _find_field(payload, "gender")
        assert cat == qb.CAT_EEO
        assert gender["sensitivity"] == qb.SENS_PROTECTED
        # EEO default is the form's real DECLINE option (never an inferred value).
        assert gender["default"] == qb.DECLINE
        assert gender["current_value"] is None
        # EEO is voluntary -> not a required_onboarding field -> not "missing".
        assert gender["required"] is False
        assert gender["missing"] is False

        # a decline_default sensitive field (pay history) also defaults to decline
        # only when it has a decline option; a money field has none -> None.
        salary, _ = _find_field(payload, "current_salary")
        assert salary["default"] is None


# ---------------------------------------------------------------------------
# PUT — persistence + the declared-store round-trip
# ---------------------------------------------------------------------------
class TestSaveAnswers:
    def test_persists_to_declared_store_and_resolver_round_trip(self, onb_db):
        # Declare a factual work-auth + a voluntary EEO "decline" at onboarding.
        onb.save_answers(USER, {
            "work_authorized_us": True,
            "gender": qb.DECLINE,
        })

        # The SAME declared store the answer library resolves from now yields them,
        # as explicit source="declared" values (never guessed).
        res = lib.resolve(USER, [
            _nq("work_authorized_us", _WORK_AUTH_Q, required=True, is_legal=True),
            _nq("gender", _GENDER_Q, is_legal=True),
        ])
        wa = res["resolved"]["work_authorized_us"]
        ge = res["resolved"]["gender"]
        assert wa["value"] is True and wa["source"] == "declared"
        assert ge["value"] == qb.DECLINE and ge["source"] == "declared"
        assert res["has_unconfirmed_legal_attestation"] is False

        # and the refreshed onboarding payload reflects the new current values.
        payload = onb.build_questions_payload(USER)
        wa_f, _ = _find_field(payload, "work_authorized_us")
        assert wa_f["current_value"] is True
        assert wa_f["missing"] is False

    def test_unknown_field_id_rejected(self, onb_db):
        with pytest.raises(onb.OnboardingError) as ei:
            onb.save_answers(USER, {"pet_riddle": "zorp"})
        assert ei.value.status == 400
        assert ei.value.error == "unknown_field"

    def test_free_text_field_is_not_an_onboarding_field(self, onb_db):
        # cover_letter is a generated essay, never declared -> rejected as unknown.
        with pytest.raises(onb.OnboardingError) as ei:
            onb.save_answers(USER, {"cover_letter": "hi"})
        assert ei.value.status == 400

    def test_bad_select_value_rejected_before_persist(self, onb_db):
        with pytest.raises(onb.OnboardingError) as ei:
            onb.save_answers(USER, {"gender": "not-an-option"})
        assert ei.value.status == 400
        assert ei.value.error == "invalid_value"
        # nothing half-persisted.
        assert onb_db.user_preferences.find_one({"user_email": USER}) is None

    def test_sets_mode(self, onb_db):
        onb.save_answers(USER, {"work_authorized_us": True},
                         mode=autonomy.MODE_APPROVAL)
        assert autonomy.get_mode(USER) == autonomy.MODE_APPROVAL

    def test_invalid_mode_rejected(self, onb_db):
        with pytest.raises(onb.OnboardingError) as ei:
            onb.save_answers(USER, {"work_authorized_us": True}, mode="turbo")
        assert ei.value.status == 400
        assert ei.value.error == "invalid_mode"


# ---------------------------------------------------------------------------
# The READY gate: contact + work_auth + sponsorship
# ---------------------------------------------------------------------------
class TestReadyGate:
    def test_ready_flips_true_once_required_set_present(self, onb_db):
        _seed_contact(onb_db)                     # contact from the résumé
        assert onb.build_questions_payload(USER)["ready_for_autoapply"] is False

        # work-auth alone is not enough — sponsorship still missing.
        onb.save_answers(USER, {"work_authorized_us": True})
        assert onb.build_questions_payload(USER)["ready_for_autoapply"] is False

        onb.save_answers(USER, {"requires_sponsorship": False})
        assert onb.build_questions_payload(USER)["ready_for_autoapply"] is True

    def test_missing_contact_keeps_not_ready(self, onb_db):
        # work-auth declared but NO résumé/contact -> still not ready.
        onb.save_answers(USER, {"work_authorized_us": True,
                                "requires_sponsorship": False})
        assert onb.build_questions_payload(USER)["ready_for_autoapply"] is False


# ---------------------------------------------------------------------------
# Never-fabricate: protected/legal only used when DECLARED
# ---------------------------------------------------------------------------
class TestNeverFabricate:
    def test_undeclared_eeo_is_never_inferred(self, onb_db):
        _seed_contact(onb_db)
        # No gender declared anywhere -> the resolver leaves it unresolved and
        # flags an unconfirmed legal/EEO attestation (never a guess).
        res = lib.resolve(USER, [_nq("gender", _GENDER_Q, required=True, is_legal=True)])
        assert "gender" not in res["resolved"]
        assert "gender" in res["unresolved"]
        assert res["has_unconfirmed_legal_attestation"] is True

    def test_declared_decline_is_used(self, onb_db):
        onb.save_answers(USER, {"disability_status": qb.DECLINE})
        res = lib.resolve(USER, [_nq("disability_status", "Disability status",
                                     is_legal=True)])
        hit = res["resolved"]["disability_status"]
        assert hit["value"] == qb.DECLINE
        assert hit["source"] == "declared"


# ---------------------------------------------------------------------------
# J — stale deep links point at the `/next` shell (no bare /applications)
# ---------------------------------------------------------------------------
class TestDeepLinksJ:
    def test_app_applications_url_is_next_based(self, monkeypatch):
        monkeypatch.setenv("FRONTEND_URL", "https://app.test")
        load_service_app("jobs")                  # registers blueprints.apply
        from blueprints.apply import _app_applications_url
        url = _app_applications_url()
        assert url == "https://app.test/next/applications"
        assert "/next/applications" in url
        assert not url.endswith("/applications") or url.endswith("/next/applications")

    def test_today_inbox_link_constants_are_next_based(self):
        import services.today_inbox_service as inbox
        assert inbox._LINK_APPLICATIONS == "/next/applications"
        assert inbox._LINK_APPLICATIONS_INBOX == "/next/applications/inbox"


# ---------------------------------------------------------------------------
# Route contract on the jobs Lambda: 401 ONLY for a missing JWT; every business
# failure is 400 — never 401 (the FE force-logs-out on any 401).
# ---------------------------------------------------------------------------
class TestOnboardingRoutes:
    @pytest.fixture()
    def app(self, onb_db):
        return load_service_app("jobs")

    def _hdr(self, app, email):
        from flask_jwt_extended import create_access_token
        with app.app_context():
            token = create_access_token(identity=email)
        return {"Authorization": f"Bearer {token}"}

    def test_get_questions_requires_jwt(self, app):
        # No JWT -> 401 (the ONLY 401 path — @jwt_required).
        resp = app.test_client().get("/api/apply/onboarding/questions")
        assert resp.status_code == 401

    def test_get_questions_ok(self, app):
        resp = app.test_client().get(
            "/api/apply/onboarding/questions", headers=self._hdr(app, USER))
        assert resp.status_code == 200
        body = resp.get_json()
        assert "groups" in body
        assert body["ready_for_autoapply"] is False
        assert body["mode"] is None

    def test_put_unknown_field_is_400_not_401(self, app):
        resp = app.test_client().put(
            "/api/apply/onboarding/answers",
            json={"answers": {"pet_riddle": "zorp"}},
            headers=self._hdr(app, USER))
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "unknown_field"

    def test_put_non_object_answers_is_400(self, app):
        resp = app.test_client().put(
            "/api/apply/onboarding/answers",
            json={"answers": "nope"},
            headers=self._hdr(app, USER))
        assert resp.status_code == 400

    def test_put_persists_and_sets_mode(self, app):
        resp = app.test_client().put(
            "/api/apply/onboarding/answers",
            json={"answers": {"work_authorized_us": True,
                              "requires_sponsorship": False},
                  "mode": "approval"},
            headers=self._hdr(app, USER))
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["mode"] == "approval"
        wa = next(f for g in body["groups"] for f in g["fields"]
                  if f["field_id"] == "work_authorized_us")
        assert wa["current_value"] is True
