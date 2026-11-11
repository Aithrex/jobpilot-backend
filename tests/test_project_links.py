"""Project links: only a link the uploaded résumé shows, never one a model chose."""
import copy
import io

from services import project_links as pl
from services.integrity_guard import IntegrityGuard
from services.resume_renderer import ResumeRenderer


def _resume(projects, contact=None):
    return {
        "contact": contact or {
            "name": "Priya Raman", "email": "priya@example.com", "phone": "(408) 555-0134",
            "linkedin": "https://linkedin.com/in/priya", "github": "https://github.com/priya",
            "location": "San Jose, CA", "portfolio": "",
        },
        "summary": "",
        "experience": [{
            "title": "Backend Engineering Intern", "company": "Ledgerline", "location": "San Jose, CA",
            "dates": "Jun 2025 - Aug 2025", "type": "",
            "bullets": ["Built Python services for payment reconciliation used by the finance team."],
        }],
        "projects": projects,
        "skills": {"Languages": ["Python"]},
        "education": [{"degree": "MS Computer Science", "institution": "San Jose State University",
                       "location": "San Jose, CA", "dates": "May 2026", "gpa": "", "coursework": ""}],
        "certifications": [],
    }


def _proj(name, url=None, bullets=None):
    p = {"name": name, "dates": "2026", "tech": "Python",
         "bullets": bullets or [f"Built {name} with Python and tests for every endpoint."]}
    if url is not None:
        p["url"] = url
    return p


RAW = """Priya Raman
priya@example.com | linkedin.com/in/priya | github.com/priya
PROJECTS
Notes Q&A Bot
Built a retrieval bot over course notes.
Campus Events App  demo: campus-events.vercel.app
Event listings for 300 students.
[Extracted Link: https://github.com/priya/notes-qa-bot]
[Extracted Link: https://github.com/priya]
[Extracted Link: https://linkedin.com/in/priya]
"""


# --- attaching -------------------------------------------------------------

def test_hyperlink_whose_slug_names_the_project_is_attached():
    r = _resume([_proj("Notes Q&A Bot"), _proj("Campus Events App")])
    r, attached = pl.attach_project_links(r, RAW)
    urls = {p["name"]: p["url"] for p in r["projects"]}
    assert pl.normalize(urls["Notes Q&A Bot"]) == "github.com/priya/notes-qa-bot"
    assert {a["basis"] for a in attached} >= {"slug_names_project"}


def test_url_printed_inside_the_project_block_goes_to_that_project_only():
    r = _resume([_proj("Notes Q&A Bot"), _proj("Campus Events App")])
    r, _ = pl.attach_project_links(r, RAW)
    urls = {p["name"]: pl.normalize(p["url"]) for p in r["projects"]}
    assert urls["Campus Events App"] == "campus-events.vercel.app"
    assert urls["Notes Q&A Bot"] != "campus-events.vercel.app"


def test_profile_links_are_never_project_links():
    raw = "PROJECTS\nPriya Tools\nSmall scripts.\n[Extracted Link: https://github.com/priya]\n" \
          "[Extracted Link: https://linkedin.com/in/priya]\n"
    r = _resume([_proj("Priya Tools")])
    r, attached = pl.attach_project_links(r, raw)
    assert r["projects"][0]["url"] == "" and attached == []


def test_a_link_the_document_does_not_hold_is_cleared():
    r = _resume([_proj("Notes Q&A Bot", url="https://github.com/priya/invented-repo")])
    r, _ = pl.attach_project_links(r, RAW)
    # The invented link is gone; the real one the document holds takes its place.
    assert pl.normalize(r["projects"][0]["url"]) == "github.com/priya/notes-qa-bot"
    r2 = _resume([_proj("Unrelated Thing", url="https://github.com/priya/invented-repo")])
    r2, _ = pl.attach_project_links(r2, RAW)
    assert r2["projects"][0]["url"] == ""


def test_one_link_is_never_given_to_two_projects():
    raw = "PROJECTS\nNotes Bot\nx\nNotes Bot v2\ny\n[Extracted Link: https://github.com/priya/notes-bot]\n"
    r = _resume([_proj("Notes Bot"), _proj("Notes Bot v2")])
    r, _ = pl.attach_project_links(r, raw)
    assert sum(1 for p in r["projects"] if p["url"]) == 1


def test_backfill_mode_keeps_a_stored_link():
    r = _resume([_proj("Notes Q&A Bot", url="https://gitlab.com/priya/notes")])
    r, _ = pl.attach_project_links(r, "no links here", validate_existing=False)
    assert r["projects"][0]["url"] == "https://gitlab.com/priya/notes"


def test_link_formatting_helpers():
    assert pl.display("https://www.github.com/priya/x/") == "github.com/priya/x"
    assert pl.href("github.com/priya/x") == "https://github.com/priya/x"
    assert pl.is_link("github.com/priya/x") and not pl.is_link("priya@example.com")
    assert pl.link_of({"url": "not a link"}) is None


# --- tailoring can never invent or redirect a link -------------------------

