"""§6.12.9 eval — ATS roundtrip: structured resume → PDF → extracted text.

Contract under test (services/pdf_ats_check.py + services/resume_renderer.py,
fully offline — fpdf2 renders, pypdf extracts, no network):
  * The rendered PDF has a real text layer (extraction_ok).
  * Every key fact survives the roundtrip and is findable the way a real ATS
    finds it: candidate name, email, employers, job titles, every skill
    keyword, and every experience date range token (Workday-style regex
    scan).
  * All four routing section headers (SUMMARY / EXPERIENCE / SKILLS /
    EDUCATION) are present and in the expected order.
  * No glyph corruption (ligatures / undefined-char boxes) is introduced.

Hard-fail criteria: any missing contact field, missing/misordered section,
unextractable skill or date token, glyph corruption, or a parseability score
below the ship floor (90).
"""
from services.pdf_ats_check import check_pdf_parseability, extract_pdf_text
from services.resume_renderer import ResumeRenderer

TAILORED = {
    "contact": {
        "name": "Priya Raman",
        "email": "priya.raman@example.com",
        "phone": "480-555-0134",
        "location": "Tempe, AZ",
        "linkedin": "linkedin.com/in/priyaraman",
        "github": "github.com/priyaraman",
    },
    "summary": ("Backend engineer with four years building event-driven "
                "data platforms on AWS."),
    "experience": [
        {
            "company": "Vantage Analytics",
            "location": "Tempe, AZ",
            "title": "Senior Backend Engineer",
            "dates": "May 2023 - Present",
            "bullets": [
                "Rewrote the nightly ingestion consumers in Kafka and cut "
                "end-to-end lag from nine minutes to forty seconds.",
                "Moved the checkpoint store to PostgreSQL so replays became "
                "deterministic.",
                "Cut the deploy pipeline from 25 minutes to 4 minutes.",
            ],
        },
        {
            "company": "Bluegrid Systems",
            "location": "Austin, TX",
            "title": "Backend Engineer",
            "dates": "Jun 2021 - Apr 2023",
            "bullets": [
                "Built the metering service that priced 2 million events a "
                "day.",
                "Reduced API p99 latency from 900ms to 180ms.",
            ],
        },
    ],
    "projects": [
        {
            "name": "Chronicle",
            "dates": "2024",
            "bullets": [
                "Built an open-source changelog generator adopted by 40 "
                "repositories.",
            ],
        },
    ],
    "skills": {
        "Languages": ["Python", "TypeScript", "SQL"],
        "Data": ["Kafka", "PostgreSQL", "Redis"],
        "Cloud": ["AWS Lambda", "Terraform", "Docker"],
    },
    "education": [
        {
            "institution": "Arizona State University",
            "degree": "M.S. Computer Science",
            "dates": "2019 - 2021",
            "location": "Tempe, AZ",
        },
    ],
}

KEY_FACTS = [
    "Priya Raman",                    # candidate
    "priya.raman@example.com",        # reply channel
    "Vantage Analytics",              # employers
    "Bluegrid Systems",
    "Senior Backend Engineer",        # titles
    "Backend Engineer",
    "Arizona State University",       # education
    "M.S. Computer Science",
]


def _render():
    return ResumeRenderer().generate_pdf(TAILORED)


def test_pdf_renders_with_a_real_text_layer():
    pdf = _render()
    assert isinstance(pdf, bytes) and pdf.startswith(b"%PDF")
    text, pages = extract_pdf_text(pdf)
    assert pages >= 1
    assert text.strip(), "no extractable text layer — an ATS would reject this"


def test_key_facts_survive_the_roundtrip():
    text, _ = extract_pdf_text(_render())
    low = text.lower()
    for fact in KEY_FACTS:
        assert fact.lower() in low, f"key fact lost in roundtrip: {fact!r}"
    # every skill keyword, individually
    for group in TAILORED["skills"].values():
        for skill in group:
            assert skill.lower() in low, f"skill lost in roundtrip: {skill!r}"
    # every ATS date token (what Workday/Greenhouse regex-scan for)
    for token in ("May 2023", "Present", "Jun 2021", "Apr 2023"):
        assert token.lower() in low, f"date token lost: {token!r}"


def test_parseability_checks_all_green():
    pdf = _render()
    report = check_pdf_parseability(pdf, TAILORED)
    checks = report["checks"]

    assert checks["extraction_ok"] is True
    assert checks["contact_name_found"] is True
    assert checks["contact_email_found"] is True
    assert checks["contact_at_top"] is True
    assert checks["sections_missing"] == []
    assert checks["sections_in_order"] is True
    assert checks["skills_keywords_found"] == checks["skills_keywords_total"] > 0
    assert (checks["experience_dates_parseable"]
            == checks["experience_dates_total"] == 2)
    assert checks["no_glyph_corruption"] is True

    assert report["pdf_parseable_score"] >= 90, (
        f"score {report['pdf_parseable_score']} below ship floor; "
        f"warnings: {report['warnings']}"
    )


def test_checker_catches_a_broken_pdf():
    """The checker itself must fail loudly on garbage — a checker that
    passes everything protects nothing."""
    report = check_pdf_parseability(b"not a pdf at all", TAILORED)
    assert report["pdf_parseable_score"] == 0
    assert report["checks"]["extraction_ok"] is False
    assert report["warnings"], "a rejected PDF must carry actionable warnings"
