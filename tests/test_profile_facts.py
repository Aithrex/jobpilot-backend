"""profile_facts — fact store, enrichment candidates, confirmation lifecycle.

Runs on mongomock (wired by tests/_vg_support). The fact store is a computed
view: resume facts are implicit; LinkedIn/GitHub facts enter only after an
explicit user confirmation recorded in `enrichment_confirmations`.
"""
import pytest

from services.profile_facts import (
    CONFIRMATIONS_COLLECTION,
    build_fact_store,
    build_fact_sheet,
    enrichment_context,
    grounding_texts,
    list_candidate_facts,
    record_decisions,
)
from utils.db_connect import DBConnect

EMAIL = "facts-tester@example.com"

STRUCTURED = {
    "contact": {"name": "Jane Doe"},
    "summary": "Backend engineer building Python services.",
    "skills": {"Languages": ["Python"], "Cloud": ["AWS"]},
    "experience": [{
        "title": "Engineer", "company": "Acme", "location": "",
        "dates": "March 2022 - Present", "type": "Full-time",
        "bullets": ["Built Python microservices on AWS"],
    }],
    "education": [{"degree": "BS CS", "institution": "State U",
                   "location": "", "dates": "May 2020", "gpa": "", "coursework": ""}],
    "projects": [], "certifications": [],
}


@pytest.fixture()
def clean_enrichment():
    db = DBConnect().get_db()
    for coll in (CONFIRMATIONS_COLLECTION, "linkedin_archives", "github_profiles"):
        db[coll].delete_many({})
    return db


def _seed_sources(db):
    db["linkedin_archives"].insert_one({
        "user_email": EMAIL,
        "positions": [{
            "company": "Acme", "title": "Engineer",
            "description": "Led migration of billing pipeline to Kafka",
            "location": "NYC", "started_on": "2022-03", "finished_on": "",
        }],
        "skills": ["Terraform", "Python"],
        "educations": [], "certifications": [],
        # Third-party PII — must never surface as a candidate fact.
        "connections": [{"name": "Someone Else", "email": "private@person.com"}],
    })
    db["github_profiles"].insert_one({
        "user_email": EMAIL,
        "username": "janedoe",
        "top_repos": [{
            "name": "fraud-sim", "stars": 12, "language": "Python",
            "description": "Real-time fraud scoring demo",
        }],
        "proof_score": 60,
    })


def test_resume_only_fact_store():
    store = build_fact_store(structured_resume=STRUCTURED, include_enrichment=False)
    ids = {f["fact_id"] for f in store["facts"]}
    assert "res:exp:0" in ids
    assert "res:exp:0:b0" in ids
    assert "res:edu:0" in ids
    assert "res:skills" in ids
    assert store["sources"] == {
        "resume": True, "linkedin": False, "github": False, "voicegraph": False,
    }

    sheet = build_fact_sheet(store)
    assert "[F1]" in sheet
    assert "Engineer at Acme" in sheet


def test_candidates_derived_and_connections_never_leak(clean_enrichment):
    _seed_sources(clean_enrichment)
    payload = list_candidate_facts(EMAIL)
    displays = " | ".join(c["display"] for c in payload["candidates"])
    kinds = {c["kind"] for c in payload["candidates"]}
    assert {"position", "skill", "repo"} <= kinds
    assert "fraud-sim" in displays
    assert "Terraform" in displays
    # PII boundary: nothing from Connections.csv may appear anywhere.
    assert "Someone Else" not in displays
    assert "private@person.com" not in displays
    assert payload["confirmed"] == [] and payload["rejected"] == []
    assert payload["sources"]["github"]["username"] == "janedoe"


def test_confirmation_lifecycle_and_store_merge(clean_enrichment):
    _seed_sources(clean_enrichment)
    candidates = list_candidate_facts(EMAIL)["candidates"]
    skill_id = next(c["fact_id"] for c in candidates if c["display"] == "Skill: Terraform")
    repo_id = next(c["fact_id"] for c in candidates if c["kind"] == "repo")
    pos_id = next(c["fact_id"] for c in candidates if c["kind"] == "position")

    updated = record_decisions(EMAIL, [
        {"fact_id": skill_id, "status": "confirmed"},
        {"fact_id": repo_id, "status": "confirmed"},
        {"fact_id": pos_id, "status": "rejected"},
        {"fact_id": "li:skill:doesnotexist", "status": "confirmed"},  # ignored
        {"fact_id": skill_id, "status": "maybe"},  # invalid status → ignored
    ])
    assert updated == 3

    relisted = list_candidate_facts(EMAIL)
    assert {c["fact_id"] for c in relisted["confirmed"]} == {skill_id, repo_id}
    assert {c["fact_id"] for c in relisted["rejected"]} == {pos_id}
    assert skill_id not in {c["fact_id"] for c in relisted["candidates"]}

    # Confirmed facts (and ONLY confirmed) merge into the fact store.
    store = build_fact_store(user_email=EMAIL, structured_resume=STRUCTURED)
    enriched_ids = {f["fact_id"] for f in store["facts"] if f["source"] != "resume"}
    assert enriched_ids == {skill_id, repo_id}
    assert store["sources"]["linkedin"] is True
    assert store["sources"]["github"] is True

    ctx = enrichment_context(store)
    assert "Terraform" in ctx and "fraud-sim" in ctx
    assert "billing pipeline" not in ctx  # rejected position stays out

    texts = grounding_texts(store)
    assert any("fraud-sim" in t for t in texts)


def test_tailor_prompt_carries_enrichment_block():
    from services.resume_tailor import ResumeTailor

    block = (
        "================================================================\n"
        "CONFIRMED CANDIDATE FACTS (user-verified from LinkedIn/GitHub)\n"
        "================================================================\n"
        '- CONFIRMED (Linkedin): skill "Terraform"\n\n'
    )
    prompt = ResumeTailor._build_tailor_prompt(
        keyword_context="",
        role_context="",
        resume_payload="{}",
        jd_json="{}",
        enrichment_context=block,
    )
    assert "CONFIRMED CANDIDATE FACTS" in prompt
    assert 'skill "Terraform"' in prompt
    # Block sits before the hard constraints so it's in scope while planning.
    assert prompt.index("CONFIRMED CANDIDATE FACTS") < prompt.index("HARD CONSTRAINTS")


def test_github_evidence_extends_project_tech_grounding():
    """A confirmed repo's language is valid `tech` even when the uploaded
    resume never mentions it (observable fact from the repo)."""
    from services.project_generator import ProjectGenerator

    gen = ProjectGenerator()
    raw = {
        "purpose": "Real-time fraud scoring sandbox",
        "name": "Fraud Scoring Sandbox",
        "dates": "",
        "bullets": [
            "Implemented streaming fraud-score evaluation for synthetic card transactions with replayable event fixtures.",
            "Designed rule and model blending so precision tradeoffs are configurable per merchant category profile.",
        ],
        "tech": "Rust, Python",
    }
    without = gen._validate_and_clean(raw, STRUCTURED, {}, 5)
    with_evidence = gen._validate_and_clean(
        dict(raw), STRUCTURED, {}, 5,
        extra_grounding_text='repo "fraud-sim": Rust, 12 stars — fraud scoring demo',
    )
    assert with_evidence is not None
    assert "Rust" in with_evidence["tech"]
    # Without evidence, Rust must be dropped (not in resume skills/text).
    assert without is None or "Rust" not in without["tech"]
