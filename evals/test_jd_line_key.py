"""CI gate for the line reader. Floors sit just under the measured scores on JDs the model
never saw, so a change that makes it worse fails, and each floor is meant to rise."""
import json

import pytest

from evals import eval_jd_line_key as ev


@pytest.fixture(scope="module")
def key():
    return ev.load_key()


@pytest.fixture(scope="module")
def test_score(key):
    return ev.score([r for r in key if r["split"] == "test"])


def test_the_key_is_what_it_says(key):
    assert len(key) == 100
    assert sum(r["split"] == "dev" for r in key) == 50
    assert sum(r["split"] == "test" for r in key) == 50
    assert sum("second_reader" in r for r in key) == 20
    for row in key:
        assert len(row["lines"]) == len(row["roles"])
        assert set(row["roles"]) <= set(ev.ROLES) | {"other"}


def test_labels_are_marked_as_unreviewed_until_a_person_checks_them(key):
    assert all(row["review"] in ("draft", "reviewed") for row in key)


def test_it_reads_lines_it_never_saw(test_score):
    assert test_score["accuracy"] >= FLOOR_ACCURACY


def test_it_finds_what_the_job_does(test_score):
    assert test_score["does"][2] >= FLOOR_DOES


def test_it_finds_what_the_job_asks_for(test_score):
    assert test_score["asks"][2] >= FLOOR_ASKS


def test_required_and_preferred_stay_apart(test_score):
    assert test_score["required_v_preferred"] >= FLOOR_REQUIRED_V_PREFERRED


def test_little_reaches_a_resume_prompt_that_should_not(test_score):
    assert test_score["leak"] <= CEILING_LEAK


def test_it_stays_under_what_two_readers_agree_on(key, test_score):
    """Scoring above the ceiling would mean the model memorised the key."""
    assert test_score["accuracy"] <= ev.ceiling(key)["accuracy"]


def test_the_key_never_trains_the_first_stage():
    """Only the second stage may see blind lines: the first must meet them as evidence."""
    from pathlib import Path

    source = (Path(ev.ROOT) / "scripts" / "train_jd_line_model.py").read_text(encoding="utf-8")
    assert 'key_rows("test")' not in source
    assert "first = stage_one(*fit(_load_silver(" in source


# Measured once on the test split: accuracy .808, does .910, asks .884,
# required v preferred .924, leak .021.
FLOOR_ACCURACY = 0.78
FLOOR_DOES = 0.88
FLOOR_ASKS = 0.85
FLOOR_REQUIRED_V_PREFERRED = 0.89
CEILING_LEAK = 0.03
