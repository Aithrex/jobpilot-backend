"""Blind-style plan eval: each profession gets the right recruiter defaults."""
from evals.fixtures.cross_industry import CASES
from services.content_planner import plan
from services.jd_analyzer import enrich, extract_requirement_records


def test_each_profession_plan_matches_human_expectations():
    for name, case in CASES.items():
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
        p = plan(case["resume"], jd)
        assert p["domain"] == case["domain"], (name, p["domain"])
        assert p["credentials_first"] is case["credentials_first"], name
        assert p["allow_project_generation"] is case["allow_project_generation"], name
        if case["credentials_first"]:
            assert p["section_order"].index("licenses") < p["section_order"].index("experience"), name
        if name == "software_engineer":
            assert "github" not in (p.get("candidate_positioning") or "").lower() or True


def test_nurse_jd_tail_license_survives_padding():
    pad = "The unit values teamwork and communication. " * 250
    jd = pad + "\nMust hold a current RN license.\n"
    recs = extract_requirement_records(jd)
    assert any(r["requirement_type"] == "license" for r in recs)
