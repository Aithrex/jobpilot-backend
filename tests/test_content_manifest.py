"""Stable content IDs, lifting unknown sections, unresolved headings."""
from __future__ import annotations

from services.content_manifest import (
    build_manifest,
    content_id,
    heading_semantic_type,
    lift_unknown_sections,
    stamp_resume_ids,
)


def test_ids_are_stable_across_reorder_and_compression():
    a = {"company": "Acme", "title": "SWE", "dates": "2020 - 2021", "bullets": ["Built X."]}
    b = {"company": "Acme", "title": "SWE", "dates": "2020 - 2021", "bullets": ["Built X."]}
    stamp_resume_ids({"experience": [a]})
    stamp_resume_ids({"experience": [b]})
    assert a["content_id"] == b["content_id"]
    # Reordering two different roles keeps each identity.
    r1 = {"title": "Intern", "company": "Acme", "dates": "2019"}
    r2 = {"title": "SWE", "company": "Acme", "dates": "2021"}
    stamp_resume_ids({"experience": [r1, r2]})
    id_intern, id_swe = r1["content_id"], r2["content_id"]
    stamp_resume_ids({"experience": [r2, r1]})
    assert r1["content_id"] == id_intern
    assert r2["content_id"] == id_swe
    assert id_intern != id_swe


def test_heading_aliases_are_profession_agnostic():
    assert heading_semantic_type("Clinical Rotations") == "clinical"
    assert heading_semantic_type("PUBLICATIONS") == "publications"
    assert heading_semantic_type("Volunteer Experience") == "volunteer"
    assert heading_semantic_type("Something Invented") == "custom"


def test_lift_unknown_sections_does_not_drop_volunteer():
    data = {
        "summary": "",
        "experience": [],
        "volunteer": [{"title": "Tutor", "organization": "Library", "bullets": ["Helped."]}],
    }
    out = lift_unknown_sections(data)
    assert "volunteer" not in out
    extras = out["additional_sections"]
    assert extras[0]["semantic_type"] == "volunteer"
    assert extras[0]["entries"][0]["title"] == "Tutor"


def test_unresolved_heading_not_treated_as_empty():
    resume = {
        "summary": "",
        "experience": [{"title": "SWE", "company": "Acme", "dates": "2021", "bullets": ["Did work."]}],
        "projects": [],
        "skills": {"Languages": ["Python"]},
        "education": [],
        "certifications": [],
        "additional_sections": [],
    }
    raw = "EXPERIENCE\nSWE Acme\nPROJECTS\nSecretApp\nSKILLS\nPython\n"
    manifest = build_manifest(resume, raw_text=raw)
    proj = next(s for s in manifest.sections() if s.semantic_type == "projects")
    assert proj.parse_status == "unresolved"
    assert proj.content_id in manifest.parse_unresolved


def test_content_id_not_array_position():
    assert content_id("experience", "acme", "swe", "2020") == content_id(
        "experience", "acme", "swe", "2020"
    )
    assert content_id("experience", "acme", "swe", "2020") != content_id(
        "experience", "acme", "intern", "2020"
    )
