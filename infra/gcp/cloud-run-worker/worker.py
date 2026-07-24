"""JobPilot auto-apply Playwright worker (GCP browser-execution plane, P5).

A PRIVATE Cloud Run service. It is reachable ONLY by a Cloud Task carrying the
invoker SA's OIDC token (Cloud Run enforces that at the platform edge — this code
does not re-verify it). Each request drives EXACTLY ONE application submission and
then the instance is free to scale to zero.

The security envelope (every step below is deliberate):

  Task body carries ONLY {execution_id, capability} — no answers, no résumé, no
  credentials, no PII.

  1. CLAIM    POST {CALLBACK}/claim {capability}  -> {callback_token, envelope}
              Atomic one-use lease on the AWS side. A redelivered task loses the
              claim (403) and aborts here — the double-dispatch backstop, BEFORE
              a browser launches.
  2. RESUME   POST {CALLBACK}/resume-url          -> one-use presigned GET
              Download exactly one résumé PDF to a temp file.
  3. CRED     POST {CALLBACK}/credential          -> {username, password}  (only
              if the envelope says credential_required; held in memory, never
              written to disk, never logged).
  4. FILL     the ATS adapter (P4 seam) fills the vetted answers + attaches the
              résumé. This worker never invents field values.
  5. PRECHECK POST {CALLBACK}/precheck            -> {ok, reasons}
              The LAST gate before Submit. On ok=False the worker ABORTS without
              clicking Submit (kill switch, pause/revocation, approval expiry,
              form/résumé/answer drift, entitlement, adapter health, posting).
  6. SUBMIT   click. AFTER the click, a timeout/lost connection is
              `submission_unknown` — reported as such and NEVER retried.
  7. RECEIPT  POST {CALLBACK}/receipt-url         -> one-use presigned PUT
              Upload the proof screenshot.
  8. TERMINAL POST {CALLBACK}/terminal {status,...} -> finalize the ledger.
  9. Always: delete ALL local artifacts; emit structured milestones.

LOGGING RULE: never log the capability/callback token, credentials, answers,
résumé bytes, cookies, or any PII. Only execution/application ids, milestone
names, and coarse status.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile

import requests
from flask import Flask, jsonify, request

import ats_adapter

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("worker")

app = Flask(__name__)

_CALLBACK_BASE = (os.getenv("EXECUTOR_CALLBACK_BASE_URL", "") or "").rstrip("/")
_HTTP_TIMEOUT = 20          # seconds for a callback round-trip
_NAV_TIMEOUT_MS = 30_000
_FORM_TIMEOUT_MS = 15_000
_SUBMIT_SETTLE_MS = 6_000


def _metric(phase: str, **fields) -> None:
    """Emit ONE structured, PII-free milestone metric (spec §11). Uses the shared
    ``apply_metrics`` emitter when the layer is bundled into the image; otherwise
    falls back to an inline structured line with the SAME ``apply_metric`` prefix
    so the CloudWatch/GCP metric filters match either way. Best-effort — a metric
    fault never touches the run."""
    try:
        from services import apply_metrics  # bundled with the shared layer/image
        apply_metrics.emit(phase, **fields)
        return
    except Exception:
        pass
    try:
        import json as _json
        allowed = ("execution_id", "application_id", "adapter", "outcome",
                   "duration_ms", "reason")
        line = {"metric": "apply.milestone", "phase": phase}
        for k in allowed:
            v = fields.get(k)
            if isinstance(v, (str, int, float)) and not isinstance(v, bool) and v != "":
                line[k] = v if not isinstance(v, str) else v[:64]
        logger.info("apply_metric %s", _json.dumps(line, separators=(",", ":"),
                                                   sort_keys=True))
    except Exception:  # pragma: no cover - defensive
        pass


class _AbortSubmit(Exception):
    """Pre-submit gate said no — abort cleanly WITHOUT clicking Submit."""


class _SubmissionUnknown(Exception):
    """A timeout/lost connection AFTER the Submit click — outcome unknown."""


# ---------------------------------------------------------------------------
# Callback surface client (the worker's ONLY link to durable state / secrets).
# ---------------------------------------------------------------------------
class _Callback:
    def __init__(self):
        self._token = None

    def claim(self, capability: str) -> dict:
        r = requests.post(f"{_CALLBACK_BASE}/claim",
                          json={"capability": capability}, timeout=_HTTP_TIMEOUT)
        if r.status_code != 200:
            # 403 = capability unusable (duplicate/expired) → abort, no submit.
            raise _AbortSubmit(f"claim_failed:{r.status_code}")
        data = r.json()
        self._token = data["callback_token"]
        return data["envelope"]

    def _post(self, path: str, payload: dict | None = None) -> requests.Response:
        return requests.post(f"{_CALLBACK_BASE}/{path}",
                             json=payload or {},
                             headers={"X-Executor-Callback": self._token or ""},
                             timeout=_HTTP_TIMEOUT)

    def resume_url(self) -> str | None:
        r = self._post("resume-url")
        if r.status_code == 409:
            return None  # not pinned — the form may allow submit without it
        r.raise_for_status()
        return r.json()["url"]

    def credential(self, ats_family: str, tenant: str) -> dict:
        r = self._post("credential", {"ats_family": ats_family, "tenant": tenant})
        r.raise_for_status()
        return r.json()

    def receipt_url(self) -> dict | None:
        r = self._post("receipt-url")
        if r.status_code != 200:
            return None
        return r.json()

    def precheck(self) -> dict:
        r = self._post("precheck")
        r.raise_for_status()
        return r.json()

    def milestone(self, name: str) -> None:
        try:
            self._post("milestone", {"name": name})
        except Exception:  # milestones are best-effort breadcrumbs
            pass

    def terminal(self, status: str, **fields) -> None:
        payload = {"status": status}
        payload.update({k: v for k, v in fields.items() if v is not None})
        # Terminal MUST land — retry a couple of times on transient failure.
        for attempt in range(3):
            try:
                r = self._post("terminal", payload)
                if r.status_code in (200, 409):
                    return
            except Exception as e:
                logger.warning("terminal report attempt %d failed: %s", attempt, e)
        logger.error("terminal report FAILED after retries (status=%s)", status)


# ---------------------------------------------------------------------------
# The run — claim → fetch → fill → precheck → submit → receipt → terminal.
# ---------------------------------------------------------------------------
def _run(execution_id: str, capability: str) -> dict:
    cb = _Callback()
    workdir = tempfile.mkdtemp(prefix="jp-exec-")
    submit_clicked = False
    try:
        envelope = cb.claim(capability)
        cb.milestone("claimed")
        cb.milestone("envelope_loaded")

        apply_url = envelope.get("apply_url") or ""
        ats_family = envelope.get("ats_family") or ""
        answers = envelope.get("answers") or {}
        aid = str(envelope.get("application_id") or "")
        _metric("task_start", execution_id=execution_id, application_id=aid,
                adapter=ats_family, outcome="started")

        # P0-1 SSRF: a BLOCKED envelope (apply_url failed the https + ATS
        # allow-list gate on the AWS side) never dispatches a browser.
        if envelope.get("blocked"):
            reason = envelope.get("blocked_reason") or "blocked_url"
            cb.terminal("failed", error=f"blocked_url:{reason}")
            return {"status": "failed", "reason": "blocked_url"}

        adapter = ats_adapter.get_adapter(ats_family)
        if adapter is None:
            cb.terminal("failed", error=f"no_ats_adapter:{ats_family}")
            return {"status": "failed", "reason": "no_ats_adapter"}

        # Résumé (one-use presigned GET → temp file).
        resume_path = None
        if envelope.get("resume_available"):
            url = cb.resume_url()
            if url:
                resume_path = os.path.join(workdir, "resume.pdf")
                _download(url, resume_path)
                cb.milestone("resume_downloaded")

        # Credential (in memory only) — never written to disk / logged.
        creds = None
        if envelope.get("credential_required"):
            creds = cb.credential(ats_family, envelope.get("tenant") or "")
            cb.milestone("credential_received")

        from playwright.sync_api import sync_playwright, Error as PWError

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            try:
                context = browser.new_context()
                # P0-1 SSRF: intercept EVERY navigation/redirect/subresource/
                # popup/download/websocket; abort anything non-http(s) or that
                # resolves to a private/loopback/link-local/metadata/reserved IP
                # (DNS-rebinding pinned). Defense-in-depth atop the egress FW.
                _install_ssrf_guard(context)
                page = context.new_page()
                page.set_default_timeout(_FORM_TIMEOUT_MS)
                adapter.open(page, apply_url)

                if creds is not None and hasattr(adapter, "authenticate"):
                    adapter.authenticate(page, creds)
                # Drop the plaintext ASAP.
                creds = None

                _metric("fill_start", execution_id=execution_id, application_id=aid,
                        adapter=ats_family)
                adapter.fill(page, answers)
                if resume_path:
                    adapter.attach_resume(page, resume_path)
                cb.milestone("form_filled")

                # LAST gate before the irreversible click.
                gate = cb.precheck()
                cb.milestone("presubmit_checked")
                if not gate.get("ok"):
                    reasons = ",".join(gate.get("reasons") or []) or "gate_blocked"
                    raise _AbortSubmit(reasons)

                # ---- the irreversible boundary ----
                try:
                    adapter.click_submit(page)
                    submit_clicked = True
                    cb.milestone("submit_clicked")
                    _metric("submit", execution_id=execution_id, application_id=aid,
                            adapter=ats_family, outcome="clicked")
                    page.wait_for_timeout(_SUBMIT_SETTLE_MS)
                    outcome = adapter.classify(page)
                except (PWError, requests.Timeout) as e:
                    # We clicked (or may have) and then lost the page/connection.
                    raise _SubmissionUnknown(str(e)[:200])

                # P1-SHOT: PREFER the adapter's structured, PII-FREE receipt over
                # a screenshot. Only when no receipt is available do we capture a
                # BOUNDED confirmation-region proof (never a full-page shot of a
                # filled form carrying name/contact/work-auth/EEO/salary PII).
                structured_receipt = None
                if hasattr(adapter, "receipt"):
                    try:
                        structured_receipt = adapter.receipt()
                    except Exception:
                        structured_receipt = None
                screenshot_ref = None
                if structured_receipt is None:
                    receipt = cb.receipt_url()
                    if receipt:
                        png = _confirmation_region_png(page)
                        if png is not None:
                            _upload(receipt["url"], receipt.get("headers") or {}, png)
                            screenshot_ref = receipt["key"]
                            cb.milestone("receipt_uploaded")

                if outcome == "submitted":
                    _metric("confirmation", execution_id=execution_id,
                            application_id=aid, adapter=ats_family,
                            outcome="submitted")
                    cb.terminal("submitted", screenshot_ref=screenshot_ref,
                                confirmation="submitted")
                    return {"status": "submitted"}
                if outcome == "submission_unknown":
                    # Clicked, ambiguous → HELD, NEVER auto-retried.
                    _metric("submission_unknown", execution_id=execution_id,
                            application_id=aid, adapter=ats_family,
                            outcome="ambiguous")
                    cb.terminal("submission_unknown", screenshot_ref=screenshot_ref)
                    return {"status": "submission_unknown"}
                if outcome in ("waiting_verify", "waiting_human"):
                    _metric("waiting_human", execution_id=execution_id,
                            application_id=aid, adapter=ats_family,
                            outcome=outcome, reason="captcha")
                    cb.terminal("waiting_verify",
                                wall={"kind": "captcha", "needs": ["captcha"]},
                                screenshot_ref=screenshot_ref)
                    return {"status": outcome}
                cb.terminal("failed", error="no_confirmation_after_submit",
                            screenshot_ref=screenshot_ref)
                return {"status": "failed"}
            finally:
                browser.close()

    except _AbortSubmit as e:
        # Never clicked Submit → clean, retryable pre-submit failure. The gate
        # tripping (kill switch / pause / revocation / drift) is a PAUSE milestone.
        _metric("pause", execution_id=execution_id, adapter="",
                outcome="presubmit_abort")
        cb.terminal("failed", error=f"presubmit_abort:{e}")
        return {"status": "aborted", "reason": str(e)}
    except _SubmissionUnknown as e:
        # Clicked Submit, outcome unknown → HELD, never auto-retried.
        _metric("submission_unknown", execution_id=execution_id, adapter="",
                outcome="lost_after_click")
        cb.terminal("submission_unknown", error=str(e))
        return {"status": "submission_unknown"}
    except Exception as e:  # unexpected fault
        logger.exception("worker run fault execution_id=%s", execution_id)
        if submit_clicked:
            # If we got as far as clicking, an unexpected fault after is ambiguous.
            cb.terminal("submission_unknown", error=str(e)[:200])
            return {"status": "submission_unknown"}
        cb.terminal("failed", error=str(e)[:200])
        return {"status": "failed"}
    finally:
        # Delete ALL local artifacts (résumé, temp files) unconditionally.
        shutil.rmtree(workdir, ignore_errors=True)
        cb.milestone("artifacts_deleted")


def _install_ssrf_guard(context) -> None:
    """Install the shared SSRF ``context.route`` interceptor. Bundled with the
    shared layer/image; if it cannot be imported the egress firewall (separate
    devops control) is the backstop and we proceed WITHOUT self-crashing."""
    try:
        from services import ssrf_guard
        ssrf_guard.install_route_guard(context)
    except Exception as e:  # pragma: no cover - defense-in-depth import guard
        logger.warning("ssrf route guard unavailable (%s) — relying on egress FW", e)


def _confirmation_region_png(page):
    """P1-SHOT: a BOUNDED confirmation-region capture (never full_page). Prefer a
    visible confirmation node; else a clipped top strip; else the viewport."""
    for sel in ("[data-automation-id*=confirmation]", "#application-confirmation",
                "#application_confirmation", "[class*=confirmation]", "[role=status]"):
        try:
            el = page.query_selector(sel)
            if el is not None and el.is_visible():
                return el.screenshot()
        except Exception:
            continue
    try:
        return page.screenshot(clip={"x": 0, "y": 0, "width": 1280, "height": 320})
    except Exception:
        try:
            return page.screenshot()   # viewport only — never full_page
        except Exception:
            return None


def _download(url: str, dest: str) -> None:
    with requests.get(url, timeout=_HTTP_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                fh.write(chunk)


def _upload(url: str, headers: dict, body: bytes) -> None:
    r = requests.put(url, data=body, headers=headers, timeout=_HTTP_TIMEOUT)
    r.raise_for_status()


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
@app.route("/healthz", methods=["GET"])
def healthz():
    """Liveness + config sanity (no secrets). Used by the Cloud Run startup probe
    and as a smoke test after deploy."""
    return jsonify({"status": "ok",
                    "callback_configured": bool(_CALLBACK_BASE)}), 200


@app.route("/task", methods=["POST"])
def task():
    """Cloud Tasks HTTP target. Body: {execution_id, capability}. Returns 200 on a
    handled run (terminal already reported). Returns 4xx/5xx ONLY for a request
    we could not even start — those are the only cases where a Cloud Tasks retry
    is desirable (and the one-use claim makes a retry safe)."""
    body = request.get_json(silent=True) or {}
    execution_id = str(body.get("execution_id") or "")
    capability = str(body.get("capability") or "")
    if not execution_id or not capability:
        return jsonify({"error": "missing_execution_id_or_capability"}), 400
    if not _CALLBACK_BASE:
        return jsonify({"error": "worker_misconfigured"}), 500

    logger.info("task received execution_id=%s", execution_id)
    result = _run(execution_id, capability)
    logger.info("task done execution_id=%s status=%s",
                execution_id, result.get("status"))
    return jsonify({"ok": True, "result": result}), 200


if __name__ == "__main__":  # local smoke only; prod runs under gunicorn.
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
