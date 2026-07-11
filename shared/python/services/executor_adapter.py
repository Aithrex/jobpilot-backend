"""Executor adapter — the seam between the auto-apply orchestrator and the
FUTURE headless-browser fleet that actually drives ATS forms.

Auto-Apply Tranche 1/2 boundary. The orchestrator (`auto_apply_service`) is
executor-agnostic: it owns the ledger, autonomy, answer resolution and the
freeze/thaw of Oracle waits, but it NEVER touches a browser. Everything that
requires a live browser session — reconning a form, filling + submitting it,
creating an ATS account, or re-checking a verification/CAPTCHA wall — is
delegated through this `ExecutorAdapter` interface.

Why a seam and not the real thing here:
  * The live executor (a Fargate/Playwright "Oracle" fleet, or the Chrome
    extension) is gated on an INFRA decision and is out of scope for this
    tranche. Building it here would couple the correctness spine (ledger +
    autonomy) to an un-decided runtime.
  * With this interface + `StubExecutor`, the ENTIRE pipeline
    (gather → decide → submit → freeze → re-check) is complete and testable
    end-to-end today, with zero browsers, zero flakiness, and no network.
    The stub always returns a clean, structured "unavailable" — it NEVER
    crashes — so the orchestrator's happy path, gate path and freeze path can
    all be exercised now and light up unchanged the day a real executor lands.

Swapping in a real executor is a one-line change in
`auto_apply_service.get_executor()`; nothing else moves.

--------------------------------------------------------------------------
WHAT A REAL EXECUTOR MUST IMPLEMENT (the contract this ABC pins down)
--------------------------------------------------------------------------
A production `ExecutorAdapter` (Playwright/Fargate or the extension bridge)
MUST satisfy, for every method:

  is_available() -> bool
      Cheap, side-effect-free liveness probe. True only when a submission can
      actually be attempted right now (fleet warm, capacity available). The
      orchestrator calls this BEFORE grabbing the ledger row for submit, so a
      cold/absent fleet leaves the row `ready` (picked up later) instead of
      churning it through `submitting`.

  recon_form(apply_url, ats_family) -> ReconResult
      Open the apply URL, walk the (possibly multi-step) form, and return the
      full question set WITHOUT submitting anything. Each question:
        id                  stable per-field handle the executor can later fill
        text                the human label shown to the applicant
        type                one of QTYPE_* (text/textarea/select/checkbox/
                            radio/file/date/attestation/…)
        required            whether the ATS blocks submit without it
        is_legal_attestation  TRUE for work-authorization, sponsorship, EEO,
                            veteran/disability, background-check consent, and
                            any "I certify/agree" gate. The orchestrator treats
                            these specially: their values may ONLY come from the
                            user's declared onboarding/visa data, NEVER from an
                            LLM or a heuristic (IntegrityGuard extended to form
                            answers). Mis-labelling one as non-legal is a
                            correctness/compliance bug — err toward TRUE.
      Plus `needs_account: bool` — whether an authenticated ATS account is
      required before the form is reachable (drives the account-creation Oracle
      hook). On any inability to recon (fleet down, page unreachable), return
      the clean unavailable shape (see StubExecutor) rather than raising.

  submit(application, answers, resume_pdf) -> SubmitResult
      Fill the reconned form with `answers` (a {field_id: value} map the
      orchestrator resolved and vetted — the executor MUST NOT invent values
      for missing fields), attach `resume_pdf` (a reference the executor
      resolves to bytes — see auto_apply_service._resume_ref), submit, and
      capture proof. Return one of STATUS_*:
        SUBMIT_SUCCESS       + confirmation (ATS confirmation id/number/text)
                             + screenshot_ref (audit artifact handle, e.g. S3)
        SUBMIT_WAITING_VERIFY + wall={kind: WALL_*, needs:[...]} when blocked on
                             an email/SMS verification, a forced password reset,
                             or a CAPTCHA. The executor MUST NOT hold the browser
                             open waiting — it reports the wall and returns; the
                             orchestrator freezes the row at the DATA layer and a
                             scheduled sweep thaws it via verify_state().
        EXECUTOR_UNAVAILABLE when it cannot act at all (leave row ready).
        SUBMIT_FAILED        + error for a hard, non-retryable form failure.
      MUST be safe to call at-least-once for the same application: the
      orchestrator guards double-submit via the ledger CAS, but the executor
      should still avoid creating duplicate submissions if it can detect one.

  create_account(user_email, ats_family, tenant, credentials) -> OracleResult
      (Oracle hook) Register a new ATS account when recon reported
      needs_account. `credentials.password` is server-generated and delivered
      via the KMS vault (§4) — the executor receives it, never persists it.
      Return status READY (account usable), WALL_* (blocked on verify/CAPTCHA,
      same freeze/thaw contract as submit), or EXECUTOR_UNAVAILABLE.

  verify_state(application, freeze) -> OracleResult
      (Oracle hook) Re-drive a frozen verification/reset/CAPTCHA wall on a
      scheduled sweep. `freeze` is the data-layer record written when the wall
      was first hit (kind + needs + attempt bookkeeping). Return
      status READY with `resolved=True` when the wall has cleared (the
      orchestrator thaws: row → ready, re-enqueues submit); WALL_* /
      still-blocked with `resolved=False` to be re-checked later; or
      EXECUTOR_UNAVAILABLE.

Every method returns a STRUCTURED dict and NEVER raises for an expected
condition (unavailability, a wall, a page miss). Reserve exceptions for truly
unexpected faults — the orchestrator catches those at its boundary and marks
the ledger `failed`.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional

try:  # TypedDict is stdlib on the Lambda runtime; degrade gracefully if not.
    from typing import TypedDict
except ImportError:  # pragma: no cover
    TypedDict = None  # type: ignore

# ---------------------------------------------------------------------------
# Result status vocabulary (imported by auto_apply_service — single source)
# ---------------------------------------------------------------------------
# submit() / create_account() / verify_state() status values:
SUBMIT_SUCCESS = "submitted"
SUBMIT_WAITING_VERIFY = "waiting_verify"
SUBMIT_FAILED = "failed"
EXECUTOR_UNAVAILABLE = "executor_unavailable"
# Oracle-hook "the thing is now usable" status (account created / wall cleared):
ORACLE_READY = "ready"

# Oracle wall kinds (the `wall.kind` a WAITING_VERIFY result carries):
WALL_VERIFY = "verify"      # email/SMS confirmation link/code required
WALL_RESET = "reset"        # forced password reset before proceeding
WALL_CAPTCHA = "captcha"    # human-challenge gate
VALID_WALL_KINDS = (WALL_VERIFY, WALL_RESET, WALL_CAPTCHA)

# recon question types (stable across ATS families; the executor maps native
# widgets onto these). QTYPE_ATTESTATION pairs with is_legal_attestation=True.
QTYPE_TEXT = "text"
QTYPE_TEXTAREA = "textarea"
QTYPE_SELECT = "select"
QTYPE_CHECKBOX = "checkbox"
QTYPE_RADIO = "radio"
QTYPE_FILE = "file"
QTYPE_DATE = "date"
QTYPE_ATTESTATION = "attestation"


# ---------------------------------------------------------------------------
# Documented return shapes (TypedDicts — advisory; dicts at runtime)
# ---------------------------------------------------------------------------
if TypedDict is not None:  # pragma: no branch

    class ReconQuestion(TypedDict, total=False):
        id: str
        text: str
        type: str
        required: bool
        is_legal_attestation: bool

    class ReconResult(TypedDict, total=False):
        questions: List[ReconQuestion]
        needs_account: bool
        status: str            # EXECUTOR_UNAVAILABLE when the fleet can't recon

    class SubmitResult(TypedDict, total=False):
        status: str            # SUBMIT_* / EXECUTOR_UNAVAILABLE
        confirmation: Optional[str]
        screenshot_ref: Optional[str]
        wall: Optional[Dict[str, Any]]   # {kind: WALL_*, needs: [...]}
        error: Optional[str]

    class OracleResult(TypedDict, total=False):
        status: str            # ORACLE_READY / WALL_* / EXECUTOR_UNAVAILABLE
        resolved: bool
        wall: Optional[Dict[str, Any]]
        error: Optional[str]


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------
class ExecutorAdapter(abc.ABC):
    """Abstract browser-fleet driver. See the module docstring for the full
    per-method contract a production implementation must satisfy."""

    #: short label for logs/audit (e.g. "stub", "playwright-fargate").
    name: str = "abstract"

    @abc.abstractmethod
    def is_available(self) -> bool:
        """True only when a submission can actually be attempted right now."""
        raise NotImplementedError

    @abc.abstractmethod
    def recon_form(self, apply_url: str,
                   ats_family: Optional[str]) -> "Dict[str, Any]":
        """Walk the form and return {questions:[...], needs_account:bool}."""
        raise NotImplementedError

    @abc.abstractmethod
    def submit(self, application: Dict[str, Any], answers: Dict[str, Any],
               resume_pdf: Any) -> "Dict[str, Any]":
        """Fill + submit the form. Return a SubmitResult-shaped dict."""
        raise NotImplementedError

    # --- Oracle hooks -------------------------------------------------------
    @abc.abstractmethod
    def create_account(self, user_email: str, ats_family: Optional[str],
                       tenant: Optional[str],
                       credentials: Dict[str, Any]) -> "Dict[str, Any]":
        """Register an ATS account. Return an OracleResult-shaped dict."""
        raise NotImplementedError

    @abc.abstractmethod
    def verify_state(self, application: Dict[str, Any],
                     freeze: Dict[str, Any]) -> "Dict[str, Any]":
        """Re-drive a frozen verify/reset/CAPTCHA wall. OracleResult-shaped."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# StubExecutor — the null object that makes the pipeline complete WITHOUT a
