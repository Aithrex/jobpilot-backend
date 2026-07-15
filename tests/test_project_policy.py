"""Experience-first project policy — total_experience_years math + the
generation gate (early-career: free generation; experienced: confirmed
GitHub evidence only; experienced without evidence: none).
"""
import pytest

from services.ats_vendor_checks import parse_start_date, total_experience_years
from services.project_generator import (
    PROJECT_GEN_MAX_YEARS,
    project_generation_policy,
)


def _resume(date_ranges):
    return {
        "experience": [
            {"title": "Engineer", "company": f"C{i}", "dates": d, "bullets": []}
            for i, d in enumerate(date_ranges)
        ]
    }


# ---------------------------------------------------------------------------
# Date math
# ---------------------------------------------------------------------------

def test_parse_start_date():
    assert parse_start_date("March 2022 - Present") == (2022, 3)
    assert parse_start_date("06/2019 - 02/2021") == (2019, 6)
    assert parse_start_date("2018 - 2020") == (2018, 1)
    assert parse_start_date("") is None


def test_years_single_closed_role():
    years = total_experience_years(_resume(["January 2020 - December 2021"]))
    assert 1.9 <= years <= 2.1


def test_years_overlapping_roles_merge():
    # Two fully-overlapping years must count once, not twice.
    years = total_experience_years(_resume([
        "January 2020 - December 2021",
        "June 2020 - June 2021",
    ]))
    assert 1.9 <= years <= 2.1


def test_years_gap_between_roles_not_counted():
    years = total_experience_years(_resume([
        "January 2016 - December 2016",
        "January 2020 - December 2020",
    ]))
    assert 1.8 <= years <= 2.2


def test_years_present_counts_to_now():
    # An ongoing role started ~4 years before today's date (2026) must
    # measure ≥ 3.5 years even though the end is "Present".
    years = total_experience_years(_resume(["March 2022 - Present"]))
    assert years >= 3.5


def test_years_zero_when_unparseable():
    assert total_experience_years(_resume(["n/a"])) == 0.0
    assert total_experience_years({"experience": []}) == 0.0


# ---------------------------------------------------------------------------
# Policy gate
# ---------------------------------------------------------------------------

JUNIOR = _resume(["June 2024 - Present"])          # ~1-2 years
EXPERIENCED = _resume(["January 2019 - Present"])  # ~7 years


def test_junior_gets_free_generation():
    policy = project_generation_policy(JUNIOR, github_evidence=None)
    assert policy["allow"] is True
    assert policy["evidence_only"] is False
    assert policy["years"] <= PROJECT_GEN_MAX_YEARS


def test_experienced_without_evidence_gets_none():
    policy = project_generation_policy(EXPERIENCED, github_evidence=None)
    assert policy["allow"] is False
    assert "no confirmed GitHub evidence" in policy["reason"]


def test_experienced_with_evidence_is_evidence_only():
    policy = project_generation_policy(
        EXPERIENCED, github_evidence=['repo "fraud-sim": Python, 12 stars']
    )
    assert policy["allow"] is True
    assert policy["evidence_only"] is True


def test_evidence_only_generation_requires_evidence(monkeypatch):
    """evidence_only + no evidence → no LLM call, empty result."""
    from services.project_generator import ProjectGenerator

    def _boom(*a, **kw):
        raise AssertionError("LLM must not be called")

    import services.claude_client as cc
    monkeypatch.setattr(cc, "claude_json", _boom)
    out = ProjectGenerator().generate_batch(
        3, EXPERIENCED, {}, evidence_only=True, github_evidence=None
    )
    assert out == []


def test_evidence_only_caps_count(monkeypatch):
    """evidence_only caps generation at the number of confirmed repos."""
    from services.project_generator import ProjectGenerator

    captured = {}

    def _fake(prompt=None, **kw):
        captured["prompt"] = prompt
        return {"projects": []}

    import services.claude_client as cc
    monkeypatch.setattr(cc, "claude_json", _fake)
    ProjectGenerator().generate_batch(
        3, EXPERIENCED, {},
        github_evidence=['repo "fraud-sim": Python'],
        evidence_only=True,
    )
    assert "generating 1 portfolio project" in captured["prompt"]
    assert "MUST correspond to one of these repositories" in captured["prompt"]


def test_augmenter_phase1_respects_policy_gate(monkeypatch):
    """allow_project_generation=False skips Phase 1 without touching the LLM."""
    from services.content_augmenter import ContentAugmenter

    augmenter = ContentAugmenter(renderer=object(), project_generator=object())

    def _no_projects(*a, **kw):
        raise AssertionError("project generation must not run under the gate")

    monkeypatch.setattr(augmenter, "_augment_projects", _no_projects)
    # Full fill → the pipeline exits right after the (skipped) Phase 1 path.
    monkeypatch.setattr(augmenter, "_measure_fill", lambda t: 1.0)
    monkeypatch.setattr(augmenter, "_ats_harden", lambda t, jd: t)
    monkeypatch.setattr(augmenter, "_finalize_safe_substitutions", lambda t: t)
    monkeypatch.setattr(augmenter, "_overflow_trim", lambda t, f: t)

    tailored = {
        "skills": {"Languages": ["Python"]},
        "experience": [{"title": "Engineer", "company": "Acme",
                        "dates": "January 2019 - Present",
                        "bullets": ["Did real work"]}],
        "projects": [],
    }
    out = augmenter.augment(
        tailored, dict(tailored), {}, allow_project_generation=False
    )
    assert out.get("projects") == []
