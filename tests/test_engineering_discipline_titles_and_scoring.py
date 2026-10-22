"""An electrical-power graduate is searched, suggested and scored as an
electrical engineer - never as a software or IT-support candidate.

Reported 2026-10-21 on a live account (MS Electrical Power & Energy Systems,
held titles: assistantships and an "Electrical Design Trainee"):

* "Suggest from résumé" filled the title chips with SDE and IT-support
  titles. Every intent the suggester knew was a software intent, so the résumé
  became a `backend` "generalist", got the generic SWE safety net, and a
  prompt ending "ALWAYS include Software Engineer".
* The stored suggestion then poisoned the SEARCH: one software title flips
  `canonical_search_titles` onto its technical branch, which never emits the
  discipline's own role names.
* Scores were flat. The lexicon read MATLAB and Python off the résumé and
  nothing else, so Electrical Engineer I, Power Systems Engineer and a Field
  Service Technician job all scored 40, and a technician job was #1 live.
"""
from __future__ import annotations

import sys

import pytest

from _vg_support import current_db

from services import fit_score
from services import job_title_seed as seed

USER = "ee.graduate@example.com"


def _ee_structured():
    return {
        "contact": {"name": "Test Graduate", "email": USER},
        "summary": "Electrical power engineering graduate focused on power system "
                   "studies, protection and renewable integration.",
        "skills": {"technical": ["ETAP", "PSCAD", "MATLAB", "Simulink", "AutoCAD",
                                 "Power flow", "Short circuit analysis",
                                 "Protection coordination", "Python"]},
        "experience": [
            {"title": "Graduate Research Assistant", "company": "State University",
             "start_date": "2024-08", "end_date": "2026-05",
             "bullets": ["Performed load flow and short circuit studies in ETAP for "
                         "a 69 kV substation", "Modeled inverter-based resources in PSCAD"]},
            {"title": "Graduate Service Assistant", "company": "State University",
             "start_date": "2024-08", "end_date": "2025-05",
             "bullets": ["Supported lab equipment"]},
            {"title": "Electrical Design Trainee", "company": "Power Co",
             "start_date": "2023-01", "end_date": "2023-07",
             "bullets": ["Prepared single line diagrams in AutoCAD"]},
        ],
        "education": [{"degree": "M.S. Electrical Power and Energy Systems",
                       "institution": "State University"}],
    }


JOBS = {
    "ee": ("Electrical Engineer I",
           "Entry level electrical engineer. Perform power flow, short circuit and arc "
           "flash studies using ETAP or SKM PowerTools. Prepare one-line diagrams in "
           "AutoCAD. Protection coordination. Bachelor's in electrical engineering."),
    "power": ("Power Systems Engineer",
              "Interconnection studies, PSS/E and PSCAD modeling, transmission planning, "
              "power flow, short circuit."),
    "tech": ("Field Service Technician, Electrical Controls",
             "Install and troubleshoot electrical controls and PLC programming on poultry "
             "equipment. Travel 70%. Read wiring diagrams. High school diploma."),
    "it": ("IT Support Technician",
           "Help desk support, Windows, Active Directory, troubleshooting hardware and "
           "software, ticketing, customer service."),
    "swe": ("Software Engineer",
            "Build backend services in Python and Java, REST APIs, AWS, Docker, "
            "Kubernetes, SQL."),
}


def _score(resume, key):
    title, jd = JOBS[key]
    return fit_score.score(resume, {"title": title, "description": jd})


# ---------------------------------------------------------------------------
# Title plan
# ---------------------------------------------------------------------------

