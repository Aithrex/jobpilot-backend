"""CI gates for evals/eval_final_feed_ranking.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))

from eval_final_feed_ranking import (  # noqa: E402
    MIN_ARCHETYPE_NDCG, MIN_MEAN_NDCG, MAX_FALSE_ELIGIBLE, run,
)


@pytest.fixture(scope="module")
def results():
    return run(verbose=False)


def test_mean_ndcg_gate(results):
    assert results["mean_ndcg_at_5"] >= MIN_MEAN_NDCG, results


def test_each_archetype_ndcg_gate(results):
    bad = {k: v for k, v in results["per_archetype_ndcg_at_5"].items()
           if v < MIN_ARCHETYPE_NDCG}
    assert not bad, bad


def test_no_ineligible_above_eligible(results):
    assert results["ineligible_above_eligible"] == 0


def test_false_eligible_rate(results):
    assert results["false_eligible_rate"] <= MAX_FALSE_ELIGIBLE


def test_no_duplicate_variants_in_top_five(results):
    assert results["duplicate_top5_archetypes"] == 0


def test_diversity_preserves_within_employer_order(results):
    assert results["diversity_preserves_within_employer_order"] is True
