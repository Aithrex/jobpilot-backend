"""Prompt-consistency regression net (Craft-overhaul Part D).

An audit found ~19 collisions where résumé prompts contradicted each other or the
deterministic guards — most notably content_augmenter instructing the model to
emit `~15%`/`1K+`, the exact AI-tell tokens the tailor bans, IntegrityGuard
strips, and the scorer penalizes. Part D reconciled every rule onto one authority
(`services.prompt_rules`). These tests lock that reconciliation so the prompts
cannot silently drift apart again.

They inspect prompt *source text* statically — no LLM, no DB.
"""
import inspect

import pytest

from services import prompt_rules, content_augmenter, project_generator
from services.resume_tailor import ResumeTailor, BANNED_PHRASES


# Source of the two modules whose prompts previously fabricated metrics.
_AUG_SRC = inspect.getsource(content_augmenter)
_PROJ_SRC = inspect.getsource(project_generator)

_GAP = {
    "matched_keywords": ["Python", "AWS"],
    "missing_keywords": ["Kafka", "Terraform"],
    "partial_matches": [],
    "coverage_percentage": 40,
    "required_coverage": 50,
    "required_matched": ["Python"],
    "required_missing": ["Kafka"],
}


def _tailor_prompt() -> str:
    kw = ResumeTailor._build_keyword_context(_GAP)
    return ResumeTailor._build_tailor_prompt(
        kw,
        role_context="",
        resume_payload='{"summary": "sample"}',
        jd_json='{"job_title": "Software Engineer"}',
    )


# --- The core reconciliation: no prompt instructs the banned AI-tell tokens ---

@pytest.mark.parametrize("token", ["~15%", "1K+", "50+", "10K+", "~200ms", "~$10K"])
def test_augmenter_no_longer_instructs_fabricated_metric_tokens(token):
    # These literal hedged-metric examples were the anchor collision. They must
    # not appear as instructions in the augmenter prompts anymore.
    assert token not in _AUG_SRC, (
        f"content_augmenter still instructs the banned metric token {token!r}"
    )


def test_project_examples_drop_banned_sub_and_tilde_forms():
    # The 'Good' example bullets modeled 'sub-second' and '~200ms' — banned forms
    # the guard rewrites. They should now use 'under ...' / 'about ...'.
    assert "sub-second" not in _PROJ_SRC
    assert "~200ms" not in _PROJ_SRC


# --- Banned phrases come from ONE tuple, never a hand-copied list ---

def test_banned_phrase_rule_is_derived_from_the_canonical_tuple():
    rule = prompt_rules.banned_phrases_rule()
    # Every canonical banned phrase must be present (a previous hand-copied
    # list in the old alignment pass had drifted — it omitted several).
    for phrase in BANNED_PHRASES:
        assert phrase in rule, f"banned-phrase rule missing canonical entry {phrase!r}"


def test_summary_alignment_uses_the_shared_banned_list_not_a_copy():
    # Guards against re-introducing a divergent hand-copied banned list. The
    # phrase 'the candidate' is in BANNED_PHRASES but was absent from the old
    # inline copy — its presence in the rendered rule proves interpolation.
    assert "the candidate" in prompt_rules.banned_phrases_rule()


# --- Full-resume-regenerating prompts carry the floor rules ---

def test_correction_prompt_carries_the_bullet_floor():
    class _Report:
        hallucinated_experience = []

    prompt = ResumeTailor._build_correction_prompt(
        {"experience": []}, {}, {"summary": "x"}, _Report()
    )
    assert "METRIC HONESTY" in prompt
    assert "NEVER use `~`" in prompt
    assert "end with a period" in prompt


# --- One bullet-length band per section-type (no more 4 disagreeing windows) ---

def test_bullet_length_bands_are_internally_consistent():
    assert prompt_rules.EXPERIENCE_BULLET_MIN_CHARS < prompt_rules.EXPERIENCE_BULLET_MAX_CHARS
    assert prompt_rules.PROJECT_BULLET_MIN_CHARS < prompt_rules.PROJECT_BULLET_MAX_CHARS
    # The rendered rule strings quote the same numbers as the constants.
    exp_rule = prompt_rules.experience_bullet_length_rule()
    assert str(prompt_rules.EXPERIENCE_BULLET_MAX_CHARS) in exp_rule


def test_project_cap_matches_integrity_guard():
    # prompt_rules must not advertise more projects than the guard allows.
    from services.content_augmenter import _MAX_PROJECTS
    assert prompt_rules.MAX_PROJECTS == _MAX_PROJECTS


def test_summary_length_agrees_with_deferred_writer_contract():
    # The shared SUMMARY_RULE is the deferred writer's length band. Keep it
    # locked so a later prompt cannot silently widen to 3-4 sentences.
    assert prompt_rules.SUMMARY_MIN_SENTENCES == 2
    assert prompt_rules.SUMMARY_MAX_SENTENCES == 3
    assert prompt_rules.SUMMARY_MIN_WORDS == 45
    assert prompt_rules.SUMMARY_MAX_WORDS == 70


def test_every_bullet_is_star_defensible_in_tailor_prompt():
    # C2/D4: the XYZ rule was promoted from "the strongest bullets" to EVERY
    # bullet, framed as interview-defensible (STAR). Lock that framing in.
    prompt = _tailor_prompt()
    assert "STAR-DEFENSIBLE" in prompt
    assert "EVERY bullet" in prompt
    assert "interview story" in prompt.lower() or "interviewer can probe" in prompt.lower()


# --- The reconciled floor is actually wired into the augmenter prompts ---

def test_augmenter_prompts_reference_the_shared_kernel():
    # The bullet-writing methods interpolate prompt_rules.* — proven by the
    # module importing the kernel and the rule text appearing in the source.
    assert "prompt_rules" in _AUG_SRC
    assert "METRIC_HONESTY_RULE" in _AUG_SRC
    assert "BANNED_SYMBOLS_RULE" in _AUG_SRC


def test_augmenter_allows_grounded_dollar_metrics():
    # P3E de-SWE: the blanket "NEVER add revenue figures or dollar amounts" rule
    # is gone (it broke finance/sales where revenue IS the metric). Dollars are
    # now allowed ONLY when grounded in the source — never fabricated.
    assert "NEVER add revenue figures or dollar amounts" not in _AUG_SRC
    assert "Dollar figures are allowed ONLY when the source already states them" in _AUG_SRC