class TestDomainTitlePlan:
    def test_power_graduate_is_an_electrical_discipline(self):
        plan = seed.domain_title_plan(
            ["Graduate Research Assistant", "Electrical Design Trainee"],
            ["M.S. Electrical Power and Energy Systems"])
        assert plan["discipline"] == "electrical" and not plan["software"]
        assert set(plan["titles"][:2]) == {"Power Systems Engineer", "Electrical Engineer"}

    def test_every_planned_title_stays_in_the_discipline(self):
        """A planned title reaching a software / IT family would readmit that
        family to retrieval (`inventory_feed._role_families` unions them)."""
        for discipline, titles in seed._DISCIPLINE_TITLES.items():
            for title in titles:
                assert not seed._is_software_title(title), (discipline, title)
                assert fit_score.classify_role(title) not in seed._OFF_DISCIPLINE_FAMILIES

    def test_a_computer_science_degree_is_software(self):
        plan = seed.domain_title_plan(["Teaching Assistant"], ["BS Computer Science"])
        assert plan["software"] and plan["discipline"] is None

    def test_a_held_software_title_is_software(self):
        plan = seed.domain_title_plan(["Software Engineer", "Electrical Engineer"],
                                      ["BS Electrical Engineering"])
        assert plan["discipline"] is None, "an EE who writes software keeps software titles"

    def test_mechanical_and_unknown(self):
        assert seed.domain_title_plan(["Design Intern"],
                                      ["BS Mechanical Engineering"])["discipline"] == "mechanical"
        assert seed.domain_title_plan(["Registered Nurse"], ["BSN Nursing"])["discipline"] is None

    def test_off_domain_titles(self):
        plan = seed.domain_title_plan([], ["MS Electrical Engineering"])
        off = seed.off_domain_titles(
            ["Software Development Engineer", "Backend Engineer", "IT Support Technician",
             "Help Desk Analyst", "Embedded Software Engineer", "Controls Engineer",
             "Electrical Engineer", "Firmware Engineer"], plan)
        assert off == ["Software Development Engineer", "Backend Engineer",
                       "IT Support Technician", "Help Desk Analyst"]
        assert seed.off_domain_titles(["Software Engineer"],
                                      {"discipline": None}) == [], "software users untouched"


# ---------------------------------------------------------------------------
# Suggest from résumé
# ---------------------------------------------------------------------------

def _suggester():
    sys.path.insert(0, "services/jobs")
    from services import filter_suggester_service as fss
    return fss


def test_suggest_from_resume_gives_electrical_titles_without_an_llm(monkeypatch):
    fss = _suggester()

    def _no_llm(*a, **k):
        raise AssertionError("the discipline path must not call the LLM")

    monkeypatch.setattr(fss, "claude_json", _no_llm)
    out = fss.suggest_filters(_ee_structured())
    titles = out["workday_titles"]
    assert set(titles[:2]) == {"Power Systems Engineer", "Electrical Engineer"}
    assert not [t for t in titles if seed._is_software_title(t)], titles
    assert "Graduate Service Assistant" not in titles
    excluded = {t.lower() for t in out["exclude_title_terms"]}
    assert {"software engineer", "it support", "help desk"} <= excluded
    assert out["suggester_version"] == seed.TITLE_SUGGESTION_VERSION
    assert out["intent"]["primary"] == "electrical"


def test_a_software_resume_still_takes_the_software_path(monkeypatch):
    fss = _suggester()
    calls = []
    monkeypatch.setattr(fss, "_synthesize_titles_from_resume",
                        lambda r, p: calls.append(1) or {})
    fss.suggest_filters({
        "summary": "Backend engineer", "skills": {"lang": ["Python", "Go"]},
        "experience": [{"title": "Backend Engineer", "company": "X", "bullets": []}],
        "education": [{"degree": "BS Computer Science"}],
    })
    assert calls == [1]


# ---------------------------------------------------------------------------
# The title seed heals a stored software suggestion
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    d = current_db()
    for c in ("resume_assets", "users", "job_filter_preferences", "user_preferences"):
        d[c].delete_many({})
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    d.users.insert_one({"email": USER, "active_resume_id": "ra-ee"})
    d.resume_assets.insert_one({
        "resume_id": "ra-ee", "user_email": USER, "owner_type": "user",
        "filename": "ee.pdf", "parse_status": "parsed", "structured": _ee_structured(),
        "object_key": "resume-assets/ra-ee/original", "created_at": now, "parsed_at": now,
    })
    yield d
    for c in ("resume_assets", "users", "job_filter_preferences", "user_preferences"):
        d[c].delete_many({})


