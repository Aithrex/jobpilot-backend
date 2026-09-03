"""Company intake never mints canonical ids from a name match."""
from __future__ import annotations

import pytest

from _vg_support import current_db
from services import company_discovery_intake as intake


@pytest.fixture(autouse=True)
def clean():
    db = current_db()
    db[intake.CANDIDATES].delete_many({})
    yield


def test_two_similar_names_stay_two_candidates():
    a = intake.upsert_candidate({
        "company_name": "OpenEvidence", "owner_domain": "openevidence.com",
        "official_career_url": "https://www.openevidence.com/",
    })
    b = intake.upsert_candidate({
        "company_name": "Open Evidence", "owner_domain": "open-evidence.com",
        "official_career_url": "https://open-evidence.com/",
    })
    assert a["intake_key"] != b["intake_key"]
    assert b["possible_name_collisions"]


def test_researched_seed_states():
    rows = {r["owner_domain"]: r for r in intake.seed_researched_intake()}
    assert rows["ssi.inc"]["verification_state"] == intake.STATE_CANDIDATE
    assert rows["thinkingmachines.ai"]["redirect_history"]
    assert rows["openevidence.com"]["verification_state"] == intake.STATE_BLOCKED
    assert "not open-evidence.com" in rows["openevidence.com"]["verification_reason"].lower() or \
        "open-evidence.com" in rows["openevidence.com"]["verification_reason"]
