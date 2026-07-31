"""LambdaInvokeExecutor — the thin remote proxy the resume Lambda uses to reach
the browser fleet running in the dedicated ``jobpilot-apply-executor`` container
Lambda.

The orchestrator (ledger/autonomy/freeze) stays in the resume Lambda; only the
heavy Playwright/Chromium work is isolated in its own container image (so the
resume Lambda keeps a lean, fast cold start). This adapter marshals each
``ExecutorAdapter`` method call into a synchronous ``lambda:invoke``
(RequestResponse) of the executor function and returns the structured dict it
produces. Any transport failure (missing function, timeout, throttle, bad
payload) degrades to the contract's clean ``EXECUTOR_UNAVAILABLE`` / failed
shape — it NEVER raises for an expected condition, so a cold or broken fleet just
leaves ledger rows ``ready`` for a later attempt.

Wiring: ``AUTO_APPLY_EXECUTOR=remote`` selects this in ``get_executor()``. The
target function name is ``APPLY_EXECUTOR_FUNCTION`` (default
``jobpilot-apply-executor-prod``).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from services.executor_adapter import (
    ExecutorAdapter,
    EXECUTOR_UNAVAILABLE,
    SUBMIT_FAILED,
)

logger = logging.getLogger(__name__)

_FUNCTION = os.getenv("APPLY_EXECUTOR_FUNCTION", "jobpilot-apply-executor-prod")
_INVOKE_TIMEOUT_S = 300  # matches the executor Lambda's own timeout ceiling

_client = None


def _lambda():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config
        _client = boto3.client(
            "lambda",
            config=Config(read_timeout=_INVOKE_TIMEOUT_S + 15, retries={"max_attempts": 0}),
        )
    return _client


def _remote_enabled() -> bool:
    raw = (os.getenv("EXECUTOR_REMOTE_ENABLED", "") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _ssrf_block(apply_url: str, *, submit: bool = False) -> Optional[Dict[str, Any]]:
    """Fail closed before invoking the fleet when the apply URL is not allowed."""
    from services import ssrf_guard
    allowed, reason = ssrf_guard.apply_url_allowed(apply_url or "")
    if allowed:
        return None
    logger.warning("apply-executor pre-invoke SSRF block: %s (%s)", reason, apply_url)
    if submit:
        return {
            "status": SUBMIT_FAILED,
            "confirmation": None,
            "screenshot_ref": None,
            "wall": None,
            "error": f"ssrf_blocked:{reason}",
        }
    return {
        "questions": [],
        "needs_account": False,
        "status": EXECUTOR_UNAVAILABLE,
        "error": f"ssrf_blocked:{reason}",
    }


class LambdaInvokeExecutor(ExecutorAdapter):
    name = "lambda-invoke"

    def is_available(self) -> bool:
        # Cheap: a config flag, not a probe invoke — the browser fleet is "there"
        # when we've been told it's deployed + enabled. submit() still handles a
        # runtime miss by returning unavailable from the invoke wrapper.
        return _remote_enabled()

    def recon_form(self, apply_url: str, ats_family: Optional[str]) -> Dict[str, Any]:
        blocked = _ssrf_block(apply_url)
        if blocked is not None:
            return blocked
        return self._invoke("recon_form",
                            {"apply_url": apply_url, "ats_family": ats_family},
                            on_error={"questions": [], "needs_account": False,
                                      "status": EXECUTOR_UNAVAILABLE})

    def submit(self, application: Dict[str, Any], answers: Dict[str, Any],
               resume_pdf: Any) -> Dict[str, Any]:
        url = ""
        if isinstance(application, dict):
            url = (application.get("apply_url") or application.get("url") or "")
        blocked = _ssrf_block(url, submit=True)
        if blocked is not None:
            return blocked
        return self._invoke("submit",
                            {"application": application, "answers": answers,
                             "resume_pdf": resume_pdf},
                            on_error={"status": EXECUTOR_UNAVAILABLE, "confirmation": None,
                                      "screenshot_ref": None, "wall": None, "error": None})

    def create_account(self, user_email: str, ats_family: Optional[str],
                       tenant: Optional[str], credentials: Dict[str, Any]) -> Dict[str, Any]:
        return self._invoke("create_account",
                            {"user_email": user_email, "ats_family": ats_family,
                             "tenant": tenant, "credentials": credentials},
                            on_error={"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                                      "wall": None, "error": None})

    def verify_state(self, application: Dict[str, Any], freeze: Dict[str, Any]) -> Dict[str, Any]:
        return self._invoke("verify_state",
                            {"application": application, "freeze": freeze},
                            on_error={"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                                      "wall": None, "error": None})

    # -----------------------------------------------------------------------
    def _invoke(self, method: str, args: Dict[str, Any],
                on_error: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = _lambda().invoke(
                FunctionName=_FUNCTION,
                InvocationType="RequestResponse",
                Payload=json.dumps({"method": method, "args": args}).encode("utf-8"),
            )
        except Exception as e:
            logger.error("apply-executor invoke(%s) transport error: %s", method, e)
            return dict(on_error)
        if resp.get("FunctionError"):
            logger.error("apply-executor invoke(%s) function error: %s",
                         method, resp.get("FunctionError"))
            return dict(on_error)
        try:
            body = resp["Payload"].read()
            result = json.loads(body)
        except Exception as e:
            logger.error("apply-executor invoke(%s) bad response: %s", method, e)
            return dict(on_error)
        if not isinstance(result, dict):
            return dict(on_error)
        return result
