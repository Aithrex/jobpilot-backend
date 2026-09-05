import copy
import pytest

from services import tailoring_pipeline as pipeline
from services import tailoring_assessment as assessment
from services.tailoring_policy import content_hash, digest, enabled
from services.tailoring_planner import build_plan
from services.resume_versioning import append_version, build_initial_version, canonical_content_hash, StaleVersionError


@pytest.fixture
def source():
    return {"contact": {"name": "Alex Doe", "email": "alex@example.com"},
            "summary": "Engineer building reliable services.",
            "skills": {"Skills": ["Python", "Kafka", "SQL"]},
            "experience": [{"company": "Acme", "title": "Engineer", "dates": "2022 - 2024",
                            "bullets": ["Added retries for failed payment requests.",
                                        "Added SQL indexes to reduce query latency by about 20%."]},
                           {"company": "Other", "title": "Analyst", "dates": "2020 - 2022",
                            "bullets": ["Reconciled customer invoices with monthly account statements."]}],
            "education": [], "projects": [], "certifications": []}


@pytest.fixture
def jd():
    return {"job_title": "Software Engineer", "required_skills": ["reliability", "Kafka"],
            "preferred_skills": ["SQL"], "analysis_completeness": "complete"}


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(pipeline, "_semantic_plan", lambda plan: None)
    monkeypatch.setattr(assessment, "finalize", lambda resume, jd, renderer: resume)
    monkeypatch.setattr("services.summary_tailor.write_summary", lambda resume, *args, **kw: resume)
    monkeypatch.setattr(pipeline, "_call", lambda *a, **k: {})


def test_opt_in(monkeypatch):
    monkeypatch.delenv("EVIDENCE_TAILORING_ENABLED", raising=False)
    assert not enabled()
    monkeypatch.setenv("EVIDENCE_TAILORING_ENABLED", "true")
    assert enabled()


def test_semantic_capabilities_and_listed_skills_are_distinct(source, jd):
    plan = build_plan(source, jd)
    reliability, kafka, sql = plan.requirements
    assert reliability.state == "demonstrated" and reliability.support_basis == "semantic"
    assert kafka.state == "listed"
    assert sql.state == "demonstrated"
    assert all("experience.0" in fid for fid in reliability.fact_ids)


def test_employer_alternatives_are_one_requirement(source):
    plan = build_plan(source, {"required_skills": ["Python or Java"]})
    assert len(plan.requirements) == 1
    assert plan.requirements[0].state == "listed"


def test_credible_reframe_can_change_words_substantially(source, jd, offline, monkeypatch):
    fid = build_plan(source, jd).facts[0].id
    rewritten = "Improved payment failure recovery by retrying unsuccessful requests."
    def model(prompt, schema, **kw):
        if "patches" in schema:
            return {"patches": [{"id": fid, "text": rewritten}]}
        assert "ORIGINAL BULLET" not in prompt
        assert "Reconciled" not in prompt  # Other employer cannot ground this claim.
        return {"results": [{"id": fid, "state": "supported_semantic", "reason": "Retries recover failures."}]}
    monkeypatch.setattr(pipeline, "_call", model)
    result = pipeline.run(source, jd)
    assert result["experience"][0]["bullets"][0] == rewritten
    assert result["experience"][0]["title"] == "Engineer"
    assert set(result["skills"]["Skills"]) == set(source["skills"]["Skills"])
    assert source["experience"][0]["bullets"][0].startswith("Added")


@pytest.mark.parametrize("changed", [
    "Reduced SQL query latency by 20% by adding indexes.",  # approximate -> exact
    "Added SQL indexes to reduce query latency by about 40%.",
    "Led 10 engineers to add SQL indexes and reduce latency by about 20%.",
])
def test_metric_scope_and_qualifiers_cannot_be_added(source, jd, changed):
    facts = build_plan(source, jd).facts
    result = pipeline._validate_patches([{"id": facts[1].id, "text": changed}], {f.id: f for f in facts})
    assert result[facts[1].id]["state"] == "unsupported"


def test_unavailable_verifier_keeps_source(source, jd, offline, monkeypatch):
    fid = build_plan(source, jd).facts[0].id
    def model(prompt, schema, **kw):
        if "patches" in schema:
            return {"patches": [{"id": fid, "text": "Led an organization-wide payment platform migration."}]}
        raise RuntimeError("offline")
    monkeypatch.setattr(pipeline, "_call", model)
    result = pipeline.run(source, jd)
    assert result["experience"][0]["bullets"][0] == source["experience"][0]["bullets"][0]
    assert "unverified_patch" in result["tailoring_run"]["reasons"]


