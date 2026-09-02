"""Remaining plan increments: flags, layout plan, confirmations, writers."""
import os

from services.guest_service import cached_file_is_current
from services.plan_writers import experience_writer
from services.profile_facts import build_fact_store, confirm_review_skills
from services.resume_renderer import ResumeRenderer
from utils import feature_flags
from utils.db_connect import DBConnect


def test_flag_defaults_are_identical_across_environments(monkeypatch):
    snapshot = dict(feature_flags.DEFAULTS)
    for env in ("development", "production", "test", "staging", ""):
        monkeypatch.setenv("ENVIRONMENT", env)
        os.environ["ENVIRONMENT"] = env
        assert feature_flags.DEFAULTS == snapshot
        assert feature_flags.enabled("ALLOW_TWO_PAGE") is True
        assert feature_flags.enabled("QUALITY_REFLECTION_GATE") is False
        assert feature_flags.enabled("JD_ANALYZER_V2") is True
        assert feature_flags.enabled("PLAN_GUIDED_WRITERS") is True


def test_pdf_and_docx_share_one_layout_plan():
    tailored = {
        "_page_format": "Letter",
        "contact": {"name": "Ada Lovelace", "email": "ada@example.com"},
        "summary": "Mathematician writing analytical engines.",
        "skills": {"Languages": ["English"]},
        "experience": [{
            "title": "Analyst", "company": "Babbage", "location": "London",
            "dates": "1842 - 1843", "type": "Full-time",
            "bullets": ["Wrote the first algorithm for the Analytical Engine"],
        }],
        "education": [{"degree": "Private", "institution": "Home", "dates": "1835"}],
        "projects": [],
        "certifications": [],
        "licenses": [],
        "content_plan": {"credentials_first": False, "section_order": ["summary", "experience"]},
    }
    renderer = ResumeRenderer()
    a = renderer.layout_plan(tailored)
    b = renderer.layout_plan(tailored)
    assert a["body_size"] == b["body_size"]
    assert a["body_size"] >= 10.0
    assert a["max_pages"] in (1, 2)
    pdf = renderer.generate_pdf(tailored)
    docx = renderer.generate_docx(tailored)
    assert pdf and len(pdf) > 200
    assert docx and len(docx) > 200
    assert cached_file_is_current(
        {"s3_key": "x", "render_version": renderer.RENDER_VERSION, "content_hash": "abc"},
        renderer.RENDER_VERSION,
        "abc",
    )


def test_confirm_review_skills_are_reusable_facts():
    db = DBConnect().get_db()
    db["enrichment_confirmations"].delete_many({"user_email": "nurse@example.com"})
    n = confirm_review_skills("nurse@example.com", [
        {"term": "Epic", "status": "confirmed"},
        {"term": "Kubernetes", "status": "rejected"},
    ])
    assert n == 2
    store = build_fact_store(
        user_email="nurse@example.com",
        structured_resume={"skills": {"Clinical": ["Med-Surg"]}, "experience": []},
    )
    displays = " ".join(f.get("display", "") for f in store["facts"])
    assert "Epic" in displays
    assert "Kubernetes" not in displays


def test_experience_writer_respects_bullet_budget():
    result = {
        "experience": [{
            "title": "Nurse", "company": "County",
            "bullets": ["a", "b", "c", "d", "e"],
        }],
    }
    experience_writer(result, {"bullet_budget": {"bullets_per_role": 3}})
    assert result["experience"][0]["bullets"] == ["a", "b", "c"]