# browser. Every method returns a clean, structured "unavailable" and never
# raises. This is what ships in Tranche 1; a real executor replaces it later.
# ---------------------------------------------------------------------------
class StubExecutor(ExecutorAdapter):
    """No-op executor: the fleet does not exist yet.

    * is_available() is always False, so the orchestrator's submit() leaves
      rows `ready` (untouched) for the future fleet to pick up.
    * recon_form/submit/create_account/verify_state return structured
      EXECUTOR_UNAVAILABLE results — the orchestrator handles each as a
      well-defined no-op (no fabricated questions, no fabricated answers, no
      false confirmations), so the whole gather/decide/submit/freeze path is
      exercisable end-to-end today.
    """

    name = "stub"

    def is_available(self) -> bool:
        return False

    def recon_form(self, apply_url: str,
                   ats_family: Optional[str]) -> Dict[str, Any]:
        # No browser → no questions discovered. Empty (not fabricated) set +
        # an explicit unavailable status the orchestrator keys off to leave
        # the row queued for the future fleet.
        return {
            "questions": [],
            "needs_account": False,
            "status": EXECUTOR_UNAVAILABLE,
        }

    def submit(self, application: Dict[str, Any], answers: Dict[str, Any],
               resume_pdf: Any) -> Dict[str, Any]:
        return {
            "status": EXECUTOR_UNAVAILABLE,
            "confirmation": None,
            "screenshot_ref": None,
            "wall": None,
            "error": None,
        }

    def create_account(self, user_email: str, ats_family: Optional[str],
                       tenant: Optional[str],
                       credentials: Dict[str, Any]) -> Dict[str, Any]:
        return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                "wall": None, "error": None}

    def verify_state(self, application: Dict[str, Any],
                     freeze: Dict[str, Any]) -> Dict[str, Any]:
        # Cannot confirm the wall cleared → resolved=False, unavailable. The
        # sweep will re-check on the next pass (no state is lost).
        return {"status": EXECUTOR_UNAVAILABLE, "resolved": False,
                "wall": None, "error": None}
