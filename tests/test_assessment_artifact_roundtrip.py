"""Real renderer PDF/DOCX round-trips and association checks (Package 3/§11)."""
from __future__ import annotations

import pytest

from services.resume_renderer import ResumeRenderer
from services.artifact_assessment import (
    assess_artifact,
    extract_docx_ordered,
    token_boundary_match,
)


def _resume():
    return {
        "contact": {"name": "Alex Rivera", "email": "alex@example.com", "phone": "555-0100"},
        "summary": "Engineer building reliable billing services.",
        "skills": {"Languages": ["Python", "SQL"]},
        "experience": [{
            "company": "Acme Corp",
            "title": "Software Engineer",
            "dates": "2022 - Present",
            "location": "Remote",
            "type": "Full-time",
            "bullets": [
                "Built billing APIs in Python for Acme Corp.",
                "Wrote SQL against the warehouse to cut latency.",
            ],
        }],
        "education": [{
            "degree": "B.S. Computer Science",
            "institution": "State University",
            "location": "",
            "dates": "2018 - 2022",
            "gpa": "",
            "coursework": "",
        }],
        "projects": [],
        "certifications": [],
    }


@pytest.fixture(scope="module")
def renderer():
    return ResumeRenderer()


def test_pdf_roundtrip_preserves_employer_and_bullets(renderer):
    resume = _resume()
    pdf, resolution = renderer.generate_pdf(resume)
    assert pdf and len(pdf) > 500
    art = assess_artifact(pdf, resume, fmt="pdf",
                          renderer_version=getattr(renderer, "RENDER_VERSION", ""))
    assert art["status"] in {"passed", "issues"}
    text = art.get("raw_text") or ""
    checks = art.get("checks") or {}
    # Employer / title / dates / bullet association targets from structured resume.
    assert checks.get("experience.0.company") is True or token_boundary_match(text, "Acme Corp")
    assert checks.get("experience.0.title") is True or "Software Engineer" in text
    assert checks.get("experience.0.dates") is True or "2022" in text
    assert checks.get("experience.0.bullets.0") is True or "billing" in text.lower()
    assert token_boundary_match(text, "Python") or "Python" in text


def test_docx_roundtrip_ordered_and_hashes(renderer):
    resume = _resume()
    data = renderer.generate_docx(resume)
    assert data and len(data) > 500
    parsed = extract_docx_ordered(data)
    assert parsed.blocks
    assert parsed.raw_text
    art = assess_artifact(data, resume, fmt="docx")
    assert art["bytes_hash"]
    assert art["cache_key"]
    # Format-specific: PDF key differs
    pdf, _ = renderer.generate_pdf(resume)
    art_pdf = assess_artifact(pdf, resume, fmt="pdf")
    assert art["cache_key"] != art_pdf["cache_key"]


def test_visual_regression_cohort_smoke(renderer):
    """Representative cohort: early-career, dense, qualitative — offline render only.

    Not a pixel diff suite; asserts each variant produces extractable text without
    replacement glyphs. Live visual review remains a Package 7 human step.
    """
    variants = [
        _resume(),
        {**_resume(), "summary": "", "education": [], "projects": []},  # sparse
        {**_resume(), "experience": [{
            "company": "Clinic", "title": "RN", "dates": "2021 - Present",
            "location": "", "type": "",
            "bullets": [
                "Provided bedside care for complex patients.",
                "Precepted new graduate nurses on night shift.",
            ],
        }], "skills": {"Clinical": ["Bedside care"]}},
    ]
    for resume in variants:
        pdf, _ = renderer.generate_pdf(resume)
        art = assess_artifact(pdf, resume, fmt="pdf")
        assert "\ufffd" not in (art.get("raw_text") or "")
        assert art["status"] in {"passed", "issues", "failed"}
