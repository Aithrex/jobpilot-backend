"""Deferred summary writer — brief, validator, sidecar, and fail-safe fallback.

No Bedrock/Mongo: generation and claim_guard are stubbed. The brief and
scorer are deterministic and run for real.
"""
import pytest

from services import summary_tailor
from services.summary_tailor import (
    WRITER_REVISION,
    SummaryBrief,
    allocate_summary_keywords,
    allocate_tech_slots,
    build_summary_brief,
    refresh_summary_if_stale,
    validate_summary,
    write_summary,
)
from tests.fixtures.summary_role_matrix import (
    BACKEND_ENGINEER,
    FINANCE_ANALYST,
    JDS,
    MARKETING_COORDINATOR,
    NURSE,
    gap_for,
    tailored_from,
)
from utils.keyword_normalizer import is_programming_language


_ORIGINAL = BACKEND_ENGINEER["structured"]
_BASE_SUMMARY = _ORIGINAL["summary"]
_JD = JDS["fullstack"]
_GAP = gap_for(
    required=["PostgreSQL", "Django"],
    matched=["Python", "APIs", "React", "Kafka"],
)
_TAILORED = tailored_from(_ORIGINAL)


def _good_candidate() -> str:
    return (
        "Backend engineer building production APIs and data models in Python "
        "and Java across regulated healthcare platforms. Owns features end to "
        "end, from PostgreSQL schema design through Django and FastAPI services "
        "to React interfaces in the clinical UI. Ships Spring Boot and Kafka "
        "pipelines to AWS and Kubernetes with production observability."
    )


@pytest.fixture
def stub_grounding(monkeypatch):
    monkeypatch.setattr(
        summary_tailor,
        "_summary_introduces_ungrounded_claim",
        lambda *a, **k: False,
    )


def _stub_generate(monkeypatch, texts):
    queue = list(texts)

    def _fake(brief, user_feedback=None, failure_reasons=None):
        if not queue:
            return "", ["exhausted"]
        return queue.pop(0), []

    monkeypatch.setattr(summary_tailor, "_generate", _fake)


def _brief(jd=None, original=None, tailored=None, gap=None) -> SummaryBrief:
    return build_summary_brief(
        tailored or _TAILORED,
        original or _ORIGINAL,
        jd or _JD,
        gap if gap is not None else _GAP,
    )


# ---------------------------------------------------------------------------
# Brief
# ---------------------------------------------------------------------------

def test_allocate_summary_keywords_dedups_and_caps():
    gap = {
        "required_matched": ["Python", "AWS"],
        "matched_keywords": ["Python", "Docker", "Kafka", "Go", "Redis", "Terraform"],
    }
    out = allocate_summary_keywords(gap)
    assert out.count("Python") == 1
    assert len(out) <= 6
    assert out[0] == "Python"


def test_selected_terms_rank_jd_ahead_of_base_only_java():
    brief = _brief()
    selected = [t.lower() for t in brief.selected_terms]
    assert "postgresql" in selected
    assert "django" in selected
    if "java" in selected:
        assert selected.index("java") > selected.index("postgresql")


def test_bridge_budget_keeps_backend_identity_for_fullstack_jd():
    brief = _brief()
    assert brief.role_family.lower().startswith("backend")
    assert not brief.role_family.lower().startswith("full-stack")
    assert brief.jd_family_key == "fullstack"
    assert brief.support < 0.5
    assert brief.role_rationale in {"keep_candidate", "bridge"}


def test_broad_swe_adopts_software_engineer_without_fifty_percent_support():
    brief = _brief(jd=JDS["sde"])
    assert brief.role_family == "Software engineer"
    assert brief.role_rationale == "adopt_broad_swe"
    assert brief.jd_family_key == "swe"


def test_specialist_titles_do_not_mint_unsupported_identity():
    frontend = _brief(jd=JDS["frontend"])
    fullstack = _brief(jd=JDS["fullstack"])
    ml = _brief(jd=JDS["ml"])
    manager = _brief(jd=JDS["manager"])
    assert not frontend.role_family.lower().startswith("frontend")
    assert not fullstack.role_family.lower().startswith("full-stack")
    assert "machine learning" not in ml.role_family.lower()
    assert "manager" not in manager.role_family.lower()


def test_untitled_jd_uses_responsibilities_and_supported_neutral_label():
    brief = _brief(jd=JDS["untitled"])
    assert brief.role_family == "Software engineer"
    assert brief.role_rationale in {"adopt_broad_swe", "neutral_fallback"}


