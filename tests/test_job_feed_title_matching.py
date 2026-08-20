"""Job feed family gate: resume titles → role families → whole-family results.

Resume / chip titles classify into role families. The feed may show the whole
family (e.g. Software Engineer also surfaces Backend / Full Stack). Off-family
titles that keyword recall still returns (e.g. bare Systems Engineer under
devops while chips are software_engineering) are dropped.
"""
from __future__ import annotations

from services import fit_score
from services.workday_jobs_service import _allowed_families_for_titles, _score_jobs


def test_systems_engineer_is_devops_not_software():
    assert fit_score.classify_role("Software Engineer") == "software_engineering"
    assert fit_score.classify_role("Backend Engineer") == "software_engineering"
    assert fit_score.classify_role("Systems Engineer") == "devops"
    assert fit_score.classify_role("Staff Systems Engineer, Messaging Technologies") == "devops"
    assert fit_score.classify_role("Software Systems Engineer") == "software_engineering"


def test_software_chip_allowed_families_exclude_devops():
    allowed = _allowed_families_for_titles(["Software Engineer"], profile=None)
    assert allowed == {"software_engineering"}
    assert "devops" not in allowed


def test_score_jobs_keeps_software_family_drops_systems_engineer():
    jobs = [
        {"title": "Software Engineer", "description": "Build APIs in Python."},
        {"title": "Backend Engineer", "description": "Services and databases."},
        {"title": "Systems Engineer", "description": "Servers and networking."},
    ]
    out = _score_jobs(
        jobs,
        titles=["Software Engineer"],
        skills=["python", "apis"],
        skill_terms=[],
        resume_years=2,
    )
    titles = {j["title"] for j in out}
    assert "Software Engineer" in titles
    assert "Backend Engineer" in titles
    assert "Systems Engineer" not in titles
