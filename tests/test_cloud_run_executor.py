"""Offline tests for the GCP browser-execution plane's AWS side (defect P5):

  * CloudRunExecutor.submit() — task-enqueue with a MOCK Cloud Tasks client:
    proves the task body carries ONLY {execution_id, capability}, the OIDC token
    targets the private worker URL, and submit() returns SUBMIT_DISPATCHED (it
    does NOT drive a browser). Also proves an unconfigured/dark plane degrades to
    EXECUTOR_UNAVAILABLE (Lambda fallback preserved).
  * executor_capability_service — capability minting, one-use claim, expiry, the
    scoped callback-token verifier, and per-resource one-use issuance flags.
  * auto_apply_service.presubmit_recheck — the pre-submit re-check GATE (mocked
    ledger + executor): every gate re-computed server-side; a single failure
    (kill switch off / drifted approval / dead adapter) blocks the Submit click.

Runs fully offline (mongomock via conftest; no GCP, no AWS, no network).
"""
import json

import pytest

from _vg_support import current_db

import services.application_ledger_service as als
import services.auto_apply_service as aas
import services.executor_capability_service as caps
from services.cloud_run_executor import CloudRunExecutor, set_client
from services.executor_adapter import SUBMIT_DISPATCHED, EXECUTOR_UNAVAILABLE

USER_A = "alice@aspirely.me"
_JD = "We are hiring a backend engineer to build distributed systems in Python."


# ---------------------------------------------------------------------------
# Fake Cloud Tasks client (no GCP)
# ---------------------------------------------------------------------------
class _FakeTasksClient:
    def __init__(self, raise_on_create=False):
        self.created = []
        self._raise = raise_on_create

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def task_path(self, project, location, queue, task):
        return f"{self.queue_path(project, location, queue)}/tasks/{task}"

    def create_task(self, request=None):
        if self._raise:
            raise RuntimeError("cloud tasks boom")
        self.created.append(request)
        return {"name": request["task"]["name"]}


@pytest.fixture(autouse=True)
def _reset():
    yield
    set_client(None)
    aas.set_executor(None)
    current_db()["executor_capabilities"].delete_many({})


def _arm_plane(monkeypatch):
    monkeypatch.setenv("CLOUD_RUN_EXECUTOR_ENABLED", "true")
    monkeypatch.setenv("GCP_PROJECT_ID", "jobpilot-exec-prod")
    monkeypatch.setenv("GCP_TASKS_LOCATION", "us-east1")
    monkeypatch.setenv("GCP_TASKS_QUEUE", "jobpilot-apply-executor")
    monkeypatch.setenv("CLOUD_RUN_WORKER_URL", "https://worker-abc.run.app/task")
    monkeypatch.setenv("GCP_TASKS_INVOKER_SA",
                       "tasks-invoker@jobpilot-exec-prod.iam.gserviceaccount.com")


# ---------------------------------------------------------------------------
# CloudRunExecutor.submit() — enqueue
# ---------------------------------------------------------------------------
def test_submit_enqueues_one_task_with_only_execution_id_and_capability(monkeypatch):
    _arm_plane(monkeypatch)
    client = _FakeTasksClient()
    ex = CloudRunExecutor(client=client)
    assert ex.is_available() is True

    app = {"application_id": "app1"}
    res = ex.submit(app, {"first_name": "Ada"},
                    {"tailoring_record_id": "t1", "user_email": USER_A})

    assert res["status"] == SUBMIT_DISPATCHED
    eid = res["execution_id"]
    assert eid

    # Exactly one task, targeting the private worker URL via OIDC.
    assert len(client.created) == 1
    task = client.created[0]["task"]
    hr = task["http_request"]
    assert hr["url"] == "https://worker-abc.run.app/task"
    assert hr["http_method"] == "POST"
    assert hr["oidc_token"]["audience"] == "https://worker-abc.run.app/task"

    # Body carries ONLY execution_id + capability — no answers/résumé/PII.
    body = json.loads(hr["body"].decode("utf-8"))
    assert set(body) == {"execution_id", "capability"}
    assert body["execution_id"] == eid
    assert body["capability"]

    # The capability persisted (digest-only) and is claimable exactly once.
    row = current_db()["executor_capabilities"].find_one({"execution_id": eid})
    assert row and row["claimed"] is False
    claimed = caps.claim_capability(body["capability"])
    assert claimed["doc"]["application_id"] == "app1"