def test_regeneration_preserves_edits_and_explicit_empty_role(source, jd, offline):
    current = pipeline.run(source, jd)
    current["experience"][0]["bullets"][0] = "My shorter payment recovery wording."
    second_id = next(p["fact_id"] for p in current["tailoring_run"]["provenance"] if p["entity"] == "experience.1")
    current["tailoring_preferences"] = {"omitted_fact_ids": [second_id], "omit_summary": True}
    refined = pipeline.run(source, jd, current=current, feedback="Make it concise")
    assert refined["experience"][0]["bullets"][0] == "My shorter payment recovery wording."
    assert refined["experience"][1]["bullets"] == []
    assert refined["summary"] == ""
    assert refined["tailoring_run"]["provenance"][0]["state"] == "user_edit_unverified"


def test_confirmed_work_stays_with_its_actual_employer(source, jd, offline):
    fact = {"source": "user", "source_hash": content_hash(source), "entity": "experience.1",
            "text": "Maintained Kafka consumers for invoice processing."}
    result = pipeline.run(source, jd, fact_store={"facts": [fact]})
    assert fact["text"] in result["experience"][1]["bullets"]
    assert fact["text"] not in result["experience"][0]["bullets"]
    assert result["tailoring_run"]["source_hash"] == content_hash(source)
    fact["source_hash"] = "different upload"
    assert fact["text"] not in pipeline.run(source, jd, fact_store={"facts": [fact]})["experience"][1]["bullets"]


def test_content_and_jd_changes_invalidate_checks(source, jd, offline):
    resume = pipeline.run(source, jd)
    resume["summary"] = "Changed wording."
    a = assessment.assess(resume, jd, run_reader=False)
    assert a["source_status"] == "stale" and not a["ready"]
    a = assessment.assess(pipeline.run(source, jd), {**jd, "job_title": "Accountant"}, run_reader=False)
    assert a["source_status"] == "stale"


def test_export_checks_actual_skill_and_bullet_text(source, monkeypatch):
    monkeypatch.setattr("services.pdf_ats_check.extract_pdf_text", lambda _: ("Alex Doe alex@example.com", 1))
    check, _ = assessment.check_export(b"pdf", source)
    assert check["status"] == "issues"
    assert not check["checks"]["skills.0"]
    assert not check["checks"]["experience.0.bullets.0"]


def test_review_drops_invented_quotes_and_hides_evidence_map(monkeypatch):
    def model(**kwargs):
        assert "secret provenance" not in kwargs["prompt"]
        return {"findings": [{"kind": "strength", "quote": "Invented work", "explanation": "bad"},
                             {"kind": "clarity", "quote": "Actual work", "explanation": "clear"}]}
    monkeypatch.setattr("services.claude_client.claude_json", model)
    result = assessment.reader_review("Actual work", {"provenance": "secret provenance"})
    assert len(result["findings"]) == 1


def test_version_hash_ignores_review_metadata_but_rejects_stale_parent(source, jd, offline):
    resume = pipeline.run(source, jd)
    changed = copy.deepcopy(resume)
    changed["document_assessment"] = {"content_hash": content_hash(resume), "ready": True}
    assert canonical_content_hash(resume) == canonical_content_hash(changed)
    versions = [build_initial_version(resume)]
    changed["summary"] = "Different."
    with pytest.raises(StaleVersionError):
        append_version(versions, changed, source="edited", parent_version_id="old")
    v, _ = append_version(versions, changed, source="edited")
    assert "document_assessment" not in v["tailored_resume"]


def test_discovery_owner_scope_and_snapshot(mock_db, source):
    from services.tailoring_discovery import confirm
    mock_db.tailoring_records.insert_one({"record_id": "r", "user_email": "owner", "tailoring_source_snapshot": source})
    with pytest.raises(LookupError):
        confirm(mock_db, "other", "r", "experience.0", "Worked on payment recovery.")
    with pytest.raises(ValueError):
        confirm(mock_db, "owner", "r", "experience.50", "Worked on payment recovery.")
    key = confirm(mock_db, "owner", "r", "experience.0", "Worked on payment recovery.")
    doc = mock_db.tailoring_evidence.find_one({"user_email": "owner"})
    assert doc["facts"][key]["entity"] == "experience.0"
    assert doc["source_hash"] == content_hash(source)


def test_new_scores_never_simulate_vendor_results(source, jd, offline, monkeypatch):
    monkeypatch.setattr(assessment, "reader_review", lambda *a: {"status": "unverified", "findings": []})
    payload = assessment.score_payload(pipeline.run(source, jd), jd, None, "failed")
    assert payload["overall"] is None
    assert "scanners" not in payload and "format_score" not in payload
    assert payload["document_assessment"]["artifact"]["status"] == "failed"


def test_manually_added_bullet_survives_multiple_refinements(source, jd, offline):
    current = pipeline.run(source, jd)
    current["experience"][0]["bullets"].append("Documented the payment recovery process for colleagues.")
    first = pipeline.run(source, jd, current=current, feedback="Shorten the summary")
    second = pipeline.run(source, jd, current=first, feedback="Make the wording clearer")
    assert "Documented the payment recovery process for colleagues." in second["experience"][0]["bullets"]
    assert second["tailoring_run"]["provenance"][2]["state"] == "user_edit_unverified"


