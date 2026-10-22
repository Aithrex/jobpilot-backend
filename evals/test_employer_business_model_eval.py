"""CI gate for `eval_employer_business_model.py`, set from its MEASURED result.

Measured 2026-10-21 on the 60-company holdout with the holdout removed from
the seed (unseen companies): precision 5/5, recall 5/21, false exclusions of
operating employers 0/28. The gates are exactly what that justifies:

* ZERO false exclusions of the held-out operating employers. A false
  exclusion hides a real direct job; 0/28 was measured and is held.
* Recall never drops below the measured 5/21. This is a REGRESSION FLOOR,
  not a quality claim: on unseen companies the name rule is the only basis
  that can fire, and most staffing firms are not named "Staffing". Recall on
  known companies comes from the register seed and is not measured here.
* The review queue never enters binary truth and is never predicted
  `operating` (it may be unknown, mixed or a domain-resolved fact).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eval_employer_business_model as ev  # noqa: E402


def test_no_false_exclusions_on_held_out_operating_employers():
    res = ev.evaluate()
    assert res["negatives"] == 28
    assert res["fp"] == 0, res["false_exclusions"]


def test_recall_floor_on_unseen_companies():
    res = ev.evaluate()
    assert res["positives"] == 21
    assert res["tp"] >= 5, res["misses"]


def test_review_queue_is_never_called_operating():
    res = ev.evaluate()
    assert res["ambiguous_rows"] == 11
    assert not any(k.endswith(":operating") for k in res["ambiguous_predictions"])