def test_submit_unavailable_when_plane_not_configured(monkeypatch):
    # Enabled flag on, but no queue/url → is_available False AND submit degrades.
    monkeypatch.setenv("CLOUD_RUN_EXECUTOR_ENABLED", "true")
    monkeypatch.delenv("GCP_TASKS_QUEUE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_WORKER_URL", raising=False)
    client = _FakeTasksClient()
    ex = CloudRunExecutor(client=client)
    assert ex.is_available() is False
    res = ex.submit({"application_id": "app1"}, {}, {"user_email": USER_A})
    assert res["status"] == EXECUTOR_UNAVAILABLE
    assert client.created == []


def test_submit_enqueue_failure_degrades_to_unavailable(monkeypatch):
    _arm_plane(monkeypatch)
    ex = CloudRunExecutor(client=_FakeTasksClient(raise_on_create=True))
    res = ex.submit({"application_id": "app1"}, {},
                    {"tailoring_record_id": "t1", "user_email": USER_A})
    assert res["status"] == EXECUTOR_UNAVAILABLE


def test_submit_missing_owner_is_unavailable(monkeypatch):
    _arm_plane(monkeypatch)
    ex = CloudRunExecutor(client=_FakeTasksClient())
    res = ex.submit({"application_id": "app1"}, {}, None)  # no user_email anywhere
    assert res["status"] == EXECUTOR_UNAVAILABLE


# ---------------------------------------------------------------------------
# Capability: mint / one-use claim / expiry
# ---------------------------------------------------------------------------
def test_capability_mint_and_single_use_claim():
    cap = caps.mint_capability(execution_id="e1", user_email=USER_A,
                               application_id="app1")
    first = caps.claim_capability(cap)
    assert first["doc"]["execution_id"] == "e1"
    assert first["callback_token"]
    # Second claim of the SAME capability → 403 (double-dispatch backstop).
    with pytest.raises(caps.CapabilityError) as ei:
        caps.claim_capability(cap)
    assert ei.value.status == 403


def test_capability_unknown_is_forbidden():
    with pytest.raises(caps.CapabilityError):
        caps.claim_capability("not-a-real-token")


def test_capability_expired_is_forbidden():
    import datetime as _dt
    cap = caps.mint_capability(execution_id="e2", user_email=USER_A,
                               application_id="app2")
    # Backdate expiry into the past (TTL sweep may lag; claim must still refuse).
    current_db()["executor_capabilities"].update_one(
        {"execution_id": "e2"},
        {"$set": {"expires_at": _dt.datetime(2000, 1, 1, tzinfo=_dt.timezone.utc)}})
    with pytest.raises(caps.CapabilityError):
        caps.claim_capability(cap)


# ---------------------------------------------------------------------------
# Callback-token verifier
# ---------------------------------------------------------------------------
def test_callback_token_verifies_and_binds_owner():
    cap = caps.mint_capability(execution_id="e3", user_email=USER_A,
                               application_id="app3")
    token = caps.claim_capability(cap)["callback_token"]
    doc = caps.verify_callback(token)
    assert doc["execution_id"] == "e3"
    assert doc["user_email"] == USER_A          # owner from token, not body
    assert doc["application_id"] == "app3"


def test_callback_token_rejects_garbage_and_empty():
    for bad in ("", "garbage", None):
        with pytest.raises(caps.CapabilityError):
            caps.verify_callback(bad)


# ---------------------------------------------------------------------------
# Per-resource one-use issuance flags
# ---------------------------------------------------------------------------
def test_resource_issuance_is_one_use():
    cap = caps.mint_capability(execution_id="e4", user_email=USER_A,
                               application_id="app4")
    doc = caps.claim_capability(cap)["doc"]

    class _FakeStorage:
        def generate_presigned_get(self, key, expiry=300):
            return f"https://s3/{key}?sig=x"

    import services.s3_service as s3
    s3.get_storage_service = lambda: _FakeStorage()

    url = caps.issue_resume_get(doc, "resumes/app4/tailored.pdf")
    assert url.startswith("https://s3/")
    # Second issuance of the same résumé GET → refused (one-use).
    with pytest.raises(caps.CapabilityError) as ei:
        caps.issue_resume_get(doc, "resumes/app4/tailored.pdf")
    assert ei.value.status == 409


