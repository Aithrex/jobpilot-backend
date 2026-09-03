"""Deferred summary writer — brief, validator, and fail-safe fallback.

No Bedrock/Mongo: generation and claim_guard are stubbed. The brief and
scorer are deterministic and run for real.
"""
import pytest

from services import summary_tailor
from services.summary_tailor import (
    SummaryBrief,
    allocate_summary_keywords,
    allocate_tech_slots,
    build_summary_brief,
    validate_summary,
    write_summary,
)
from utils.keyword_normalizer import is_programming_language


_BASE_SUMMARY = (
    "Backend engineer with nearly 4 years building production APIs and data "
    "models in Python and Java. Ships FastAPI and Spring Boot services with "
    "Kafka pipelines and PostgreSQL on AWS and Kubernetes."
)

_ORIGINAL = {
    "summary": _BASE_SUMMARY,
    "skills": {"Languages": ["Python", "Java"], "Cloud": ["AWS"]},
    "experience": [
        {
            "company": "HealthCo",
            "title": "Software Engineer",
            "dates": "2022 - Present",
            "bullets": [
                "Built production APIs in Python, Django, and FastAPI against PostgreSQL.",
                "Shipped Spring Boot services in Java with Kafka pipelines.",
                "Operated Kubernetes workloads on AWS with production observability.",
                "Designed PostgreSQL schemas for regulated healthcare data models.",
                "Tuned Kafka consumers for clinical event throughput.",
                "Hardened AWS IAM and deployment pipelines for the API platform.",
                "Reduced p95 API latency from 420ms to 95ms on the billing service.",
                "Wrote pytest coverage for FastAPI handlers in the claims service.",
                "Automated Terraform-backed AWS environments for staging.",
                "Owned on-call for backend platform incidents across two regions.",
                "Added a React admin view for internal clinical ops dashboards.",
                "Shipped end-to-end schema-to-UI work on one patient-intake feature.",
            ],
        }
    ],
}

_JD = {
    "job_title": "Full-Stack Product Engineer",
    "required_skills": ["Django", "PostgreSQL", "React"],
    "keywords": ["APIs", "Python"],
    "experience_years": "5+ years",
}

_GAP = {
    "required_matched": ["PostgreSQL", "Django"],
    "matched_keywords": ["Python", "APIs", "React", "Kafka"],
}

_TAILORED = {
    "summary": "",
    "skills": {"Languages": ["Python", "Java"]},
    "experience": [
        {"company": "HealthCo", "title": "Software Engineer",
         "bullets": list(_ORIGINAL["experience"][0]["bullets"])},
    ],
}


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
    """texts is a list of summaries returned in order (retries consume next)."""
    queue = list(texts)

    def _fake(brief, user_feedback=None, failure_reasons=None):
        if not queue:
            return "", ["exhausted"]
        return queue.pop(0), []

    monkeypatch.setattr(summary_tailor, "_generate", _fake)


# ---------------------------------------------------------------------------
# Phase 1 — brief
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


def test_must_retain_keeps_java_when_base_and_bullets_have_it():
    brief = build_summary_brief(_TAILORED, _ORIGINAL, _JD, _GAP)
    retained = {t.lower() for t in brief.must_retain}
    assert "java" in retained
    assert "python" in retained


def test_bridge_budget_keeps_backend_identity_for_fullstack_jd():
    brief = build_summary_brief(_TAILORED, _ORIGINAL, _JD, _GAP)
    assert brief.role_family.lower().startswith("backend")
    assert not brief.role_family.lower().startswith("full-stack")
    assert brief.jd_family_key == "fullstack"
    assert brief.support < 0.5


def test_brief_is_deterministic():
    a = build_summary_brief(_TAILORED, _ORIGINAL, _JD, _GAP)
    b = build_summary_brief(_TAILORED, _ORIGINAL, _JD, _GAP)
    assert a.as_dict() == b.as_dict()


# ---------------------------------------------------------------------------
# Phase 3 — hard gates
# ---------------------------------------------------------------------------

def _brief() -> SummaryBrief:
    return build_summary_brief(_TAILORED, _ORIGINAL, _JD, _GAP)


def test_generation_prompt_uses_shared_summary_rule():
    from services import prompt_rules
    prompt = summary_tailor._build_generation_prompt(_brief())
    assert prompt_rules.SUMMARY_RULE in prompt
    assert prompt_rules.VOICE_RULE in prompt
    assert "must-retain" in prompt.lower()
    assert "THREE-SLOT STRUCTURE" in prompt
    assert "Every sentence MUST contain" in prompt
    assert "8-11" in prompt


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


def test_must_retain_gate_rejects_dropped_java(stub_grounding):
    brief = _brief()
    text = (
        "Backend engineer building production APIs in Python on AWS. "
        "Ships FastAPI services with Kafka pipelines and PostgreSQL on Kubernetes."
    )
    verdict = validate_summary(text, brief, _TAILORED, _ORIGINAL, _JD)
    assert not verdict.ok
    assert any("java" in r.lower() for r in verdict.reasons)


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


