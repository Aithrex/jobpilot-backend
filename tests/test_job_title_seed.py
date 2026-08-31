"""Job-search title seed: union ALL parsed resumes, LLM only on fingerprint change."""
from datetime import datetime, timezone, timedelta

import pytest

from _vg_support import current_db

from services import resume_access
from services.job_title_seed import (
    fingerprint_assets,
    merge_structured_for_titles,
    public_title_seed,
    suggest_filters_cached,
    union_titles_from_assets,
)


USER = "jordan@example.com"
COLLECTIONS = ("resume_assets", "users", "job_filter_preferences")


def _backend_structured():
    return {
        "contact": {"name": "Jordan Alvarez", "email": USER, "location": "Austin, TX"},
        "summary": "Backend engineer.",
        "skills": {"languages": ["Python", "SQL"], "cloud": ["AWS", "Docker"]},
        "experience": [{
            "title": "Backend Engineer", "company": "Acme",
            "dates": "Jan 2019 - Jan 2025",
            "bullets": ["Built Python services on AWS with Docker and PostgreSQL."],
        }],
        "education": [{"degree": "BS Computer Science", "institution": "UT"}],
        "projects": [],
        "certifications": [],
    }


def _nurse_structured():
    return {
        "contact": {"name": "Jordan Alvarez", "email": USER, "location": "Columbus, OH"},
        "summary": "Registered nurse in acute care.",
        "skills": {"clinical": ["Patient care", "Medication administration", "EHR"]},
        "experience": [{
            "title": "Registered Nurse", "company": "Mercy Hospital",
            "dates": "Jun 2017 - Jun 2025",
            "bullets": ["Delivered direct patient care and medication administration."],
        }],
        "education": [{"degree": "BSN Nursing", "institution": "Ohio State"}],
        "projects": [],
        "certifications": ["RN License, Ohio"],
    }


def _data_structured():
    return {
        "contact": {"name": "Jordan Alvarez", "email": USER},
        "summary": "Data engineer.",
        "skills": {"languages": ["Python", "SQL"], "data": ["Spark", "dbt"]},
        "experience": [{
            "title": "Data Engineer", "company": "Lakehouse Co",
            "dates": "Jan 2021 - Jan 2026",
            "bullets": ["Built Spark pipelines."],
        }],
        "education": [],
        "projects": [],
        "certifications": [],
    }


def _insert_asset(db, *, resume_id, filename, structured, parsed_at=None, email=USER):
    now = parsed_at or datetime.now(timezone.utc)
    db.resume_assets.insert_one({
        "resume_id": resume_id,
        "user_email": email,
        "owner_type": "user",
        "filename": filename,
        "parse_status": "parsed",
        "structured": structured,
        "object_key": f"resume-assets/{resume_id}/original",
        "created_at": now,
        "parsed_at": now,
    })


@pytest.fixture()
def db():
    d = current_db()
    for c in COLLECTIONS:
        d[c].delete_many({})
    d.users.insert_one({"email": USER, "active_resume_id": "ra-nurse"})
    yield d
    for c in COLLECTIONS:
        d[c].delete_many({})


def test_union_includes_titles_from_all_parsed_resumes(db):
    t0 = datetime.now(timezone.utc)
    _insert_asset(
        db, resume_id="ra-be", filename="backend.pdf",
        structured=_backend_structured(), parsed_at=t0,
    )
    _insert_asset(
        db, resume_id="ra-nurse", filename="nurse.pdf",
        structured=_nurse_structured(), parsed_at=t0 + timedelta(minutes=1),
    )
    _insert_asset(
        db, resume_id="ra-data", filename="data.pdf",
        structured=_data_structured(), parsed_at=t0 + timedelta(minutes=2),
    )

    titles = union_titles_from_assets(resume_access.list_parsed_assets(USER))
    assert "Backend Engineer" in titles
    assert "Registered Nurse" in titles
    assert "Data Engineer" in titles


def test_union_skips_pending_and_other_users(db):
    _insert_asset(
        db, resume_id="ra-be", filename="backend.pdf",
        structured=_backend_structured(),
    )
    db.resume_assets.insert_one({
        "resume_id": "ra-pending",
        "user_email": USER,
        "filename": "pending.pdf",
        "parse_status": "pending",
        "structured": None,
        "created_at": datetime.now(timezone.utc),
    })
    _insert_asset(
        db, resume_id="ra-other", filename="other.pdf",
        structured=_nurse_structured(), email="mallory@example.com",
    )

    titles = union_titles_from_assets(resume_access.list_parsed_assets(USER))
    assert titles == ["Backend Engineer"]


