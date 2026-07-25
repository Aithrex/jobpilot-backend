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
