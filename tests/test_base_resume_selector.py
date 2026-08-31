"""JD best-base resume selection (per-job, never mutates active_resume_id).

Offline: mongomock via _vg_support. Ranking uses the real fit-v2.0 scorer
(no Bedrock). Covers the retrofit contract: one-resume users short-circuit;
multi-resume users auto-pick; pins are owner-scoped; ties prefer active.
"""
from datetime import datetime, timezone, timedelta

import pytest

from _vg_support import current_db

from services import resume_access
from services.base_resume_selector import select_base_resume


USER = "jordan@example.com"
OTHER = "mallory@example.com"

COLLECTIONS = ("resume_assets", "users")

SWE_JD = """
Senior Backend Engineer

Requirements:
- 5+ years of software engineering experience
- Strong Python and SQL
- Experience with AWS and Docker
- Familiarity with Kubernetes
"""

NURSE_JD = """
Registered Nurse, Acute Care

Requirements:
- Active RN license
- Patient care and medication administration
- EHR / Epic documentation
"""


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
        "skills": {"clinical": ["Patient care", "Medication administration",
                                "Wound care", "EHR"]},
        "experience": [{
            "title": "Registered Nurse", "company": "Mercy Hospital",
            "dates": "Jun 2017 - Jun 2025",
            "bullets": [
                "Delivered direct patient care and medication administration.",
                "Completed patient assessment and clinical documentation in Epic.",
            ],
        }],
        "education": [{"degree": "BSN Nursing", "institution": "Ohio State"}],
        "projects": [],
        "certifications": ["RN License, Ohio", "BLS", "ACLS"],
    }