def test_guard_restores_the_original_link_and_drops_an_invented_one():
    original = _resume([_proj("Notes Q&A Bot", url="https://github.com/priya/notes-qa-bot"),
                        _proj("Campus Events App", url="")])
    tailored = copy.deepcopy(original)
    tailored["projects"][0].pop("url")                               # writer omitted it
    tailored["projects"][1]["url"] = "https://github.com/priya/fake"  # writer invented one
    fixed, report = IntegrityGuard().enforce(original, tailored, {})
    by_name = {p["name"]: p["url"] for p in fixed["projects"]}
    assert by_name["Notes Q&A Bot"] == "https://github.com/priya/notes-qa-bot"
    assert by_name["Campus Events App"] == ""
    assert "projects[Campus Events App].url" in report.immutable_overwrites
    assert "projects[Notes Q&A Bot].url" not in report.immutable_overwrites


def test_schema_keeps_the_field_and_defaults_it():
    from schemas.resume_schemas import PARSED_RESUME_SCHEMA, validate_and_coerce
    out = validate_and_coerce(_resume([_proj("A Project")]), PARSED_RESUME_SCHEMA)
    assert out["projects"][0]["url"] == ""
    out2 = validate_and_coerce(_resume([_proj("A Project", url="github.com/priya/a")]), PARSED_RESUME_SCHEMA)
    assert out2["projects"][0]["url"] == "github.com/priya/a"


# --- rendering ---------------------------------------------------------------

def _pdf_text_and_uris(pdf_bytes):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    uris = []
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            obj = annot.get_object()
            action = obj.get("/A")
            if action and action.get("/URI"):
                uris.append(str(action.get("/URI")))
    return text, uris


def test_pdf_shows_the_link_and_makes_it_clickable():
    r = _resume([_proj("Notes Q&A Bot", url="https://github.com/priya/notes-qa-bot")])
    pdf, _ = ResumeRenderer().generate_pdf(r)
    text, uris = _pdf_text_and_uris(pdf)
    assert "github.com/priya/notes-qa-bot" in text.replace(" ", "")
    assert "https://github.com/priya/notes-qa-bot" in uris


def test_pdf_long_name_puts_the_link_on_its_own_line():
    long_name = "A Very Long Distributed Retrieval Evaluation Platform For Course Notes And Lectures"
    r = _resume([_proj(long_name, url="https://github.com/priya/a-very-long-distributed-retrieval-platform")])
    pdf, _ = ResumeRenderer().generate_pdf(r)
    text, uris = _pdf_text_and_uris(pdf)
    assert "github.com/priya/a-very-long-distributed-retrieval-platform" in text.replace(" ", "").replace("\n", "")
    assert any("a-very-long-distributed-retrieval-platform" in u for u in uris)


def test_pdf_without_a_link_is_unchanged():
    without = _resume([_proj("Notes Q&A Bot")])
    empty = _resume([_proj("Notes Q&A Bot", url="")])
    a, _ = ResumeRenderer().generate_pdf(without)
    b, _ = ResumeRenderer().generate_pdf(empty)
    assert _pdf_text_and_uris(a)[0] == _pdf_text_and_uris(b)[0]


def test_docx_shows_the_link_text():
    from docx import Document
    r = _resume([_proj("Notes Q&A Bot", url="https://github.com/priya/notes-qa-bot")])
    data = ResumeRenderer().generate_docx(r)
    text = "\n".join(p.text for p in Document(io.BytesIO(data)).paragraphs)
    assert "Notes Q&A Bot | github.com/priya/notes-qa-bot" in text


def test_regenerate_keeps_the_link_the_user_typed():
    original = _resume([_proj("Notes Q&A Bot", url="")])
    current = _resume([_proj("Notes Q&A Bot", url="github.com/priya/notes-qa-bot")])
    regenerated = _resume([_proj("Notes Q&A Bot", url="https://github.com/priya/model-guess")])
    fixed, _ = IntegrityGuard().enforce(original, regenerated, {})
    assert fixed["projects"][0]["url"] == ""          # the guard drops the model's guess
    pl.carry_current_links(current, fixed)
    assert fixed["projects"][0]["url"] == "github.com/priya/notes-qa-bot"


def test_regenerate_keeps_a_deleted_link_deleted_and_ignores_junk():
    original = _resume([_proj("A", url="https://github.com/priya/a"), _proj("B")])
    current = _resume([_proj("A", url=""), _proj("B", url="not a link")])
    target = copy.deepcopy(original)
    pl.carry_current_links(current, target)
    assert [p["url"] for p in target["projects"]] == ["", ""]


def test_regenerate_applies_the_carry_after_the_guard():
    """Wiring check: the legacy regenerate path calls the carry right after the
    integrity guard (driving the whole regenerate needs six stubbed model calls)."""
    import inspect
    from services.resume_tailor import ResumeTailor
    src = inspect.getsource(ResumeTailor.regenerate)
    guard_at = src.index("self._guard.enforce(structured_resume, validated, jd_analysis)")
    carry_at = src.index("carry_current_links(current_tailored, corrected)")
    assert guard_at < carry_at
