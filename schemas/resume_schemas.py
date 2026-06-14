"""
Resume JSON schemas and validation.

Schemas mirror the TypeScript types in portfolio-frontend/src/types/resume.ts
to ensure backend ↔ frontend contract consistency.

Validation is lightweight (no jsonschema/pydantic) to keep Lambda cold starts fast.
"""
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

# Matches TailoredFullResume / structured parsed resume
PARSED_RESUME_SCHEMA = {
    "contact": {
        "name": str,
        "email": str,
        "phone": str,
        "linkedin": str,
        "github": str,
        # New ATS/recruiter-visible fields — match the LaTeX template's
        # reference contact line (location first, portfolio URL between
        # email and linkedin).
        "location": str,
        "portfolio": str,
    },
    "summary": str,
    "skills": {"_dict_of": [str]},  # Record<string, string[]>
    "experience": [{
        "title": str,
        "company": str,
        "location": str,
        "dates": str,
        "type": str,
        "bullets": [str],
    }],
    "education": [{
        "degree": str,
        "institution": str,
        "location": str,
        "dates": str,
        "gpa": str,
        "coursework": str,
    }],
    "projects": [{
        "name": str,
        "dates": str,
        "bullets": [str],
        "tech": str,
    }],
    # Certifications are extracted ONLY if explicitly present in the resume
    # (the parser prompt forbids fabrication). For users without a certs
    # section, this stays []. Downstream the tailor pipeline now PRESERVES
    # this list instead of forcing it empty — IntegrityGuard mirrors
    # original.certifications onto tailored.certifications.
    "certifications": [str],
}

# TailoredFullResume mirrors the parsed schema 1:1.
TAILORED_RESUME_SCHEMA = dict(PARSED_RESUME_SCHEMA)

# Matches JDAnalysis
JD_ANALYSIS_SCHEMA = {
    "job_title": str,
    "company": str,
    "location": str,
    "employment_type": str,
    "required_skills": [str],
    "preferred_skills": [str],
    "responsibilities": [str],
    "qualifications": [str],
    "experience_years": str,
    "industry": str,
    "keywords": [str],
}

# Matches ATSScores
ATS_SCORES_SCHEMA = {
    "overall": int,
    "keyword_match": int,
    "keyword_frequency": int,
    "skills_alignment": int,
    "experience_relevance": int,
    "quantifiable_impact": int,
    "bullet_quality": int,
    "format_score": int,
    "section_completeness": int,
    "scanners": {
        "workday": int,
        "greenhouse": int,
        "lever": int,
        "icims": int,
        "taleo": int,
        "smartrecruiters": int,
    },
    "ai_screener": {
        "overall": int,
        "relevance": int,
        "seniority_fit": int,
        "culture_fit": int,
    },
    "suggestions": [str],
    "missing_keywords": [str],
    "strengths": [str],
}


# ---------------------------------------------------------------------------
# Validation + coercion
# ---------------------------------------------------------------------------

def _default_for_type(spec):
    """Return a safe default value for the given schema spec."""
    if spec is str:
        return ""
    if spec is int:
        return 0
    if isinstance(spec, list):
        return []
    if isinstance(spec, dict):
        return {}
    return ""


def _coerce(value, spec):
    """Coerce *value* to match *spec*, returning a safe default on failure."""
    # str field
    if spec is str:
        return str(value) if value is not None else ""

    # int field
    if spec is int:
        if isinstance(value, int):
            return value
        if isinstance(value, (float, str)):
            try:
                return int(float(value))
            except (ValueError, TypeError):
                return 0
        return 0

    # list of primitives: [str] or [int]
    if isinstance(spec, list) and len(spec) == 1 and spec[0] in (str, int):
        if not isinstance(value, list):
            return []
        return [_coerce(item, spec[0]) for item in value]

    # list of dicts (objects): [{"key": type, ...}]
    if isinstance(spec, list) and len(spec) == 1 and isinstance(spec[0], dict):
        if not isinstance(value, list):
            return []
        obj_spec = spec[0]
        return [_validate_object(item, obj_spec) for item in value if isinstance(item, dict)]

    # dict with "_dict_of" marker → Record<string, T>
    if isinstance(spec, dict) and "_dict_of" in spec:
        if not isinstance(value, dict):
            # Safety net: if value is a list of strings, wrap as {"General": value}
            # instead of silently dropping all data
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                logger.warning(
                    "Schema coercion: skills was a flat list (%d items), "
                    "auto-wrapping as {\"General\": [...]}",
                    len(value),
                )
                return {"General": value}
            logger.warning(
                "Schema coercion: expected dict for _dict_of field, got %s — returning empty dict",
                type(value).__name__,
            )
            return {}
        inner_spec = spec["_dict_of"]
        return {str(k): _coerce(v, inner_spec) for k, v in value.items()}

    # nested dict (fixed keys)
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return {k: _default_for_type(v) for k, v in spec.items()}
        return _validate_object(value, spec)

    return value


