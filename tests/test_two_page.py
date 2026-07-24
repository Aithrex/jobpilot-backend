"""Two-page rendering (Craft-overhaul Phase 5, opt-in ALLOW_TWO_PAGE).

A dense senior resume renders on two pages at a readable 10pt instead of being
crammed to 9pt on one page — but ONLY when the flag is on, and ONLY when content
genuinely overflows. Flag off (prod default) must be byte-identical single-page.
Real fpdf2 render, no LLM.
"""
import io

import pytest
from pypdf import PdfReader

from services import resume_renderer
from services.resume_renderer import ResumeRenderer


def _bullet(i):
    return (f"Delivered a large-scale distributed platform improvement number {i} "
            f"by refactoring the ingestion pipeline and tuning database indexes for "
            f"high-throughput production workloads across multiple regions.")


def _role(company, n_bullets):
    return {"company": company, "title": "Senior Staff Engineer",
            "dates": "2019 - Present",
            "bullets": [_bullet(i) for i in range(n_bullets)]}


def _dense_senior(n_roles=6, bullets=6):
    return {
        "contact": {"name": "Senior Person", "email": "s@x.com",
                    "phone": "555-0100", "location": "New York, NY"},
        "summary": ("Staff engineer with 15 years building large distributed "
                    "systems across many domains at global scale."),
        "skills": {"Languages": ["Python", "Java", "Go", "Rust", "C++"],
                   "Cloud": ["AWS", "GCP", "Azure", "Kubernetes", "Docker", "Terraform"]},
        "experience": [_role(f"Company{i}", bullets) for i in range(n_roles)],
        "education": [{"degree": "M.S.", "institution": "MIT", "dates": "2008"}],
        "projects": [], "certifications": [],
    }


def _normal():
    return {
        "contact": {"name": "Regular Person", "email": "r@x.com", "location": "Remote"},
        "summary": "Backend engineer with 4 years building services.",
        "skills": {"Lang": ["Python", "SQL"]},
        "experience": [_role("Acme", 3)],
        "education": [{"degree": "B.S.", "institution": "State U", "dates": "2019"}],
        "projects": [], "certifications": [],
    }


def _pages(pdf_bytes):
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def test_flag_off_dense_resume_stays_one_page(monkeypatch):
    monkeypatch.setattr(resume_renderer, "ALLOW_TWO_PAGE", False, raising=False)
    pdf = ResumeRenderer().generate_pdf(_dense_senior())
    assert _pages(pdf) == 1  # shrink cascade forces one page (unchanged behavior)


def test_flag_on_dense_resume_uses_two_pages(monkeypatch):
    monkeypatch.setattr(resume_renderer, "ALLOW_TWO_PAGE", True, raising=False)
    pdf = ResumeRenderer().generate_pdf(_dense_senior())
    assert _pages(pdf) == 2  # flows to a second page at readable font


def test_flag_on_normal_resume_still_one_page(monkeypatch):
    # Two-page is opt-in for OVERFLOW, not forced. A normal resume that fits one
    # page must still render one page even with the flag on.
    monkeypatch.setattr(resume_renderer, "ALLOW_TWO_PAGE", True, raising=False)
    pdf = ResumeRenderer().generate_pdf(_normal())
    assert _pages(pdf) == 1


def test_two_page_output_is_parseable(monkeypatch):
    monkeypatch.setattr(resume_renderer, "ALLOW_TWO_PAGE", True, raising=False)
    pdf = ResumeRenderer().generate_pdf(_dense_senior())
    reader = PdfReader(io.BytesIO(pdf))
    text = "".join(p.extract_text() or "" for p in reader.pages)
    # Content from both the first and last roles survives (nothing evicted).
    assert "Company0" in text and "Company5" in text
    assert "SENIOR PERSON" in text.upper()  # renderer uppercases the name


def test_allocator_keeps_more_bullets_with_two_page_budget():
    # The allocator must not evict a dense senior resume's bullets down to one
    # page when given a two-page budget.
    import copy
    from services.bullet_allocator import allocate_bullets
    resume = _dense_senior(n_roles=6, bullets=6)

    one = allocate_bullets(copy.deepcopy(resume), ResumeRenderer(),
                           jd_analysis={}, max_pages=1)
    two = allocate_bullets(copy.deepcopy(resume), ResumeRenderer(),
                           jd_analysis={}, max_pages=2)

    def _count(r):
        return sum(len(e.get("bullets", [])) for e in r.get("experience", []))

    assert _count(two) > _count(one), "two-page budget should retain more bullets"
