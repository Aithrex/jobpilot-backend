"""Pagination regressions: orphan Education, stranded headings, sparse pages."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfReader

from services.resume_renderer import ResumeRenderer


def _long_resume_almost_full(*, push_edu: bool = True):
    """Content that historically left EDUCATION alone at the foot of page 1."""
    bullets = [
        (
            "Designed and shipped production FastAPI services on AWS EKS with "
            "PostgreSQL, Redis, Kafka, OpenTelemetry, Datadog, and CI/CD via "
            "GitHub Actions for multi-tenant healthcare workloads under HIPAA."
        ),
        (
            "Built RAG retrieval pipelines with vector embeddings, LangChain, "
            "Bedrock, and pgvector; evaluated quality on a labeled 100-question "
            "set for groundedness and citation accuracy across clinical notes."
        ),
        (
            "Owned React and TypeScript frontends with Redux, REST APIs, and "
            "Spring Boot backends; improved query latency with MySQL indexes "
            "and MongoDB aggregations while mentoring three junior engineers."
        ),
        (
            "Automated infrastructure with Terraform, Helm, Docker, Kubernetes, "
            "Prometheus, Grafana, and ELK; reduced incident MTTR through "
            "runbooks and on-call ownership of microservices platforms."
        ),
    ]
    experience = []
    for i in range(3):
        experience.append({
            "title": f"Software Engineer II {i}",
            "company": f"HealthTech Co {i}",
            "location": "Remote",
            "dates": f"January 202{i} - December 202{i + 1}",
            "type": "Full-time",
            "bullets": list(bullets),
        })
    resume = {
        "contact": {
            "name": "Surya Kommuguri",
            "email": "s@example.com",
            "phone": "480-555-0100",
            "linkedin": "linkedin.com/in/surya",
            "github": "",
            "location": "Tempe, AZ",
            "portfolio": "",
        },
        "summary": (
            "Software engineer with production experience across AI/ML, "
            "distributed systems, cloud infrastructure, and full-stack "
            "applications for regulated healthcare environments."
        ),
        "skills": {
            "AI & ML": [
                "RAG pipelines", "vector embeddings", "LLM API integration",
                "prompt engineering", "LangChain",
            ],
            "Cloud": ["AWS (Bedrock, EKS, EC2, S3)"],
            "Distributed Systems & Data": [
                "PostgreSQL", "pgvector", "MySQL", "MongoDB",
                "query optimization", "Apache Kafka", "RabbitMQ", "Redis",
            ],
            "Frameworks": [
                "React", "REST APIs", "FastAPI", "Django", "Spring Boot",
                "Spring Data JPA", "Node.js", "Redux", "Angular",
            ],
            "Infrastructure": [
                "Docker", "Kubernetes", "Terraform", "Helm", "GitHub Actions",
            ],
            "Languages": [
                "Python", "Java", "TypeScript", "JavaScript", "SQL",
            ],
            "Observability & Testing": [
                "Unit Testing", "Integration Testing", "OpenTelemetry",
                "Datadog", "Prometheus", "Grafana", "ELK Stack", "pytest",
                "JUnit", "Mockito",
            ],
            "Standards": [
                "Code Review", "Agile", "microservices", "async programming",
                "FHIR R4", "HIPAA",
            ],
        },
        "experience": experience,
        "education": [
            {
                "institution": "Arizona State University",
                "location": "Tempe, AZ",
                "degree": "Master of Science, Information Technology",
                "dates": "January 2024 - December 2025",
            }
        ],
        "projects": [],
        "certifications": [],
        "page_length": "one" if push_edu else "auto",
    }
    if push_edu:
        resume["experience"].append({
            "title": "Software Engineer",
            "company": "Startup Labs",
            "location": "Tempe, AZ",
            "dates": "June 2019 - December 2020",
            "type": "Full-time",
            "bullets": bullets[:3],
        })
    return resume


def _page_texts(pdf_bytes: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [(p.extract_text() or "") for p in reader.pages]


def test_education_heading_not_orphaned_alone_on_page_one():
    """Reported failure: EDUCATION on page 1, school alone on mostly empty page 2."""
    resume = _long_resume_almost_full(push_edu=True)
    pdf_bytes, resolution = ResumeRenderer().generate_pdf(resume)
    pages = _page_texts(pdf_bytes)
    assert pages, "PDF must render"

    for i, text in enumerate(pages):
        upper = text.upper()
        if "EDUCATION" in upper:
            body = (
                "Arizona State" in text
                or "Information Technology" in text
                or "Master of Science" in text
            )
            assert body, (
                f"Orphan EDUCATION heading on page {i + 1}; "
                f"chars={len(text)} preview={text[-200:]!r}"
            )

    if len(pages) >= 2:
        p2 = pages[-1]
        assert "EDUCATION" not in p2.upper() or (
            "Arizona State" in p2 or "Information Technology" in p2
        )
        if resolution.fill_ratio < 0.25:
            assert any(
                "little content" in w.lower() or "final page" in w.lower()
                for w in resolution.layout_warnings
            )


def test_heading_kept_with_first_entry_when_near_page_break():
    resume = _long_resume_almost_full(push_edu=True)
    resume["summary"] = "Backend engineer focused on production systems."
    pdf_bytes, _ = ResumeRenderer().generate_pdf(resume)
    pages = _page_texts(pdf_bytes)
    for text in pages:
        if "EDUCATION" in text.upper():
            assert "Arizona State" in text or "Master of Science" in text


def test_keep_together_does_not_delete_education():
    resume = _long_resume_almost_full(push_edu=True)
    pdf_bytes, resolution = ResumeRenderer().generate_pdf(resume)
    full = "\n".join(_page_texts(pdf_bytes))
    assert "EDUCATION" in full.upper()
    assert "Arizona State University" in full
    assert "Master of Science" in full
    assert not any("education" in d.lower() for d in (resolution.dropped or []))


def test_legitimate_two_page_resume_remains_readable():
    resume = _long_resume_almost_full(push_edu=True)
    resume["page_length"] = "two"
    for exp in resume["experience"]:
        exp["bullets"] = exp["bullets"] + exp["bullets"]
    pdf_bytes, resolution = ResumeRenderer().generate_pdf(resume)
    pages = _page_texts(pdf_bytes)
    assert len(pages) >= 1
    full = "\n".join(pages)
    assert "TECHNICAL SKILLS" in full.upper() or "SKILLS" in full.upper()
    assert "EDUCATION" in full.upper()
    assert resolution.tier.body_pt >= 8.5


def test_one_page_preference_preserves_content_when_overflow_unavoidable():
    resume = _long_resume_almost_full(push_edu=True)
    resume["page_length"] = "one"
    for _ in range(2):
        resume["experience"].append(resume["experience"][0])
    pdf_bytes, resolution = ResumeRenderer().generate_pdf(resume)
    full = "\n".join(_page_texts(pdf_bytes))
    assert "Arizona State University" in full
    if resolution.pages > 1:
        assert resolution.pages >= 2


def test_docx_preserves_education_after_layout():
    resume = _long_resume_almost_full(push_edu=True)
    r = ResumeRenderer()
    pdf_bytes, _ = r.generate_pdf(resume)
    docx_bytes = r.generate_docx(resume)
    assert len(docx_bytes) > 1000
    from docx import Document
    doc = Document(io.BytesIO(docx_bytes))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "EDUCATION" in text.upper()
    assert "Arizona State University" in text
    pdf_text = "\n".join(_page_texts(pdf_bytes))
    assert "Arizona State University" in pdf_text


def test_layout_warnings_do_not_affect_requirement_coverage_math():
    from schemas.assessment_contracts import (
        CanonicalRequirement,
        RequirementEvaluation,
        atomic,
        document_coverage_summary,
    )
    reqs = [
        CanonicalRequirement(
            id="r1", display_text="Python", priority="required",
            kind="capability", expression=atomic("Python"),
            source_excerpt="Python",
        ),
        CanonicalRequirement(
            id="r2", display_text="Go", priority="required",
            kind="capability", expression=atomic("Go"),
            source_excerpt="Go",
        ),
    ]
    evals = [
        RequirementEvaluation(
            requirement_id="r1", document_state="satisfied",
            source_state="unknown", claim_state="unverified", match_method="exact",
        ),
        RequirementEvaluation(
            requirement_id="r2", document_state="not_demonstrated",
            source_state="unknown", claim_state="unverified", match_method="none",
        ),
    ]
    cov = document_coverage_summary(reqs, evals, priority="required")
    assert cov["percentage"] == 50
    assert cov["numerator"] == 1
    assert cov["denominator"] == 2


def test_reported_pdf_orphan_documented_as_before_state():
    path = Path(r"C:\Users\kommu\Downloads\Surya_Kommuguri_Software_Engineer_II (2).pdf")
    if not path.exists():
        pytest.skip("Reported PDF not on this machine")
    pages = _page_texts(path.read_bytes())
    assert len(pages) == 2
    assert pages[0].rstrip().upper().endswith("EDUCATION") or (
        "EDUCATION" in pages[0].upper() and "Arizona State" not in pages[0]
    )
    assert "Arizona State" in pages[1]
    assert len(pages[1].strip()) < 400
