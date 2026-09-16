"""CI gates for the two expanded evaluations.

The runners (`eval_live_titles.py`, `eval_fit_score_domains.py`) are meant to
be run by hand and read — they print distributions, per-component tables and
the heaviest misses, which is what you want when you are changing a scorer.
This file is the part that has to fail a build, so it asserts the gates and
nothing else.

Both are offline and deterministic: checked-in datasets, no database, no
network, no LLM. They belong in the same suite as everything else.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "shared" / "python"), str(ROOT / "evals"),
           str(ROOT / "evals" / "data")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import eval_fit_score_domains as fit_eval      # noqa: E402
import eval_live_titles as title_eval          # noqa: E402


@pytest.fixture(scope="module")
def titles():
    return title_eval.run(verbose=False)


@pytest.fixture(scope="module")
def fit():
    return fit_eval.run(verbose=False)


class TestLiveTitleClassification:
    """559 live job titles, each reviewed by a human."""

    def test_the_dataset_is_at_least_five_hundred_reviewed_titles(self, titles):
        assert titles["dataset_rows"] >= 500

    def test_scope_accuracy(self, titles):
        assert titles["scope_accuracy"] >= title_eval.MIN_SCOPE_ACCURACY

    def test_the_reviewer_corrections_still_hold(self, titles):
        """The rows where a human disagreed with the classifier ARE the known
        defects. A change that improves the headline while regressing these
        has traded a measured bug for an unmeasured one."""
        assert (titles["reviewer_override_accuracy"]
                >= title_eval.MIN_OVERRIDE_ACCURACY)

    def test_no_non_technical_title_reads_as_core(self, titles):
        """The expensive direction: a Pharmacy Technician in a backend feed."""
        assert titles["non_tech_leakage"] <= title_eval.MAX_NON_TECH_LEAKAGE


class TestFitScoreAcrossDomains:
    """115 labelled scenarios across the fourteen technical domains."""

    def test_the_scenario_set_covers_the_named_domains(self, fit):
        assert fit["scenarios"] >= 100
        assert fit["domains"] >= 14

    def test_no_reachable_job_is_gated_out(self, fit):
        """A false INELIGIBLE removes a job the candidate could have taken,
        silently and unrecoverably. Held at zero."""
        assert fit["eligibility"]["false_ineligible"] == []

    def test_hard_gates_still_close(self, fit):
        assert (fit["eligibility"]["false_eligible_rate"]
                <= fit_eval.MAX_FALSE_ELIGIBLE_RATE)

    def test_a_stated_hard_requirement_is_surfaced(self, fit):
        """Unknown is surfaced, never priced — and surfacing is the half that
        can silently go missing."""
        assert fit["hard_requirements_not_surfaced"] == []

    def test_ranking(self, fit):
        assert (fit["ranking"]["mean_precision_at_k"]
                >= fit_eval.MIN_MEAN_PRECISION_AT_K)
        assert fit["ranking"]["mean_ndcg_at_k"] >= fit_eval.MIN_MEAN_NDCG_AT_K

    def test_scores_are_monotone_in_the_human_grade(self, fit):
        c = fit["calibration"]
        assert c["monotone"], c["mean_score_by_grade"]
        assert c["min_separation"] >= fit_eval.MIN_GRADE_SEPARATION

    def test_experience_separates_relevant_from_irrelevant(self, fit):
        """The brief's own criterion. Before `_EXPERIENCE_RELEVANCE` this was
        3.62 points out of 20: the component gave an irrelevant match 81% of
        its maximum, because it measured calendar time and nothing else."""
        assert (fit["experience_separation"]
                >= fit_eval.MIN_EXPERIENCE_SEPARATION)

    def test_every_resume_is_classifiable(self, fit):
        """The bug this gate exists for, stated as a test.

        A résumé whose titles the phrase table cannot read scores
        `role_alignment: unknown` against EVERY posting. `_normalized_score`
        excludes unknown - correctly - so the largest component silently
        leaves the average and the score becomes skill-overlap noise. Nothing
        errors, and every other metric in this file stays green, because they
        are all pool-relative. Measured live: 4,000 postings, 4,000 unknowns,
        and not one Data Scientist role in the candidate's top 25.
        """
        assert fit["unreadable_resumes"] == [], fit["resume_alignment_known"]
        assert (fit["role_alignment_known"]
                >= fit_eval.MIN_ROLE_ALIGNMENT_KNOWN)

    def test_no_component_is_inverted(self, fit):
        """A component that scores irrelevant matches HIGHER than relevant
        ones is worse than absent, and invisible in the total."""
        inverted = {c: m["separation"] for c, m in fit["components"].items()
                    if m["separation"] < 0}
        assert not inverted, inverted
