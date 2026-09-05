"""Stage 1–2 unit tests: expression-driven plan, dates, Claude reader stub."""
from __future__ import annotations

from datetime import date

import pytest

from services.assessment_dates import (
    DateInterval,
    merge_intervals,
    meets_duration_months,
    tool_scoped_intervals,
    total_months,
)
from services.tailoring_planner import build_plan
from services.requirement_assessment import attach_expressions
from services.assessment_reader import reader_pass_v2
from schemas.assessment_contracts import RequirementEvaluation


def test_overlapping_intervals_not_double_counted():
    a = DateInterval(date(2020, 1, 1), date(2022, 6, 1), entity="experience.0")
    b = DateInterval(date(2021, 1, 1), date(2023, 1, 1), entity="experience.1")
    merged = merge_intervals([a, b])
    assert len(merged) == 1
    assert total_months(merged) < total_months([a]) + total_months([b])


def test_tenure_alone_does_not_credit_tool():
    roles = [{
        "company": "Acme", "title": "Eng", "dates": "2018 - 2024",
        "bullets": ["Led the billing team."],
    }]
    intervals = tool_scoped_intervals(roles=roles, capability="Terraform")
    assert intervals == []


def test_tool_scoped_interval_when_evidenced():
    roles = [{
        "company": "Acme", "title": "Eng", "dates": "2022 - 2024",
        "bullets": ["Operated Terraform modules for VPC networking."],
    }]
    intervals = tool_scoped_intervals(roles=roles, capability="Terraform")
    assert intervals
    met, total, uncertain = meets_duration_months(intervals, 12, allow_uncertain=False)
    assert uncertain is True
    assert met is False


def test_expression_driven_and_or_plan():
    source = {
        "contact": {}, "summary": "",
        "skills": {"S": ["Java"]},
        "experience": [{
            "company": "A", "title": "E",
            "bullets": ["Built services in Python.", "Wrote SQL against the warehouse."],
        }],
        "education": [], "projects": [], "certifications": [],
    }
    jd = attach_expressions({
        "requirements": [{
            "text": "Python or Java, plus SQL",
            "priority": "required",
            "kind": "skill",
            "alternatives": ["Python", "Java"],
            "source_excerpt": "Python or Java, plus SQL",
        }],
        "analysis_completeness": "complete",
    })
    plan = build_plan(source, jd)
    req = plan.requirements[0]
    assert req.state == "demonstrated"
    assert req.fact_ids


def test_reader_pass_unavailable_without_text():
    out = reader_pass_v2("", {"job_title": "X"}, [], [], run_claude=False)
    assert out["status"] in {"unavailable", "partial"}


def test_reader_claude_failure_is_partial(monkeypatch):
    class FakeMod:
        CLAUDE_PRO = "x"

        @staticmethod
        def claude_json(**kwargs):
            raise RuntimeError("offline")

    monkeypatch.setitem(__import__("sys").modules, "services.claude_client", FakeMod())
    out = reader_pass_v2(
        "Built Python APIs.",
        {"job_title": "Eng", "required_skills": ["Python"]},
        [RequirementEvaluation(
            requirement_id="r1", document_state="not_demonstrated",
            source_state="not_evidenced", claim_state="unverified", match_method="none",
        )],
        [{"id": "r1", "display_text": "Rust"}],
        run_claude=True,
    )
    assert out["status"] in {"partial", "unavailable", "completed"}
    assert any(f.get("kind") == "absence" for f in out.get("findings") or [])
