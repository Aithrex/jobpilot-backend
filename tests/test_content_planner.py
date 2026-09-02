"""Content plan is profession-aware — no SWE defaults for a nurse."""
from services.content_planner import plan, recruiter_persona
from services.plan_writers import apply_plan
from services.profile_facts import build_fact_store


def test_nursing_plan_is_clinical_not_software():
    resume = {
        "experience": [{
            "title": "Staff Nurse",
            "company": "County Hospital",
            "location": "Austin, TX",
            "dates": "2021 - Present",
            "type": "Full-time",
            "bullets": ["Provided bedside care for med-surg patients"],
        }],
        "skills": {"Clinical": ["Med-Surg", "Epic"]},
        "licenses": ["RN, Texas"],
        "projects": [],
        "education": [],
        "certifications": ["BLS"],
    }
    jd = {
        "job_title": "Registered Nurse",
        "industry": "Healthcare",
        "required_skills": ["Epic"],
        "preferred_skills": [],
        "keywords": ["Epic"],
    }
    p = plan(resume, jd)
    assert p["domain"] == "healthcare"
    assert "clinical" in p["recruiter_persona"]
    assert "technical recruiter" not in p["recruiter_persona"]
    assert p["credentials_first"] is True
    assert p["force_metrics"] is False
    assert p["allow_project_generation"] is False
    assert p["section_order"].index("licenses") < p["section_order"].index("experience")


def test_software_plan_still_allows_projects():
    resume = {
        "experience": [{
            "title": "Software Engineer",
            "company": "Acme",
            "bullets": ["Built Python APIs"],
        }],
        "skills": {"Languages": ["Python"]},
    }
    p = plan(resume, {"job_title": "Backend Engineer", "industry": "Software"})
    assert p["domain"] == "software"
    assert p["allow_project_generation"] is True
    assert p["force_metrics"] is True


def test_plan_writers_attach_evidence_and_drop_unconfirmed_skills():
    original = {
        "experience": [{
            "title": "Staff Nurse",
            "company": "County Hospital",
            "bullets": ["Provided bedside care for med-surg patients"],
        }],
        "skills": {"Clinical": ["Med-Surg", "Epic"]},
        "summary": "Registered nurse with med-surg experience.",
    }
    tailored = {
        "experience": [{
            "title": "Staff Nurse",
            "company": "County Hospital",
            "bullets": ["Provided bedside care for med-surg patients"],
        }],
        "projects": [{
            "name": "Duplicate",
            "bullets": ["Provided bedside care for med-surg patients"],
        }],
        "skills": {"Clinical": ["Med-Surg", "Epic", "Kubernetes"]},
        "summary": "Registered nurse with med-surg experience.",
    }
    store = build_fact_store(structured_resume=original, include_enrichment=False)
    p = plan(original, {"job_title": "Registered Nurse", "industry": "Healthcare"})
    apply_plan(tailored, original, p, store)
    assert "Kubernetes" not in str(tailored.get("skills"))
    assert "Kubernetes" in (tailored.get("_review_skills") or [])
    evidence = tailored.get("_evidence") or {}
    assert evidence.get("experience.0.b0")
    assert all(evidence[addr] for addr in evidence)
    # Duplicate project bullet removed; empty project dropped.
    assert tailored.get("projects") == []
    assert tailored["content_plan"]["domain"] == "healthcare"


def test_recruiter_persona_covers_licensed_domains():
    assert "clinical" in recruiter_persona("healthcare")
    assert "finance" in recruiter_persona("finance")
    assert "construction" in recruiter_persona("construction")
