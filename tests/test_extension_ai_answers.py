"""`POST /api/apply/answer` with client facts (ApplyPilot BR-2) and the three
catalog ids the extension shares (BR-1, BR-3, BR-4).

Offline: mongomock via `_vg_support`, a fake `claude_json`, no network.
"""
import pytest
from flask_jwt_extended import create_access_token

from _vg_support import current_db, load_service_app

import services.answer_citations as cit
import services.application_question_bank as qb
import services.claude_client as claude_client
import services.apply_onboarding_service as onb
import services.entitlements_service as ent
from utils.security import InputSanitizer

USER = "cite@aspirely.me"

FACTS = [
    {"id": "exp[0].title", "kind": "experience", "text": "Staff Engineer at Globex (2022 - Present)"},
    {"id": "exp[0].highlights[0]", "kind": "experience", "text": "Cut p99 checkout latency 40%."},
    {"id": "skill[0]", "kind": "skill", "text": "Python"},
    {"id": "jd.company", "kind": "job", "text": "Acme"},
]

_COLLECTIONS = ("users", "resume_assets", "user_resumes", "application_answers",
                "feature_usage", "quota_ledger", "quota_counters", "user_preferences",
                "visa_profiles", "application_answer_library", "auto_submit_settings")


@pytest.fixture()
def db():
    d = current_db()
    for c in _COLLECTIONS:
        d[c].delete_many({})
    yield d
    for c in _COLLECTIONS:
        d[c].delete_many({})


@pytest.fixture()
def app(db):
    return load_service_app("jobs")


def _hdr(app, email=USER):
    with app.app_context():
        token = create_access_token(identity=email)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def llm(monkeypatch):
    """A fake model. `llm.reply` is what it returns; `llm.calls` what it got."""
    class _L:
        calls = []
        reply = {"variants": []}

    def _fake(*args, **kwargs):
        if args:
            kwargs.setdefault("prompt", args[0])
        _L.calls.append(kwargs)
        return _L.reply

    _L.calls = []
    monkeypatch.setattr(claude_client, "claude_json", _fake)
    return _L


def _post(app, body):
    return app.test_client().post("/api/apply/answer", json=body, headers=_hdr(app))


GOOD = {"answer": "As a Staff Engineer at Globex I cut p99 checkout latency 40%.",
        "angle": "Specific project", "used_fact_ids": ["exp[0].title", "exp[0].highlights[0]"]}


# ---------------------------------------------------------------------------
# The pure checks
# ---------------------------------------------------------------------------
class TestCheckVariant:
    by_id = {f["id"]: f for f in FACTS}

    def test_cited_and_grounded_passes(self):
        ok, reason, cited = cit.check_variant(GOOD["answer"], GOOD["used_fact_ids"], self.by_id)
        assert ok and reason == "" and cited == GOOD["used_fact_ids"]

    def test_unknown_id_is_dropped(self):
        ok, reason, _ = cit.check_variant("I led Globex.", ["exp[9].title"], self.by_id)
        assert (ok, reason) == (False, "unknown_fact_id")

    def test_only_job_facts_is_uncited(self):
        ok, reason, _ = cit.check_variant("I admire Acme.", ["jd.company"], self.by_id)
        assert (ok, reason) == (False, "uncited")
        assert cit.check_variant("I admire Acme.", [], self.by_id)[1] == "uncited"

    def test_a_number_no_cited_fact_holds_is_dropped(self):
        ok, reason, _ = cit.check_variant(
            "I cut latency 60% over 5 years.", ["exp[0].highlights[0]"], self.by_id)
        assert (ok, reason) == (False, "ungrounded_number")

    def test_a_number_in_an_uncited_fact_does_not_count(self):
        # 2022 is in exp[0].title, but only the skill fact was cited.
        ok, reason, _ = cit.check_variant("Python since 2022.", ["skill[0]"], self.by_id)
        assert (ok, reason) == (False, "ungrounded_number")