def test_same_title_different_responsibilities_change_selected_content():
    reliability = _brief(jd=JDS["same_title_reliability"], gap=gap_for(["Python", "AWS"]))
    delivery = _brief(jd=JDS["same_title_delivery"], gap=gap_for(["Python", "Django"]))
    assert reliability.role_family == delivery.role_family
    assert reliability.prioritized_capabilities != delivery.prioritized_capabilities or (
        set(reliability.selected_terms) != set(delivery.selected_terms)
    )


def test_project_bullet_is_included_when_it_backs_a_required_term():
    structured = {
        "summary": "Backend engineer using Python.",
        "experience": [
            {
                "company": "Acme",
                "title": "Software Engineer",
                "bullets": [
                    "Built billing APIs in Python.",
                    "Tuned PostgreSQL queries on the ledger.",
                    "Wrote pytest coverage for payment handlers.",
                    "Owned on-call for the billing service.",
                    "Shipped Kafka consumers for invoice events.",
                    "Deployed the API to AWS.",
                ],
            }
        ],
        "projects": [
            {"name": "Ops UI", "bullets": ["Built a React dashboard for invoice ops."]},
        ],
    }
    tailored = tailored_from(structured)
    jd = {
        "job_title": "Full-Stack Product Engineer",
        "required_skills": ["React", "Python"],
        "responsibilities": ["Own schema-to-UI features"],
    }
    brief = build_summary_brief(
        tailored, structured, jd, gap_for(["React", "Python"]),
    )
    assert any("react" in b.lower() for b in brief.evidence)
    assert any(ref.startswith("projects.") for ref in brief.evidence_refs)


def test_domain_omitted_when_jd_does_not_share_it():
    brief = _brief(jd=JDS["sde"])
    assert brief.domain_qualifier == ""


def test_brief_includes_jd_responsibilities_and_target_title():
    brief = _brief()
    assert brief.target_title == _JD["job_title"]
    assert brief.responsibilities == _JD["responsibilities"]
    assert brief.qualifications


def test_brief_is_deterministic():
    a = _brief()
    b = _brief()
    assert a.as_dict() == b.as_dict()


def test_nontechnical_profile_does_not_import_engineering_assumptions():
    structured = MARKETING_COORDINATOR["structured"]
    brief = build_summary_brief(
        tailored_from(structured), structured, JDS["office_manager"], gap_for(),
    )
    blob = " ".join(brief.as_dict().get("selected_terms", [])).lower()
    assert "fastapi" not in blob
    assert "healthcare platforms" not in (brief.domain_qualifier or "").lower()
    assert brief.role_family != "Software engineer"
    assert "genai" not in " ".join(brief.prioritized_capabilities).lower()


# ---------------------------------------------------------------------------
# Hard gates
# ---------------------------------------------------------------------------

def test_generation_prompt_uses_shared_summary_rule():
    from services import prompt_rules
    prompt = summary_tailor._build_generation_prompt(_brief())
    assert prompt_rules.SUMMARY_RULE in prompt
    assert prompt_rules.VOICE_RULE in prompt
    assert "selected" in prompt.lower()
    assert "THREE-SLOT STRUCTURE" in prompt
    assert "ROLE RESPONSIBILITIES" in prompt
    assert "8-11" not in prompt
    assert "must-retain" not in prompt.lower()


@pytest.mark.parametrize("phrase", [
    "ready to",
    "seeking",
    "looking to",
    "eager to",
    "bring this foundation",
    "hoping to",
    "excited to",
    "passionate about",
    "aiming to",
])
def test_filler_rejection(stub_grounding, phrase):
    brief = _brief()
    text = (
        f"Backend engineer building Python and Java APIs on AWS. "
        f"{phrase.capitalize()} apply this Kafka and PostgreSQL work next."
    )
    verdict = validate_summary(text, brief, _TAILORED, _ORIGINAL, _JD)
    assert not verdict.ok
    assert any("filler" in r for r in verdict.reasons)


def test_base_only_java_is_not_a_hard_gate(stub_grounding):
    brief = _brief()
    text = (
        "Backend engineer building production APIs and data models in Python "
        "on AWS across billing and claims services. Owns features end to end "
        "through Django and FastAPI services with PostgreSQL schemas, Kafka "
        "pipelines, and Kubernetes deployment, including pytest coverage and "
        "on-call ownership for the billing API."
    )
    verdict = validate_summary(text, brief, _TAILORED, _ORIGINAL, _JD)
    assert verdict.ok, verdict.reasons
    assert not any("java" in r.lower() and "must" in r.lower() for r in verdict.reasons)


