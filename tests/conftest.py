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
