"""Content selection: no silent third-bullet truncation; preview==export."""
from __future__ import annotations

import copy
import io

from docx import Document
from pypdf import PdfReader

from schemas.resume_schemas import build_resume_text
from services.content_selection import compress_bullet, select_resume_content
from services.requirement_assessment import evaluate_requirements
from services.resume_renderer import ResumeRenderer


def _base_resume(**kwargs):
    resume = {
        "contact": {
            "name": "Alex Rivera", "email": "a@example.com", "phone": "480-555-0100",
            "linkedin": "", "github": "", "location": "Tempe, AZ", "portfolio": "",
        },
        "summary": "Backend engineer with production experience.",
        "skills": {"Languages": ["Python", "SQL"], "Cloud": ["AWS"]},
        "experience": [{
            "title": "Software Engineer",
            "company": "Acme",
            "location": "Remote",
            "dates": "2022 - Present",
            "type": "Full-time",
            "bullets": [
                "Built FastAPI services on AWS with PostgreSQL and Redis.",
                "Owned CI/CD with GitHub Actions and Docker.",
                "Improved API latency for customer-facing traffic.",
            ],
        }],
        "projects": [{
            "name": "Health Data Hub",
            "dates": "2024",
            "tech": "Python",
            "bullets": [
                "Implemented ingestion pipelines for clinical FHIR events.",
                "Added monitoring dashboards for pipeline health.",
                "Deployed the service with Kubernetes and Helm in production.",
            ],
        }],
        "education": [{
            "institution": "State University", "degree": "B.S. CS",
            "dates": "2020", "location": "AZ",
        }],
        "certifications": [],
    }
    resume.update(kwargs)
    return resume


JD = {
    "required_skills": ["Kubernetes", "Python", "FastAPI"],
    "keywords": ["FHIR", "AWS"],
    "preferred_skills": [],
}


def test_third_project_bullet_with_unique_required_capability_is_kept():
    resume = _base_resume()
    resume["experience"][0]["bullets"] = resume["experience"][0]["bullets"] * 2
    selected = select_resume_content(resume, JD, ResumeRenderer(), max_pages=1)
    proj_bullets = selected["projects"][0]["bullets"]
    assert any("Kubernetes" in b for b in proj_bullets), proj_bullets


def test_repetitive_bullets_do_not_crowd_out_complementary_evidence():
    resume = _base_resume()
    resume["projects"][0]["bullets"] = [
        "Improved pipeline reliability for clinical events.",
        "Improved pipeline reliability across clinical workloads.",
        "Deployed the service with Kubernetes and Helm in production.",
    ]
    selected = select_resume_content(resume, JD, ResumeRenderer(), max_pages=1)
    text = " ".join(selected["projects"][0]["bullets"])
    assert "Kubernetes" in text


def test_pinned_bullets_and_user_omissions_survive():
    resume = _base_resume()
    pinned = "PINNED: Owned the GraphQL gateway for partner integrations."
    resume["projects"][0]["pinned_bullets"] = [pinned]
    resume["projects"][0]["bullets"] = [
        pinned,
        "Implemented ingestion pipelines for clinical FHIR events.",
        "Deployed the service with Kubernetes and Helm in production.",
        "Added monitoring dashboards for pipeline health.",
    ]
    prefs = {
        "omitted_bullet_texts": [
            "Added monitoring dashboards for pipeline health.",
        ],
    }
    selected = select_resume_content(
        resume, JD, ResumeRenderer(), preferences=prefs, max_pages=2,
    )
    bullets = selected["projects"][0]["bullets"]
    assert pinned in bullets
    assert not any("monitoring dashboards" in b.lower() for b in bullets)


def test_compression_preserves_accomplishment_and_scope():
    long = (
        "Successfully and effectively built production Kubernetes services "
        "in order to deploy clinical APIs carefully with Helm charts."
    )
    out = compress_bullet(long)
    assert "Kubernetes" in out
    assert "Helm" in out
    assert "clinical" in out.lower()
    assert len(out) <= len(long)


def test_preview_pdf_docx_share_selected_bullets():
    resume = _base_resume()
    selected = select_resume_content(resume, JD, ResumeRenderer(), max_pages=1)
    expected = list(selected["projects"][0]["bullets"])
    r = ResumeRenderer()
    pdf, res = r.generate_pdf(selected)
    assert not any(d.kind == "project_bullet" for d in res.dropped)
    pdf_text = "\n".join(
        (p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages
    )
    for b in expected:
        assert b.split()[0] in pdf_text
    docx = r.generate_docx(selected)
    doc = Document(io.BytesIO(docx))
    docx_text = "\n".join(p.text for p in doc.paragraphs)
    for b in expected:
        assert any(tok in docx_text for tok in b.split()[:3])


def test_excluded_bullets_do_not_contribute_to_final_coverage():
    resume = _base_resume()
    prefs = {
        "omitted_bullet_texts": [
            "Deployed the service with Kubernetes and Helm in production.",
        ],
    }
    selected = select_resume_content(
        resume, JD, ResumeRenderer(), preferences=prefs, max_pages=2,
    )
    assert "Kubernetes" not in build_resume_text(selected)
    out = evaluate_requirements(
        document_text=build_resume_text(selected),
        jd={"requirements": [{
            "text": "Kubernetes", "priority": "required", "kind": "capability",
        }]},
        resume=selected,
    )
    assert out["ledger"]["requirements"][0]["document_state"] in {
        "not_demonstrated", "unknown",
    }


def test_renderer_does_not_silently_trim_third_project_bullet():
    resume = _base_resume()
    resume["experience"] = [
        {
            "title": f"Engineer {i}",
            "company": f"Co {i}",
            "location": "Remote",
            "dates": f"202{i} - 202{i}",
            "bullets": [
                f"Delivered platform work stream {i}.{j} with measurable reliability gains "
                f"across APIs, data pipelines, and on-call ownership for regulated systems."
                for j in range(4)
            ],
        }
        for i in range(3)
    ]
    resume["projects"] = [{
        "name": "Health Data Hub",
        "bullets": [
            "First project bullet about ingestion.",
            "Second project bullet about dashboards.",
            "Third unique: production Kubernetes and Helm deployment.",
        ],
    }]
    r = ResumeRenderer()
    working, dropped = r._trim_to_fit(copy.deepcopy(resume), max_pages=1)
    assert working["projects"][0]["bullets"] == resume["projects"][0]["bullets"]
    assert dropped == []
    pdf, res = r.generate_pdf(resume)
    text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)
    assert "Kubernetes" in text
    assert not any(d.kind == "project_bullet" for d in res.dropped)


def test_important_overflow_gets_explicit_page_outcome_not_silent_loss():
    resume = _base_resume(page_length="one")
    resume["experience"] = [
        {
            "title": f"Engineer {i}",
            "company": f"Co {i}",
            "dates": "2020 - 2021",
            "bullets": [
                f"Long accomplishment {i}.{j} describing architecture, ownership, "
                f"reliability, customer impact, and cross-team delivery in production."
                for j in range(5)
            ],
        }
        for i in range(4)
    ]
    selected = select_resume_content(resume, JD, ResumeRenderer(), max_pages=1)
    outcome = (selected.get("content_selection") or {}).get("page_outcome")
    assert outcome in {
        "fit_one", "fit_two", "one_page_not_possible", "needs_shortening_choice",
    }
    if outcome != "fit_one":
        assert (selected.get("content_selection") or {}).get("disclosure")
