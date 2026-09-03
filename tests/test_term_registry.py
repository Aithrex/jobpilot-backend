"""Term registry — classify, expand, match. No LLM, no Mongo."""
from services.term_registry import (
    classify_term,
    evidence_weight,
    expand_parenthetical,
    match_forms,
    sanitize_jd_analysis,
    skill_term_ceiling,
    text_contains,
)
from services.keyword_gap_engine import KeywordGapEngine
from services.resume_scorer import ResumeScorer


def test_classify_parks_junk():
    assert classify_term("remote") == "logistics"
    assert classify_term("entry level") == "seniority"
    assert classify_term("software engineer") == "title"
    assert classify_term("fintech") == "domain"
    assert classify_term("financial services") == "domain"
    assert classify_term("Python") == "skill"
    assert classify_term("Acme", company="Acme") == "title"


def test_sanitize_jd_keeps_skills_parks_rest():
    jd = sanitize_jd_analysis({
        "company": "Stripe",
        "industry": "Fintech",
        "required_skills": [
            "Python", "remote", "fintech", "entry level",
            "software engineer", "Stripe",
        ],
        "preferred_skills": ["hybrid", "PostgreSQL"],
        "keywords": ["payments", "Django"],
    })
    req = {s.lower() for s in jd["required_skills"]}
    assert "python" in req
    assert "remote" not in req
    assert "fintech" not in req
    assert "entry level" not in req
    assert "software engineer" not in req
    assert "stripe" not in req
    ctx = jd["jd_context"]
    assert any(x.lower() == "remote" for x in ctx["logistics"])
    assert any(x.lower() == "fintech" for x in ctx["domain"])
    pref = {s.lower() for s in jd["preferred_skills"]}
    assert "postgresql" in pref
    assert "hybrid" not in pref


def test_parenthetical_expansion():
    parts = expand_parenthetical("cloud platforms (aws, azure, or gcp)")
    lowered = [p.lower() for p in parts]
    assert "cloud platforms" in lowered
    assert "aws" in lowered
    assert "gcp" in lowered


def test_parenthetical_satisfied_by_member():
    hay = "built services on aws lambda and rds"
    assert text_contains(hay, "cloud platforms (aws, azure, or gcp)")


def test_bidirectional_long_form_aliases():
    hay = "deployed to gcp and used rag over company docs plus an llm"
    assert text_contains(hay, "google cloud platform")
    assert text_contains(hay, "retrieval-augmented generation (rag)")
    assert text_contains(hay, "large language model")
    forms = {f.lower() for f in match_forms("google cloud platform")}
    assert "gcp" in forms


def test_umbrella_relational_databases():
    hay = "tuned postgresql queries for the billing service"
    assert text_contains(hay, "relational databases")


def test_short_token_never_substring():
    assert not text_contains("django rest framework", "go")
    assert not text_contains("spring boot", "r")
    assert text_contains("wrote go services", "go")
    assert text_contains("wrote golang services", "go")
    assert text_contains("used r for analysis", "r")


def test_gap_engine_inherits_sanitize_and_aliases():
    resume = {
        "summary": "Backend engineer.",
        "skills": {"Cloud": ["AWS"]},
        "experience": [{
            "title": "Engineer", "company": "X", "dates": "2022 - Present",
            "bullets": ["Shipped APIs on AWS Lambda"],
        }],
        "projects": [], "education": [], "certifications": [],
    }
    jd = {
        "required_skills": [
            "cloud platforms (aws, azure, or gcp)",
            "remote",
            "fintech",
        ],
        "preferred_skills": [],
        "keywords": [],
    }
    gap = KeywordGapEngine().analyze(resume, jd)
    missing = {m.lower() for m in gap["missing_keywords"]}
    matched = {m.lower() for m in gap["matched_keywords"]}
    assert "remote" not in missing
    assert "fintech" not in missing
    assert any("aws" in m or "cloud" in m for m in matched)


def test_evidence_weight_zero_for_skills_list_only():
    structured = {
        "summary": "Experienced professional.",
        "skills": {"Skills": ["GraphQL"]},
        "experience": [{
            "title": "Engineer", "company": "X", "dates": "2022 - Present",
            "bullets": ["Worked on internal tooling"],
        }],
        "projects": [], "education": [], "certifications": [],
    }
    assert evidence_weight("GraphQL", structured) == 0.0
    evidenced = dict(structured)
    evidenced["experience"] = [{
        "title": "Engineer", "company": "X", "dates": "2022 - Present",
        "bullets": ["Designed the GraphQL schema for checkout"],
    }]
    assert evidence_weight("GraphQL", evidenced) == 1.0


def test_scored_text_excludes_invisible_project_tech():
    from schemas.resume_schemas import build_resume_text
    text = build_resume_text({
        "summary": "Hello",
        "skills": {},
        "experience": [],
        "projects": [{
            "name": "P",
            "tech": "InvisibleTech",
            "bullets": ["Used VisibleTech"],
        }],
        "education": [],
        "certifications": ["AWS Certified Developer"],
    }).lower()
    assert "invisibletech" not in text
    assert "visibletech" in text
    assert "aws certified developer" in text


def test_skill_term_ceiling_scales_with_years():
    assert skill_term_ceiling(2) == 27
    assert skill_term_ceiling(5) == 45
    assert skill_term_ceiling(10) == 45
    assert skill_term_ceiling(0, has_roles=False) == 15
    assert skill_term_ceiling(0, has_roles=True) == 45


def test_empty_jd_hard_skill_is_full_credit():
    """Empty JD keyword lists are not a candidate failure — there is nothing
    to match. The honest v2 scorer awards full hard-skill credit and notes
    why, rather than marking the dimension unscored/zero."""
    score, found, missing, detail = ResumeScorer()._hard_skill_score(
        {"summary": "x", "skills": {}, "experience": [], "projects": [],
         "education": [], "certifications": []},
        {"required_skills": [], "preferred_skills": [], "keywords": []},
    )
    assert score == 100
    assert found == missing == []
    assert "unscored" not in detail
    assert "no keywords" in detail.get("note", "").lower()