def test_fingerprint_stable_until_resume_set_changes(db):
    t0 = datetime.now(timezone.utc)
    _insert_asset(
        db, resume_id="ra-be", filename="backend.pdf",
        structured=_backend_structured(), parsed_at=t0,
    )
    _insert_asset(
        db, resume_id="ra-nurse", filename="nurse.pdf",
        structured=_nurse_structured(), parsed_at=t0,
    )
    a = fingerprint_assets(resume_access.list_parsed_assets(USER))
    b = fingerprint_assets(resume_access.list_parsed_assets(USER))
    assert a and a == b

    _insert_asset(
        db, resume_id="ra-data", filename="data.pdf",
        structured=_data_structured(), parsed_at=t0 + timedelta(hours=1),
    )
    c = fingerprint_assets(resume_access.list_parsed_assets(USER))
    assert c != a


def test_fingerprint_changes_on_reparse(db):
    t0 = datetime.now(timezone.utc)
    _insert_asset(
        db, resume_id="ra-be", filename="backend.pdf",
        structured=_backend_structured(), parsed_at=t0,
    )
    before = fingerprint_assets(resume_access.list_parsed_assets(USER))
    db.resume_assets.update_one(
        {"resume_id": "ra-be"},
        {"$set": {"parsed_at": t0 + timedelta(days=1)}},
    )
    after = fingerprint_assets(resume_access.list_parsed_assets(USER))
    assert before != after


def test_public_title_seed_never_calls_synthesizer(db):
    _insert_asset(
        db, resume_id="ra-be", filename="backend.pdf",
        structured=_backend_structured(),
    )
    _insert_asset(
        db, resume_id="ra-nurse", filename="nurse.pdf",
        structured=_nurse_structured(),
    )
    seed = public_title_seed(USER)
    assert seed["resume_count"] == 2
    assert "Backend Engineer" in seed["resume_titles"]
    assert "Registered Nurse" in seed["resume_titles"]
    assert seed["synth_titles"] == []
    assert seed["cached"] is True
    assert seed["fingerprint"]


def test_suggest_filters_cached_hits_synthesizer_once(db):
    _insert_asset(
        db, resume_id="ra-be", filename="backend.pdf",
        structured=_backend_structured(),
    )
    _insert_asset(
        db, resume_id="ra-nurse", filename="nurse.pdf",
        structured=_nurse_structured(),
    )
    calls = {"n": 0}

    def fake_synth(structured):
        calls["n"] += 1
        titles = [e["title"] for e in structured.get("experience") or [] if e.get("title")]
        return {
            "headline": "hybrid",
            "rationale": "merged",
            "linkedin_keyword_sets": ["Backend Engineer Python"],
            "workday_titles": titles + ["Staff Backend Engineer"],
            "custom_role_terms": [],
            "past_days": 7,
            "preset_tags": ["backend"],
        }

    first, cached_first = suggest_filters_cached(USER, synthesizer=fake_synth)
    assert cached_first is False
    assert calls["n"] == 1
    assert "Backend Engineer" in first["workday_titles"]
    assert "Registered Nurse" in first["workday_titles"]

    second, cached_second = suggest_filters_cached(USER, synthesizer=fake_synth)
    assert cached_second is True
    assert calls["n"] == 1
    assert second == first

    seed = public_title_seed(USER)
    assert "Staff Backend Engineer" in seed["synth_titles"]
    _, cached_after_get = suggest_filters_cached(USER, synthesizer=fake_synth)
    assert cached_after_get is True
    assert calls["n"] == 1


def test_suggest_filters_cached_reruns_only_when_fingerprint_changes(db):
    t0 = datetime.now(timezone.utc)
    _insert_asset(
        db, resume_id="ra-be", filename="backend.pdf",
        structured=_backend_structured(), parsed_at=t0,
    )
    calls = {"n": 0}

    def fake_synth(structured):
        calls["n"] += 1
        return {
            "headline": "be",
            "rationale": "x",
            "linkedin_keyword_sets": ["kw"],
            "workday_titles": ["Backend Engineer"],
            "custom_role_terms": [],
            "past_days": 7,
            "preset_tags": [],
        }

    suggest_filters_cached(USER, synthesizer=fake_synth)
    suggest_filters_cached(USER, synthesizer=fake_synth)
    assert calls["n"] == 1

    _insert_asset(
        db, resume_id="ra-data", filename="data.pdf",
        structured=_data_structured(), parsed_at=t0 + timedelta(hours=1),
    )
    _, cached = suggest_filters_cached(USER, synthesizer=fake_synth)
    assert cached is False
    assert calls["n"] == 2


def test_merge_structured_covers_all_experience(db):
    _insert_asset(
        db, resume_id="ra-be", filename="backend.pdf",
        structured=_backend_structured(),
    )
    _insert_asset(
        db, resume_id="ra-nurse", filename="nurse.pdf",
        structured=_nurse_structured(),
    )
    merged = merge_structured_for_titles(resume_access.list_parsed_assets(USER))
    titles = {e["title"] for e in merged["experience"]}
    assert titles == {"Backend Engineer", "Registered Nurse"}
    assert "backend.pdf" in merged["summary"]
    assert "nurse.pdf" in merged["summary"]


def test_suggest_filters_cached_no_resume_raises(db):
    with pytest.raises(ValueError, match="No resume"):
        suggest_filters_cached(USER, synthesizer=lambda s: {})
