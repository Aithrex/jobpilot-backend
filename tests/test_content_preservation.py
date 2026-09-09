"""Section representation, fair allocation, locks, and export invariants."""
from __future__ import annotations

import copy
import io

import pytest
from docx import Document
from pypdf import PdfReader

from schemas.resume_schemas import build_resume_text
from services.content_preservation import assert_preservation
from services.content_selection import select_resume_content
from services.integrity_guard import IntegrityGuard
from services.resume_renderer import ResumeRenderer
from tests.fixtures.resume_preservation.cases import (
    CASES,
    JD_SDE,
    custom_paragraph_section,
    grouped_employer,
    many_jobs_one_project,
    required_license_vs_summary,
    student_projects_primary,
)


def _pdf_text(pdf: bytes) -> str:
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)


def _docx_text(blob: bytes) -> str:
    return "\n".join(p.text for p in Document(io.BytesIO(blob)).paragraphs)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_nonempty_source_section_survives_one_page_fit(case):
    resume = case["resume"]()
    selected = select_resume_content(resume, case["jd"], ResumeRenderer(), max_pages=1)
    sel = selected.get("content_selection") or {}
    assert sel.get("page_outcome") != "preservation_failed", sel
    check = sel.get("preservation") or {}
    assert check.get("ok") is not False, check
    semantic = case["must_keep_section"]
    if semantic == "projects":
        assert selected.get("projects"), selected.get("projects")
        blob = " ".join(
            p.get("name", "") + " " + " ".join(p.get("bullets") or [])
            for p in selected["projects"]
        )
    else:
        extras = selected.get("additional_sections") or []
        assert any(str(s.get("semantic_type")) == semantic for s in extras), extras
        blob = build_resume_text(selected)
    assert case["must_keep_text"].lower() in blob.lower(), blob


def test_supplied_project_loss_analog_keeps_project_section():
    resume = many_jobs_one_project()
    selected = select_resume_content(resume, JD_SDE, ResumeRenderer(), max_pages=1)
    assert selected.get("projects"), "Projects section was dropped"
    names = " ".join(p.get("name") or "" for p in selected["projects"])
    assert "InfraGraph" in names
    bullets = [b for p in selected["projects"] for b in (p.get("bullets") or [])]
    assert bullets, "Project kept only as an empty heading"
    pdf, _ = ResumeRenderer().generate_pdf(selected)
    text = _pdf_text(pdf)
    assert "PROJECTS" in text or "InfraGraph" in text
    docx = _docx_text(ResumeRenderer().generate_docx(selected))
    assert "InfraGraph" in docx or "PROJECTS" in docx


def test_third_project_bullet_unique_required_evidence():
    resume = many_jobs_one_project()
    resume["projects"][0]["bullets"].append(
        "Deployed the service with Kubernetes and Helm in production."
    )
    jd = dict(JD_SDE)
    jd["required_skills"] = list(JD_SDE["required_skills"]) + ["Kubernetes"]
    selected = select_resume_content(resume, jd, ResumeRenderer(), max_pages=1)
    text = " ".join(b for p in selected["projects"] for b in p.get("bullets") or [])
    assert "Kubernetes" in text


def test_last_project_bullet_is_not_silently_removed():
    resume = many_jobs_one_project()
    resume["projects"][0]["bullets"] = [
        "Only remaining project bullet describing the graph solver evidence path."
    ]
    selected = select_resume_content(resume, JD_SDE, ResumeRenderer(), max_pages=1)
    assert selected["projects"]
    assert selected["projects"][0]["bullets"]


def test_student_projects_are_primary_evidence():
    resume = student_projects_primary()
    selected = select_resume_content(
        resume, {"required_skills": ["Python"]}, ResumeRenderer(), max_pages=1,
    )
    assert selected.get("projects")
    assert any("MQTT" in " ".join(p.get("bullets") or []) for p in selected["projects"])
    extras = selected.get("additional_sections") or []
    assert any(s.get("semantic_type") == "research" for s in extras)


def test_required_license_survives_verbose_summary():
    resume = required_license_vs_summary()
    selected = select_resume_content(
        resume,
        {"job_title": "RN", "required_skills": ["RN license"], "qualifications": ["RN license"]},
        ResumeRenderer(),
        max_pages=1,
    )
    blob = build_resume_text(selected)
    assert "Registered Nurse" in blob or "RN license" in blob
    extras = selected.get("additional_sections") or []
    assert any(s.get("semantic_type") == "licenses" for s in extras)


def test_custom_section_without_bullets_is_kept():
    resume = custom_paragraph_section()
    selected = select_resume_content(resume, {}, ResumeRenderer(), max_pages=1)
    extras = selected.get("additional_sections") or []
    bodies = [e.get("body") for s in extras for e in (s.get("entries") or [])]
    assert any("food-drive" in (b or "") for b in bodies)


def test_employer_grouping_preserves_role_dates():
    resume = grouped_employer()
    r = ResumeRenderer()
    pdf, _ = r.generate_pdf(resume)
    text = _pdf_text(pdf)
    assert "Northwind" in text
    assert "Dec 2025" in text or "2025" in text
    assert "May 2022" in text or "2022" in text
    assert "Intern" in text


