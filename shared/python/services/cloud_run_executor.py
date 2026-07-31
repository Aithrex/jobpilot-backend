"""CloudRunExecutor — the ASYNC-DISPATCH ExecutorAdapter for the GCP browser-
execution plane (defect P5).

Topology:  AWS orchestrator (resume Lambda)
              -> WIF (this Lambda's IAM role federates into a GCP SA, NO static
                 key) -> Cloud Tasks queue -> PRIVATE Cloud Run Playwright worker.

Unlike the in-process executors (Stub / Ats / Browser) and the synchronous
``LambdaInvokeExecutor``, this adapter does NOT drive a browser and does NOT
block on the outcome. ``submit()`` mints a one-use capability, enqueues a single
Cloud Task whose HTTP target is the private Cloud Run service, and returns
``SUBMIT_DISPATCHED`` immediately. The worker later:

  * claims the capability (one-use lease — the double-dispatch backstop),
  * fetches a minimal envelope + one-use presigned résumé GET + (if needed) a
    one-use decrypted ATS credential,
  * re-checks the pre-submit gate, clicks Submit, uploads a receipt via a one-use
    presigned PUT, and
  * reports the TERMINAL outcome (submitted / submission_unknown / wall / failed)
    on the executor callback surface, which finalizes the ledger.

So this file owns exactly ONE thing: minting the capability + enqueuing the task.
Everything security-sensitive (envelope, artifacts, credentials, the terminal
finalize) lives behind the callback surface (``executor_capability_service`` +
``blueprints/executor.py``), reachable only with the capability/callback token.

Any transport failure (missing config, client import error, queue error) degrades
to the contract's clean ``EXECUTOR_UNAVAILABLE`` — it NEVER raises for an expected
condition, so a mis-provisioned plane just leaves the ledger row in its approved/
running state for a later attempt, exactly like the Lambda fallback.

Wiring: ``AUTO_APPLY_EXECUTOR=cloud_run`` selects this in ``get_executor()``.
Config (SSM/env):
  CLOUD_RUN_EXECUTOR_ENABLED   "true" to arm dispatch (dark by default).
  GCP_PROJECT_ID               GCP project hosting the plane.
  GCP_TASKS_LOCATION           Cloud Tasks + Cloud Run region (e.g. us-east1).
  GCP_TASKS_QUEUE              queue id (e.g. jobpilot-apply-executor).
  CLOUD_RUN_WORKER_URL         https URL of the private Cloud Run service (+/task).
  GCP_TASKS_INVOKER_SA         SA email whose OIDC token the task carries (the
                               audience is CLOUD_RUN_WORKER_URL).
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

from services.executor_adapter import (
    ExecutorAdapter,
    EXECUTOR_UNAVAILABLE,
    SUBMIT_DISPATCHED,
)
from services import executor_capability_service as caps

logger = logging.getLogger(__name__)

_client = None


def _cfg(key: str, default: str = "") -> str:
    from utils.config import _get_config_value
    return (_get_config_value(key, default) or default).strip()


def _enabled() -> bool:
    return _cfg("CLOUD_RUN_EXECUTOR_ENABLED", "false").lower() in (
        "1", "true", "yes", "on")


def _tasks_client():
    """Lazily build the Cloud Tasks client (google-cloud-tasks ships in the
    worker image, NOT the resume layer's default deps — import defensively so a
    missing dep degrades to unavailable rather than crashing import)."""
    global _client
    if _client is None:
        from google.cloud import tasks_v2  # lazy: optional dep
        _client = tasks_v2.CloudTasksClient()
    return _client


def set_client(client) -> None:
    """Test seam: inject a fake Cloud Tasks client."""
    global _client
    _client = client


class CloudRunExecutor(ExecutorAdapter):
    name = "cloud-run"

    def __init__(self, *, client: Optional[Any] = None):
        if client is not None:
            set_client(client)

    # -- liveness ------------------------------------------------------------
    def is_available(self) -> bool:
        # Config flag only (cheap; no network probe). Dispatch still fails safe
        # to unavailable if the queue/URL are missing at submit() time.
        return _enabled() and bool(_cfg("CLOUD_RUN_WORKER_URL")) \
            and bool(_cfg("GCP_TASKS_QUEUE"))

    # -- recon / oracle hooks: the Cloud Run plane is submit-only for now -----
    # Recon stays on the cheap HTTP path (AtsExecutor) in the orchestrator; this
    # adapter reports unavailable for recon/account/verify so those flows fall
    # through to their existing handlers unchanged.
    def recon_form(self, apply_url: str, ats_family: Optional[str]) -> Dict[str, Any]:
        return {"questions": [], "needs_account": False,
                "status": EXECUTOR_UNAVAILABLE}

    def create_account(self, user_email, ats_family, tenant, credentials) -> Dict[str, Any]:
        return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                "wall": None, "error": None}

    def verify_state(self, application, freeze) -> Dict[str, Any]:
        return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                "wall": None, "error": None}

    # -- submit = mint capability + enqueue ONE Cloud Task -------------------
    def submit(self, application: Dict[str, Any], answers: Dict[str, Any],
               resume_pdf: Any) -> Dict[str, Any]:
        """Dispatch the submission to the Cloud Run worker. Returns
        SUBMIT_DISPATCHED on a successful enqueue (the worker drives the browser
        and finalizes via the callback surface). Any misconfig/transport error →
        EXECUTOR_UNAVAILABLE (row untouched, retried later)."""
        application_id = str(application.get("application_id") or "")
        user_email = (resume_pdf.get("user_email")
                      if isinstance(resume_pdf, dict) else None) \
            or application.get("user_email") or ""
        if not application_id or not user_email:
            logger.warning("cloud_run submit: missing application_id/user_email")
            return self._unavailable()

        project = _cfg("GCP_PROJECT_ID")
        location = _cfg("GCP_TASKS_LOCATION", "us-east1")
        queue = _cfg("GCP_TASKS_QUEUE")
        worker_url = _cfg("CLOUD_RUN_WORKER_URL")
        invoker_sa = _cfg("GCP_TASKS_INVOKER_SA")
        if not (project and queue and worker_url):
            logger.warning("cloud_run submit: plane not configured — unavailable")
            return self._unavailable()

        # One execution id per dispatch (also the Cloud Tasks task name → de-dupes
        # a redelivered enqueue at the queue layer, belt-and-suspenders with the
        # one-use capability claim).
        execution_id = uuid.uuid4().hex

        try:
            capability = caps.mint_capability(
                execution_id=execution_id, user_email=user_email,
                application_id=application_id)
        except Exception as e:
            logger.error("cloud_run submit: capability mint failed: %s", e)
            return self._unavailable()

        # The task body carries ONLY the execution id + the one-use capability —
        # never answers, résumé bytes, credentials or PII. The worker pulls those
        # from the callback surface with the capability.
        payload = json.dumps({"execution_id": execution_id,
                              "capability": capability}).encode("utf-8")

        try:
            client = _tasks_client()
            parent = client.queue_path(project, location, queue)
            http_request: Dict[str, Any] = {
                "http_method": "POST",
                "url": worker_url,
                "headers": {"Content-Type": "application/json"},
                "body": payload,
            }
            if invoker_sa:
                # OIDC so the PRIVATE (no-unauth) Cloud Run service accepts it;
                # audience = the worker URL.
                http_request["oidc_token"] = {
                    "service_account_email": invoker_sa,
                    "audience": worker_url,
                }
            task: Dict[str, Any] = {
                "name": client.task_path(project, location, queue, execution_id),
                "http_request": http_request,
            }
            client.create_task(request={"parent": parent, "task": task})
        except Exception as e:
            logger.error("cloud_run submit: enqueue failed: %s", e)
            return self._unavailable()

        logger.info("cloud_run submit: dispatched execution_id=%s app=%s",
                    execution_id, application_id)
        try:
            from services import apply_metrics
            apply_metrics.emit("queue_ack", execution_id=execution_id,
                               application_id=application_id, adapter=self.name,
                               outcome="enqueued")
        except Exception:  # pragma: no cover - metrics never load-bearing
            pass
        return {"status": SUBMIT_DISPATCHED, "execution_id": execution_id,
                "confirmation": None, "screenshot_ref": None,
                "wall": None, "error": None}

    def _unavailable(self) -> Dict[str, Any]:
        return {"status": EXECUTOR_UNAVAILABLE, "confirmation": None,
                "screenshot_ref": None, "wall": None, "error": None}
