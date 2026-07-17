"""Skill implication map + gap-engine promotion (Craft-overhaul).

A JD-required skill that an owned skill provably implies (React => HTML/CSS/DOM)
is counted as covered, not a gap. Guards: only the SOLID tier promotes, and
forbidden reverse edges (Docker=>K8s, React=>TypeScript) never do. Pure Python.
"""
import pytest

from utils.skill_implications import is_implied, implied_skills, _canon
from services.keyword_gap_engine import KeywordGapEngine


# --- The implication primitive ---

@pytest.mark.parametrize("target", ["HTML", "CSS", "DOM", "JavaScript"])
def test_react_implies_frontend_substrate(target):
    assert is_implied(target, ["React"])


@pytest.mark.parametrize("target,owner", [
    # SWE
    ("Java", "Spring Boot"),
    ("SQL", "PostgreSQL"),
    ("SQL", "Hibernate"),
    ("JPA", "Hibernate"),
    ("JavaScript", "TypeScript"),
    ("Docker", "Kubernetes"),
    ("Unit Testing", "TDD"),
    ("CI/CD", "GitHub Actions"),
    ("Git", "GitHub Actions"),
    # Finance
    ("Variance Analysis", "FP&A"),
    ("GAAP", "CPA"),
    ("Reconciliation", "Month-End Close"),
    # Healthcare
    ("BLS", "ACLS"),
    ("Patient Assessment", "Critical Care"),
    ("Electronic Health Records", "Epic"),
    # Marketing
    ("SEM", "Google Ads"),
    ("A/B Testing", "Conversion Rate Optimization"),
    # Sales
    ("CRM", "Salesforce"),
    ("Account Management", "Account Executive"),
])
def test_solid_edges(target, owner):
    assert is_implied(target, [owner]), f"{owner} should imply {target}"


@pytest.mark.parametrize("target,owner", [
    # SWE
    ("TypeScript", "React"),        # reverse trap
    ("TypeScript", "JavaScript"),   # reverse trap
    ("Kubernetes", "Docker"),       # reverse trap
    ("MySQL", "PostgreSQL"),        # non-subset
    ("PostgreSQL", "MySQL"),        # non-subset
    ("Hibernate", "Spring Boot"),   # could be JdbcTemplate/MyBatis
    ("Hibernate", "JPA"),           # could be EclipseLink
    ("TDD", "Unit Testing"),        # reverse trap
    ("Kubernetes", "ECS"),          # ECS is not K8s
    ("RabbitMQ", "Kafka"),          # different brokers
    ("Redux", "React"),             # optional state lib
    # Finance
    ("IFRS", "GAAP"),               # different standards
    ("CPA", "GAAP"),                # knowing GAAP != being a CPA
    ("FP&A", "Variance Analysis"),  # reverse of a valid edge
    # Healthcare
    ("ACLS", "BLS"),                # BLS is the prerequisite
    ("Epic", "Cerner"),             # different systems
    ("Registered Nurse", "Patient Assessment"),  # skill != license
    # Marketing
    ("SEO", "SEM"),                 # organic vs paid
    ("Google Ads", "PPC"),          # PPC != a specific platform
    # Sales
    ("Salesforce", "CRM"),          # CRM != a specific vendor
])
def test_forbidden_edges_never_promote(target, owner):
    assert not is_implied(target, [owner]), f"{owner} must NOT imply {target}"


def test_transitive_closure_bounded():
    # EKS => Kubernetes => Docker/YAML (transitive), plus AWS directly.
    c = implied_skills("EKS")
    assert {"kubernetes", "docker", "aws", "yaml"} <= c


def test_qualifier_noise_resolves():
    # JD phrasings the alias normalizer alone doesn't strip.
    assert is_implied("DOM Manipulation", ["React"])
    assert is_implied("JavaScript (Vanilla)", ["React"])
    assert is_implied("Java 8+", ["Spring Boot"])


# --- End-to-end through the gap engine ---

def _resume(skills):
    return {
        "skills": skills, "summary": "", "education": [], "projects": [],
        "experience": [{"company": "X", "bullets": ["Developed React components"]}],
    }


def test_gap_engine_promotes_implied_required_skills():
    resume = _resume({"Frontend": ["React"], "Backend": ["Spring Boot"]})
    jd = {"required_skills": ["React", "HTML", "CSS", "DOM Manipulation", "Java 8+"],
          "preferred_skills": [], "keywords": []}
    g = KeywordGapEngine().analyze(resume, jd)
    missing = {m.lower() for m in g["required_missing"]}
    for kw in ("html", "css", "dom manipulation", "java 8+"):
        assert kw not in missing, f"{kw} should be implied-covered, not missing"
    assert g["required_coverage"] == 100


def test_gap_engine_keeps_genuine_gaps():
    # Owns Spring Boot but NOT Hibernate/JDBC — those must stay gaps.
    resume = _resume({"Backend": ["Spring Boot", "Java"]})
    jd = {"required_skills": ["Hibernate/JPA", "JDBC"],
          "preferred_skills": [], "keywords": []}
    g = KeywordGapEngine().analyze(resume, jd)
    missing = {m.lower() for m in g["required_missing"]}
    assert "hibernate/jpa" in missing or "hibernate" in " ".join(missing)
    assert "jdbc" in missing


def test_promoted_skills_flagged_as_partial():
    # Implied (not literal) coverage is recorded in partial_matches so the
    # prompt/audit know it's implied, not verbatim.
    resume = _resume({"Frontend": ["React"]})
    jd = {"required_skills": ["HTML"], "preferred_skills": [], "keywords": []}
    g = KeywordGapEngine().analyze(resume, jd)
    assert "HTML" in g["required_matched"]
    assert "HTML" in g["partial_matches"]
