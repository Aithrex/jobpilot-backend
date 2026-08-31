"""Renderer bidirectional density — sparse resumes scale UP to fill the page,
dense resumes keep the shrink tiers. Real fpdf2 measure passes, no LLM.

These assert the SINGLE-PAGE shrink engine (the default/prod contract), so they
force ALLOW_TWO_PAGE off. Two-page behavior is covered by test_two_page.py.
"""
import pytest

from services import resume_renderer
from services.resume_renderer import ResumeRenderer


@pytest.fixture(autouse=True)
def _force_single_page(monkeypatch):
    monkeypatch.setattr(resume_renderer, "ALLOW_TWO_PAGE", False, raising=False)


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


def test_sparse_resume_refuses_the_up_tier():
    """A resume with roughly half a page of content must NOT up-tier. Bigger
    type on a page that will still be mostly empty only makes the emptiness
    louder — the sparse floor drops it back to the professional 10pt base."""
    r = ResumeRenderer()
    body, lh, lh_s, height = r._choose_up_tier(SPARSE)
    assert body == 10.0
    assert height < r._AVAIL_H * r._SPARSE_FILL_FLOOR


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


def test_docx_renders_with_times(tmp_path):
    from docx import Document
    import io

    data = ResumeRenderer().generate_docx(SPARSE)
    doc = Document(io.BytesIO(data))
    assert doc.styles["Normal"].font.name == "Times New Roman"
    text = "\n".join(p.text for p in doc.paragraphs)
    # Name renders in Title Case (e.g. "Surya Kommuguri", not all caps).
    assert "Surya Kommuguri" in text
    assert "EXPERIENCE" in text


def test_pdf_embeds_tinos():
    """The PDF must carry the embedded Tinos family (Times metrics, per the
    user's Overleaf template) — if the TTFs go missing the fallback silently
    changes the product look."""
    pdf_bytes = ResumeRenderer().generate_pdf(SPARSE)
    assert b"Tinos" in pdf_bytes


def test_pdf_name_renders_title_case():
    """Name renders in Title Case (e.g. "Surya Kommuguri", not all caps) —
    and the parseability checker must still find it in the text layer."""
    from services.pdf_ats_check import extract_pdf_text

    pdf_bytes = ResumeRenderer().generate_pdf(SPARSE)
    text, _ = extract_pdf_text(pdf_bytes)
    assert "Surya Kommuguri" in text


# ---------------------------------------------------------------------------
# Plain-text contacts (product decision: no hyperlinks in either format)
# ---------------------------------------------------------------------------

def test_pdf_contacts_are_plain_text():
    """No link annotations anywhere in the PDF — email/LinkedIn render as
    plain black text."""
    pdf_bytes = ResumeRenderer().generate_pdf(SPARSE)
    assert b"/URI" not in pdf_bytes


def test_docx_contacts_are_plain_text():
    import io
    from docx import Document

    data = ResumeRenderer().generate_docx(SPARSE)
    doc = Document(io.BytesIO(data))
    hyperlink_rels = [
        rel for rel in doc.part.rels.values() if "hyperlink" in rel.reltype
    ]
    assert not hyperlink_rels
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "s@example.com" in text            # email still present, as text
    assert "linkedin.com/in/surya" in text


# ---------------------------------------------------------------------------
# Pass-3 leading stretch — page fill for resumes between the font rungs
# ---------------------------------------------------------------------------

# The dead-band shape this fix exists for: content that just misses the 11pt
# up-tier (like the user's real 2-role 8+6-bullet resume) used to render at
# 10.5pt with ~25% of the page empty because the gap ceilings could only
# absorb ~27mm.
MID_DENSITY = _resume(n_roles=2, bullets_per_role=7)


def test_mid_density_fills_page():
    r = ResumeRenderer()
    layout = r._compute_pdf_layout(MID_DENSITY)
    fill = layout["predicted_height"] / r._AVAIL_H
    assert fill >= 0.95, f"dead-band case must fill the page (got {fill:.1%})"
    # stretch stays within the leading ceiling
    assert layout["stretch"] <= r._STRETCH_CAP + 1e-9
    assert layout["lh"] * layout["stretch"] <= r._MAX_LEADING_MM + 1e-6


def test_sparse_resume_renders_compactly_not_padded():
    """A genuinely thin resume is laid out at its natural density and simply
    ends where it ends. Saturating every knob at once — 11pt type, all five
    gaps at their ceilings, 1.40x leading — still left a blank band, so the
    page read as padded AND unfinished at the same time."""
    r = ResumeRenderer()
    layout = r._compute_pdf_layout(SPARSE)
    assert layout["body_size"] == 10.0
    assert layout["stretch"] == 1.0
    assert layout["section_gap"] == pytest.approx(r._MIN_SECTION_GAP)
    assert layout["entry_gap"] == pytest.approx(r._MIN_ENTRY_GAP)
    assert layout["post_header"] == pytest.approx(r._MIN_POST_HEADER)
    assert layout["header_gap"] == pytest.approx(r._MIN_HEADER_GAP)
    assert layout["skill_gap"] == pytest.approx(r._MIN_SKILL_GAP)
    # It does not pretend to fill the page.
    assert layout["predicted_height"] / r._AVAIL_H < r._FILL_RAMP_LO


