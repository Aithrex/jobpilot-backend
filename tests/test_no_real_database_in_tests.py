"""A test run can never open a real MongoDB connection.

2026-10-06: a test run on a developer machine emptied `users`,
`resume_assets`, `user_preferences`, `companies`, `company_ats_endpoints` and
`job_postings` in PRODUCTION. The suite's fixtures clear collections with
`delete_many({})`; the repo's `.env` names the production cluster; and the
only thing between the two was a monkeypatch of `DBConnect.get_db` that some
path got around. 21 accounts were restored from an Atlas snapshot.

The guard is in the connector itself, so it does not depend on HOW the patch
is bypassed. These tests bypass it three different ways.
"""
import importlib.util
from pathlib import Path

import mongomock
import pytest

from utils import db_connect

SRC = Path(db_connect.__file__)


class _Boom:
    """Stands in for pymongo.MongoClient; constructing it is the failure."""
    calls = 0

    def __init__(self, *a, **k):
        type(self).calls += 1
        raise AssertionError("a real MongoClient was constructed in a test run")


def test_the_suite_database_is_in_memory():
    assert isinstance(db_connect.DBConnect().get_db(), mongomock.Database)


def test_the_unpatched_connector_refuses_under_pytest(monkeypatch):
    """What a fixture reaches if the `get_db` patch is simply not there."""
    monkeypatch.setattr(db_connect, "MongoClient", _Boom)
    monkeypatch.setattr(db_connect.DBConnect, "_client", None)
    _Boom.calls = 0
    with pytest.raises(db_connect.RealDatabaseInTestRun):
        db_connect.DBConnect()._connect()
    assert _Boom.calls == 0


def test_a_second_copy_of_the_module_refuses_too(monkeypatch):
    """A reloaded or re-imported `utils.db_connect` is a NEW class the
    suite's patch never touched. Its `get_db` is the real one."""
    spec = importlib.util.spec_from_file_location("utils.db_connect_copy", SRC,
                                                  submodule_search_locations=None)
    copy = importlib.util.module_from_spec(spec)
    copy.__package__ = "utils"
    spec.loader.exec_module(copy)
    assert not getattr(copy.DBConnect, "_vg_suite_patched", False)
    monkeypatch.setattr(copy, "MongoClient", _Boom)
    _Boom.calls = 0
    with pytest.raises(copy.RealDatabaseInTestRun):
        copy.DBConnect().get_db()
    with pytest.raises(copy.RealDatabaseInTestRun):
        copy.DBConnect().get_collection("users")
    assert _Boom.calls == 0


def test_fixtures_refuse_a_database_that_is_not_mongomock():
    from _vg_support import assert_in_memory

    class Real:  # anything that is not mongomock.Database
        pass
    with pytest.raises(db_connect.RealDatabaseInTestRun):
        assert_in_memory(Real())
    db = db_connect.DBConnect().get_db()
    assert assert_in_memory(db) is db


def test_the_guard_is_off_outside_a_test_run(monkeypatch):
    """Lambda and the ingest box never import pytest; the check must be
    exactly that and nothing an environment variable can flip on by accident."""
    import sys
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delitem(sys.modules, "pytest", raising=False)
    assert db_connect._under_test_runner() is False