class TestParseFacts:
    s = staticmethod(InputSanitizer.sanitize_string)

    def test_absent_is_none(self):
        assert cit.parse_facts(None, self.s) is None

    @pytest.mark.parametrize("bad", [
        "x", [1], [{"id": "", "text": "a"}], [{"id": "a b", "text": "a"}],
        [{"id": "exp[0].title", "text": 3}],
        [{"id": "x", "text": "a"}, {"id": "x", "text": "b"}],
        [{"id": "jd.company", "text": "Acme"}],          # nothing about the user
        [{"id": f"skill[{i}]", "text": "x"} for i in range(cit.MAX_FACTS + 1)],
    ])
    def test_malformed_is_refused(self, bad):
        with pytest.raises(cit.FactsError):
            cit.parse_facts(bad, self.s)

    def test_extension_ids_parse(self):
        ids = ["pf.summary", "exp[0].highlights[12]", "persona.emphasis[1]", "cert[0]", "jd.role"]
        out = cit.parse_facts([{"id": i, "text": "t"} for i in ids], self.s)
        assert [f["id"] for f in out] == ids


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------
class TestCitedRoute:
    def test_cited_variants_come_back_with_their_ids(self, app, llm):
        llm.reply = {"variants": [GOOD]}
        resp = _post(app, {"question": "Why Acme?", "company": "Acme", "facts": FACTS})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["grounding"] == "client_facts"
        assert body["variants"][0]["used_fact_ids"] == GOOD["used_fact_ids"]
        # The model saw the ids, inside the untrusted block; no base résumé needed.
        prompt = llm.calls[0]["prompt"]
        assert "[exp[0].highlights[0]] Cut p99 checkout latency 40%." in prompt
        assert llm.calls[0]["schema"]["variants"][0]["used_fact_ids"] == [str]

    def test_bad_variants_are_dropped_and_good_ones_kept(self, app, llm):
        llm.reply = {"variants": [
            {"answer": "I led Initech.", "angle": "a", "used_fact_ids": ["exp[7].title"]},
            {"answer": "Acme is great.", "angle": "b", "used_fact_ids": ["jd.company"]},
            {"answer": "I cut latency 90%.", "angle": "c", "used_fact_ids": ["exp[0].highlights[0]"]},
            GOOD,
        ]}
        body = _post(app, {"question": "Why Acme?", "facts": FACTS}).get_json()
        assert [v["answer"] for v in body["variants"]] == [GOOD["answer"]]

    def test_all_dropped_is_502_no_grounded_answer(self, app, llm):
        llm.reply = {"variants": [{"answer": "I have 10 years.", "angle": "a",
                                   "used_fact_ids": ["skill[0]"]}]}
        resp = _post(app, {"question": "Experience?", "facts": FACTS})
        assert resp.status_code == 502
        assert resp.get_json()["error"] == "no_grounded_answer"

    def test_malformed_facts_are_400_and_spend_nothing(self, app, llm, monkeypatch):
        charged = []
        monkeypatch.setattr(ent, "check_and_increment",
                            lambda *a, **k: charged.append(a) or (True, {}))
        resp = _post(app, {"question": "Why?", "facts": [{"id": "bad id", "text": "x"}]})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_facts"
        assert charged == [] and llm.calls == []

    def test_quota_gate_runs_before_the_model(self, app, llm, monkeypatch):
        monkeypatch.setattr(ent, "check_and_increment",
                            lambda *a, **k: (False, {"limit": 5, "used": 5}))
        resp = _post(app, {"question": "Why Acme?", "facts": FACTS})
        assert resp.status_code == 429
        assert llm.calls == []


class TestOldShape:
    def test_no_facts_keeps_the_old_contract_plus_empty_citations(self, app, db, llm):
        db.users.insert_one({"email": USER, "active_resume_id": "asset-cite"})
        db.resume_assets.insert_one({
            "resume_id": "asset-cite", "owner_type": "user", "user_email": USER,
            "structured": {"contact": {"name": "A"}, "summary": "Backend engineer.",
                           "skills": {"Languages": ["Python"]},
                           "experience": [{"title": "SWE", "company": "Globex",
                                           "bullets": ["Built things."]}]},
            "parse_status": "parsed", "parsed_at": "2026-01-01T00:00:00+00:00",
        })
        llm.reply = {"variants": [{"answer": "I built things at Globex.", "angle": "x"}]}
        resp = _post(app, {"question": "Why us?"})
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["grounding"] == "resume"
        assert body["variants"][0]["answer"] == "I built things at Globex."
        assert body["variants"][0]["used_fact_ids"] == []
        assert set(body["variants"][0]) == {"answer", "angle", "used_fact_ids"}
        assert "used_fact_ids" not in llm.calls[0]["schema"]["variants"][0]

    def test_no_facts_and_no_resume_is_still_no_resume(self, app, llm):
        resp = _post(app, {"question": "Why us?"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "no_resume"


# ---------------------------------------------------------------------------
# BR-1 / BR-3 / BR-4 — shared catalog ids
# ---------------------------------------------------------------------------
class TestSharedCatalogIds:
    @pytest.mark.parametrize("fid,cat", [
        ("requires_sponsorship_future", qb.CAT_WORK_AUTH),
        ("work_location", qb.CAT_SCREENING),
        ("skills", qb.CAT_SCREENING),
    ])
    def test_ids_exist_with_the_extension_shape(self, fid, cat):
        f = qb.get_field(fid)
        assert f is not None and f["category"] == cat

    def test_future_sponsorship_is_a_gated_boolean(self):
        f = qb.get_field("requires_sponsorship_future")
        assert f["value_type"] == qb.VT_BOOLEAN and f["sensitivity"] == qb.SENS_LEGAL
        assert qb.is_gated("requires_sponsorship_future")

    def test_classification_of_existing_labels_is_unchanged(self):
        assert qb.normalize_field("Will you now or in the future require sponsorship?")[
            "field_id"] == "requires_sponsorship"
        assert qb.normalize_field("Do you require visa sponsorship?")["field_id"] == "requires_sponsorship"

    @pytest.mark.parametrize("label,fid", [
        ("Skills", "skills"),
        ("Technical skills", "skills"),
        ("Employer location", "work_location"),
        ("Location of employer", "work_location"),
    ])
    def test_anchored_aliases_classify(self, label, fid):
        assert qb._deterministic_match(qb._norm(label))["field_id"] == fid

    @pytest.mark.parametrize("label", [
        "Preferred work location", "Location", "Do you have the skills for this role?",
        "What skills would you like to develop?",
    ])
    def test_generic_labels_never_collapse_onto_them(self, label):
        hit = qb._deterministic_match(qb._norm(label))
        assert (hit or {}).get("field_id") not in ("work_location", "skills")

    def test_onboarding_put_accepts_the_three_ids(self, app):
        resp = app.test_client().put(
            "/api/apply/onboarding/answers",
            json={"answers": {"requires_sponsorship_future": True,
                              "work_location": "Austin, TX",
                              "skills": "Python, Go"}},
            headers=_hdr(app))
        assert resp.status_code == 200, resp.get_json()
        values = {f["field_id"]: f["current_value"]
                  for g in resp.get_json()["groups"] for f in g["fields"]}
        assert values["requires_sponsorship_future"] is True

    def test_future_sponsorship_does_not_unready_a_finished_onboarding(self):
        assert "requires_sponsorship_future" not in onb.READY_REQUIRED_FIELDS
