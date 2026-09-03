"""Tailor prompt v2 — contract drift guard.

Asserts the built prompt carries the research-backed v2 additions AND still
carries every truth-critical anchor from v1 (proving the tightening didn't
drop anti-fabrication rules). Pure string checks — no LLM, no Mongo.
"""
from services.resume_tailor import ResumeTailor, BANNED_PHRASES


GAP = {
    "matched_keywords": ["Python", "AWS"],
    "missing_keywords": ["Kafka", "Terraform", "Remote"],
    "partial_matches": [],
    "coverage_percentage": 40,
    "required_coverage": 50,
    "required_matched": ["Python"],
    "required_missing": ["Kafka"],
}


def _build_prompt() -> str:
    keyword_context = ResumeTailor._build_keyword_context(GAP)
    return ResumeTailor._build_tailor_prompt(
        keyword_context,
        role_context="",
        resume_payload='{"summary": "sample"}',
        jd_json='{"job_title": "Software Engineer"}',
    )


def test_keyword_context_contents():
    ctx = ResumeTailor._build_keyword_context(GAP)
    assert "current coverage: 40%" in ctx
    assert "MATCHED" in ctx and "Python" in ctx
    assert "MISSING REQUIRED" in ctx and "Kafka" in ctx
    assert "weave" in ctx.lower()
    assert "80-90% of required JD keywords covered" in ctx
    assert "not just listed in skills" in ctx.lower() or "NOT 100%" in ctx
    # General missing excludes the required ones already listed
    assert "Terraform" in ctx


def test_v2_additions_present():
    prompt = _build_prompt()
    # Anti-stuffing coverage target
    assert "JD keywords covered" in prompt
    # Honest quantification menu
    assert "HONEST QUANTIFICATION MENU" in prompt
    assert "SCOPE:" in prompt and "FREQUENCY:" in prompt
    # Summary is deferred — primary must not write one
    assert "Do NOT write a professional summary" in prompt
    assert "TITLE MIRROR" not in prompt
    assert "2-3 sentence professional summary" not in prompt
    # Acronym pairs
    assert "Amazon Web Services (AWS)" in prompt
    assert "ACRONYM PAIRS" in prompt
    # Placement rule
    assert "8-12 most critical JD keywords" in prompt
    # XYZ formula
    assert "as measured by" in prompt
    # Dates handled by the system
    assert "copy every date string EXACTLY" in prompt
    # New few-shot
    assert "Example G" in prompt
    assert "Invented percentage" in prompt


def test_truth_anchors_survived():
    """v2 tightens truthfulness — it must never loosen it."""
    prompt = _build_prompt()
    assert "NEVER fabricate" in prompt
    assert "DEFENSIBILITY" in prompt.upper()
    assert "IMMUTABLE FIELDS" in prompt
    assert "PRESERVE every number" in prompt
    assert "HARD CONSTRAINTS" in prompt
    assert "keyword-stuffing" in prompt
    # Banned phrases still enumerated
    for phrase in list(BANNED_PHRASES)[:5]:
        assert phrase in prompt, phrase
    # Untrusted-data wrapping intact
    assert "original_resume" in prompt
    assert "job_description_analysis" in prompt


def test_summary_schema_line_is_empty():
    prompt = _build_prompt()
    assert '"summary": ""' in prompt
    assert "2-3 sentence professional summary" not in prompt
    assert "3-4 sentence professional summary" not in prompt
