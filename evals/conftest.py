"""pytest wiring for evals/ — reuses tests/_vg_support.py.

The support module (sys.path bootstrap, offline env, mongomock patch,
``mock_db`` + ``fake_llm`` fixtures) is imported as the same top-level
``_vg_support`` module that tests/conftest.py uses, so its one-time setup is
shared and never runs twice.
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (
    str(_REPO_ROOT),
    str(_REPO_ROOT / "shared" / "python"),
    str(_REPO_ROOT / "tests"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _vg_support import *  # noqa: F401,F403,E402

import pytest  # noqa: E402

@pytest.fixture(autouse=True)
def _reset_posting_sightings():
    """`posting_sightings` outlives `job_postings` BY DESIGN - it is how a
    re-inserted posting gets its original `first_seen_at` and `job_id` back
    (`job_inventory_service._resume_sighting`). In one shared mongomock
    database that would let one test's sighting re-date another test's
    posting, so every test starts without any."""
    from _vg_support import current_db as _cdb
    try:
        _cdb()["posting_sightings"].delete_many({})
    except Exception:
        pass
    yield
