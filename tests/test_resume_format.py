"""Résumé format preferences: length, spacing, accent, section order.

Pinned first: no preferences renders exactly what rendered before, and a
format change never adds a line the résumé did not already hold.
"""
import copy
import io
from datetime import date

import pytest

from services import resume_format as rf
from services.content_selection import select_resume_content
from services.resume_renderer import ResumeRenderer


def _bullet(i, role):
    return (f"Built and maintained {role} service number {i} in Python and PostgreSQL, "
            f"cutting the weekly report run from {40 + i} minutes to {10 + i} minutes for the finance team.")


def _resume(*, edu_dates="May 2018", exp_years=(2019, 2025), roles=4, bullets=6, summary=True):
    exp = []
    start, end = exp_years
    span = max(1, (end - start) // max(1, roles))
    for r in range(roles):
        a = end - (r + 1) * span
        b = end - r * span
        exp.append({
            "title": f"Software Engineer {r + 1}", "company": f"Company {r + 1}", "location": "Austin, TX",
            "dates": f"Jan {a} - Dec {b}" if r else f"Jan {a} - Present", "type": "",
            "bullets": [_bullet(i, f"role{r}") for i in range(bullets)],
        })
    return {
        "contact": {"name": "Priya Raman", "email": "priya@example.com", "phone": "(408) 555-0134",
                    "linkedin": "linkedin.com/in/priya", "github": "github.com/priya",
                    "location": "San Jose, CA", "portfolio": ""},
        "summary": ("Backend engineer building Python services and data pipelines on AWS." if summary else ""),
        "experience": exp,
        "projects": [{"name": "Notes Q&A Bot", "dates": "2026", "tech": "Python, FAISS", "url": "",
                      "bullets": ["Built a retrieval bot over course notes with FAISS and the OpenAI API."]}],
        "skills": {"Languages": ["Python", "SQL", "Go"], "Cloud": ["AWS", "Docker", "Kubernetes"]},
        "education": [{"degree": "MS Computer Science", "institution": "San Jose State University",
                       "location": "San Jose, CA", "dates": edu_dates, "gpa": "", "coursework": ""}],
        "certifications": [],
    }


def _pdf_text(pdf_bytes):
    from pypdf import PdfReader
    return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf_bytes)).pages)


def _bullets(resume):
    return [b for sec in ("experience", "projects") for e in resume.get(sec) or [] for b in e.get("bullets") or []]


# --- preferences ---------------------------------------------------------------

def test_normalize_keeps_known_values_and_drops_the_rest():
    assert rf.normalize_preferences({"page_length": "2", "density": "Comfortable", "accent": "navy",
                                     "section_order": "education-first", "font": "Comic Sans",
                                     "locked_text": {"x": "y"}}) == {
        "page_length": "two", "density": "comfortable", "accent": "navy", "section_order": "education_first"}
    assert rf.normalize_preferences({"page_length": "three"}) == {}
    assert rf.normalize_preferences("two") == {}


def test_apply_preferences_keeps_other_tailoring_preferences():
    r = {"tailoring_preferences": {"locked_fact_ids": ["f1"]}}
    rf.apply_preferences(r, {"page_length": "two"})
    assert r["tailoring_preferences"] == {"locked_fact_ids": ["f1"], "page_length": "two"}


def test_no_preferences_means_the_default_renderer():
    r = ResumeRenderer()
    assert r.for_preferences({}) is r
    assert r.for_preferences({"density": "compact", "accent": "black"}) is r
    navy = r.for_preferences({"accent": "navy"})
    assert navy is not r and navy.spec.accent_rgb == (0, 79, 144)
    assert r.for_preferences({"accent": "navy"}) is navy  # cached


# --- the default output is unchanged -------------------------------------------

def test_default_choices_render_exactly_as_before():
    base = _resume()
    explicit = copy.deepcopy(base)
    explicit["tailoring_preferences"] = {"density": "compact", "accent": "black",
                                         "section_order": "experience_first"}
    a, res_a = ResumeRenderer().generate_pdf(base)
    b, res_b = ResumeRenderer().generate_pdf(explicit)
    assert _pdf_text(a) == _pdf_text(b)
    assert res_a.tier.body_pt == res_b.tier.body_pt


# --- accent --------------------------------------------------------------------

