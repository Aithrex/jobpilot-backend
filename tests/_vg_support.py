"""Shared offline-test support for the Voice Graph test + eval suite.

Imported by BOTH tests/conftest.py and evals/conftest.py (always as the
top-level module ``_vg_support``, so the setup below runs exactly once per
pytest process).

Responsibilities, in order:
  1. sys.path bootstrap — wires ``shared/python`` (the Lambda layer) onto the
     path the same way ``local.py`` does, so ``services.*`` / ``utils.*`` /
     ``schemas.*`` resolve without needing PYTHONPATH.
  2. Offline env — no SSM, no AWS, and a deterministic Fernet key so
     ``services.voicegraph.crypto`` can encrypt/decrypt writing samples without
     a real secret. The whole suite must run with NO network and NO creds.
  3. Mongo — patches ``utils.db_connect.DBConnect.get_db`` to an in-memory
     mongomock database BEFORE any service module is imported (the same
     technique as tests/test_voicegraph_cascade.py; that file keeps its own
     module-level patch and both resolve dynamically, so they coexist —
     the ``mock_db`` fixture always reads whichever mock is currently live).
  4. FakeLLM — a drop-in stand-in for ``services.claude_client.claude_json``
     returning canned, schema-shaped responses and recording every call.
     Real Bedrock is NEVER invoked (there is no moto for Bedrock).
"""
import base64
import os
import sys
from pathlib import Path

# --- 1. sys.path bootstrap (mirror local.py) ---------------------------
_TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = _TESTS_DIR.parent
_SHARED = REPO_ROOT / "shared" / "python"
# Repo root first so ``tests.*`` / ``evals.*`` resolve under CI's
# PYTHONPATH=shared/python (cwd is not guaranteed on sys.path).
for _p in (str(REPO_ROOT), str(_SHARED), str(_TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# --- 2. Offline environment (before ANY utils/services import) -------------
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("USE_SSM_SECRETS", "false")
os.environ.setdefault("VOICE_GRAPH_ENABLED", "true")
# Deterministic 44-char urlsafe Fernet key (32 fixed bytes, base64) so
# services.voicegraph.crypto works offline. Test-only, not a secret.
os.environ.setdefault(
    "INTEGRATION_TOKEN_ENCRYPTION_KEY",
    base64.urlsafe_b64encode(b"voicegraph-eval-suite-static-key").decode("ascii"),
)

# --- 3. Mongo: in-memory mongomock, patched before service imports ---------
import mongomock  # noqa: E402

from utils import db_connect  # noqa: E402

if not getattr(db_connect.DBConnect, "_vg_suite_patched", False):
    _MOCK_CLIENT = mongomock.MongoClient()
    db_connect.DBConnect.get_db = (
        lambda self=None, _c=_MOCK_CLIENT: _c["aspirely_db"]
    )
    db_connect.DBConnect._vg_suite_patched = True

import pytest  # noqa: E402

# Every collection the Voice Graph pipeline touches (+ the parsed-resume
# store), cleared before each test that uses the ``mock_db`` fixture.
VG_COLLECTIONS = (
    "graph_nodes",
    "graph_edges",
    "graph_evidence",
    "writing_samples",
    "style_profiles",
    "authored_resumes",
    "consents",
    "merge_candidates",
    "user_resumes",
)


def current_db():
    """Resolve the live (mock) DB dynamically — never capture it at import
    time, because tests/test_voicegraph_cascade.py installs its own
    module-level mongomock patch and the last import wins."""
    return db_connect.DBConnect().get_db()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Rate limiting is now DISTRIBUTED (Mongo-backed), so the counters
    survive in mongomock across tests in one pytest process just like the
    process-global in-memory limiter does. Clear both before every test so
    route tests stay order-independent."""
    from utils.security import get_rate_limiter

    get_rate_limiter()._requests.clear()
    try:
        current_db()["rate_limit_buckets"].delete_many({})
    except Exception:
        pass
    yield


@pytest.fixture()
def mock_db():
    """A clean in-memory Mongo for one test."""
    db = current_db()
    for c in VG_COLLECTIONS:
        db[c].delete_many({})
    return db


def load_service_app(svc: str):
    """Build a service Lambda's Flask app for a route-level test.

    Each service has its own top-level ``app.py``. A plain ``import app`` caches
    under the shared name ``app`` in ``sys.modules`` — so when the whole suite
    runs in ONE pytest process, the second service's ``import app`` returns the
    first service's module and its routes 404. Load each ``app.py`` under a
    unique module name via importlib so route tests are order-independent.
    """
    import importlib.util

    svc_dir = REPO_ROOT / "services" / svc
    if str(svc_dir) not in sys.path:
        sys.path.insert(0, str(svc_dir))
    spec = importlib.util.spec_from_file_location(f"_svcapp_{svc}", svc_dir / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    flask_app = mod.create_app()
    flask_app.config["TESTING"] = True
    return flask_app


# --- 4. FakeLLM -------------------------------------------------------------
class FakeLLM:
    """Deterministic stand-in for ``services.claude_client.claude_json``.

    Signature mirrors the real facade exactly. Configure per-eval with either:
      * ``queue(resp1, resp2, ...)``   — FIFO canned responses;
      * ``set_handler(fn)``            — fn(call_dict) -> response, for evals
                                         that must react to prompt content;
      * ``fail_with(exc)``             — every call raises (provider outage).

    Every call is recorded in ``self.calls`` (prompt/system/model/temperature/
    schema/max_tokens) so evals can assert the *contract* the pipeline sends —
    e.g. that untrusted writing-sample text only ever appears in the user
    ``prompt``, wrapped in its data tags, and never in ``system``.

    An unconfigured call raises AssertionError: no test may silently reach an
    LLM boundary it didn't declare.
    """

    def __init__(self):
        self.calls = []
        self._queue = []
        self._handler = None
        self._error = None

    # configuration -----------------------------------------------------
    def queue(self, *responses):
        self._queue.extend(responses)
        return self

    def set_handler(self, fn):
        self._handler = fn
        return self

    def fail_with(self, exc):
        self._error = exc
        return self

    # the claude_json-compatible callable --------------------------------
    def __call__(self, prompt=None, max_tokens=8192, temperature=0.3,
                 model=None, max_retries=2, schema=None, parts=None,
                 system=None):
        call = {
            "prompt": prompt,
            "system": system,
            "model": model,
            "temperature": temperature,
            "schema": schema,
            "max_tokens": max_tokens,
            "parts": parts,
        }
        self.calls.append(call)
        if self._error is not None:
            raise self._error
        if self._handler is not None:
            return self._handler(call)
        if self._queue:
            return self._queue.pop(0)
        raise AssertionError(
            "FakeLLM: unexpected LLM call — no canned response configured "
            f"(schema keys: {sorted(schema) if isinstance(schema, dict) else schema})"
        )


@pytest.fixture()
def fake_llm(monkeypatch):
    """Patch claude_json at EVERY import site the Voice Graph uses.

    extract.py and authored.py bind ``claude_json`` into their own module
    namespace at import (``from services.claude_client import claude_json``),
    so patching the facade module alone would not intercept them.
    """
    fake = FakeLLM()
    import services.claude_client as claude_client
    from services.voicegraph import authored as vg_authored
    from services.voicegraph import extract as vg_extract

    monkeypatch.setattr(claude_client, "claude_json", fake)
    monkeypatch.setattr(vg_extract, "claude_json", fake)
    monkeypatch.setattr(vg_authored, "claude_json", fake)
    return fake
