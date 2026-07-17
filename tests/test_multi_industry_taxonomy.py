"""Multi-industry keyword taxonomies (Craft-overhaul Phase 3A — de-SWE).

Non-tech JD phrasings must alias-match the candidate's canonical skills exactly
as tech ones do, so the ATS score/gap report is reliable for finance, healthcare,
marketing, and sales — not just SWE.
"""
import pytest

from utils.keyword_normalizer import normalize_single, get_all_forms
from services.keyword_gap_engine import KeywordGapEngine


@pytest.mark.parametrize("alias,canonical", [
    # finance
    ("Financial Planning and Analysis", "FP&A"),
    ("budget variance", "Variance Analysis"),
    ("US GAAP", "GAAP"),
    ("accounts payable", "Accounts Payable"),
    ("monthly close", "Month-End Close"),
    # healthcare
    ("advanced cardiac life support", "ACLS"),
    ("electronic medical records", "Electronic Health Records"),
    ("nursing assessment", "Patient Assessment"),
    ("epic ehr", "Epic"),
    # marketing
    ("search engine optimization", "SEO"),
    ("google adwords", "Google Ads"),
    ("split testing", "A/B Testing"),
    ("conversion optimization", "Conversion Rate Optimization"),
    # sales
    ("sfdc", "Salesforce"),
    ("customer relationship management", "CRM"),
    ("sales pipeline", "Pipeline Management"),
])
def test_alias_normalizes_to_canonical(alias, canonical):
    assert normalize_single(alias) == canonical


@pytest.mark.parametrize("term", ["FP&A", "GAAP", "ACLS", "Epic", "SEO",
                                  "Salesforce", "Patient Assessment"])
def test_get_all_forms_includes_aliases(term):
    forms = get_all_forms(term)
    assert len(forms) >= 2, f"{term} should have aliases, got {forms}"


def _resume(skills, bullet):
    return {"skills": skills, "summary": "",
            "experience": [{"company": "X", "bullets": [bullet]}],
            "education": [], "projects": []}


def test_finance_jd_alias_coverage():
    resume = _resume({"Analysis": ["FP&A", "Variance Analysis"], "Standards": ["GAAP"]},
                     "Led financial planning for a 40M budget.")
    jd = {"required_skills": ["Financial Planning and Analysis", "budget variance", "US GAAP"],
          "preferred_skills": [], "keywords": []}
    g = KeywordGapEngine().analyze(resume, jd)
    # All three alias-phrased requirements match the canonical skills.
    assert g["required_coverage"] == 100, g["required_missing"]


def test_marketing_jd_alias_coverage():
    resume = _resume({"Channels": ["SEO", "Google Ads"], "Skills": ["A/B Testing"]},
                     "Ran paid search campaigns.")
    jd = {"required_skills": ["search engine optimization", "google adwords", "split testing"],
          "preferred_skills": [], "keywords": []}
    g = KeywordGapEngine().analyze(resume, jd)
    assert g["required_coverage"] == 100, g["required_missing"]


def test_new_industry_aliases_dont_collide_with_existing():
    # Guards the de-SWE additions: no alias in the four NEW categories may
    # resolve (via first-writer-wins) to a canonical outside its own group.
    # This is what caught the "rn" -> React Native collision.
    from utils.keyword_normalizer import _SYNONYM_GROUPS, normalize_single

    new_cats = ("finance", "healthcare", "marketing", "sales",
                "analytics", "cybersecurity", "it_support", "product", "hr",
                "construction", "engineering_disciplines", "project_mgmt",
                "consulting")
    conflicts = []
    for cat in new_cats:
        for group in _SYNONYM_GROUPS[cat]:
            canonical = group[0]
            for alias in group[1:]:
                resolved = normalize_single(alias)
                if resolved != canonical:
                    conflicts.append((cat, alias, "->", resolved, "expected", canonical))
    assert not conflicts, f"new-taxonomy aliases hijacked by other groups: {conflicts[:10]}"