def _validate_object(data: dict, schema: dict) -> dict:
    """Validate a dict against a schema, filling missing keys and coercing types."""
    result = {}
    for key, spec in schema.items():
        value = data.get(key)
        if value is None:
            result[key] = _default_for_type(spec)
        else:
            result[key] = _coerce(value, spec)
    return result


def validate_and_coerce(data: Any, schema: dict) -> dict:
    """Validate LLM output against schema.

    - Ensures all required keys exist (fills missing with type-appropriate defaults)
    - Coerces types where safe (e.g. str → int for score fields)
    - Strips unexpected keys
    - Returns cleaned dict

    Raises ValueError if data is fundamentally malformed (not a dict).
    """
    if not isinstance(data, dict):
        raise ValueError(f"Expected dict, got {type(data).__name__}")

    result = _validate_object(data, schema)

    # Log any keys that were missing or coerced
    missing = [k for k in schema if k not in data]
    if missing:
        logger.warning("Schema validation filled missing keys: %s", missing)

    # Log type mismatches for critical sections (helps debug silent coercion)
    for key, spec in schema.items():
        value = data.get(key)
        if value is None:
            continue  # Already logged as missing above
        if isinstance(spec, dict) and "_dict_of" in spec and not isinstance(value, dict):
            logger.warning(
                "Schema coercion: '%s' was %s, expected dict — data may be lost",
                key, type(value).__name__,
            )
        elif isinstance(spec, list) and not isinstance(value, list):
            logger.warning(
                "Schema coercion: '%s' was %s, expected list — coerced to []",
                key, type(value).__name__,
            )

    return result


# ---------------------------------------------------------------------------
# Helpers for extracting flat fields from structured resume
# ---------------------------------------------------------------------------

def flatten_skills(structured: dict) -> List[str]:
    """Extract a flat list of all skills from the categorized skills dict."""
    skills = structured.get("skills", {})
    if isinstance(skills, list):
        return skills
    flat = []
    for category_skills in skills.values():
        if isinstance(category_skills, list):
            flat.extend(category_skills)
    return flat


def extract_job_titles(structured: dict) -> List[str]:
    """Extract job titles from experience entries."""
    return [
        exp.get("title", "")
        for exp in structured.get("experience", [])
        if exp.get("title")
    ]


def build_resume_text(structured: dict) -> str:
    """Flatten a structured resume into plain text for keyword matching."""
    parts = []

    contact = structured.get("contact", {})
    parts.append(contact.get("name", ""))

    parts.append(structured.get("summary", ""))

    # Skills
    skills = structured.get("skills", {})
    if isinstance(skills, dict):
        for cat, skill_list in skills.items():
            if isinstance(skill_list, list):
                parts.append(f"{cat}: {', '.join(skill_list)}")
    elif isinstance(skills, list):
        parts.append(", ".join(skills))

    # Experience
    for exp in structured.get("experience", []):
        parts.append(exp.get("title", ""))
        parts.append(exp.get("company", ""))
        for b in exp.get("bullets", []):
            parts.append(b)

    # Projects
    for proj in structured.get("projects", []):
        parts.append(proj.get("name", ""))
        parts.append(proj.get("tech", ""))
        for b in proj.get("bullets", []):
            parts.append(b)

    # Education
    for edu in structured.get("education", []):
        parts.append(edu.get("degree", ""))
        parts.append(edu.get("institution", ""))
        parts.append(edu.get("coursework", ""))

    return "\n".join(p for p in parts if p)