def _insert_asset(
    db,
    *,
    resume_id,
    filename,
    structured,
    email=USER,
    parse_status="parsed",
    parsed_at=None,
):
    now = parsed_at or datetime.now(timezone.utc)
    db.resume_assets.insert_one({
        "resume_id": resume_id,
        "user_email": email,
        "owner_type": "user",
        "filename": filename,
        "parse_status": parse_status,
        "structured": structured if parse_status == "parsed" else None,
        "object_key": f"resume-assets/{resume_id}/original",
        "created_at": now,
        "parsed_at": now if parse_status == "parsed" else None,
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


def test_single_parsed_resume_short_circuits_without_ranking(db):
    _insert_asset(
        db, resume_id="ra-only", filename="only.pdf",
        structured=_backend_structured(),
    )
    db.users.update_one({"email": USER}, {"$set": {"active_resume_id": "ra-only"}})

    picked = select_base_resume(
        USER, jd_text=SWE_JD, jd_analysis={"job_title": "Senior Backend Engineer"},
    )
    assert picked.error is None
    assert picked.mode == "single"
    assert picked.resume_id == "ra-only"
    assert picked.filename == "only.pdf"
    assert picked.candidates == []
    assert "only parsed resume" in picked.reason.lower()
    # Active pointer untouched.
    assert db.users.find_one({"email": USER})["active_resume_id"] == "ra-only"


def test_auto_picks_backend_resume_for_swe_jd_even_when_nurse_is_active(db):
    older = datetime.now(timezone.utc) - timedelta(days=30)
    newer = datetime.now(timezone.utc)
    _insert_asset(
        db, resume_id="ra-nurse", filename="nurse.pdf",
        structured=_nurse_structured(), parsed_at=newer,
    )
    _insert_asset(
        db, resume_id="ra-backend", filename="backend.pdf",
        structured=_backend_structured(), parsed_at=older,
    )

    picked = select_base_resume(
        USER, jd_text=SWE_JD, jd_analysis={"job_title": "Senior Backend Engineer"},
    )
    assert picked.error is None
    assert picked.mode == "auto"
    assert picked.resume_id == "ra-backend"
    assert picked.filename == "backend.pdf"
    assert picked.match_score is not None
    selected = [c for c in picked.candidates if c["is_selected"]]
    assert selected and selected[0]["resume_id"] == "ra-backend"
    nurse = next(c for c in picked.candidates if c["resume_id"] == "ra-nurse")
    assert nurse["is_active"] is True
    assert nurse["is_selected"] is False
    assert picked.match_score >= nurse["match_score"]
    # MUST NOT flip the global active pointer (job matching stays on nurse).
    assert db.users.find_one({"email": USER})["active_resume_id"] == "ra-nurse"


def test_auto_picks_nurse_resume_for_nurse_jd(db):
    _insert_asset(
        db, resume_id="ra-nurse", filename="nurse.pdf",
        structured=_nurse_structured(),
    )
    _insert_asset(
        db, resume_id="ra-backend", filename="backend.pdf",
        structured=_backend_structured(),
    )
    picked = select_base_resume(
        USER, jd_text=NURSE_JD, jd_analysis={"job_title": "Registered Nurse"},
    )
    assert picked.resume_id == "ra-nurse"
    assert picked.mode == "auto"


def test_pin_uses_owned_asset_even_when_it_is_the_worse_fit(db):
    _insert_asset(
        db, resume_id="ra-nurse", filename="nurse.pdf",
        structured=_nurse_structured(),
    )
    _insert_asset(
        db, resume_id="ra-backend", filename="backend.pdf",
        structured=_backend_structured(),
    )
    picked = select_base_resume(
        USER,
        jd_text=SWE_JD,
        jd_analysis={"job_title": "Senior Backend Engineer"},
        resume_id="ra-nurse",
    )
    assert picked.mode == "pinned"
    assert picked.resume_id == "ra-nurse"
    assert picked.filename == "nurse.pdf"
    assert "you chose" in picked.reason.lower()


def test_pin_other_users_id_is_not_found(db):
    _insert_asset(
        db, resume_id="ra-backend", filename="backend.pdf",
        structured=_backend_structured(),
    )
    _insert_asset(
        db, resume_id="ra-other", filename="stolen.pdf",
        structured=_backend_structured(), email=OTHER,
    )
    picked = select_base_resume(USER, jd_text=SWE_JD, resume_id="ra-other")
    assert picked.error == "not_found"
    assert picked.asset is None


def test_pin_unparsed_asset_is_not_parsed(db):
    _insert_asset(
        db, resume_id="ra-pending", filename="pending.pdf",
        structured=None, parse_status="pending",
    )
    picked = select_base_resume(USER, jd_text=SWE_JD, resume_id="ra-pending")
    assert picked.error == "not_parsed"


def test_pending_sibling_is_ignored_in_auto(db):
    _insert_asset(
        db, resume_id="ra-backend", filename="backend.pdf",
        structured=_backend_structured(),
    )
    _insert_asset(
        db, resume_id="ra-pending", filename="pending.pdf",
        structured=None, parse_status="pending",
    )
    picked = select_base_resume(USER, jd_text=SWE_JD)
    assert picked.mode == "single"
    assert picked.resume_id == "ra-backend"


def test_selection_mode_active_opt_out_uses_pointer_not_best_fit(db):
    _insert_asset(
        db, resume_id="ra-nurse", filename="nurse.pdf",
        structured=_nurse_structured(),
    )
    _insert_asset(
        db, resume_id="ra-backend", filename="backend.pdf",
        structured=_backend_structured(),
    )
    picked = select_base_resume(
        USER,
        jd_text=SWE_JD,
        jd_analysis={"job_title": "Senior Backend Engineer"},
        selection_mode="active",
    )
    assert picked.mode == "active"
    assert picked.resume_id == "ra-nurse"
    assert db.users.find_one({"email": USER})["active_resume_id"] == "ra-nurse"


def test_tie_prefers_currently_active(db, monkeypatch):
    _insert_asset(
        db, resume_id="ra-nurse", filename="a.pdf",
        structured=_backend_structured(),
    )
    _insert_asset(
        db, resume_id="ra-backend", filename="b.pdf",
        structured=_backend_structured(),
    )

    def _same_score(asset, job):
        return 50

    monkeypatch.setattr(
        "services.base_resume_selector._score_asset", _same_score
    )
    picked = select_base_resume(USER, jd_text=SWE_JD)
    assert picked.mode == "auto"
    assert picked.resume_id == "ra-nurse"


def test_none_parsed_returns_error(db):
    picked = select_base_resume(USER, jd_text=SWE_JD)
    assert picked.error == "none_parsed"


def test_list_parsed_assets_is_owner_scoped(db):
    _insert_asset(
        db, resume_id="ra-mine", filename="mine.pdf",
        structured=_backend_structured(),
    )
    _insert_asset(
        db, resume_id="ra-other", filename="other.pdf",
        structured=_backend_structured(), email=OTHER,
    )
    mine = resume_access.list_parsed_assets(USER)
    assert {a["resume_id"] for a in mine} == {"ra-mine"}


def test_scorer_failure_fails_open_to_active(db, monkeypatch):
    _insert_asset(
        db, resume_id="ra-nurse", filename="nurse.pdf",
        structured=_nurse_structured(),
    )
    _insert_asset(
        db, resume_id="ra-backend", filename="backend.pdf",
        structured=_backend_structured(),
    )

    monkeypatch.setattr(
        "services.base_resume_selector._score_asset",
        lambda asset, job: None,
    )
    picked = select_base_resume(USER, jd_text=SWE_JD)
    assert picked.error is None
    assert picked.resume_id == "ra-nurse"
    assert picked.mode == "active"
    assert db.users.find_one({"email": USER})["active_resume_id"] == "ra-nurse"
