"""PDF and DOCX must both emit certifications, licenses, and project tech."""
from services.resume_renderer import ResumeRenderer
from schemas.resume_schemas import build_resume_text


def _resume():
    return {
        "contact": {
            "name": "Jordan Alvarez", "email": "j@example.com",
            "phone": "555-0100", "linkedin": "", "github": "",
            "location": "Tempe, AZ", "portfolio": "",
        },
        "summary": "Registered nurse with acute-care experience.",
        "skills": {"Clinical": ["Triage", "IV therapy"]},
        "experience": [{
            "title": "Staff Nurse", "company": "Valley Clinic",
            "location": "Phoenix, AZ", "dates": "Jan 2020 - Present",
            "type": "Full-time",
            "bullets": ["Managed a 12-bed telemetry unit."],
        }],
        "education": [{
            "degree": "BSN", "institution": "ASU", "location": "Tempe",
            "dates": "2019", "gpa": "", "coursework": "Pathophysiology",
        }],
        "projects": [{
            "name": "Care coordination pilot", "dates": "2023",
            "tech": "Epic, Excel",
            "bullets": ["Built a handoff checklist used by two units."],
        }],
        "certifications": ["BLS", "ACLS"],
        "licenses": ["RN - Arizona"],
        "_page_format": "Letter",
    }


def test_build_resume_text_includes_certs_and_licenses():
    text = build_resume_text(_resume())
    assert "BLS" in text
    assert "RN - Arizona" in text
    assert "Pathophysiology" in text


def test_pdf_and_docx_emit_same_material_facts():
    r = ResumeRenderer()
    tailored = _resume()
    pdf = r.generate_pdf(tailored)
    docx = r.generate_docx(tailored)
    assert pdf and pdf.startswith(b"%PDF")
    assert docx and len(docx) > 1000

    from services.pdf_ats_check import extract_pdf_text
    extracted, _pages = extract_pdf_text(pdf)
    for token in ("BLS", "ACLS", "RN - Arizona", "Epic", "Pathophysiology", "Full-time"):
        assert token in extracted, f"{token!r} missing from PDF text"

    # DOCX text layer
    import io
    from docx import Document
    doc = Document(io.BytesIO(docx))
    docx_text = "\n".join(p.text for p in doc.paragraphs)
    for token in ("BLS", "ACLS", "RN - Arizona", "Epic", "Pathophysiology", "Full-time"):
        assert token in docx_text, f"{token!r} missing from DOCX text"
    assert "Professional Summary" not in docx_text


def test_body_font_never_below_10pt():
    r = ResumeRenderer()
    layout = r._compute_pdf_layout(_resume())
    assert layout["body_size"] >= 10.0
