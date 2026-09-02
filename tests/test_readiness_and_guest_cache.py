"""Readiness sidecar + guest download cache must key on content hash."""
from services.guest_service import cached_file_is_current
from services.profile_facts import build_fact_store
from services.readiness_service import analyze


def test_resume_facts_include_v2_fields_and_licenses():
    store = build_fact_store(
        structured_resume={
            "experience": [{
                "title": "Staff Nurse", "company": "County",
                "bullets": ["Cared for patients"],
            }],
            "skills": {"Clinical": ["Epic"]},
            "licenses": ["RN, Texas"],
            "contact": {"portfolio": "https://example.com/portfolio"},
        },
        include_enrichment=False,
    )
    facts = store["facts"]
    assert all("fact_type" in f and "value" in f for f in facts)
    assert all(f.get("candidate_confirmed") for f in facts)
    ids = {f["fact_id"] for f in facts}
    assert "res:licenses" in ids
    assert any(f["fact_type"] == "skill" for f in facts)
    assert any(f["fact_id"] == "res:portfolio" for f in facts)


def test_readiness_flags_missing_license_not_rejection_probability():
    resume = {
        "contact": {"name": "Ada"},
        "summary": "Accountant",
        "skills": {"Tools": ["Excel"]},
        "experience": [{
            "title": "Staff Accountant",
            "company": "Firm",
            "bullets": ["Prepared monthly close in Excel"],
        }],
        "licenses": [],
        "education": [],
        "projects": [],
        "certifications": [],
    }
    jd = {
        "job_title": "Senior Accountant",
        "location": "Austin, TX",
        "required_skills": ["Excel"],
        "preferred_skills": [],
        "keywords": ["Excel"],
        "qualifications": ["CPA license required"],
        "responsibilities": ["Close the books"],
    }
    jd_text = "Senior Accountant. CPA license required. Excel daily."
    from services.jd_analyzer import enrich
    jd = enrich(jd, jd_text, resume=resume)
    out = analyze(resume, jd, jd_text=jd_text)
    assert "disclaimer" in out
    assert "rejection" in out["disclaimer"].lower() or "not a prediction" in out["disclaimer"].lower()
    assert isinstance(out["qualification_fit"], int)
    assert any(
        c.get("requirement_type") == "license" and c.get("evidence_status") == "missing"
        for c in out["requirement_coverage"]
    )


def test_skills_list_mention_is_partial_not_professional_usage():
    resume = {
        "summary": "Operations coordinator",
        "skills": {"Tools": ["SAP"]},
        "experience": [{
            "title": "Coordinator",
            "company": "Warehouse",
            "bullets": ["Scheduled inbound trucks"],
        }],
        "licenses": [],
        "education": [],
        "projects": [],
        "certifications": [],
    }
    jd = {
        "job_title": "Supply Chain Analyst",
        "required_skills": ["SAP"],
        "preferred_skills": [],
        "keywords": ["SAP"],
        "requirements": [{
            "requirement_id": "req_sap",
            "text": "SAP",
            "normalized_term": "SAP",
            "requirement_type": "skill",
            "priority": "mandatory",
            "resume_controllable": True,
        }],
    }
    out = analyze(resume, jd)
    sap = next(c for c in out["requirement_coverage"] if c["text"].lower() == "sap")
    assert sap["usage"] == "skills_list"
    assert sap["evidence_status"] == "partial"


def test_guest_cache_requires_matching_content_hash():
    assert cached_file_is_current(
        {"s3_key": "k", "render_version": "v", "content_hash": "abc"},
        "v",
        "abc",
    )
    # Version match alone is not enough (the old guest bug).
    assert not cached_file_is_current(
        {"s3_key": "k", "render_version": "v"},
        "v",
        "abc",
    )
    assert not cached_file_is_current(
        {"s3_key": "k", "render_version": "v", "content_hash": "old"},
        "v",
        "new",
    )
