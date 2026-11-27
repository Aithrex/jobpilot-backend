"""Candidate brief (2026-11-26): one markdown profile per user, its cite-able
srv.* facts, the answer cache and the cache-friendly prompt order.

Offline: mongomock via `_vg_support`, a fake `claude_json`, no network.
"""
import pytest
from flask_jwt_extended import create_access_token

from _vg_support import current_db, load_service_app
from _vg_support import _reset_rate_limits  # noqa: F401  (autouse: clean limiter per test)

import services.candidate_brief as cb
import services.claude_client as claude_client
import services.entitlements_service as ent

USER = "brief@aspirely.me"

STRUCTURED = {
    "summary": "Backend engineer who ships multi-tenant SaaS.",
    "experience": [
        {"title": "Founding Engineer", "company": "Standup AI", "dates": "2024 - Present",
         "bullets": ["Built OIDC SSO and SCIM 2.0.", "Cleared 46 security advisories before launch."]},
        {"title": "Intern", "company": "Globex", "dates": "2022",
         "bullets": ["Wrote Python ETL jobs."]},
    ],
    "education": [{"degree": "MS", "field": "Information Technology",
                   "institution": "Arizona State University", "dates": "2026"}],
    "skills": ["Python", "Postgres", "AWS"],
    "certifications": ["AWS Solutions Architect Associate"],
}

PREFS = {
    "user_email": USER,
    "requires_sponsorship": True,
    "requires_sponsorship_future": True,
    "work_authorized_us": True,
    "remote_preference": "Remote",
    "notice_period": "Immediate",
    # Never in the brief:
    "gender": "Male", "race_ethnicity": "Asian", "veteran_status": "No",
    "disability_status": "No", "email": "x@y.z", "phone": "+1 602 555 0100",
    "criminal_history": False,
}

CLIENT_FACTS = [
    {"id": "exp[0].title", "kind": "experience", "text": "Founding Engineer at Standup AI (2024 - Present)"},
    {"id": "jd.company", "kind": "job", "text": "Discord"},
]

_COLLECTIONS = ("users", "resume_assets", "user_preferences", "candidate_briefs",
                "answer_cache", "application_answers", "feature_usage", "quota_ledger",
                "quota_counters", "visa_profiles", "application_answer_library")


@pytest.fixture()
def db():
    d = current_db()
    for c in _COLLECTIONS:
        d[c].delete_many({})
    d.users.insert_one({"email": USER, "active_resume_id": "asset-brief"})
    d.resume_assets.insert_one({"resume_id": "asset-brief", "user_email": USER,
                                "structured": STRUCTURED, "parsed_at": 1})
    d.user_preferences.insert_one(dict(PREFS))
    yield d
    for c in _COLLECTIONS:
        d[c].delete_many({})


@pytest.fixture()
def app(db):
    return load_service_app("jobs")


def _hdr(app):
    with app.app_context():
        token = create_access_token(identity=USER)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def llm(monkeypatch):
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


class TestBuild:
    def test_brief_has_resume_and_answers_with_stable_ids(self):
        b = cb.build_brief(STRUCTURED, PREFS)
        ids = [f["id"] for f in b["facts"]]
        assert "srv.exp[0].title" in ids and "srv.exp[0].highlights[1]" in ids
        assert "srv.edu[0]" in ids and "srv.skills[0]" in ids and "srv.cert[0]" in ids
        assert "srv.auth.sponsorship_now" in ids and "srv.pref.notice_period" in ids
        assert b["md"].startswith("# Candidate brief")
        assert "[srv.exp[0].highlights[1]] Cleared 46 security advisories" in b["md"]
        # Same input, same output.
        assert cb.build_brief(STRUCTURED, PREFS) == b

    def test_never_eeo_contact_or_legal(self):
        b = cb.build_brief(STRUCTURED, PREFS)
        text = b["md"].lower()
        for word in ("male", "asian", "veteran", "disability", "x@y.z", "555", "criminal"):
            assert word not in text, word

    def test_every_id_passes_the_citation_grammar(self):
        from services.answer_citations import FACT_ID_RE
        for f in cb.build_brief(STRUCTURED, PREFS)["facts"]:
            assert FACT_ID_RE.match(f["id"]), f["id"]


class TestStore:
    def test_rebuilds_only_when_sources_change(self, db, monkeypatch):
        calls = []
        real = cb.build_brief
        monkeypatch.setattr(cb, "build_brief", lambda s, d: calls.append(1) or real(s, d))
        first = cb.get_brief(USER)
        again = cb.get_brief(USER)
        assert len(calls) == 1 and again["hash"] == first["hash"]
        db.user_preferences.update_one({"user_email": USER}, {"$set": {"notice_period": "2 weeks"}})
        changed = cb.get_brief(USER)
        assert len(calls) == 2 and changed["hash"] != first["hash"]
        # An EEO edit is not a source: no rebuild.
        db.user_preferences.update_one({"user_email": USER}, {"$set": {"gender": "Female"}})
        cb.get_brief(USER)
        assert len(calls) == 2

    def test_get_route(self, app):
        r = app.test_client().get("/api/apply/brief", headers=_hdr(app))
        assert r.status_code == 200
        body = r.get_json()
        assert set(body) == {"md", "facts", "hash", "built_at"}
        assert body["facts"] and body["hash"]


GOOD = {"answer": "As Founding Engineer at Standup AI I cleared 46 security advisories before launch.",
        "angle": "Security", "used_fact_ids": ["exp[0].title", "srv.exp[0].highlights[1]"]}


class TestAnswer:
    def _post(self, app, **extra):
        body = {"question": "Why Discord?", "company": "Discord", "role": "Engineer",
                "facts": CLIENT_FACTS, **extra}
        return app.test_client().post("/api/apply/answer", json=body, headers=_hdr(app))

    def test_brief_facts_come_before_the_question(self, app, llm):
        llm.reply = {"variants": [GOOD]}
        r = self._post(app)
        assert r.status_code == 200
        prompt = llm.calls[0]["prompt"]
        facts_at = prompt.index("srv.exp[0].highlights[1]")
        assert facts_at < prompt.index("=== THE JOB ===") < prompt.index("=== THE QUESTION BEING ASKED ===")

    def test_cited_server_facts_are_returned(self, app, llm):
        llm.reply = {"variants": [GOOD]}
        body = self._post(app).get_json()
        assert body["cached"] is False and body["brief_hash"]
        assert body["cited_facts"] == {"srv.exp[0].highlights[1]": "Cleared 46 security advisories before launch."}

    def test_repeat_is_served_from_cache_without_model_or_quota(self, app, llm, monkeypatch):
        llm.reply = {"variants": [GOOD]}
        assert self._post(app).status_code == 200
        charged = []
        monkeypatch.setattr(ent, "check_and_increment",
                            lambda *a, **k: charged.append(a) or (False, {"limit": 0}))
        r = self._post(app)
        assert r.status_code == 200
        body = r.get_json()
        assert body["cached"] is True and body["variants"][0]["answer"] == GOOD["answer"]
        assert body["cited_facts"]["srv.exp[0].highlights[1]"]
        assert len(llm.calls) == 1 and charged == []

    def test_a_different_question_is_not_a_cache_hit(self, app, llm):
        llm.reply = {"variants": [GOOD]}
        self._post(app)
        self._post(app, question="Why this role?")
        assert len(llm.calls) == 2
