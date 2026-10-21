"""pytest wiring for tests/ — all real setup lives in tests/_vg_support.py.

_vg_support bootstraps sys.path (shared/python, like local.py), sets the
offline env (no SSM / no AWS / deterministic Fernet key), patches Mongo to
mongomock BEFORE any service import, and defines the ``mock_db`` and
``fake_llm`` fixtures. evals/conftest.py imports the same module, so the
setup runs once per process.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE.parent / "shared" / "python"), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _vg_support import *  # noqa: F401,F403,E402

import pytest  # noqa: E402
from _vg_support import current_db  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """Process-global caches that outlive a test's database.

    The suite shares ONE mongomock database and clears DOCUMENTS between
    tests. Anything cached in a module global survives that, so a test that
    warms a cache pins the answer for every later test whose database looks
    nothing like it. `tech_feed_service`'s coverage block and
    `canonical_employer`'s board map are both TTL-cached for exactly the right
    reason in production and exactly the wrong one here.
    """
    for module, fn in (("services.tech_feed_service", "reset_coverage_cache"),
                       ("services.canonical_employer", "reset_cache")):
        try:
            import importlib
            getattr(importlib.import_module(module), fn)()
        except Exception:
            pass
    yield


@pytest.fixture(autouse=True)
def _reset_mongomock_indexes():
    """Test isolation for the shared mongomock singleton.

    The whole suite runs against ONE mongomock client (the ``DBConnect`` patch in
    ``_vg_support``). Per-test ``db`` fixtures clear DOCUMENTS with
    ``delete_many({})`` — but mongomock keeps a collection's INDEXES on that
    shared object, and ``delete_many`` never drops them. So a UNIQUE index built
    by one test's fixture (e.g. the ledger fixtures' ``(user_email,
    job_identity)`` unique index) survives into an unrelated later test and
    rejects its inserts with E11000 — the exact cross-test pollution that made
    ``test_reader_dualread.py::TestBackfill::test_outcome_projection_and_idempotent_rerun``
    fail in the full run but pass alone (the backfill's canonical rows carry no
    ``job_identity``, so the second row collided on the leaked ``(user, null)``
    key).

    Drop every secondary (non-``_id``) index on every collection BEFORE each test
    so each test starts from a pristine index state. Every index-creating fixture
    in the suite is function-scoped and rebuilds its own indexes at setup (which
    runs after this autouse fixture), so nothing that legitimately needs an index
    loses it. Best-effort and offline — never touches the network.
    """
    db = current_db()
    try:
        names = db.list_collection_names()
    except Exception:
        names = []
    for name in names:
        try:
            db[name].drop_indexes()
        except Exception:
            pass
    yield


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