def test_navy_accent_reaches_pdf_and_docx():
    r = _resume()
    r["tailoring_preferences"] = {"accent": "navy"}
    pdf, _ = ResumeRenderer().generate_pdf(r)
    plain, _ = ResumeRenderer().generate_pdf(_resume())
    assert pdf != plain and _pdf_text(pdf) == _pdf_text(plain)  # colour only
    from docx import Document
    doc = Document(io.BytesIO(ResumeRenderer().generate_docx(r)))
    xml = doc.element.xml
    assert 'w:val="004F90"' in xml


# --- section order -------------------------------------------------------------

def test_education_first_for_a_current_student():
    student = _resume(edu_dates="Aug 2024 - Expected May 2026", exp_years=(2025, 2025), roles=1, bullets=3)
    student["experience"][0]["dates"] = "Jun 2025 - Aug 2025"
    assert rf.education_first(student, today=date(2026, 1, 15))
    text = _pdf_text(ResumeRenderer().generate_pdf(student)[0]).upper()
    assert text.index("EDUCATION") < text.index("EXPERIENCE")


def test_experience_first_for_an_experienced_engineer():
    senior = _resume(edu_dates="May 2016", exp_years=(2017, 2025))
    assert not rf.education_first(senior, today=date(2026, 1, 15))
    text = _pdf_text(ResumeRenderer().generate_pdf(senior)[0]).upper()
    assert text.index("EXPERIENCE") < text.index("EDUCATION")


def test_recent_graduate_with_long_experience_keeps_experience_first():
    # Graduated last month, but four years of prior work: experience leads.
    r = _resume(edu_dates="Dec 2025", exp_years=(2020, 2025), roles=2)
    assert not rf.education_first(r, today=date(2026, 1, 15))


def test_an_explicit_order_wins():
    senior = _resume(edu_dates="May 2016", exp_years=(2017, 2025))
    senior["tailoring_preferences"] = {"section_order": "education_first"}
    assert rf.education_first(senior)
    text = _pdf_text(ResumeRenderer().generate_pdf(senior)[0]).upper()
    assert text.index("EDUCATION") < text.index("EXPERIENCE")
    from docx import Document
    paras = [p.text.upper() for p in Document(io.BytesIO(ResumeRenderer().generate_docx(senior))).paragraphs]
    assert paras.index("EDUCATION") < paras.index("EXPERIENCE")


# --- spacing: comfortable trims lines, it does not shrink type -----------------

def test_comfortable_never_goes_below_its_floor_and_keeps_half_inch_margins():
    dense = _resume(roles=4, bullets=6)
    dense["tailoring_preferences"] = {"density": "comfortable", "page_length": "one"}
    selected = select_resume_content(dense, renderer=ResumeRenderer())
    _, res = ResumeRenderer().generate_pdf(selected)
    assert res.tier.body_pt >= 9.7 - 1e-9
    assert res.gaps.margin_lr >= 0.5 * 25.4 - 1e-6
    assert res.pages == 1


def test_comfortable_keeps_fewer_lines_than_compact_on_one_page():
    dense = _resume(roles=4, bullets=6)
    compact = select_resume_content(copy.deepcopy(dense), renderer=ResumeRenderer(),
                                    preferences={"page_length": "one"})
    roomy = select_resume_content(copy.deepcopy(dense), renderer=ResumeRenderer(),
                                  preferences={"page_length": "one", "density": "comfortable"})
    assert len(_bullets(roomy)) < len(_bullets(compact))
    assert set(_bullets(roomy)) <= set(_bullets(dense))


# --- page length -----------------------------------------------------------------

def test_moving_to_two_pages_restores_the_users_own_dropped_lines():
    dense = _resume(roles=4, bullets=6)
    one = select_resume_content(copy.deepcopy(dense), renderer=ResumeRenderer(),
                                preferences={"page_length": "one"})
    assert one["content_selection"]["omitted_bullets"], "fixture must overflow one page"
    two = rf.reformat(one, {"page_length": "two"}, renderer=ResumeRenderer())
    assert two["tailoring_preferences"]["page_length"] == "two"
    assert len(_bullets(two)) > len(_bullets(one))
    assert set(_bullets(two)) <= set(_bullets(dense))  # nothing new
    _, res = ResumeRenderer().generate_pdf(two)
    assert res.pages == 2
    back = rf.reformat(two, {"page_length": "one"}, renderer=ResumeRenderer())
    _, res_back = ResumeRenderer().generate_pdf(back)
    assert res_back.pages == 1


