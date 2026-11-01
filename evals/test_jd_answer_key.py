"""CI gate over the JD answer key. Floors sit just under the held-out score, so
an accuracy drop fails the build. Raise a floor when a reader improves; lowering
one needs a reason in the commit message.

The key's labels are drafts until a person reviews them (`review` on each row),
so these floors guard against regression and are not a claim of final accuracy.
"""
import pytest

import eval_jd_answer_key as key_eval

# Measured on all 750 rows, which include the tuning rows, so they read higher
# than the held-out score in docs/specs/jd-profile.md.
FLOORS = {
    "sponsorship": 0.98,
    "authorization": 0.95,
    "min_degree": 0.86,
    "min_years": 0.84,
    "clearance_required": 0.97,
    "work_mode": 0.87,
    "employment_type": 0.83,
}


@pytest.fixture(scope="module")
def result():
    return key_eval.score(key_eval.load_key())


@pytest.mark.parametrize("field,floor", sorted(FLOORS.items()))
def test_field_stays_above_its_floor(result, field, floor):
    assert result["accuracy"][field] >= floor


def test_a_stated_refusal_is_almost_never_missed_or_invented(result):
    # A false `not_offered` hides a reachable job from a visa-holding candidate,
    # and a missed one sends them to an application that cannot succeed.
    not_offered = result["sponsorship"]["not_offered"]
    assert not_offered["precision"] >= 0.96
    assert not_offered["recall"] >= 0.97


def test_the_key_keeps_its_held_out_half():
    rows = key_eval.load_key()
    assert len(rows) == 750
    assert sum(r["split"] == "holdout" for r in rows) == 150