def test_integrity_guard_restores_experience_in_source_order():
    original = grouped_employer()
    tailored = copy.deepcopy(original)
    tailored["experience"] = [original["experience"][0], original["experience"][2]]
    guard = IntegrityGuard()
    fixed, report = guard.enforce(original, tailored, {})
    titles = [e.get("title") for e in fixed["experience"]]
    assert titles[0].startswith("Software Engineer")
    assert "Intern" in titles[1]
    assert titles[2].startswith("Cloud")
    assert report.missing_experience_reinjected


def test_locks_survive_compression_and_reorder():
    resume = many_jobs_one_project()
    pinned = "PINNED: Owned the GraphQL gateway for partner integrations."
    resume["projects"][0]["pinned_bullets"] = [pinned]
    resume["projects"][0]["bullets"] = [pinned] + resume["projects"][0]["bullets"]
    resume["experience"] = list(reversed(resume["experience"]))
    selected = select_resume_content(resume, JD_SDE, ResumeRenderer(), max_pages=1)
    bullets = [b for p in selected["projects"] for b in p.get("bullets") or []]
    assert any("PINNED" in b for b in bullets)


def test_missing_jd_still_preserves_sections():
    resume = many_jobs_one_project()
    selected = select_resume_content(resume, None, ResumeRenderer(), max_pages=1)
    assert selected.get("projects")
    assert selected.get("experience")


def test_selection_failure_preserves_content_not_silent_reduction(monkeypatch):
    resume = many_jobs_one_project()
    from services import content_selection as cs

    def boom(*_a, **_k):
        raise RuntimeError("renderer down")

    monkeypatch.setattr(cs, "_measure", boom)
    try:
        selected = select_resume_content(resume, JD_SDE, ResumeRenderer(), max_pages=1)
    except RuntimeError:
        return
    assert selected.get("projects")


def test_user_section_omission_is_the_only_way_to_drop_projects():
    resume = many_jobs_one_project()
    selected = select_resume_content(
        resume, JD_SDE, ResumeRenderer(),
        preferences={"omitted_sections": ["projects"]},
        max_pages=1,
    )
    if not selected.get("projects"):
        manifest = selected.get("content_manifest") or {}
        items = (manifest.get("items") or {}).values()
        assert any(
            (it or {}).get("user_omitted") or (it or {}).get("reason") == "user_omission"
            for it in items
            if (it or {}).get("semantic_type") == "projects"
        )


def test_repeated_selection_is_stable():
    resume = many_jobs_one_project()
    r = ResumeRenderer()
    first = select_resume_content(resume, JD_SDE, r, max_pages=1)
    second = select_resume_content(first, JD_SDE, r, max_pages=1)
    assert [p.get("name") for p in first.get("projects") or []] == [
        p.get("name") for p in second.get("projects") or []
    ]


def test_preview_pdf_docx_agree_on_project_name():
    resume = many_jobs_one_project()
    selected = select_resume_content(resume, JD_SDE, ResumeRenderer(), max_pages=1)
    name = selected["projects"][0]["name"]
    r = ResumeRenderer()
    token = name.split("|")[0].strip()
    assert token in _pdf_text(r.generate_pdf(selected)[0])
    assert token in _docx_text(r.generate_docx(selected))


def test_preservation_check_catches_dropped_project_section():
    source = many_jobs_one_project()
    gutted = copy.deepcopy(source)
    gutted["projects"] = []
    result = assert_preservation(source, gutted)
    assert not result.ok
    assert any("project" in e.lower() for e in result.errors)


def test_cv_like_length_does_not_silently_choose_two_pages():
    resume = many_jobs_one_project()
    resume["page_length"] = "one"
    resume["additional_sections"] = [{
        "heading": "Publications",
        "original_heading": "PUBLICATIONS",
        "semantic_type": "publications",
        "layout_supported": "generic",
        "parse_status": "ok",
        "raw_text": "",
        "entries": [{
            "title": f"Paper {i} on clinical operations",
            "organization": "Journal",
            "dates": "2020",
            "bullets": [],
            "body": (
                "A long-form academic abstract describing methods, results, "
                "and limitations of a multi-site study."
            ),
        } for i in range(8)],
    }]
    selected = select_resume_content(resume, JD_SDE, ResumeRenderer(), max_pages=1)
    outcome = (selected.get("content_selection") or {}).get("page_outcome")
    assert outcome in {
        "fit_one", "one_page_not_possible", "needs_shortening_choice", "preservation_failed",
    }
    if outcome == "fit_two":
        pytest.fail("one-page preference silently chose two pages")
    extras = selected.get("additional_sections") or []
    assert any(s.get("semantic_type") == "publications" for s in extras)


def test_manifest_roundtrip_keeps_source_snapshot():
    resume = many_jobs_one_project()
    selected = select_resume_content(resume, JD_SDE, ResumeRenderer(), max_pages=1)
    snap = (selected.get("content_manifest") or {}).get("source_snapshot") or {}
    assert snap.get("projects")
    assert snap["projects"][0]["name"] == resume["projects"][0]["name"]