def test_restore_omitted_ignores_bad_rows():
    r = _resume(roles=1, bullets=1)
    r["content_selection"] = {"omitted_bullets": [
        {"section": "experience", "entry_idx": 0, "text": "Kept line."},
        {"section": "experience", "entry_idx": 9, "text": "No such entry."},
        {"section": "contact", "entry_idx": 0, "text": "Wrong section."},
        {"section": "experience", "entry_idx": 0, "text": r["experience"][0]["bullets"][0]},
    ]}
    out, n = rf.restore_omitted(r)
    assert n == 1 and out["experience"][0]["bullets"][-1] == "Kept line."


def test_two_pages_never_pads(monkeypatch):
    from services.content_augmenter import ContentAugmenter

    calls = []
    aug = ContentAugmenter(ResumeRenderer(), project_generator=None)
    monkeypatch.setattr(aug, "_expand_bullets", lambda *a, **k: calls.append("expand") or a[0])
    monkeypatch.setattr(aug, "_inject_impact", lambda t, jd: t)
    monkeypatch.setattr(aug, "_ats_harden", lambda t, jd: t)
    sparse = _resume(roles=1, bullets=2)
    sparse["tailoring_preferences"] = {"page_length": "two"}
    out = aug.augment(sparse, copy.deepcopy(sparse), {}, allow_project_generation=False)
    assert calls == []
    assert out["_projects_diagnostic"]["phase_gated"] in {"two_pages_no_padding",
                                                           "projects_experience_first_policy"}
    one = _resume(roles=1, bullets=2)
    aug.augment(one, copy.deepcopy(one), {}, allow_project_generation=False)
    assert calls, "one page still expands as before"


# --- tailoring keeps the choice -----------------------------------------------------

def test_regenerate_and_tailor_carry_preferences():
    import inspect
    from services.resume_tailor import ResumeTailor
    regen = inspect.getsource(ResumeTailor.regenerate)
    assert regen.index("self._guard.enforce(") < regen.index('corrected["tailoring_preferences"]') \
        < regen.index("self._allocate_bullets(corrected")
    tailor = inspect.getsource(ResumeTailor.tailor)
    assert "apply_preferences(t, format_prefs)" in tailor


# --- the route -----------------------------------------------------------------------

@pytest.fixture
def client():
    from _vg_support import load_service_app
    app = load_service_app("resume")
    from flask_jwt_extended import create_access_token
    with app.app_context():
        token = create_access_token(identity="format-route@example.test")
    return app.test_client(), {"Authorization": f"Bearer {token}"}


def test_format_route_refits_and_validates(client):
    c, headers = client
    one = select_resume_content(_resume(roles=4, bullets=6), renderer=ResumeRenderer(),
                                preferences={"page_length": "one"})
    resp = c.post("/api/resume/format", json={"tailored_resume": one,
                                              "preferences": {"page_length": "two"}}, headers=headers)
    assert resp.status_code == 200, resp.get_json()
    out = resp.get_json()["tailored_resume"]
    assert out["tailoring_preferences"]["page_length"] == "two"
    assert len(_bullets(out)) > len(_bullets(one))
    assert c.post("/api/resume/format", json={"tailored_resume": one, "preferences": {}},
                  headers=headers).status_code == 400
    assert c.post("/api/resume/format", json={"preferences": {"page_length": "two"}},
                  headers=headers).status_code == 400


# --- prompts ---------------------------------------------------------------------------

def test_role_focus_is_short_and_only_for_known_archetypes():
    from services.resume_tailor import ResumeTailor
    focus = ResumeTailor._build_role_focus({"job_title": "Founding Engineer"})
    assert focus.startswith("ROLE FOCUS (founding engineer)") and "never add one" in focus
    assert len(focus) < 400
    assert ResumeTailor._build_role_focus({"job_title": "Data Analyst"}) == ""


def test_scorer_prompt_is_not_anchored_to_a_band():
    import inspect
    from services import resume_scorer
    src = inspect.getsource(resume_scorer)
    assert "85-95" not in src and "should score below 80" not in src
