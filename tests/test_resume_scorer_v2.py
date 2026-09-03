"""ResumeScorer v2 — offline tests for the honest deterministic rubric.

The LLM review is faked (or forced to fail) in every test; the pdf
parseability check is patched where a clean parse is needed. No Mongo.
"""
import pytest

from services.resume_scorer import ResumeScorer
from utils.keyword_normalizer import get_acronym_pair


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

FAKE_REVIEW = {
    "experience_relevance": 82,
    "suggestions": ["Lead with the AWS Lambda bullet"],
    "strengths": ["Directly relevant stack"],
}


@pytest.fixture()
def fake_review(monkeypatch):
    """Patch the FLASH review with a fixed, deterministic response."""
    import services.claude_client as cc
    monkeypatch.setattr(cc, "claude_json", lambda *a, **kw: dict(FAKE_REVIEW))


@pytest.fixture()
def broken_review(monkeypatch):
    import services.claude_client as cc

    def _boom(*a, **kw):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(cc, "claude_json", _boom)


def patch_pdf_check(monkeypatch, score=100, **check_overrides):
    """Force a specific parseability result (scorer resolves the symbol at
    call time from services.pdf_ats_check)."""
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


# ---------------------------------------------------------------------------
# Overall shape + weights
# ---------------------------------------------------------------------------

def test_v2_shape_and_removed_fields(monkeypatch, fake_review):
    patch_pdf_check(monkeypatch)
    result = ResumeScorer().score(RESUME, JD, pdf_bytes=b"%PDF-fake")

    assert result["schema_version"] == 2
    for dim in ("hard_skill_match", "title_alignment", "quantified_impact",
                "ats_parseability", "section_completeness",
                "recency_relevance", "readability"):
        assert isinstance(result[dim], int), dim
    assert "scanners" not in result
    assert "ai_screener" not in result
    assert set(result["vendor_compat"]) == {
        "workday", "greenhouse", "lever", "icims", "taleo", "smartrecruiters"
    }
    assert result["render_error"] == ""
    assert result["ai_review_available"] is True
    assert result["experience_relevance"] == 82


def test_overall_is_exact_weighted_sum(monkeypatch, fake_review):
    patch_pdf_check(monkeypatch, score=100)
    r = ResumeScorer().score(RESUME, JD, pdf_bytes=b"%PDF-fake")
    expected = round(
        r["hard_skill_match"] * 0.30 + r["title_alignment"] * 0.15
        + r["quantified_impact"] * 0.15 + r["ats_parseability"] * 0.15
        + r["section_completeness"] * 0.10 + r["recency_relevance"] * 0.08
        + r["readability"] * 0.07
    )
    assert r["overall"] == min(100, max(0, expected))


def test_legacy_aliases_mirror_new_dimensions(monkeypatch, fake_review):
    patch_pdf_check(monkeypatch)
    r = ResumeScorer().score(RESUME, JD, pdf_bytes=b"%PDF-fake")
    assert r["keyword_match"] == r["hard_skill_match"]
    assert r["keyword_frequency"] == r["hard_skill_match"]
    assert r["skills_alignment"] == r["hard_skill_match"]
    assert r["quantifiable_impact"] == r["quantified_impact"]
    assert r["bullet_quality"] == r["readability"]
    assert r["format_score"] == r["ats_parseability"]


def test_imperfect_parse_caps_overall(monkeypatch, fake_review):
    patch_pdf_check(monkeypatch, score=60)
    r = ResumeScorer().score(RESUME, JD, pdf_bytes=b"%PDF-fake")
    assert r["ats_parseability"] == 60
    assert r["overall"] <= 65  # parseable + 5


# ---------------------------------------------------------------------------
# Loud render failure
# ---------------------------------------------------------------------------

def test_empty_jd_scores_hard_skill_as_complete(monkeypatch, fake_review):
    patch_pdf_check(monkeypatch)
    empty = dict(JD)
    empty["required_skills"] = []
    empty["preferred_skills"] = []
    empty["keywords"] = []
    r = ResumeScorer().score(RESUME, empty, pdf_bytes=b"%PDF-fake")
    assert r["hard_skill_match"] == 100


def test_missing_pdf_is_loud(fake_review):
    r = ResumeScorer().score(RESUME, JD, pdf_bytes=None, render_error="boom")
    assert r["ats_parseability"] == 0
    assert r["overall"] <= 40
    assert r["render_error"] == "boom"
    assert r["pdf_parseable_score"] == 0
    assert all(v["status"] == "fail" for v in r["vendor_compat"].values())
    assert "failed to render" in r["suggestions"][0].lower()


# ---------------------------------------------------------------------------
# AI review failure → deterministic proxy
# ---------------------------------------------------------------------------

def test_ai_review_failure_uses_proxy(monkeypatch, broken_review):
    patch_pdf_check(monkeypatch)
    r = ResumeScorer().score(RESUME, JD, pdf_bytes=b"%PDF-fake")
    assert r["ai_review_available"] is False
    expected_proxy = round(0.5 * (r["title_alignment"] + r["hard_skill_match"]))
    assert r["experience_relevance"] == expected_proxy
    assert r["suggestions"]
    assert r["strengths"]
    # Overall unaffected by the LLM being down — still the weighted sum.
    expected = round(
        r["hard_skill_match"] * 0.30 + r["title_alignment"] * 0.15
        + r["quantified_impact"] * 0.15 + r["ats_parseability"] * 0.15
        + r["section_completeness"] * 0.10 + r["recency_relevance"] * 0.08
        + r["readability"] * 0.07
    )
    assert r["overall"] == min(100, max(0, expected))


