"""Deterministic content plan for a tailored resume (Increment 7).

The writer stages consume this plan. It is profession-aware: a nurse gets a
clinical recruiter persona, licenses near the top, and no pressure to invent
metrics or GitHub projects. A software engineer still gets the familiar
skills-forward layout.

No LLM is required. An optional FLASH polish can rewrite positioning later;
this module always returns a complete, testable plan on its own.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from schemas.resume_schemas import flatten_skills
from schemas.tailoring_contracts import DEFAULT_SECTION_ORDER, empty_content_plan
from services.domain_classifier import (
    PROJECT_GEN_DOMAINS,
    allows_project_generation,
    classify_domain,
)

# Recruiter the six-second read and the writers should imitate.
_PERSONA: Dict[str, str] = {
    "healthcare": "clinical recruiter who screens nurses and allied-health staff",
    "finance": "finance recruiter who screens accountants, auditors, and analysts",
    "construction": "construction recruiter who screens estimators and field staff",
    "legal": "legal recruiter who screens attorneys and paralegals",
    "sales": "sales recruiter who screens account executives and AEs",
    "marketing": "marketing recruiter who screens brand and growth managers",
    "hr": "HR recruiter who screens people-operations roles",
    "mechanical_eng": "engineering recruiter who screens mechanical and design engineers",
    "civil_eng": "engineering recruiter who screens civil and structural engineers",
    "electrical_eng": "engineering recruiter who screens electrical and controls engineers",
    "software": "technical recruiter who screens software engineers",
    "data_science": "technical recruiter who screens data scientists",
    "data_engineering": "technical recruiter who screens data engineers",
    "data_analytics": "analytics recruiter who screens analysts",
    "devops": "technical recruiter who screens platform and DevOps engineers",
    "cybersecurity": "security recruiter who screens analysts and engineers",
    "it_support": "IT recruiter who screens support and systems roles",
    "product": "product recruiter who screens PMs",
    "project_program_mgmt": "operations recruiter who screens program and project managers",
    "business_ops": "operations recruiter who screens business operations roles",
    "consulting": "consulting recruiter who screens analysts and associates",
    "design": "design recruiter who screens product and visual designers",
    "general": "experienced recruiter who hires across professions",
}

# Licensed / regulated professions: credentials must be visible in the first
# glance. Software-style metric density is not the hiring signal.
_CREDENTIAL_FIRST = frozenset({
    "healthcare", "finance", "legal", "construction",
    "civil_eng", "mechanical_eng", "electrical_eng",
})

_NO_FORCED_METRICS = frozenset({
    "healthcare", "legal", "hr",
})

_EXTRAS = (
    "languages", "awards", "publications", "patents",
    "leadership", "volunteering", "associations", "clearance",
)

_CREDENTIAL_ORDER = (
    "summary",
    "licenses",
    "certifications",
    "experience",
    "skills",
    "education",
    "projects",
) + _EXTRAS

_SOFTWARE_ORDER = (
    "summary",
    "skills",
    "experience",
    "projects",
    "education",
    "licenses",
    "certifications",
) + _EXTRAS


def recruiter_persona(domain: str) -> str:
    return _PERSONA.get(domain) or _PERSONA["general"]


def _section_order(domain: str) -> List[str]:
    if domain in _CREDENTIAL_FIRST:
        return list(_CREDENTIAL_ORDER)
    if domain in PROJECT_GEN_DOMAINS:
        return list(_SOFTWARE_ORDER)
    return list(DEFAULT_SECTION_ORDER)


def _skill_groups(resume: Dict[str, Any], jd_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    skills = resume.get("skills") or {}
    if isinstance(skills, dict):
        for heading, items in skills.items():
            if not items:
                continue
            groups.append({
                "heading": str(heading),
                "skills": [s for s in items if isinstance(s, str) and s.strip()],
            })
    if not groups:
        flat = flatten_skills(resume)
        if flat:
            groups.append({"heading": "Skills", "skills": flat})
    required = [
        s for s in (jd_analysis.get("required_skills") or [])
        if isinstance(s, str) and s.strip()
    ]
    if required:
        groups.append({"heading": "Role keywords", "skills": required[:12]})
    return groups


def _positioning(resume: Dict[str, Any], jd_analysis: Dict[str, Any], domain: str) -> str:
    title = ""
    exp = resume.get("experience") or []
    if exp and isinstance(exp[0], dict):
        title = (exp[0].get("title") or "").strip()
    target = (jd_analysis.get("job_title") or "").strip()
    if title and target:
        return f"{title} targeting {target}"
    return title or target or domain.replace("_", " ")


def _bullet_budget(resume: Dict[str, Any], domain: str) -> Dict[str, int]:
    roles = len(resume.get("experience") or [])
    per_role = 4 if domain in PROJECT_GEN_DOMAINS else 3
    return {
        "experience_roles": roles,
        "bullets_per_role": per_role,
        "projects": 2 if domain in PROJECT_GEN_DOMAINS else 0,
    }


def plan(
    resume: Dict[str, Any],
    jd_analysis: Dict[str, Any],
    fact_store: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a complete content plan. Never raises."""
    out = empty_content_plan()
    resume = resume or {}
    jd_analysis = jd_analysis or {}
    try:
        domain = classify_domain(jd_analysis, resume)
        out["domain"] = domain
        out["recruiter_persona"] = recruiter_persona(domain)
        out["candidate_positioning"] = _positioning(resume, jd_analysis, domain)
        out["skill_groups"] = _skill_groups(resume, jd_analysis)
        out["section_order"] = _section_order(domain)
        out["bullet_budget"] = _bullet_budget(resume, domain)
        out["credentials_first"] = domain in _CREDENTIAL_FIRST
        out["force_metrics"] = domain not in _NO_FORCED_METRICS and domain in PROJECT_GEN_DOMAINS
        out["allow_project_generation"] = allows_project_generation(domain)
        out["supporting_evidence_strategy"] = (
            "licenses_and_certs_first" if out["credentials_first"]
            else "experience_and_projects"
        )
        out["format_strategy"] = "letter_or_a4_two_page_ok"
        if fact_store:
            out["fact_count"] = len(fact_store.get("facts") or [])
    except Exception:
        pass
    return out
