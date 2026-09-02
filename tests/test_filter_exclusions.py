"""P0 regression: an excluded term can never ADD a job to the result set.

`filter_suggester_service` assigned the LLM synthesizer's `anti_keywords` to
`custom_role_terms`. That field is POSITIVE everywhere it is read:

  * `daily_pipeline_service._role_match` admits a posting whose title matches
    one (otherwise the row is dropped as "no role-family match");
  * `_filter_and_score` adds +15 to any row whose only family is a custom term;
  * it overrides the healthcare/non-tech noise drop;
  * on the Apify-free scheduled path it is sent to Workday AS A SEARCH TERM.

So "exclude nursing" produced: search Workday for nursing, admit nursing rows
that the noise filter would have dropped, and boost them by 15. These tests
pin the inversion shut at every one of those four points.
"""
from __future__ import annotations

import pytest

from services import daily_pipeline_service as dps


def _rec(title: str, **kw):
    base = {
        "source": "Workday Direct", "company": "Acme Health", "title": title,
        "location": "Austin, TX", "posted": "", "salary": "-",
        "applicants": "",
        "url": f"https://example.com/{title.replace(' ', '-')}",
        "description": "",
    }
    base.update(kw)
    return base


_SCORE_KW = {"experience_level": "any", "past_days": 30}


# --------------------------------------------------------------------------
# 1. the matcher itself
# --------------------------------------------------------------------------

def test_exclude_hit_is_word_bounded():
    assert dps._exclude_term_hit("Registered Nurse", ["nurse"]) == "nurse"
    assert dps._exclude_term_hit("Nurse Practitioner", ["Nurse"]) == "Nurse"


def test_exclude_does_not_match_inside_a_longer_word():
    # "ml" must not suppress "HTML Developer"; this is why the negative
    # matcher is stricter than `_custom_term_hit`, which has a loose
    # token-overlap fallback.
    assert dps._exclude_term_hit("HTML Developer", ["ml"]) is None
    assert dps._exclude_term_hit("Senior Sales Engineer", ["ale"]) is None


def test_exclude_ignores_blank_terms():
    assert dps._exclude_term_hit("Software Engineer", ["", "   ", None]) is None


def test_exclude_no_terms_is_none():
    assert dps._exclude_term_hit("Software Engineer", None) is None
    assert dps._exclude_term_hit("Software Engineer", []) is None


# --------------------------------------------------------------------------
# 2. an excluded term REMOVES, and can never ADD
# --------------------------------------------------------------------------

def test_excluded_title_is_dropped():
    apply_now, verify, excluded = dps._filter_and_score(
        [_rec("Registered Nurse"), _rec("Backend Software Engineer")],
        cutoff="2020-01-01", **_SCORE_KW,
        exclude_title_terms=["nurse"],
    )
    kept = [r["title"] for r in apply_now + verify]
    assert "Registered Nurse" not in kept
    assert any(r["title"] == "Registered Nurse"
               and "excluded title term" in r["reason"] for r in excluded)


def test_excluded_term_cannot_admit_a_job_via_custom_role_match():
    """The inversion, stated directly.

    With the bug, passing ["nurse"] as the anti-keyword list meant
    `custom_role_terms=["nurse"]`, which made `_role_match` return
    `["Custom: nurse"]` and the row survived. Passing it in the CORRECT field
    must produce the opposite result from the same input.
    """
    records = [_rec("Registered Nurse - ICU")]

    # the bug's behaviour, reproduced deliberately: as a POSITIVE term it is
    # admitted (this is what the field means, and is correct for that field)
    as_positive, verify_p, _ = dps._filter_and_score(
        records, cutoff="2020-01-01", custom_role_terms=["nurse"], **_SCORE_KW)
    assert [r["title"] for r in as_positive + verify_p] == ["Registered Nurse - ICU"]

    # the same token in the NEGATIVE field must remove it
    as_negative, verify_n, excluded = dps._filter_and_score(
        records, cutoff="2020-01-01", exclude_title_terms=["nurse"], **_SCORE_KW)
    assert as_negative == [] and verify_n == []
    assert len(excluded) == 1


def test_exclusion_beats_the_custom_term_override():
    """A term in BOTH lists must be excluded. Exclusions run first and are
    unconditional - nothing downstream may readmit."""
    _, _, excluded = dps._filter_and_score(
        [_rec("Registered Nurse")],
        cutoff="2020-01-01", **_SCORE_KW,
        custom_role_terms=["nurse"],
        exclude_title_terms=["nurse"],
    )
    assert len(excluded) == 1
    assert "excluded title term" in excluded[0]["reason"]


def test_exclusion_beats_the_plus_15_target_bonus():
    kept, verify, _ = dps._filter_and_score(
        [_rec("Registered Nurse"), _rec("Staff Nurse")],
        cutoff="2020-01-01", **_SCORE_KW,
        custom_role_terms=["Registered Nurse", "Staff Nurse"],
        exclude_title_terms=["nurse"],
    )
    assert kept == [] and verify == []


def test_unrelated_jobs_survive_an_exclusion():
    kept, verify, _ = dps._filter_and_score(
        [_rec("Backend Engineer"), _rec("Registered Nurse")],
        cutoff="2020-01-01", **_SCORE_KW,
        exclude_title_terms=["nurse"],
    )
    titles = [r["title"] for r in kept + verify]
    assert "Backend Engineer" in titles


# --------------------------------------------------------------------------
# 3. the suggester never routes anti_keywords into the positive field
# --------------------------------------------------------------------------

def test_suggester_keeps_anti_keywords_out_of_custom_role_terms(monkeypatch):
    import sys, types
    sys.path.insert(0, "services/jobs")
    from services import filter_suggester_service as fss

    monkeypatch.setattr(fss, "_synthesize_titles_from_resume", lambda r, p: {
        "detected_role": "Backend Engineer",
        "rationale": "test",
        "seniority": "mid",
        "workday_titles": [f"Backend Engineer {i}" for i in range(25)],
        "linkedin_phrases": [f"phrase {i}" for i in range(6)],
        "anti_keywords": ["nurse", "sales", "retail"],
    })

    out = fss.suggest_filters({
        "contact": {"name": "T"}, "summary": "Backend engineer",
        "skills": {"lang": ["Python", "Go"]},
        "experience": [{"title": "Backend Engineer", "company": "X", "bullets": []}],
    })

    assert out["exclude_title_terms"] == ["nurse", "sales", "retail"]
    lowered = {t.lower() for t in out["custom_role_terms"]}
    assert not (lowered & {"nurse", "sales", "retail"}), \
        "anti-keywords leaked into the POSITIVE custom_role_terms field"


def test_suggester_always_emits_the_exclusion_field():
    """Even on the static-seed fallback the key must exist, so the frontend
    contract does not depend on which branch ran."""
    import sys
    sys.path.insert(0, "services/jobs")
    from services import filter_suggester_service as fss
    out = fss._coerce({})
    assert "exclude_title_terms" in out
    assert out["exclude_title_terms"] == []