# ---------------------------------------------------------------------------
# Content model — slot / budget / backing / domain / identity
# ---------------------------------------------------------------------------

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
    text = _good_candidate()
    for sentence in summary_tailor._split_sentences(text):
        assert summary_tailor._sentence_has_evidence(sentence, brief)


def test_technology_budget_band(stub_grounding):
    brief = _brief()
    named = summary_tailor._summary_tech_terms(_good_candidate())
    assert 8 <= len(named) <= 11
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


def test_bullet_backing_rejects_unproven_tech(stub_grounding):
    brief = _brief()
    text = (
        "Backend engineer building production systems in Python and Java "
        "across regulated healthcare platforms. Delivers FastAPI and Spring Boot "
        "services with Kafka pipelines, PostgreSQL tuning, and Kubernetes on AWS. "
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


def test_identity_stability_same_base_three_jds():
    backend_jd = dict(_JD, job_title="Backend Engineer")
    fullstack_jd = _JD
    platform_jd = dict(_JD, job_title="Platform / Infrastructure Engineer")

    backend = build_summary_brief(_TAILORED, _ORIGINAL, backend_jd, _GAP)
    fullstack = build_summary_brief(_TAILORED, _ORIGINAL, fullstack_jd, _GAP)
    platform = build_summary_brief(_TAILORED, _ORIGINAL, platform_jd, _GAP)

    def _adopted(brief, jd):
        jd_key = summary_tailor.classify_role_family(jd["job_title"])
        permitted = (
            brief.jd_family_key == brief.candidate_family_key
            or brief.support >= 0.5
        )
        jd_label = summary_tailor._family_label(jd_key, jd["job_title"])
        if permitted:
            return brief.role_family == jd_label
        return brief.role_family != jd_label

    assert _adopted(backend, backend_jd)
    assert _adopted(fullstack, fullstack_jd)
    assert _adopted(platform, platform_jd)
    assert fullstack.role_family.lower().startswith("backend")
    assert not fullstack.role_family.lower().startswith("full-stack")
    if platform.support < 0.5:
        assert platform.role_family.lower().startswith("backend")


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
    assert 4 <= len(brief.tech_s2) <= 6
    assert "healthcare" in brief.domain_qualifier


# ---------------------------------------------------------------------------
# write_summary orchestration
# ---------------------------------------------------------------------------

def test_must_retain_enforced_on_output(stub_grounding, monkeypatch):
    dropped = (
        "Backend engineer building production APIs in Python on AWS. "
        "Ships FastAPI services with Kafka pipelines and PostgreSQL on Kubernetes."
    )
    _stub_generate(monkeypatch, [dropped, dropped])
    out = write_summary(dict(_TAILORED), _ORIGINAL, _JD, _GAP)
    assert out["summary"] == _BASE_SUMMARY
    assert "Java" in out["summary"]


def test_comparative_acceptance_ships_base_when_candidate_is_weaker(
    stub_grounding, monkeypatch,
):
    thin = (
        "Backend engineer using Python and Java. "
        "Uses FastAPI, Spring Boot, Kafka, PostgreSQL, AWS, and Kubernetes."
    )
    _stub_generate(monkeypatch, [thin])
    out = write_summary(dict(_TAILORED), _ORIGINAL, _JD, _GAP)
    assert out["summary"] == _BASE_SUMMARY or "Java" in out["summary"]
    # A thin keyword-list candidate must not beat the specific base.
    brief = _brief()
    thin_v = validate_summary(thin, brief, _TAILORED, _ORIGINAL, _JD)
    base_v = validate_summary(
        _BASE_SUMMARY, brief, _TAILORED, _ORIGINAL, _JD, require_hard_gates=False,
    )
    if thin_v.ok:
        assert thin_v.score <= base_v.score
        assert out["summary"] == _BASE_SUMMARY


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


def test_retry_exhaustion_ships_base_without_raising(stub_grounding, monkeypatch):
    bad = "Engineer."
    _stub_generate(monkeypatch, [bad, bad])
    out = write_summary(dict(_TAILORED), _ORIGINAL, _JD, _GAP)
    assert out["summary"] == _BASE_SUMMARY


def test_accepted_candidate_ships_when_it_beats_base(stub_grounding, monkeypatch):
    winner = _good_candidate()
    _stub_generate(monkeypatch, [winner])
    out = write_summary(dict(_TAILORED), _ORIGINAL, _JD, _GAP)
    # Winner has keyword_budget terms (Django, React) the base lacks, so it
    # should outscore the base if it clears gates.
    brief = _brief()
    cand = validate_summary(winner, brief, _TAILORED, _ORIGINAL, _JD)
    base = validate_summary(
        _BASE_SUMMARY, brief, _TAILORED, _ORIGINAL, _JD, require_hard_gates=False,
    )
    if cand.ok and cand.score > base.score:
        assert "Django" in out["summary"]
        assert out["summary"].startswith("Backend engineer")
    else:
        # Still must never ship a fullstack identity flip.
        assert not out["summary"].lower().startswith("full-stack")


def test_years_phrase_rounds_three_point_nine_to_four(monkeypatch):
    """47 months is 3.9 years. Flooring to 3 printed 'nearly 3' on a 4-year resume."""
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
