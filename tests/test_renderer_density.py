"""Renderer bidirectional density — sparse resumes scale UP to fill the page,
dense resumes keep the shrink tiers. Real fpdf2 measure passes, no LLM.
"""
import pytest

from services.resume_renderer import ResumeRenderer


def _bullet(i):
    return (
        f"Delivered measurable platform improvement number {i} by refactoring the "
        f"ingestion service and tuning PostgreSQL indexes for clinical workloads."
    )


def _resume(n_roles, bullets_per_role, n_projects=0, bullets_per_project=0):
    return {
        "contact": {
            "name": "Surya Kommuguri", "email": "s@example.com",
            "phone": "480-555-0100", "linkedin": "linkedin.com/in/surya",
            "github": "", "location": "Tempe, AZ", "portfolio": "",
        },
        "summary": (
            "Backend engineer with production experience building Python "
            "microservices, REST APIs, and event-driven systems across healthcare "
            "platforms on AWS and Kubernetes."
        ),
        "skills": {
            "Cloud & Infrastructure": ["AWS", "Docker", "Kubernetes", "Helm"],
            "Databases": ["PostgreSQL", "MySQL", "MongoDB"],
            "Languages & Frameworks": ["Python", "FastAPI", "Django", "SQL"],
            "Observability": ["Grafana", "Datadog APM", "ELK Stack", "Kafka"],
        },
        "experience": [
            {
                "title": f"Software Engineer {r}", "company": f"Company {r}",
                "location": "Tempe, AZ",
                "dates": f"January 20{20 + r} - December 20{21 + r}",
                "type": "Full-time",
                "bullets": [_bullet(f"{r}.{b}") for b in range(bullets_per_role)],
            }
            for r in range(n_roles)
        ],
        "education": [{
            "degree": "Master of Science in Information Technology",
            "institution": "Arizona State University", "location": "Tempe, AZ",
            "dates": "January 2024 - December 2025", "gpa": "", "coursework": "",
        }],
        "projects": [
            {
                "name": f"Telemetry Toolkit {p}", "dates": "",
                "bullets": [_bullet(f"p{p}.{b}") for b in range(bullets_per_project)],
                "tech": "Python, Kafka",
            }
            for p in range(n_projects)
        ],
        "certifications": [],
    }


SPARSE = _resume(n_roles=2, bullets_per_role=4)                  # experienced, no projects

# Content-volume STRESS shape (not a realistic persona — the experience-first
# policy means a senior never gets 3 generated projects). Guards the ladder's
# refusal to inflate when the page is already full.
DENSITY_STRESS = _resume(n_roles=3, bullets_per_role=6, n_projects=3, bullets_per_project=4)


def _senior(role_bullet_counts):
    """Realistic senior shape: several roles, recency-weighted bullet depth,
    NO projects section (experience-first policy)."""
    resume = _resume(len(role_bullet_counts), 1)
    for i, n in enumerate(role_bullet_counts):
        resume["experience"][i]["bullets"] = [
            _bullet(f"{i}.{b}") for b in range(n)
        ]
    resume["projects"] = []
    return resume


SENIOR = _senior([6, 5, 4, 3])  # 4 roles, 18 bullets, experience-only


def test_sparse_resume_scales_up():
    r = ResumeRenderer()
    body, lh, lh_s, height = r._choose_up_tier(SPARSE)
    assert body == 11.0, "sparse resume should take the largest presentation tier"
    assert height <= r._AVAIL_H


def test_density_stress_stays_at_base_or_below():
    r = ResumeRenderer()
    body, lh, lh_s, height = r._choose_up_tier(DENSITY_STRESS)
    assert body == 10.0, "over-full content must not inflate"


def test_realistic_senior_fills_one_page():
    """4 roles at 6/5/4/3 bullets, no projects — professional density (10pt,
    content-rich resumes never inflate) that fills the page without spilling."""
    from services.pdf_ats_check import check_pdf_parseability

    r = ResumeRenderer()
    body, _, _, height = r._choose_up_tier(SENIOR)
    assert body == 10.0  # content-rich: base size, gaps absorb the slack
    assert height / r._AVAIL_H >= 0.80

    pdf = r.generate_pdf(SENIOR)
    check = check_pdf_parseability(pdf, SENIOR, {})
    assert check["checks"]["page_count"] == 1
    assert check["pdf_parseable_score"] >= 85


def test_up_tier_measures_monotonic():
    """Bigger presentation must measure taller — guards the ladder ordering."""
    r = ResumeRenderer()
    h11 = r._measure_min(SPARSE, 11.0, 4.00, 3.60)
    h10 = r._measure_min(SPARSE, 10.0, 3.40, 3.08)
    assert h11 > h10


@pytest.mark.parametrize(
    "resume", [SPARSE, DENSITY_STRESS, SENIOR], ids=["sparse", "stress", "senior"]
)
def test_render_single_page_and_parseable(resume):
    """Full render at the new margins/font stays one page and ATS-parseable."""
    from services.pdf_ats_check import check_pdf_parseability

    pdf_bytes = ResumeRenderer().generate_pdf(resume)
    check = check_pdf_parseability(pdf_bytes, resume, {})
    assert check["checks"]["page_count"] == 1
    assert check["checks"]["extraction_ok"] is True
    assert check["checks"]["no_glyph_corruption"] is True
    assert check["pdf_parseable_score"] >= 85


def test_docx_renders_with_calibri(tmp_path):
    from docx import Document
    import io

    data = ResumeRenderer().generate_docx(SPARSE)
    doc = Document(io.BytesIO(data))
    assert doc.styles["Normal"].font.name == "Calibri"
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Surya Kommuguri" in text
    assert "EXPERIENCE" in text


def test_pdf_embeds_carlito():
    """The PDF must carry the embedded Carlito family (Calibri metrics) —
    if the TTFs go missing the fallback silently changes the product look."""
    pdf_bytes = ResumeRenderer().generate_pdf(SPARSE)
    assert b"Carlito" in pdf_bytes
