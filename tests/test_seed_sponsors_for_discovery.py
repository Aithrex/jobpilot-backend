"""Every H-1B sponsor is put on the path to being crawled.

On 2026-10-06 we held postings for 1,090 of the 41,133 measurable sponsors in
the USCIS Data Hub, and 39,102 had no employer record at all - so discovery,
which works from `companies`, was never going to look for them.
"""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture()
def seed():
    spec = importlib.util.spec_from_file_location(
        "seed_sponsors", Path(__file__).resolve().parents[1] / "scripts" / "seed_sponsors_for_discovery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_priority_rises_with_approvals_and_never_passes_100(seed):
    assert seed.priority_for(1) == 53
    assert seed.priority_for(100) == 70
    assert seed.priority_for(4450) > seed.priority_for(100) > seed.priority_for(1) >= 50
    assert seed.priority_for(10 ** 9) == 90


def test_state_is_read_from_either_shape(seed):
    assert seed.state_of({"city": "Cupertino", "state": "ca"}) == "CA"
    assert seed.state_of("Mountain View, CA") == "CA"
    assert seed.state_of({"raw": "Austin, TX"}) == "TX"
    assert seed.state_of("") == "" and seed.state_of(None) == ""


def test_a_gap_row_becomes_a_registry_record_with_the_h1b_signal(seed):
    rec = seed.record_from_gap_row({"employer_display": " INNOVA SOLUTIONS INC ", "state": "ga",
                                    "naics_code": "541511", "domain": "InnovaSolutions.com",
                                    "approvals": 218, "bucket": "no_employer_row"})
    assert rec == {"name": "INNOVA SOLUTIONS INC", "state": "GA", "source": "uscis-h1b",
                   "signals": {"h1b": True}, "naics": "541511", "domain": "innovasolutions.com"}


def test_only_sponsors_without_a_board_are_seeded(seed):
    """The first draft matched on name + state and would have re-queued
    Amazon, Microsoft, Google and NVIDIA as 41,017 new companies."""
    assert "covered" not in seed.SEED_BUCKETS
    assert "source_unvalidated" not in seed.SEED_BUCKETS
    assert set(seed.SEED_BUCKETS) == {"no_employer_row", "no_career_source"}
