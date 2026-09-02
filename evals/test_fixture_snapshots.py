"""Increment 0 / 10: golden fixture harness + blinded recruiter defaults."""
from evals.fixtures.cross_industry import CASES
from services.content_planner import plan
from services.jd_analyzer import enrich, extract_requirement_records


def _plan_for(name: str, case: dict):
    jd = enrich(
        {
            "job_title": name.replace("_", " "),
            "industry": case["domain"],
            "required_skills": [],
            "preferred_skills": [],
            "keywords": [],
        },
        case["jd_text"],
        resume=case["resume"],
    )
    return plan(case["resume"], jd), jd


def test_ten_profession_snapshot_harness():
    assert len(CASES) == 10
    for name, case in CASES.items():
        p, jd = _plan_for(name, case)
        snapshot = {
            "domain": p["domain"],
            "credentials_first": p["credentials_first"],
            "allow_project_generation": p["allow_project_generation"],
            "force_metrics": p["force_metrics"],
            "has_requirements": bool(jd.get("requirements")),
        }
        assert snapshot["domain"] == case["domain"], (name, snapshot)
        assert snapshot["credentials_first"] is case["credentials_first"], name
        assert snapshot["allow_project_generation"] is case["allow_project_generation"], name
        if case["credentials_first"]:
            assert snapshot["force_metrics"] is False, name
            assert "licenses" in p["section_order"]
            assert p["section_order"].index("licenses") < p["section_order"].index("experience")


def test_blinded_licensed_roles_never_get_software_defaults():
    """Blinded: no profession label in the assertion — only the plan output."""
    for name, case in CASES.items():
        p, _ = _plan_for(name, case)
        if not case["credentials_first"]:
            continue
        persona = p["recruiter_persona"].lower()
        assert "technical recruiter who screens software" not in persona
        assert p["allow_project_generation"] is False
        assert case["resume"].get("licenses") or case["resume"].get("certifications") or extract_requirement_records(case["jd_text"])
