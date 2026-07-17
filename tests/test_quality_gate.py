"""Quality reflection gate (Craft-overhaul Phase 1B).

After the integrity-clean draft, an optional FLASH judge scores it; if weak, ONE
regeneration is attempted with the critique. Fail-open in every branch, and the
regeneration still passes the fabrication floor. These stub the LLM so no
Bedrock is needed.
"""
import pytest

from services import resume_tailor
from services.resume_tailor import ResumeTailor


_ORIGINAL = {
    "contact": {"name": "T", "email": "t@x.com"},
    "summary": "Engineer with 5 years.",
    "skills": {"Lang": ["Python"]},
    "experience": [{"company": "Acme", "title": "SWE", "dates": "2020 - Present",
                    "bullets": ["Built a Python service handling 2M requests."]}],
    "education": [], "projects": [], "certifications": [],
}
_JD = {"job_title": "SWE", "required_skills": ["Python"], "preferred_skills": [], "keywords": []}


def _finalize_noop(t):
    return t


def _draft(summary="A tailored summary about Python services."):
    return {
        "contact": dict(_ORIGINAL["contact"]),
        "summary": summary,
        "skills": {"Lang": ["Python"]},
        "experience": [{"company": "Acme", "title": "SWE", "dates": "2020 - Present",
                        "bullets": ["Built a Python service handling 2M requests."]}],
        "education": [], "projects": [], "certifications": [],
    }


@pytest.fixture()
def gate_on(monkeypatch):
    monkeypatch.setattr(resume_tailor, "QUALITY_REFLECTION_GATE", True, raising=False)


def _stub_judge(monkeypatch, verdict):
    monkeypatch.setattr(ResumeTailor, "_judge_quality",
                        lambda self, d, o, j: verdict)


def test_high_score_skips_regeneration(gate_on, monkeypatch):
    _stub_judge(monkeypatch, {"overall": 85, "weakest_dimensions": [], "critique": ""})
    import services.claude_client as cc

    def _boom(*a, **k):
        raise AssertionError("no regeneration should happen for a high score")
    monkeypatch.setattr(cc, "claude_json", _boom)

    t = ResumeTailor()
    draft = _draft()
    out = t._finalize_with_quality(_finalize_noop, draft, _ORIGINAL, _JD)
    assert out is draft  # unchanged, no regen


def test_low_score_triggers_one_regeneration(gate_on, monkeypatch):
    _stub_judge(monkeypatch, {"overall": 55, "weakest_dimensions": ["jd_alignment"],
                              "critique": "Foreground Python more."})
    import services.claude_client as cc

    better = _draft("A stronger Python-forward summary.")
    calls = {"n": 0}

    def _fake(*a, **k):
        calls["n"] += 1
        return better
    monkeypatch.setattr(cc, "claude_json", _fake)

    out = ResumeTailor()._finalize_with_quality(_finalize_noop, _draft(), _ORIGINAL, _JD)
    assert calls["n"] == 1  # exactly one regeneration
    assert out["summary"] == "A stronger Python-forward summary."


def test_regeneration_that_hallucinates_keeps_original(gate_on, monkeypatch):
    _stub_judge(monkeypatch, {"overall": 50, "weakest_dimensions": ["jd_alignment"],
                              "critique": "x"})
    import services.claude_client as cc

    # Regeneration invents a new employer -> integrity guard flags needs_retry.
    fabricated = _draft()
    fabricated["experience"].append(
        {"company": "FakeCorp", "title": "Staff", "dates": "2015 - 2019",
         "bullets": ["Invented role."]}
    )
    monkeypatch.setattr(cc, "claude_json", lambda *a, **k: fabricated)

    original = _draft()
    out = ResumeTailor()._finalize_with_quality(_finalize_noop, original, _ORIGINAL, _JD)
    # The fabricated regen is rejected; original kept.
    companies = {e["company"] for e in out["experience"]}
    assert "FakeCorp" not in companies


def test_judge_error_fails_open(gate_on, monkeypatch):
    def _boom(self, d, o, j):
        raise RuntimeError("judge down")
    monkeypatch.setattr(ResumeTailor, "_judge_quality", _boom)

    draft = _draft()
    out = ResumeTailor()._finalize_with_quality(_finalize_noop, draft, _ORIGINAL, _JD)
    assert out is draft  # gate failed open, draft returned


def test_flag_off_skips_gate_entirely(monkeypatch):
    monkeypatch.setattr(resume_tailor, "QUALITY_REFLECTION_GATE", False, raising=False)

    def _boom(self, d, o, j):
        raise AssertionError("judge must not run when flag is off")
    monkeypatch.setattr(ResumeTailor, "_judge_quality", _boom)

    draft = _draft()
    out = ResumeTailor()._finalize_with_quality(_finalize_noop, draft, _ORIGINAL, _JD)
    assert out is draft