def test_terminal_report_flag_is_one_use():
    caps.mint_capability(execution_id="e5", user_email=USER_A, application_id="app5")
    assert caps.mark_terminal_reported("e5") is True
    assert caps.mark_terminal_reported("e5") is False


# ---------------------------------------------------------------------------
# Pre-submit re-check gate
# ---------------------------------------------------------------------------
class _AvailExecutor:
    def is_available(self):
        return True


def _seat_running_row(db):
    """A row seated in a live execution state with a frozen approval snapshot."""
    db.applications.delete_many({})
    db.auto_apply_answers.delete_many({})
    db.applications.create_index([("application_id", 1)], unique=True,
                                 name="application_id_unique")
    view, _ = als.create_application(
        USER_A, company="GitLab", title="AI Engineer", jd_text=_JD,
        source="greenhouse", ats_family="greenhouse",
        apply_url="https://boards.greenhouse.io/gitlab/jobs/1")
    aid = view["application_id"]
    als.mark_gathering(USER_A, aid)
    als.mark_ready_for_review(USER_A, aid, autonomy="auto", confidence_score=0.95,
                              tailoring_record_id=None)
    aas._system_approve(USER_A, aid)
    als.transition(USER_A, aid, als.STATUS_QUEUED_FOR_EXECUTION)
    als.transition(USER_A, aid, als.STATUS_RUNNING)
    return aid


def test_presubmit_recheck_passes_when_everything_holds(monkeypatch):
    db = current_db()
    aid = _seat_running_row(db)
    monkeypatch.setattr(aas, "_auto_submit_enabled", lambda: True)
    aas.set_executor(_AvailExecutor())

    res = aas.presubmit_recheck(aid, USER_A)
    assert res["ok"] is True, res
    assert res["checks"]["kill_switch"] is True
    assert res["checks"]["row_live"] is True
    assert res["checks"]["approval_fresh"] is True
    assert res["checks"]["adapter_healthy"] is True


def test_presubmit_recheck_blocks_when_kill_switch_off(monkeypatch):
    db = current_db()
    aid = _seat_running_row(db)
    monkeypatch.setattr(aas, "_auto_submit_enabled", lambda: False)
    aas.set_executor(_AvailExecutor())

    res = aas.presubmit_recheck(aid, USER_A)
    assert res["ok"] is False
    assert "master_switch_off" in res["reasons"]


def test_presubmit_recheck_blocks_when_adapter_unavailable(monkeypatch):
    db = current_db()
    aid = _seat_running_row(db)
    monkeypatch.setattr(aas, "_auto_submit_enabled", lambda: True)

    class _Dead:
        def is_available(self):
            return False
    aas.set_executor(_Dead())

    res = aas.presubmit_recheck(aid, USER_A)
    assert res["ok"] is False
    assert "adapter_unavailable" in res["reasons"]


def test_presubmit_recheck_blocks_when_approval_snapshot_drifts(monkeypatch):
    db = current_db()
    aid = _seat_running_row(db)
    monkeypatch.setattr(aas, "_auto_submit_enabled", lambda: True)
    aas.set_executor(_AvailExecutor())

    # Drift: write a recon fingerprint AFTER approval froze a None snapshot, so
    # the current snapshot no longer matches the frozen one.
    db.auto_apply_answers.update_one(
        {"application_id": aid, "user_email": USER_A},
        {"$set": {"application_id": aid, "user_email": USER_A,
                  "recon_fingerprint": "deadbeef", "answers": {"x": "1"}}},
        upsert=True)

    res = aas.presubmit_recheck(aid, USER_A)
    assert res["ok"] is False
    assert "approval_snapshot_stale" in res["reasons"]


def test_presubmit_recheck_blocks_when_row_not_live(monkeypatch):
    db = current_db()
    aid = _seat_running_row(db)
    als.mark_submitted(USER_A, aid)          # already terminal
    monkeypatch.setattr(aas, "_auto_submit_enabled", lambda: True)
    aas.set_executor(_AvailExecutor())

    res = aas.presubmit_recheck(aid, USER_A)
    assert res["ok"] is False
    assert any(r.startswith("row_not_live") for r in res["reasons"])
