"""Summary-refiner safety tests (Craft-overhaul Part A / A2).

The refiner rewrites ONLY the summary and is fail-safe: a rewrite that
introduces an ungrounded claim (or any audit/LLM failure) must leave the base
summary untouched. Only ``summary`` may ever change. These stub the LLM and the
claim-guard gate so no Bedrock/Mongo is needed.
"""
import pytest

from services import resume_tailor
from services.resume_tailor import ResumeTailor


_ORIGINAL = {
    "summary": "Backend engineer with 5 years building payment systems.",
    "skills": {"Languages": ["Python"], "Cloud": ["AWS"]},
    "experience": [
        {"company": "Acme", "title": "SWE",
         "bullets": ["Built the payments pipeline in Python on AWS."]},
    ],
}

_TAILORED = {
    "summary": "Backend engineer focused on payments infrastructure.",
    "skills": {"Languages": ["Python"], "Cloud": ["AWS"]},
    "experience": [
        {"company": "Acme", "title": "SWE",
         "bullets": ["Built the payments pipeline in Python on AWS, cutting latency."]},
    ],
}

_GAP = {"required_matched": ["Python", "AWS"], "matched_keywords": ["Python", "AWS"]}


@pytest.fixture
def tailor():
    return ResumeTailor()


def _stub_llm(monkeypatch, returned_summary):
    """Patch claude_json (imported lazily inside _refine_summary)."""
    import services.claude_client as cc

    def _fake(*args, **kwargs):
        return {"summary": returned_summary}

    monkeypatch.setattr(cc, "claude_json", _fake)


def _stub_gate(monkeypatch, ungrounded: bool):
    monkeypatch.setattr(
        ResumeTailor,
        "_summary_introduces_ungrounded_claim",
        staticmethod(lambda *a, **k: ungrounded),
    )


def test_clean_refinement_is_applied(tailor, monkeypatch):
    _stub_llm(monkeypatch, "Payments-focused backend engineer who built Python services on AWS.")
    _stub_gate(monkeypatch, ungrounded=False)

    out = tailor._refine_summary(dict(_TAILORED), _ORIGINAL, {"job_title": "SWE"}, _GAP)
    assert out["summary"] == "Payments-focused backend engineer who built Python services on AWS."
    # Nothing else changed.
    assert out["experience"] == _TAILORED["experience"]


def test_ungrounded_refinement_is_discarded(tailor, monkeypatch):
    # The rewrite invents a metric not in the source; the gate flags it.
    _stub_llm(monkeypatch, "Backend engineer who scaled payments to 5M users and cut costs 40%.")
    _stub_gate(monkeypatch, ungrounded=True)

    base = dict(_TAILORED)
    out = tailor._refine_summary(base, _ORIGINAL, {"job_title": "SWE"}, _GAP)
    # Base summary preserved.
    assert out["summary"] == _TAILORED["summary"]


def test_llm_error_keeps_base_summary(tailor, monkeypatch):
    import services.claude_client as cc

    def _boom(*a, **k):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(cc, "claude_json", _boom)
    out = tailor._refine_summary(dict(_TAILORED), _ORIGINAL, {"job_title": "SWE"}, _GAP)
    assert out["summary"] == _TAILORED["summary"]


def test_too_short_rewrite_is_rejected(tailor, monkeypatch):
    _stub_llm(monkeypatch, "Engineer.")  # under the 40-char floor
    _stub_gate(monkeypatch, ungrounded=False)
    out = tailor._refine_summary(dict(_TAILORED), _ORIGINAL, {"job_title": "SWE"}, _GAP)
    assert out["summary"] == _TAILORED["summary"]


def test_empty_base_summary_is_noop(tailor, monkeypatch):
    _stub_llm(monkeypatch, "Some rewritten summary that is plenty long to pass the floor.")
    _stub_gate(monkeypatch, ungrounded=False)
    t = {"summary": "", "experience": []}
    out = tailor._refine_summary(t, _ORIGINAL, {"job_title": "SWE"}, _GAP)
    assert out["summary"] == ""


def test_gate_audit_failure_rejects_rewrite(tailor, monkeypatch):
    # If claim_guard/fact-store raises, the gate must fail CLOSED (reject the
    # rewrite, keep the safe base) — return True from the gate.
    import services.claim_guard as cg

    def _boom(*a, **k):
        raise RuntimeError("audit exploded")

    monkeypatch.setattr(cg, "audit_resume", _boom)
    result = ResumeTailor._summary_introduces_ungrounded_claim(
        dict(_TAILORED), _ORIGINAL, {"job_title": "SWE"}
    )
    assert result is True  # rejected → base kept


def test_allocate_summary_keywords_dedups_and_caps():
    gap = {
        "required_matched": ["Python", "AWS"],
        "matched_keywords": ["Python", "Docker", "Kafka", "Go", "Redis", "Terraform"],
    }
    out = ResumeTailor._allocate_summary_keywords(gap)
    assert out.count("Python") == 1  # dedup across both lists
    assert len(out) <= 6  # cap
    assert out[0] == "Python"  # required-matched first