def test_stale_software_suggestions_are_dropped_from_the_search(db):
    from services import resume_access
    fp = seed.fingerprint_assets(resume_access.list_parsed_assets(USER))
    poisoned = ["Software Development Engineer", "Backend Engineer",
                "IT Support Technician", "Software Engineer"]
    seed.save_title_seed(USER, {
        "fingerprint": fp, "resume_titles": [], "synth_titles": poisoned,
        "synth_fingerprint": fp,
        "suggestions": {"workday_titles": poisoned + ["Electrical Engineer"]},
    })
    out = seed.public_title_seed(USER)
    assert "Electrical Engineer" in out["match_titles"]
    assert "Power Systems Engineer" in out["match_titles"]
    assert not [t for t in out["match_titles"] if seed._is_software_title(t)], out["match_titles"]
    assert set(poisoned) <= set(out["off_domain_titles"])
    assert out["discipline"] == "electrical"


def test_cached_software_bundle_is_regenerated_for_a_discipline(db):
    from services import resume_access
    fp = seed.fingerprint_assets(resume_access.list_parsed_assets(USER))
    seed.save_title_seed(USER, {
        "fingerprint": fp, "synth_fingerprint": fp, "synth_titles": [],
        "suggestions": {"workday_titles": ["Software Development Engineer"]},
    })
    calls = []
    out, cached = seed.suggest_filters_cached(
        USER, synthesizer=lambda s: calls.append(1) or {
            "workday_titles": ["Electrical Engineer"],
            "suggester_version": seed.TITLE_SUGGESTION_VERSION})
    assert calls == [1] and cached is False
    again, cached_again = seed.suggest_filters_cached(USER, synthesizer=lambda s: {})
    assert cached_again is True and again["workday_titles"] == ["Electrical Engineer"]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class TestElectricalScoring:
    def test_power_skills_are_read_off_the_resume(self):
        prof = fit_score.build_resume_profile(_ee_structured())
        skills = prof["listed_skills"] | prof["experience_skills"]
        assert {"etap", "pscad", "power systems analysis", "short circuit analysis",
                "protection and control", "autocad"} <= skills

    def test_the_board_orders_electrical_work_first(self):
        resume = _ee_structured()
        s = {k: _score(resume, k)["score"] for k in JOBS}
        assert s["ee"] > s["power"] > s["tech"], s
        assert min(s["ee"], s["power"]) > max(s["it"], s["swe"]), s
        assert s["ee"] >= 75, s

    def test_a_technician_role_is_capped_at_the_adjacent_rung(self):
        out = _score(_ee_structured(), "tech")
        assert out["role_alignment"]["tier"] == "adjacent_family"
        assert out["role_alignment"]["source"] == "role_level"

    def test_someone_who_has_been_a_technician_is_not_capped(self):
        resume = _ee_structured()
        resume["experience"].insert(0, {
            "title": "Electrical Technician", "company": "Grid Co",
            "start_date": "2020-01", "end_date": "2023-01",
            "bullets": ["Installed and tested switchgear and PLC panels."]})
        assert _score(resume, "tech")["role_alignment"].get("source") != "role_level"

    def test_tech_lead_is_not_a_technician(self):
        assert not fit_score._TECHNICIAN_TITLE.search("senior tech lead, platform")


class TestLexiconCollisions:
    """A lexicon entry fires on EVERY posting; these must stay silent."""

    @pytest.mark.parametrize("text,absent", [
        ("Frontend engineer with React, Relay and GraphQL.", "protection and control"),
        ("Barclays PLC is hiring a backend developer.", "plc programming"),
        ("Fine-tune transformer models and transformers libraries.", "transformer design"),
        ("Property & Casualty (P&C) underwriting analyst.", "protection and control"),
    ])
    def test_no_false_power_skill(self, text, absent):
        assert absent not in fit_score.find_skills(text)

    def test_version_bump_keeps_stored_profiles_valid(self):
        assert fit_score.FIT_SCORE_VERSION == "fit-v2.4"
        assert "fit-v2.3" in fit_score._COMPATIBLE_REQUIREMENT_VERSIONS
