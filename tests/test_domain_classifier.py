"""Domain/profession classifier (Craft-overhaul Phase 2)."""
import pytest

from services.domain_classifier import (
    classify_domain, allows_project_generation, DOMAINS, PROJECT_GEN_DOMAINS,
)


@pytest.mark.parametrize("jd,expected", [
    # tech
    ({"job_title": "Senior Backend Engineer", "industry": "Software"}, "software"),
    ({"job_title": "Full Stack Developer", "industry": "SaaS"}, "software"),
    ({"job_title": "Machine Learning Engineer", "industry": "AI"}, "data_science"),
    ({"job_title": "Data Scientist", "industry": "Analytics"}, "data_science"),
    ({"job_title": "Data Engineer", "industry": "Tech"}, "data_engineering"),
    ({"job_title": "Analytics Engineer", "industry": "Tech"}, "data_engineering"),
    ({"job_title": "Data Analyst", "industry": "Analytics"}, "data_analytics"),
    ({"job_title": "Business Analyst", "industry": "Tech"}, "data_analytics"),
    ({"job_title": "Cybersecurity Analyst", "industry": "Security"}, "cybersecurity"),
    ({"job_title": "Security Engineer", "industry": "InfoSec"}, "cybersecurity"),
    ({"job_title": "DevOps Engineer", "industry": "Cloud"}, "devops"),
    ({"job_title": "Site Reliability Engineer", "industry": "Cloud"}, "devops"),
    ({"job_title": "IT Support Specialist", "industry": "IT"}, "it_support"),
    ({"job_title": "Network Administrator", "industry": "IT"}, "it_support"),
    # engineering disciplines (the bug this fixes)
    ({"job_title": "Civil Engineer", "industry": "Infrastructure"}, "civil_eng"),
    ({"job_title": "Mechanical Engineer", "industry": "Manufacturing"}, "mechanical_eng"),
    ({"job_title": "Electrical Engineer", "industry": "Power"}, "electrical_eng"),
    ({"job_title": "Construction Project Manager", "industry": "Construction"}, "construction"),
    # business / knowledge work
    ({"job_title": "FP&A Manager", "industry": "Finance"}, "finance"),
    ({"job_title": "Senior Accountant", "industry": "Accounting"}, "finance"),
    ({"job_title": "Product Manager", "industry": "SaaS"}, "product"),
    ({"job_title": "Technical Program Manager", "industry": "Tech"}, "project_program_mgmt"),
    ({"job_title": "Scrum Master", "industry": "Agile"}, "project_program_mgmt"),
    ({"job_title": "HR Business Partner", "industry": "HR"}, "hr"),
    ({"job_title": "Technical Recruiter", "industry": "Staffing"}, "hr"),
    ({"job_title": "Management Consultant", "industry": "Consulting"}, "consulting"),
    ({"job_title": "Operations Manager", "industry": "Operations"}, "business_ops"),
    ({"job_title": "Supply Chain Analyst", "industry": "Logistics"}, "business_ops"),
    ({"job_title": "Growth Marketing Manager", "industry": "Marketing"}, "marketing"),
    ({"job_title": "SEO Specialist", "industry": "Digital Marketing"}, "marketing"),
    ({"job_title": "Account Executive", "industry": "Sales"}, "sales"),
    # licensed / specialized
    ({"job_title": "ICU Registered Nurse", "industry": "Healthcare"}, "healthcare"),
    ({"job_title": "Clinical Documentation Specialist", "industry": "Medical"}, "healthcare"),
    ({"job_title": "Corporate Counsel", "industry": "Legal"}, "legal"),
    ({"job_title": "Product Designer", "industry": "Design"}, "design"),
])
def test_classifies_by_title_and_industry(jd, expected):
    assert classify_domain(jd) == expected


def test_data_subfields_beat_generic_software():
    # All contain "engineer"/"developer" but their specific rule must win.
    assert classify_domain({"job_title": "Data Engineer"}) == "data_engineering"
    assert classify_domain({"job_title": "ML Engineer"}) == "data_science"
    assert classify_domain({"job_title": "Security Engineer"}) == "cybersecurity"
    assert classify_domain({"job_title": "DevOps Engineer"}) == "devops"


def test_engineering_disciplines_not_software():
    # The core bug fix: non-software engineers must NOT route to software
    # (which would wrongly enable GitHub project generation).
    for title in ("Civil Engineer", "Mechanical Engineer", "Electrical Engineer",
                  "Structural Engineer", "Manufacturing Engineer"):
        d = classify_domain({"job_title": title})
        assert d in ("civil_eng", "mechanical_eng", "electrical_eng"), (title, d)
        assert not allows_project_generation(d), f"{title} wrongly got project-gen"


def test_empty_and_ambiguous_default_to_general():
    assert classify_domain({}) == "general"
    assert classify_domain({"job_title": "Coordinator", "industry": ""}) == "general"
    assert classify_domain({"job_title": None, "industry": None}) == "general"


def test_never_raises_on_garbage():
    assert classify_domain(None) == "general"  # type: ignore[arg-type]
    assert classify_domain({"job_title": 123, "required_skills": "notalist"}) in DOMAINS


def test_resume_skills_sharpen_signal():
    # Thin JD, but the resume clearly shows nursing.
    resume = {"experience": [{"title": "Registered Nurse"}],
              "skills": {"Clinical": ["Patient Assessment", "Telemetry"]}}
    assert classify_domain({"job_title": "Specialist"}, resume) == "healthcare"


def test_project_gen_only_for_code_domains():
    # GitHub project-gen only for code/repo domains. Design uses a portfolio but
    # NOT GitHub, so it's excluded. Engineering disciplines are excluded.
    for d in ("software", "data_science", "data_engineering", "devops"):
        assert allows_project_generation(d), d
    for d in ("data_analytics", "cybersecurity", "it_support", "finance",
              "product", "project_program_mgmt", "business_ops", "marketing",
              "sales", "hr", "consulting", "healthcare", "legal", "design",
              "mechanical_eng", "civil_eng", "electrical_eng", "construction",
              "general"):
        assert not allows_project_generation(d), d
    assert PROJECT_GEN_DOMAINS == {"software", "data_science", "data_engineering", "devops"}


def test_all_rule_domains_are_valid():
    # Every returnable domain is in the declared DOMAINS tuple.
    samples = ["Software Engineer", "Data Scientist", "Accountant", "Nurse",
               "Marketing Manager", "Account Executive", "Attorney",
               "UX Designer", "Operations Lead", "Zookeeper"]
    for title in samples:
        assert classify_domain({"job_title": title}) in DOMAINS


# --- P3D: project-generation gate wired into _project_policy ---

def test_project_policy_disables_gen_for_non_tech():
    from services.resume_service import _project_policy
    resume = {"skills": {"Clinical": ["Patient Assessment"]},
              "experience": [{"company": "H", "title": "ICU Nurse", "bullets": ["x"]}]}
    p = _project_policy(resume, None, {"job_title": "ICU Registered Nurse",
                                       "industry": "Healthcare"})
    assert p["allow"] is False
    assert "non-tech" in p["reason"]


def test_project_policy_unchanged_without_jd():
    # Back-compat: omitting jd_analysis leaves the base policy intact.
    from services.resume_service import _project_policy
    resume = {"skills": {"Lang": ["Python"]},
              "experience": [{"company": "A", "title": "Engineer", "bullets": ["x"]}]}
    p = _project_policy(resume, None)
    assert "allow" in p  # base policy returned, no crash