def test_both_tailor_entrypoints_use_shared_runner(source, jd, monkeypatch):
    from services.resume_tailor import ResumeTailor
    monkeypatch.setenv("EVIDENCE_TAILORING_ENABLED", "true")
    seen = []
    monkeypatch.setattr(pipeline, "run", lambda *a, **kw: seen.append(kw) or {"shared": True})
    assert ResumeTailor().tailor(source, jd) == {"shared": True}
    assert ResumeTailor().regenerate(source, source, jd, "shorten") == {"shared": True}
    assert seen[1]["feedback"] == "shorten"


def test_rollout_rollback_does_not_switch_existing_drafts_to_legacy(source, jd, monkeypatch):
    from services.resume_tailor import ResumeTailor
    monkeypatch.delenv("EVIDENCE_TAILORING_ENABLED", raising=False)
    current = {**source, "tailoring_run": {"policy_version": "evidence-v1"}}
    monkeypatch.setattr(pipeline, "run", lambda *a, **kw: {"shared": True})
    assert ResumeTailor().regenerate(source, current, jd, "shorten") == {"shared": True}


def test_run_policy_context_does_not_leak_between_requests(monkeypatch):
    from services.tailoring_policy import policy_context
    monkeypatch.delenv("EVIDENCE_TAILORING_ENABLED", raising=False)
    @policy_context
    def check():
        assert enabled()
        raise ValueError("failed run")
    with pytest.raises(ValueError):
        check()
    assert not enabled()


def test_non_engineering_semantics_are_planned_without_questions(source, monkeypatch):
    plan = build_plan(source, {"required_skills": ["Account reconciliation controls"]})
    fid = next(f.id for f in plan.facts if f.entity == "experience.1")
    monkeypatch.setattr(pipeline, "_call", lambda *a, **kw: {"matches": [{
        "requirement_id": plan.requirements[0].id, "fact_id": fid, "state": "demonstrated",
        "reason": "Reconciled invoices to statements."}]})
    pipeline._semantic_plan(plan)
    assert plan.requirements[0].state == "demonstrated"
    assert plan.requirements[0].fact_ids == [fid]


def test_real_pdf_and_docx_are_checked_separately(source):
    from services.resume_renderer import ResumeRenderer
    renderer = ResumeRenderer()
    pdf, _ = renderer.generate_pdf(source)
    pdf_check, _ = assessment.check_export(pdf, source)
    docx = renderer.generate_docx(source)
    docx_check, _ = assessment.check_export(docx, source, format="docx")
    assert pdf_check["status"] == "passed", pdf_check["issues"]
    assert docx_check["status"] == "passed", docx_check["issues"]
    assert docx_check["bytes_hash"] != pdf_check["bytes_hash"]


def test_jd_chunk_failure_is_visible(monkeypatch):
    from services.tailoring_jd import extract, _chunks
    text = "Required: SQL.\n" * 1100 + "Preferred: Python."
    assert "".join(_chunks(text)) == text
    calls = []
    def model(**kwargs):
        calls.append(kwargs)
        if len(calls) > 1:
            raise RuntimeError("offline")
        return {"job_title": "Analyst", "required_skills": ["SQL"], "requirements": [{
            "text": "SQL", "priority": "required", "kind": "skill", "alternatives": ["SQL"],
            "source_excerpt": "Required: SQL."}]}
    monkeypatch.setattr("services.claude_client.claude_json", model)
    result = extract(text)
    assert result["analysis_completeness"] == "partial"
    assert result["requirements"][0]["source_excerpt"] == "Required: SQL."


def test_reader_repair_revalidates_and_rechecks_changed_content(source, jd, offline, monkeypatch):
    fid = build_plan(source, jd).facts[0].id
    proposed = "Delivered robust enterprise payment failure recovery capabilities through retry handling."
    repaired = "Added retry handling to recover failed payment requests."
    writes, reviews = [], []
    def model(prompt, schema, **kwargs):
        if "patches" in schema:
            writes.append(prompt)
            return {"patches": [{"id": fid, "text": proposed if len(writes) == 1 else repaired}]}
        return {"results": [{"id": fid, "state": "supported_semantic", "reason": "Recovery through retries."}]}
    def review(resume, jd, renderer):
        reviews.append(content_hash(resume))
        result = copy.deepcopy(resume)
        result["document_assessment"] = {"content_hash": content_hash(result), "reader_review": {
            "status": "passed", "findings": [{"kind": "over_tailoring", "quote": proposed,
            "explanation": "Abstract labels obscure the actual action."}] if len(reviews) == 1 else []}}
        return result
    monkeypatch.setattr(pipeline, "_call", model)
    monkeypatch.setattr(assessment, "finalize", review)
    result = pipeline.run(source, jd)
    assert result["experience"][0]["bullets"][0] == repaired
    assert len(reviews) == 2 and reviews[0] != reviews[1]
    assert result["document_assessment"]["content_hash"] == content_hash(result)