# ---------------------------------------------------------------------------
# Title alignment banding
# ---------------------------------------------------------------------------

def _title_score(summary, titles, jd_title="Senior Software Engineer"):
    tailored = {
        "summary": summary,
        "experience": [{"title": t, "bullets": []} for t in titles],
    }
    score, _ = ResumeScorer()._title_alignment_score(
        tailored, {"job_title": jd_title}
    )
    return score


def test_title_full_mirror_scores_100():
    assert _title_score(
        "Software Engineer with cloud experience.", ["Software Engineer II"]
    ) == 100


def test_title_seniority_never_required():
    # "Senior" in the JD is stripped — honest non-senior titles score full.
    with_senior = _title_score("Software Engineer.", ["Senior Software Engineer"])
    without = _title_score("Software Engineer.", ["Software Engineer"])
    assert with_senior == without == 100


def test_title_summary_only_scores_88():
    assert _title_score(
        "Software Engineer focused on ML.", ["Data Analyst"]
    ) == 88


def test_title_recent_role_only_scores_80():
    assert _title_score(
        "Builder of reliable systems.", ["Software Engineer"]
    ) == 80


def test_title_developer_equals_engineer():
    assert _title_score(
        "Full Stack Developer shipping web apps.", ["Full Stack Developer"],
        jd_title="Full Stack Engineer",
    ) == 100


def test_title_no_match_floors_at_10():
    assert _title_score("Marketing specialist.", ["Account Manager"]) == 10


def test_title_missing_jd_title_scores_100():
    assert _title_score("Anything.", ["Anything"], jd_title="") == 100


# ---------------------------------------------------------------------------
# Hard skill match: placement cap, stuffing, acronym pairs
# ---------------------------------------------------------------------------

def test_skills_list_only_keywords_capped():
    """All JD keywords present ONLY in the skills list → dimension ≤ 80."""
    stuffed = {
        "summary": "Experienced professional.",
        "skills": {"Skills": ["Python", "AWS", "Docker", "PostgreSQL", "Microservices"]},
        "experience": [{
            "title": "Engineer", "company": "X", "dates": "March 2022 - Present",
            "bullets": ["Worked on internal tooling and documentation"],
        }],
        "education": [], "projects": [], "certifications": [],
    }
    score, found, missing, detail = ResumeScorer()._hard_skill_score(stuffed, JD)
    assert not missing
    assert detail["context_fraction"] < 50
    assert score <= 80


def test_keywords_in_bullets_beat_skills_list():
    in_context, *_ = ResumeScorer()._hard_skill_score(RESUME, JD)
    stuffed = dict(RESUME)
    stuffed["summary"] = "Experienced professional."
    stuffed["experience"] = [{
        "title": "Software Engineer", "company": "Acme Corp",
        "dates": "March 2022 - Present",
        "bullets": ["Responsible for various engineering tasks"],
    }]
    listed_only, *_ = ResumeScorer()._hard_skill_score(stuffed, JD)
    assert in_context > listed_only


def test_stuffed_repetition_is_penalized():
    hammered = dict(RESUME)
    hammered["experience"] = [{
        "title": "Software Engineer", "company": "Acme Corp",
        "dates": "March 2022 - Present",
        "bullets": [
            "Python Python work using Python for Python services in Python",
        ],
    }]
    _, _, _, detail = ResumeScorer()._hard_skill_score(hammered, JD)
    assert detail["stuffed_keywords"] >= 1


def test_acronym_pair_helper_and_bonus():
    pair = get_acronym_pair("AWS")
    assert pair is not None
    short, long = pair
    assert short == "aws"
    assert "amazon" in long
    assert get_acronym_pair("some-nonexistent-tool-xyz") is None

    _, _, _, detail = ResumeScorer()._hard_skill_score(RESUME, JD)
    assert detail["acronym_pairs_satisfied"] >= 1  # "Amazon Web Services (AWS)"


# ---------------------------------------------------------------------------
# Recency + readability
# ---------------------------------------------------------------------------

def test_recency_rewards_recent_relevance():
    score = ResumeScorer()._recency_relevance_score(RESUME, JD)
    assert score >= 80  # ordered + JD terms + strong first bullet


def test_recency_zero_without_experience():
    assert ResumeScorer()._recency_relevance_score(
        {"experience": []}, JD
    ) == 0


def test_readability_penalizes_weak_openers():
    strong = ResumeScorer()._readability_score(RESUME)
    weak = dict(RESUME)
    weak["experience"] = [{
        "title": "Software Engineer", "company": "Acme",
        "dates": "March 2022 - Present",
        "bullets": [
            "responsible for maintaining internal services and pipelines here",
            "helped with the migration of CI systems to Docker infrastructure",
            "worked on various engineering tasks assigned during each sprint",
        ],
    }]
    assert ResumeScorer()._readability_score(weak) < strong
