"""Unit tests for the tailor truncation guard (Craft-overhaul Phase 3).

`ResumeTailor._assert_core_sections_present` fails loudly when the LLM output
was truncated mid-JSON and the repair pass "recovered" by dropping a core
section the input résumé actually had. Without this, `validate_and_coerce`
silently backfills an empty default and the user downloads a gutted résumé.

These exercise the pure static guard directly — no LLM, no DB.
"""
import pytest

from services.resume_tailor import ResumeTailor

_ORIGINAL = {
    "summary": "Backend engineer with 5 years building payment systems.",
    "skills": {"Languages": ["Python", "Go"], "Cloud": ["AWS"]},
    "experience": [
        {"company": "Acme", "title": "SWE", "bullets": ["Shipped X."]},
    ],
}


def test_passes_when_all_core_sections_present():
    # A complete tailored output must not raise.
    tailored = {
        "summary": "Backend engineer focused on payments.",
        "skills": {"Languages": ["Python"]},
        "experience": [{"company": "Acme", "title": "SWE", "bullets": ["Did Y."]}],
    }
    ResumeTailor._assert_core_sections_present(_ORIGINAL, tailored)  # no raise


@pytest.mark.parametrize(
    "dropped",
    [
        {"summary": "", "skills": {"L": ["Python"]}, "experience": [{"x": 1}]},
        {"summary": "s", "skills": {}, "experience": [{"x": 1}]},
        {"summary": "s", "skills": {"L": ["Python"]}, "experience": []},
        # skills present as a dict of only-empty lists → still "empty"
        {"summary": "s", "skills": {"L": []}, "experience": [{"x": 1}]},
    ],
)
def test_raises_when_input_present_but_output_dropped_a_core_section(dropped):
    with pytest.raises(ValueError) as exc:
        ResumeTailor._assert_core_sections_present(_ORIGINAL, dropped)
    # Message must name what's missing so the failed job is actionable.
    assert "cut off" in str(exc.value).lower() or "incomplete" in str(exc.value).lower()


def test_no_raise_when_original_also_lacked_the_section():
    # If the ORIGINAL had no projects/summary, an empty output for that section
    # is not a truncation — only input-present/output-empty counts.
    original = {"skills": {"L": ["Python"]}, "experience": [{"x": 1}]}  # no summary
    tailored = {"summary": "", "skills": {"L": ["Python"]}, "experience": [{"x": 1}]}
    ResumeTailor._assert_core_sections_present(original, tailored)  # no raise