def test_sentence_evidence_gate(stub_grounding):
    brief = _brief()
    text = (
        "Backend engineer building production APIs in Python and Java on AWS. "
        "Comfortable owning the full development lifecycle in fast-paced teams."
    )
    verdict = validate_summary(text, brief, _TAILORED, _ORIGINAL, _JD)
    assert not verdict.ok
    assert any("no evidence" in r for r in verdict.reasons)


def test_forbidden_fullstack_opener_rejected(stub_grounding):
    brief = _brief()
    text = (
        "Full-stack engineer building production APIs in Python and Java on AWS. "
        "Ships FastAPI, Spring Boot, Kafka, PostgreSQL, and Kubernetes services."
    )
    verdict = validate_summary(text, brief, _TAILORED, _ORIGINAL, _JD)
    assert not verdict.ok
    assert any("forbidden" in r for r in verdict.reasons)


def test_slot_conformance_s1_role_family_and_language_cap(stub_grounding):
    brief = _brief()
    verdict = validate_summary(_good_candidate(), brief, _TAILORED, _ORIGINAL, _JD)
    assert verdict.ok, verdict.reasons
    first = summary_tailor._split_sentences(_good_candidate())[0]
    assert 2 <= len(summary_tailor._split_sentences(_good_candidate())) <= 3
    assert summary_tailor._s1_has_role_family(first, brief.role_family)
    langs = [
        t for t in summary_tailor._summary_tech_terms(first)
        if is_programming_language(t)
    ]
    assert len(langs) <= 2


def test_sentence_evidence_uses_budget_or_metric(stub_grounding):
    brief = _brief()
    for sentence in summary_tailor._split_sentences(_good_candidate()):
        assert summary_tailor._sentence_has_evidence(sentence, brief)


def test_technology_budget_rejects_dump_not_a_missing_eighth_term(stub_grounding):
    brief = _brief()
    verdict = validate_summary(_good_candidate(), brief, _TAILORED, _ORIGINAL, _JD)
    assert verdict.ok, verdict.reasons
    dump = (
        "Backend engineer using Python and Java. "
        "Uses FastAPI, Spring Boot, Django, Kafka, PostgreSQL, React, AWS, "
        "Kubernetes, Terraform, Redis, and Celery every day."
    )
    dumped = validate_summary(dump, brief, _TAILORED, _ORIGINAL, _JD)
    assert not dumped.ok
    assert any("tech-budget" in r for r in dumped.reasons)


def test_bullet_backing_rejects_unproven_tech_including_jd_keywords(stub_grounding):
    brief = _brief()
    text = (
        "Backend engineer building production systems in Python and Java. "
        "Delivers FastAPI and Spring Boot services with Kafka pipelines, "
        "PostgreSQL tuning, and Kubernetes on AWS. "
        "Builds LangChain retrieval workflows into the same backend services."
    )
    verdict = validate_summary(text, brief, _TAILORED, _ORIGINAL, _JD)
    assert not verdict.ok
    assert any("bullet-backing" in r and "langchain" in r.lower() for r in verdict.reasons)


def test_domain_qualifier_cannot_lead_s1(stub_grounding):
    brief = _brief()
    text = (
        "Healthcare backend engineer building production APIs in Python and Java. "
        "Delivers FastAPI and Spring Boot services with Kafka pipelines, "
        "PostgreSQL tuning, and Kubernetes deployment on AWS, plus Django and React."
    )
    verdict = validate_summary(text, brief, _TAILORED, _ORIGINAL, _JD)
    assert not verdict.ok
    assert any("domain" in r for r in verdict.reasons)


def test_nontechnical_summary_validates_without_technology_quota(stub_grounding):
    structured = MARKETING_COORDINATOR["structured"]
    tailored = tailored_from(structured)
    brief = build_summary_brief(
        tailored, structured, JDS["office_manager"], gap_for(),
    )
    text = (
        "Marketing coordinator keeping campaign calendars, vendor invoices in "
        "Excel, and 18 product-line launches on schedule for brand leads each "
        "quarter. Owns weekly HubSpot performance reports in Excel and shipped "
        "18 launch readouts so each campaign goes out with accurate tracking "
        "and a clear weekly summary for stakeholders."
    )
    verdict = validate_summary(text, brief, tailored, structured, JDS["office_manager"])
    assert verdict.ok, verdict.reasons
    assert summary_tailor._tech_floor(brief) == 0


def test_identity_stability_same_base_three_jds():
    backend = _brief(jd=JDS["backend"])
    fullstack = _brief(jd=JDS["fullstack"])
    platform = _brief(jd=JDS["platform"])
    sde = _brief(jd=JDS["sde"])

    assert backend.role_family.lower().startswith("backend")
    assert fullstack.role_family.lower().startswith("backend")
    assert not fullstack.role_family.lower().startswith("full-stack")
    if platform.support < 0.5:
        assert platform.role_family.lower().startswith("backend")
    assert sde.role_family == "Software engineer"


