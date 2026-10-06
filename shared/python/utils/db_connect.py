import logging
import os
import sys

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure
from .config import DBConfig

logger = logging.getLogger(__name__)


def _pool_size() -> int:
    """Connections one process may hold open to Atlas.

    PyMongo's default is 100 PER CLIENT, which is right for Lambda: each
    container serves one request at a time and only ever needs a handful. It is
    the wrong shape for the long-lived ingest runner, where 16 concurrent
    shards x ~12 worker threads share ONE client and can queue against the
    pool. Explicit and env-tunable so the box and the Lambdas can differ
    without a code change, and so the value is visible next to the Atlas tier's
    own connection ceiling rather than implied.
    """
    try:
        return max(1, int(os.environ.get("MONGO_MAX_POOL_SIZE", "") or 100))
    except (TypeError, ValueError):
        return 100


def _under_test_runner() -> bool:
    """True inside a pytest process. Lambda and the ingest box never import
    pytest, so this can only be true where tests are running."""
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


class RealDatabaseInTestRun(RuntimeError):
    """A test run tried to open a real MongoDB connection."""


class DBConnect:
    """MongoDB connection manager"""
    _client = None

    def __init__(self):
        self.config = DBConfig.DATABASE_CONFIG
        self.mongo_uri = self.config['mongo_uri']
        self.db_name = self.config['db_name']

    def _connect(self):
        """Establish MongoDB connection if not already connected"""
        if DBConnect._client is None:
            # NEVER a real database from a test run. The suite's fixtures clear
            # collections with `delete_many({})`, and a developer's `.env`
            # names the production cluster: on 2026-10-06 a test run that got
            # past the mongomock patch emptied `users`, `resume_assets`,
            # `user_preferences`, `companies` and `job_postings` in production
            # (restored from an Atlas snapshot). The patch in
            # `tests/_vg_support.py` replaces `get_db`, so reaching this line
            # under pytest means that patch was bypassed - a reloaded module, a
            # test collected outside `tests/`, a second copy of this class.
            # There is deliberately no switch to turn this off.
            if _under_test_runner():
                raise RealDatabaseInTestRun(
                    "DBConnect refused to open a real MongoDB connection inside "
                    "a test run: the mongomock patch (tests/_vg_support.py) is "
                    "not in effect for this process. Tests must never reach a "
                    "real database.")
            try:
                DBConnect._client = MongoClient(
                    self.mongo_uri, maxPoolSize=_pool_size())
                # Verify connection
                DBConnect._client.admin.command('ping')
                logger.info("MongoDB successfully connected")
            except ConnectionFailure as e:
                logger.error(f"MongoDB connection failed: {e}")
                raise
            except Exception as e:
                logger.error(f"MongoDB initialization error: {e}")
                raise
        return DBConnect._client

    def get_db(self):
        """Get database instance"""
        client = self._connect()
        return client[self.db_name]

    def get_collection(self, collection_name):
        """Get a specific collection"""
        db = self.get_db()
        return db[collection_name]
