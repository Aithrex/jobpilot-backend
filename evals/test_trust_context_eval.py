"""CI gates for `eval_trust_context.py` (contextual fraud indicators, trust-1.0.0).

Measured numbers at the time these gates were set (2026-10-21):

  HOLDOUT   (36 rows, written before the rules): first run precision 1.000,
            recall 0.938 (one miss, the "refundable deposit ... for the laptop
            we ship you" row). A general fix for employer-pays vs obligation
            closed it, so HOLDOUT now reads 1.000 / 1.000 and is no longer a
            clean held-out number.
  HOLDOUT_B (20 rows, written after that fix and run ONCE, untuned):
            precision 1.000 (0 false alarms in 11 negatives), recall 0.444
            (4 of 9). That is the honest generalisation figure: these rules are
            a precise, low-recall indicator layer, not a fraud detector.

The gates protect the expensive direction first. A false `suspected_fraud`
on a real posting (a disclaimer, a payments engineer, Meta's WhatsApp team)
is what the old phrase matcher did, so precision is held at 1.0 on both
sets: every negative row is a hard negative a human labelled. Recall gates
are non-regression floors set at the measured values, not targets — raising
them by adding rules that flip a specific row would be memorising the set.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "shared" / "python"), str(ROOT / "evals")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import eval_trust_context as ev  # noqa: E402

MIN_PRECISION = 1.0
MIN_RECALL_A = 0.9
MIN_RECALL_B = 0.44


@pytest.fixture(scope="module")
def a():
    return ev.run(verbose=False)


@pytest.fixture(scope="module")
def b():
    return ev.run(verbose=False, rows=ev.HOLDOUT_B)


def test_dataset_sizes(a, b):
    assert a["rows"] >= 30 and a["negatives"] >= 15
    assert b["rows"] >= 20


def test_no_false_alarms_on_hard_negatives(a, b):
    assert a["precision"] >= MIN_PRECISION, a["false_alarms"]
    assert b["precision"] >= MIN_PRECISION, b["false_alarms"]


def test_recall_floors(a, b):
    assert a["recall"] >= MIN_RECALL_A, a["misses"]
    assert b["recall"] >= MIN_RECALL_B, b["misses"]


def test_hits_carry_the_right_code(a, b):
    assert a["code_accuracy_on_hits"] == 1.0
    assert b["code_accuracy_on_hits"] == 1.0