def test_allocate_tech_slots_skips_terms_missing_from_bullets():
    bullets = [
        "Built production APIs in Python and FastAPI against PostgreSQL.",
        "Shipped Spring Boot services in Java with Kafka pipelines.",
    ]
    s1, s2, s3 = allocate_tech_slots(
        ["Python", "Java"],
        ["Django", "PostgreSQL", "Python"],
        bullets,
    )
    assert "Python" in s1
    assert "Java" in s1
    assert "PostgreSQL" in s2
    assert all(t.lower() != "django" for t in s1 + s2 + s3)


def test_tech_slots_put_languages_in_s1():
    brief = _brief()
    assert brief.tech_s1
    assert all(is_programming_language(t) for t in brief.tech_s1)
    assert len(brief.tech_s1) <= 2


# ---------------------------------------------------------------------------
# write_summary orchestration
# ---------------------------------------------------------------------------

def test_omitting_irrelevant_base_term_can_still_ship(stub_grounding, monkeypatch):
    dropped_java = (
        "Backend engineer building production APIs and data models in Python "
        "on AWS across billing and claims services. Owns features end to end "
        "through Django and FastAPI services with PostgreSQL schemas, Kafka "
        "pipelines, and Kubernetes deployment, including pytest coverage and "
        "on-call ownership for the billing API."
    )
    _stub_generate(monkeypatch, [dropped_java])
    out = write_summary(dict(_TAILORED), _ORIGINAL, _JD, _GAP)
    assert "Django" in out["summary"]
    assert out["summary_write"]["source"] == "generated"
    assert out["summary_write"]["writer_revision"] == WRITER_REVISION


def test_valid_draft_ships_even_when_score_is_below_base(stub_grounding, monkeypatch):
    weaker = (
        "Backend engineer building production APIs and data models in Python "
        "and Java across billing and claims services for two regional "
        "platforms. Delivers FastAPI and Django services with PostgreSQL, "
        "Kafka pipelines, and AWS deployment, plus pytest coverage and "
        "on-call ownership for the billing API each week."
    )
    _stub_generate(monkeypatch, [weaker])
    brief = _brief()
    thin_v = validate_summary(weaker, brief, _TAILORED, _ORIGINAL, _JD)
    base_v = validate_summary(
        _BASE_SUMMARY, brief, _TAILORED, _ORIGINAL, _JD, require_hard_gates=False,
    )
    assert thin_v.ok, thin_v.reasons
    out = write_summary(dict(_TAILORED), _ORIGINAL, _JD, _GAP)
    assert out["summary"] != _BASE_SUMMARY
    assert out["summary_write"]["source"] == "generated"
    assert thin_v.score <= base_v.score or out["summary_write"]["source"] == "generated"


def test_grounding_fail_closed_keeps_base(monkeypatch):
    import services.claim_guard as cg

    def _boom(*a, **k):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(cg, "audit_resume", _boom)
    monkeypatch.setattr(
        summary_tailor,
        "_generate",
        lambda *a, **k: (_good_candidate(), []),
    )
    out = write_summary(dict(_TAILORED), _ORIGINAL, _JD, _GAP)
    assert out["summary"] == _BASE_SUMMARY
    assert out["summary_write"]["source"] == "base"
    assert "grounding" in out["summary_write"]["reasons"]


def test_retry_exhaustion_ships_base_without_raising(stub_grounding, monkeypatch):
    bad = "Engineer."
    _stub_generate(monkeypatch, [bad, bad])
    out = write_summary(dict(_TAILORED), _ORIGINAL, _JD, _GAP)
    assert out["summary"] == _BASE_SUMMARY
    assert out["summary_write"]["source"] == "base"
    assert out["summary_write"]["attempts"] == 2
    assert out["summary_write"]["reasons"]


def test_accepted_candidate_ships_regardless_of_base_score(stub_grounding, monkeypatch):
    winner = _good_candidate()
    _stub_generate(monkeypatch, [winner])
    out = write_summary(dict(_TAILORED), _ORIGINAL, _JD, _GAP)
    assert "Django" in out["summary"]
    assert out["summary"].startswith("Backend engineer")
    assert out["summary_write"]["source"] == "generated"
    assert out["summary_write"]["reasons"] == []


