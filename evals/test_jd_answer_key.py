"""CI gate over the JD answer key. Floors sit just under the held-out score, so
an accuracy drop fails the build. Raise a floor when a reader improves; lowering
one needs a reason in the commit message.

The key's labels are drafts until a person reviews them (`review` on each row),
so these floors guard against regression and are not a claim of final accuracy.
"""
import pytest

import eval_jd_answer_key as key_eval

FLOORS = {
    "sponsorship": 0.97,
    "authorization": 0.88,
    "min_degree": 0.80,
    "min_years": 0.78,
    "clearance_required": 0.93,
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
    assert not_offered["precision"] >= 0.95
    assert not_offered["recall"] >= 0.97


def test_the_key_keeps_its_held_out_half():
    rows = key_eval.load_key()
    assert len(rows) == 300
    assert sum(r["split"] == "test" for r in rows) == 150
