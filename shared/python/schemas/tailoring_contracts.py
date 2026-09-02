"""Sidecar shapes for the cross-industry tailoring pipeline.

These ride next to the validated resume / JD schemas. ``validate_and_coerce``
strips unknown keys, so callers attach them AFTER that pass. Field names are
stable so stored records stay readable.
"""
from __future__ import annotations

from typing import Any, Dict, List

REQUIREMENT_TYPES = (
    "credential",
    "license",
    "education",
    "experience",
    "domain",
    "skill",
    "location",
    "work_mode",
    "travel",
    "work_auth",
    "clearance",
    "instruction",
)

PRIORITIES = ("mandatory", "preferred", "contextual")

DEFAULT_SECTION_ORDER = (
    "summary",
    "experience",
    "projects",
    "skills",
    "education",
    "licenses",
    "certifications",
    "languages",
    "awards",
    "publications",
    "patents",
    "leadership",
    "volunteering",
    "associations",
    "clearance",
)


def empty_content_plan() -> Dict[str, Any]:
    return {
        "candidate_positioning": "",
        "recruiter_persona": "",
        "domain": "general",
        "skill_groups": [],
        "section_order": list(DEFAULT_SECTION_ORDER),
        "bullet_budget": {},
        "format_strategy": "one_or_two_page",
        "supporting_evidence_strategy": "licenses_and_certs_first",
        "credentials_first": False,
        "force_metrics": False,
        "allow_project_generation": False,
    }


def empty_readiness() -> Dict[str, Any]:
    return {
        "qualification_fit": 0,
        "knockout_failures": [],
        "requirement_coverage": [],
        "notes": [],
        "disclaimer": (
            "Readiness is a qualification check, not a prediction that a "
            "recruiter or ATS will accept or reject this application."
        ),
    }