def test_fill_ramp_leaves_fillable_pages_alone():
    """The ramp must only bite on pages that CANNOT be filled honestly — the
    dead-band and senior shapes still reach the bottom."""
    r = ResumeRenderer()
    for resume in (MID_DENSITY, SENIOR):
        layout = r._compute_pdf_layout(resume)
        assert layout["predicted_height"] / r._AVAIL_H >= 0.95


def test_heading_and_job_gaps_stay_tight():
    """Pass-2 leftover must not open holes under headings, between roles,
    or between sections (SUMMARY → EXPERIENCE, etc.)."""
    r = ResumeRenderer()
    for resume in (SPARSE, MID_DENSITY, SENIOR, DENSITY_STRESS):
        layout = r._compute_pdf_layout(resume)
        assert layout["post_header"] >= r._MIN_POST_HEADER - 1e-9
        assert layout["post_header"] <= r._MAX_POST_HEADER + 1e-9
        assert layout["entry_gap"] <= r._MAX_ENTRY_GAP + 1e-9
        assert layout["section_gap"] <= r._MAX_SECTION_GAP + 1e-9
        assert layout["post_header"] <= 1.6 + 1e-9
        assert layout["entry_gap"] <= 2.2 + 1e-9
        assert layout["section_gap"] <= 2.8 + 1e-9
        # skill_gap repeats once per category, so its ceiling costs
        # n_categories x the gap — it is held tighter than the rest.
        assert layout["skill_gap"] >= r._MIN_SKILL_GAP - 1e-9
        assert layout["skill_gap"] <= r._MAX_SKILL_GAP + 1e-9


def test_dense_resume_never_stretches():
    r = ResumeRenderer()
    layout = r._compute_pdf_layout(DENSITY_STRESS)
    assert layout["stretch"] == 1.0
    assert layout["post_header"] >= r._MIN_POST_HEADER - 1e-9


def test_stretch_still_single_page():
    """Stretched renders must not spill to page 2."""
    from services.pdf_ats_check import check_pdf_parseability

    pdf_bytes = ResumeRenderer().generate_pdf(MID_DENSITY)
    check = check_pdf_parseability(pdf_bytes, MID_DENSITY, {})
    assert check["checks"]["page_count"] == 1
    assert check["pdf_parseable_score"] >= 85


# ---------------------------------------------------------------------------
# Skills block spacing
# ---------------------------------------------------------------------------

_MEASURE_KNOBS = dict(
    section_gap=1.2, entry_gap=0.9, post_header=1.0, header_gap=0.5,
    body_size=10.0, lh=3.4, lh_s=3.08, measure_only=True,
)


def test_skill_gap_renders_between_categories_only():
    """skill_gap separates categories, so a one-category block has no gaps at
    all and the knob must not move the page height. It used to fire after the
    last category too, stacking a trailing gap onto the next section_gap."""
    r = ResumeRenderer()
    one = dict(SPARSE, skills={"Languages": ["Python", "SQL", "Go"]})

    _, tight_h = r._render_pdf(one, skill_gap=0.4, **_MEASURE_KNOBS)
    _, loose_h = r._render_pdf(one, skill_gap=3.0, **_MEASURE_KNOBS)
    assert tight_h == pytest.approx(loose_h, abs=1e-6)

    two = dict(SPARSE, skills={
        "Languages": ["Python", "SQL", "Go"],
        "Cloud & DevOps": ["AWS", "Docker"],
    })
    _, tight_2 = r._render_pdf(two, skill_gap=0.4, **_MEASURE_KNOBS)
    _, loose_2 = r._render_pdf(two, skill_gap=3.0, **_MEASURE_KNOBS)
    assert loose_2 - tight_2 == pytest.approx(2.6, abs=1e-6)  # exactly one gap


def test_skills_leading_capped_against_the_stretch():
    """Skills rows are dense comma lists — they must not take the body's full
    Pass-3 leading, or a sparse page renders them at ~5mm per line.

    Measured by differencing two resumes that vary only in category count, so
    the delta is purely skills lines.
    """
    r = ResumeRenderer()
    knobs = dict(_MEASURE_KNOBS, skill_gap=0.0, stretch=r._STRETCH_CAP)
    one = dict(SPARSE, skills={"A": ["Python"]})
    five = dict(SPARSE, skills={k: ["Python"] for k in "ABCDE"})

    _, h1 = r._render_pdf(one, **knobs)
    _, h5 = r._render_pdf(five, **knobs)

    per_line = (h5 - h1) / 4
    # Uncapped this would be lh_s * stretch = 3.08 * 1.40 = 4.31mm.
    assert per_line == pytest.approx(r._MAX_SKILL_LEADING, abs=1e-6)


# ---------------------------------------------------------------------------
# Adaptive skills-section title
# ---------------------------------------------------------------------------

def test_skills_title_whitelist():
    from services.pdf_ats_check import extract_pdf_text

    r = ResumeRenderer()
    core = dict(SPARSE, skills_section_title="Core Skills")
    text, _ = extract_pdf_text(r.generate_pdf(core))
    assert "CORE SKILLS" in text

    # Unknown/hostile values fall back to the default heading.
    junk = dict(SPARSE, skills_section_title="Best Stuff Ever")
    text, _ = extract_pdf_text(r.generate_pdf(junk))
    assert "TECHNICAL SKILLS" in text
