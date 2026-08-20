"""Job feed chip vs family listing gates.

Chip mode: resume/AI titles allowlist roles (title_matched), not whole families.
Family mode: empty chips list jobs in the resume's role families only.
"""
from __future__ import annotations

from services import fit_score
from services.workday_jobs_service import (
    _score_jobs,
    resolve_feed_search_titles,
    title_matches_chips,
)


def test_systems_engineer_is_devops_not_software():
    assert fit_score.classify_role("Software Engineer") == "software_engineering"
    assert fit_score.classify_role("Systems Engineer") == "devops"


def test_title_matches_chips_token_subset():
    assert title_matches_chips("Senior Software Engineer", ["Software Engineer"])
    assert title_matches_chips("Software Engineer II", ["Software Engineer"])
    assert not title_matches_chips("Backend Engineer", ["Software Engineer"])
    assert not title_matches_chips("Systems Engineer", ["Software Engineer"])
    assert title_matches_chips("Backend Engineer", ["Software Engineer", "Backend Engineer"])


def test_score_jobs_keeps_chip_roles_not_whole_family():
    jobs = [
        {"title": "Software Engineer", "description": "Build APIs in Python."},
        {"title": "Senior Software Engineer", "description": "Lead services."},
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
    assert "Senior Software Engineer" in titles
    assert "Backend Engineer" not in titles
    assert "Systems Engineer" not in titles
    assert all(j.get("title_matched") for j in out)


def test_resolve_feed_search_titles_chip_vs_family():
    chips, mode, fams = resolve_feed_search_titles(
        ["Software Engineer"],
        skills=["python"],
        resume_years=3,
        structured={
            "experience": [{"title": "Software Engineer"}],
            "skills": {"technical": ["python"]},
        },
    )
    assert mode == "chips"
    assert chips == ["Software Engineer"]
    assert "software_engineering" in fams

    seeds, mode2, fams2 = resolve_feed_search_titles(
        [],
        skills=["python"],
        resume_years=3,
        structured={
            "experience": [{"title": "Software Engineer"}, {"title": "DevOps Engineer"}],
            "skills": {"technical": ["python"]},
        },
    )
    assert mode2 == "family"
    assert seeds  # family seed phrases for retrieval
    assert "software_engineering" in fams2 or "devops" in fams2

    empty, mode3, fams3 = resolve_feed_search_titles([], skills=[], resume_years=None)
    assert mode3 == "none"
    assert empty == []
    assert fams3 == set()


def test_score_jobs_family_mode_keeps_resume_families():
    structured = {
        "experience": [{"title": "Software Engineer"}],
        "skills": {"technical": ["python"]},
    }
    jobs = [
        {"title": "Software Engineer", "description": "Build APIs in Python."},
        {"title": "Backend Engineer", "description": "Services and databases."},
        {"title": "Registered Nurse", "description": "Patient care."},
        {"title": "Systems Engineer", "description": "Servers and networking."},
    ]
    out = _score_jobs(
        jobs,
        titles=[],
        skills=["python"],
        skill_terms=[],
        resume_years=2,
        structured=structured,
    )
    titles = {j["title"] for j in out}
    assert "Software Engineer" in titles
    assert "Backend Engineer" in titles  # same family, chips cleared
    assert "Registered Nurse" not in titles
    assert "Systems Engineer" not in titles  # devops, not resume family here
