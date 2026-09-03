"""Hybrid ATS scorer — offline tests.

The LLM scanner pass is faked (or forced to fail) in every test; the pdf
parseability check is patched where a clean parse is needed. No Mongo.
"""
import pytest

from services.resume_scorer import ResumeScorer


RESUME = {
    "contact": {
        "name": "Jane Doe", "email": "jane@example.com", "phone": "555-0100",
        "linkedin": "", "github": "", "location": "", "portfolio": "",
    },
    "summary": (
        "Software Engineer specializing in Python and Amazon Web Services (AWS). "
        "Built cloud microservices serving 40,000 daily users."
    ),
    "skills": {
        "Languages": ["Python", "TypeScript"],
        "Cloud": ["AWS", "Docker", "PostgreSQL"],
    },
    "experience": [
        {
            "title": "Software Engineer", "company": "Acme Corp", "location": "NYC",
            "dates": "March 2022 - Present", "type": "Full-time",
            "bullets": [
                "Built Python microservices on AWS Lambda serving 40,000 users daily",
                "Reduced API latency from 420 to 95 ms by tuning PostgreSQL queries",
                "Migrated CI pipeline to Docker, cutting build times by 35%",
            ],
        },
        {
            "title": "Junior Developer", "company": "Beta LLC", "location": "NYC",
            "dates": "June 2020 - February 2022", "type": "Full-time",
            "bullets": [
                "Developed React dashboards for internal analytics used by 12 teams",
            ],
        },
    ],
    "education": [{
        "degree": "BS Computer Science", "institution": "State University",
        "location": "", "dates": "May 2020", "gpa": "", "coursework": "",
    }],
    "projects": [],
    "certifications": [],
}

JD = {
    "job_title": "Senior Software Engineer",
    "company": "Target Inc", "location": "Remote", "employment_type": "Full-time",
    "required_skills": ["Python", "AWS", "Docker"],
    "preferred_skills": ["PostgreSQL"],
    "responsibilities": [], "qualifications": [],
    "experience_years": "3-5 years", "industry": "SaaS",
    "keywords": ["microservices"],
}

FAKE_SCANNERS = {
    "workday": 88, "greenhouse": 90, "lever": 86,
    "icims": 87, "taleo": 85, "smartrecruiters": 89,
}

FAKE_REVIEW = {
    "experience_relevance": 82,
    "suggestions": ["Lead with the AWS Lambda bullet"],
    "strengths": ["Directly relevant stack"],
    "scanners": dict(FAKE_SCANNERS),
    "ai_screener": {
        "overall": 84, "relevance": 86, "seniority_fit": 80, "culture_fit": 82,
    },
}


@pytest.fixture()
def fake_review(monkeypatch):
    import services.claude_client as cc
    monkeypatch.setattr(cc, "claude_json", lambda *a, **kw: dict(FAKE_REVIEW))


@pytest.fixture()
def broken_review(monkeypatch):
    import services.claude_client as cc

    def _boom(*a, **kw):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(cc, "claude_json", _boom)


def patch_pdf_check(monkeypatch, score=100, **check_overrides):
    checks = {
        "extraction_ok": True, "contact_name_found": True,
        "contact_email_found": True, "contact_phone_found": True,
        "contact_at_top": True, "sections_found": [], "sections_missing": [],
        "sections_in_order": True, "skills_keywords_found": 5,
        "skills_keywords_total": 5, "experience_dates_parseable": 2,
        "experience_dates_total": 2, "no_glyph_corruption": True,
        "word_join_issues": [], "page_count": 1, "single_page": True,
    }
    checks.update(check_overrides)
    import services.pdf_ats_check as pac
    monkeypatch.setattr(
        pac, "check_pdf_parseability",
        lambda pdf_bytes, tailored, jd=None, outcome=None, **_kw: {
            "pdf_parseable_score": score, "checks": checks, "warnings": [],
        },
    )


def test_hybrid_shape_emits_scanners(monkeypatch, fake_review):
    patch_pdf_check(monkeypatch)
    result = ResumeScorer().score(RESUME, JD, pdf_bytes=b"%PDF-fake")

    assert "schema_version" not in result
    assert "hard_skill_match" not in result
    for dim in (
        "keyword_match", "keyword_frequency", "skills_alignment",
        "experience_relevance", "quantifiable_impact", "bullet_quality",
        "format_score", "section_completeness",
    ):
        assert isinstance(result[dim], int), dim
    assert set(result["scanners"]) == {
        "workday", "greenhouse", "lever", "icims", "taleo", "smartrecruiters"
    }
    assert result["ai_screener"]["overall"] == 84
    assert result["experience_relevance"] == 82
    assert result["pdf_parseable_score"] == 100


def test_overall_uses_hybrid_weights(monkeypatch, fake_review):
    patch_pdf_check(monkeypatch, score=100)
    r = ResumeScorer().score(RESUME, JD, pdf_bytes=b"%PDF-fake")
    expected = round(
        r["keyword_match"] * 0.25
        + r["keyword_frequency"] * 0.05
        + r["skills_alignment"] * 0.20
        + r["experience_relevance"] * 0.15
        + r["ai_screener"]["overall"] * 0.10
        + r["quantifiable_impact"] * 0.10
        + r["bullet_quality"] * 0.05
        + r["format_score"] * 0.05
        + r["section_completeness"] * 0.05
    )
    assert r["overall"] == min(100, max(0, expected))


def test_imperfect_parse_caps_overall(monkeypatch, fake_review):
    patch_pdf_check(monkeypatch, score=60)
    r = ResumeScorer().score(RESUME, JD, pdf_bytes=b"%PDF-fake")
    assert r["pdf_parseable_score"] == 60
    assert r["overall"] <= 65


def test_empty_jd_keyword_match_is_complete(monkeypatch, fake_review):
    patch_pdf_check(monkeypatch)
    empty = dict(JD)
    empty["required_skills"] = []
    empty["preferred_skills"] = []
    empty["keywords"] = []
    r = ResumeScorer().score(RESUME, empty, pdf_bytes=b"%PDF-fake")
    assert r["keyword_match"] == 100


def test_ai_failure_still_scores(monkeypatch, broken_review):
    patch_pdf_check(monkeypatch)
    r = ResumeScorer().score(RESUME, JD, pdf_bytes=b"%PDF-fake")
    assert isinstance(r["overall"], int)
    assert r["experience_relevance"] == 70
    assert r["keyword_match"] > 0


def test_jd_keywords_in_resume_are_found():
    text = (
        "Software Engineer specializing in Python and Amazon Web Services. "
        "Built microservices on AWS with Docker."
    )
    score, found, missing = ResumeScorer()._keyword_match_score(
        text, ["Python", "AWS", "Docker", "microservices"],
    )
    assert score == 100
    assert not missing
    assert {f.lower() for f in found} >= {"python", "aws", "docker", "microservices"}