def test_years_phrase_rounds_three_point_nine_to_four(monkeypatch):
    from services.resume_parser import ResumeParser

    monkeypatch.setattr(
        ResumeParser, "_experience_intervals_months", staticmethod(lambda _s: 47),
    )
    phrase = summary_tailor._years_phrase({"experience": [{}]}, {})
    assert "4" in phrase
    assert "3" not in phrase


def test_years_phrase_omits_when_below_jd_ask(monkeypatch):
    from services.resume_parser import ResumeParser

    monkeypatch.setattr(
        ResumeParser, "_experience_intervals_months", staticmethod(lambda _s: 47),
    )
    phrase = summary_tailor._years_phrase(
        {"experience": [{}]}, {"experience_years": 5},
    )
    assert phrase == ""


def test_assert_summary_preserved_raises_when_writer_drops_field():
    from services.resume_tailor import ResumeTailor
    with pytest.raises(ValueError):
        ResumeTailor._assert_summary_preserved(
            {"summary": "Backend engineer with Python."},
            {"summary": ""},
        )


def test_refresh_rewrites_once_when_augmenter_drops_backing(stub_grounding, monkeypatch):
    calls = {"n": 0}

    def _fake(brief, user_feedback=None, failure_reasons=None):
        calls["n"] += 1
        return _good_candidate(), []

    monkeypatch.setattr(summary_tailor, "_generate", _fake)
    tailored = dict(_TAILORED)
    tailored["summary"] = (
        "Backend engineer building production systems in Python and Java. "
        "Delivers FastAPI services with Kafka and PostgreSQL on AWS. "
        "Builds LangChain retrieval workflows into the same backend services."
    )
    out = refresh_summary_if_stale(tailored, _ORIGINAL, _JD, _GAP)
    assert calls["n"] >= 1
    assert "LangChain" not in out["summary"]
    assert out["summary_write"]["refreshed_after_augment"] is True


def test_refresh_leaves_text_when_backing_still_holds(stub_grounding, monkeypatch):
    monkeypatch.setattr(
        summary_tailor,
        "write_summary",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not rewrite")),
    )
    tailored = dict(_TAILORED)
    tailored["summary"] = _good_candidate()
    out = refresh_summary_if_stale(tailored, _ORIGINAL, _JD, _GAP)
    assert out["summary"] == _good_candidate()


def test_finance_and_nurse_profiles_stay_in_their_own_vocabulary():
    finance = build_summary_brief(
        tailored_from(FINANCE_ANALYST["structured"]),
        FINANCE_ANALYST["structured"],
        JDS["staff_accountant"],
        gap_for(["Excel"]),
    )
    nurse = build_summary_brief(
        tailored_from(NURSE["structured"]),
        NURSE["structured"],
        {"job_title": "Registered Nurse", "responsibilities": ["Provide bedside care"]},
        gap_for(),
    )
    assert finance.role_family != "Software engineer"
    assert nurse.role_family != "Software engineer"
    assert "kubernetes" not in " ".join(finance.selected_terms).lower()
    assert "fastapi" not in " ".join(nurse.selected_terms).lower()


def test_summary_word_band_edges_render():
    from services.resume_renderer import ResumeRenderer

    two = (
        "Software engineer building production APIs and data models in Python "
        "across billing and claims services for two regional platforms. "
        "Delivers FastAPI services with PostgreSQL, Kafka pipelines, and AWS "
        "deployment backed by pytest coverage, production observability, and "
        "on-call ownership for the production billing platform each week."
    )
    three = (
        "Software engineer building production APIs and data models in Python "
        "and Java across billing and claims services. Delivers FastAPI and "
        "Django services with PostgreSQL, Kafka pipelines, and Kubernetes on "
        "AWS. Owns production reliability end to end, including on-call for "
        "the billing platform and pytest coverage on the claims handlers."
    )
    assert 45 <= len(two.split()) <= 70
    assert 45 <= len(three.split()) <= 70
    renderer = ResumeRenderer()
    for text in (two, three):
        resume = {
            "contact": {
                "name": "Alex Rivera", "email": "a@example.com",
                "phone": "480-555-0100", "linkedin": "", "github": "",
                "location": "Tempe, AZ",
            },
            "summary": text,
            "skills": {"Languages": ["Python"]},
            "experience": [{
                "title": "Software Engineer", "company": "Acme",
                "location": "Tempe, AZ", "dates": "2022 - Present",
                "bullets": ["Built production APIs in Python."],
            }],
            "education": [],
            "projects": [],
            "certifications": [],
        }
        pdf, _res = renderer.generate_pdf(resume)
        assert pdf.startswith(b"%PDF")
        docx = renderer.generate_docx(resume)
        assert docx[:2] == b"PK"
