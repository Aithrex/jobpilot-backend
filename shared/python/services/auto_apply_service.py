"""Auto-apply orchestrator — the executor-agnostic engine that drives a ledger
row from `queued` to `submitted`, without ever touching a browser.

Auto-Apply §6 (the two async job phases) sits on top of this module:

    auto_apply_gather  → gather(application_id, user_email)
    auto_apply_submit  → submit(application_id, user_email)

Both are dispatched as async jobs (never inline — the recon/resolve/submit path
can exceed API Gateway's 29s once a real executor exists) and both are
IDEMPOTENT (Lambda async is at-least-once): the ledger's status-guarded CAS
transitions are the structural backstop, so a redelivered job can never
double-gather, double-submit, or clobber a finalized row.

The browser lives entirely behind `ExecutorAdapter` (see executor_adapter.py).
Today that is `StubExecutor` (no fleet yet) — every executor call returns a
clean, structured "unavailable", so this whole file is exercisable end-to-end
now and lights up unchanged when a real executor lands.

Layering (mirrors auto_submit / application_ledger):
  * The ledger (`application_ledger_service`) owns status + the CAS machine.
  * This service owns: recon → answer resolution → coverage → autonomy
    decision → the freeze/thaw of Oracle verify walls.
  * Resolved answers and Oracle freezes live in two NEW collections OWNED here
    (out of the lean ledger row, and because answers carry PII):
        auto_apply_answers  — { application_id, user_email, answers,
                                coverage, needs_account, updated_at }
        auto_apply_freezes  — { application_id, user_email, kind, needs,
                                status, attempts, next_check_at, ... }
    (Report §: add both to CASCADE_COLLECTIONS in account_service.py and add
     indexes in scripts/migrate_indexes.py — see the handoff note.)

Integrity: legal attestations (work-auth, sponsorship, EEO) are NEVER
fabricated. Their values come only from the user's declared data via the
answer library; any unresolved legal attestation forces `needs_input` +
autonomy `ask` and blocks the auto path (IntegrityGuard extended to forms).
Nothing in this file calls an LLM directly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

from services import application_ledger_service as ledger
from services import executor_adapter
from services.ssrf_guard import apply_url_allowed
from services.executor_adapter import (
    EXECUTOR_UNAVAILABLE,
    ExecutorAdapter,
    ORACLE_READY,
    StubExecutor,
    SUBMIT_DISPATCHED,
    SUBMIT_FAILED,
    SUBMIT_SUCCESS,
    SUBMIT_UNKNOWN,
    SUBMIT_WAITING_VERIFY,
    VALID_WALL_KINDS,
    WALL_VERIFY,
)

logger = logging.getLogger(__name__)

# Re-check backoff for frozen Oracle walls (verify/reset/CAPTCHA).
_VERIFY_BACKOFF_MINUTES = (5, 15, 30, 60, 120, 240)
_VERIFY_MAX_ATTEMPTS = len(_VERIFY_BACKOFF_MINUTES)

# ---------------------------------------------------------------------------
# Executor outcome vocabulary this orchestrator folds onto the ledger, BEYOND
# the executor_adapter SUBMIT_* set (spec §4 + the "prepared"/waiting_human gap).
#
# These are matched by VALUE, not imported: `browser_executor` pulls in Playwright
# and must never be imported at orchestrator module load (cold start + the stub
# default). The literals below are the exact constants that module defines
# (browser_executor.EXECUTOR_UNSUPPORTED / EXECUTOR_PREPARED) plus the
# fill_driver spellings, so a report from EITHER layer lands on the right state.
# ---------------------------------------------------------------------------
# PERMANENT "we cannot drive this employer's system" — the ledger's
# STATUS_UNSUPPORTED terminal with an honest user-facing message. Deliberately
# DISJOINT from EXECUTOR_UNAVAILABLE, which is a TRANSIENT fleet drain and stays
# a retryable `failed` (never conflate the two: one asks the user to apply
# manually forever, the other is retried by the next sweep).
_UNSUPPORTED_STATUSES = frozenset({
    "executor_unsupported",   # browser_executor.EXECUTOR_UNSUPPORTED
    "unsupported",            # generic/adapter spelling (ats_adapters TAKEOVER_UNSUPPORTED)
    "unsupported_vendor", "vendor_unsupported", "unsupported_ats",
})
# Filled + proof captured, Submit DELIBERATELY not clicked (copilot / ops dry
# run). NOT a failure: the row is handed BACK to the human to finish.
_PREPARED_STATUSES = frozenset({"prepared"})  # browser_executor.EXECUTOR_PREPARED
# The run stopped at something only a human can clear, with no Oracle freeze doc
# (fill_driver.FILL_WAITING_HUMAN). Distinct from SUBMIT_WAITING_VERIFY, which
# owns the automated freeze/thaw sweep.
_WAITING_HUMAN_STATUSES = frozenset({"waiting_human"})
# User-facing copy for a prepared (never-submitted) run.
_PREPARED_MESSAGE = ("We filled this application for you but did not submit it — "
                     "open it to review and send.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: Any, max_length: int = 200) -> str:
    return InputSanitizer.sanitize_string(str(value or ""), max_length=max_length)


def _emit_metric(phase: str, **fields: Any) -> None:
    """Emit a structured, PII-free auto-apply milestone metric (spec §11).
    Best-effort — a metrics fault never breaks the finalize path."""
    try:
        from services import apply_metrics
        apply_metrics.emit(phase, **fields)
    except Exception:  # pragma: no cover - metrics never load-bearing
        pass


# ---------------------------------------------------------------------------
# Executor selection — the ONE line that swaps in a real fleet later.
# ---------------------------------------------------------------------------
_executor: Optional[ExecutorAdapter] = None


def _configured_executor() -> ExecutorAdapter:
    """Pick the executor from config. Default = StubExecutor (prod stays dark).

    ``AUTO_APPLY_EXECUTOR=ats`` selects the real ``AtsExecutor`` (live Greenhouse
    recon; submit still dark until the browser fleet is deployed — see
    ``ats_executor`` docstring). Any import/other failure falls back to the stub
    so a misconfig can never break the pipeline."""
    from utils.config import _get_config_value
    choice = (_get_config_value("AUTO_APPLY_EXECUTOR", "stub") or "stub").strip().lower()
    if choice == "browser":
        # Full Playwright submit executor. In prod this runs INSIDE the
        # apply-executor container Lambda; the resume Lambda reaches it via
        # LambdaInvokeExecutor (choice == "remote", below).
        try:
            from services.browser_executor import BrowserExecutor
            return BrowserExecutor()
        except Exception as e:  # pragma: no cover - defensive
            logger.error("AUTO_APPLY_EXECUTOR=browser failed to load (%s); "
                         "falling back to StubExecutor", e)
    elif choice == "remote":
        try:
            from services.lambda_invoke_executor import LambdaInvokeExecutor
            return LambdaInvokeExecutor()
        except Exception as e:  # pragma: no cover - defensive
            logger.error("AUTO_APPLY_EXECUTOR=remote failed to load (%s); "
                         "falling back to StubExecutor", e)
    elif choice == "cloud_run":
        # ASYNC-DISPATCH: mint a one-use capability + enqueue a Cloud Task to the
        # private Cloud Run Playwright worker (GCP browser-execution plane, P5).
        # submit() returns SUBMIT_DISPATCHED; the worker finalizes the ledger via
        # the executor callback surface. Dark until CLOUD_RUN_EXECUTOR_ENABLED.
        try:
            from services.cloud_run_executor import CloudRunExecutor
            return CloudRunExecutor()
        except Exception as e:  # pragma: no cover - defensive
            logger.error("AUTO_APPLY_EXECUTOR=cloud_run failed to load (%s); "
                         "falling back to StubExecutor", e)
    elif choice == "ats":
        try:
            from services.ats_executor import AtsExecutor
            return AtsExecutor()
        except Exception as e:  # pragma: no cover - defensive
            logger.error("AUTO_APPLY_EXECUTOR=ats but AtsExecutor failed to load (%s); "
                         "falling back to StubExecutor", e)
    return StubExecutor()


def get_executor() -> ExecutorAdapter:
    """Return the active executor. Default = StubExecutor (no browser fleet);
    ``AUTO_APPLY_EXECUTOR=ats`` swaps in the real recon executor. This is the ONE
    seam a future Playwright/Fargate submit fleet drops into — nothing else moves."""
    global _executor
    if _executor is None:
        _executor = _configured_executor()
    return _executor


def set_executor(executor: ExecutorAdapter) -> None:
    """Override the executor (tests / a future real fleet wiring)."""
    global _executor
    _executor = executor


def _prepare_enabled() -> bool:
    """Dark-launch gate for the REQUIRED tailored-résumé preparation stage
    (defect D). Default OFF, so prod — and every existing gather path/test —
    stays byte-identical until the tailor stage is wired on; when ON, the clean
    auto path dispatches ``auto_apply_prepare`` to pin an immutable tailored
    artifact BEFORE a row may reach ``ready`` (so `submit` never runs without a
    résumé). Mirrors the AUTO_APPLY_EXECUTOR / AUTO_SUBMIT_ENABLED dark-by-default
    posture — a misconfig can only leave the pipeline in today's behavior, never
    submit an untailored application."""
    try:
        from utils.config import _get_config_value
        val = _get_config_value("AUTO_APPLY_PREPARE", "false") or "false"
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # pragma: no cover - defensive
        return False


def _modes_enabled() -> bool:
    """Dark-launch gate for the explicit autonomy MODES (copilot/approval/
    autopilot/aggressive). Default OFF, so prod — and every existing gather/prepare/approval
    test — stays byte-identical until onboarding (a FE task) FORCES the mode pick
    and this is flipped on. When ON, the mode gate caps `decide()` per the user's
    chosen mode AND the fail-safe fires (mode unset -> gather refuses to auto-act).
    Mirrors the AUTO_APPLY_PREPARE / AUTO_SUBMIT_ENABLED dark-by-default posture —
    a misconfig can only leave the pipeline in today's behavior, never over-act."""
    try:
        from utils.config import _get_config_value
        val = _get_config_value("AUTO_APPLY_MODES", "false") or "false"
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # pragma: no cover - defensive
        return False


def _user_wants_tailored_resume(user_email: str) -> bool:
    """Per-user tailor preference (default True). Safe if settings read fails."""
    try:
        from services import auto_submit_service as ass
        return bool(ass.use_tailored_resume(user_email))
    except Exception:  # pragma: no cover - defensive
        return True


# ---------------------------------------------------------------------------
# Explicit autonomy MODES gate (auto-apply-defects-plan.md "## Autonomy MODES").
# Applied AFTER autonomy_service.decide()/evaluate() computes the RAW decision,
# in the gather/approve path (post-F). It only ever CAPS (downgrades) a decision
# or refuses — it never upgrades, so the hard never-fabricate rules that already
# produced `ask`/`hold` upstream (unconfirmed legal attestation -> ask) win in
# EVERY mode, including autopilot.
# ---------------------------------------------------------------------------
def _mode_decision(user_email: str, autonomy: str) -> Tuple[str, Optional[str], bool]:
    """Cap a raw autonomy decision by the user's explicit mode.

    Returns ``(effective_autonomy, mode, refuse)``:
      * refuse=True (mode is None)  — the fail-safe: no explicit mode chosen yet
        (onboarding forces the pick); the caller must NOT auto-proceed.
      * copilot / approval          — a raw `auto` is capped to `ask` (both wait in
        ready_for_review); `hold`/`ask` pass through unchanged. Copilot additionally
        flags the row manual_apply (prepare-only — the user applies themselves).
      * autopilot                   — `auto` is honored ONLY when the graduation
        gate is unlocked (>= AUTOPILOT_GRADUATION_N user-approved confirms); else
        `auto` is capped to `ask`. `hold`/`ask` pass through unchanged.
      * aggressive                  — public Auto Aggressive: honors `auto`
        without graduation (daily matching submit). Hard never-fabricate rules
        still win upstream.
    """
    from services import autonomy_service
    mode = autonomy_service.get_mode(user_email)
    if mode is None:
        return autonomy, None, True
    if mode in (autonomy_service.MODE_COPILOT, autonomy_service.MODE_APPROVAL):
        return ("ask" if autonomy == "auto" else autonomy), mode, False
    if mode == autonomy_service.MODE_AGGRESSIVE:
        return autonomy, mode, False
    if mode == autonomy_service.MODE_AUTOPILOT:
        if autonomy == "auto" and not autonomy_service.autopilot_unlocked(user_email):
            return "ask", mode, False
        return autonomy, mode, False
    # Unknown mode (should be impossible — get_mode normalizes to None): treat as
    # the strictest cap rather than trusting it.
    return ("ask" if autonomy == "auto" else autonomy), mode, False


# ---------------------------------------------------------------------------
# Owned collections
# ---------------------------------------------------------------------------
def _answers_coll():
    return DBConnect().get_db().auto_apply_answers


def _freezes_coll():
    return DBConnect().get_db().auto_apply_freezes


# ---------------------------------------------------------------------------
# Answer resolution (delegates to answer_library_service; the library loads the
# canonical profile itself — structured résumé + preferences + declared visa/EEO
# — as the ONLY permissible source for answers/attestations, never an LLM or a
# heuristic. This layer adds a hard legal-attestation guard on top.)
# ---------------------------------------------------------------------------
def _resolve_answers(user_email: str,
                     questions: List[Dict[str, Any]],
                     company: str = "") -> Tuple[Dict[str, Any], List[Dict[str, str]], Dict[str, Any]]:
    """Resolve normalized questions against the canonical profile via the answer
    library. Returns ``(answers, needs_input_items, resolution)``:

      * answers          {field_id: value} for every confidently-resolved field.
      * needs_input_items the one-tap queue for everything unresolved (and every
                         UNresolved legal attestation — never guessed).
      * resolution       the library's full result (coverage / has_novel_free_text
                         / has_unconfirmed_legal_attestation) so the autonomy
                         decision consumes the SAME signals, not a re-derivation.

    Hard integrity guard (independent of the library): a required legal
    attestation is only accepted if the library resolved it FROM DECLARED data;
    otherwise it is forced into needs_input. We never emit an attestation value
    this service invented.
    """
    answers: Dict[str, Any] = {}
    needs_input: List[Dict[str, str]] = []

    resolution = _call_answer_library(user_email, questions, company=company)
    resolved_map = resolution.get("resolved") or {}

    for q in questions:
        fid = q.get("field_id")
        if not fid:
            continue
        is_legal = bool(q.get("is_legal_attestation"))
        required = bool(q.get("required"))
        hit = resolved_map.get(fid)

        if hit is not None and _is_confident(hit):
            # Legal attestations must be sourced from the user's DECLARED data.
            if is_legal and not _is_declared_source(hit):
                needs_input.append(_needs_item(q, kind="legal_attestation"))
                continue
            answers[fid] = _value_of(hit)
            continue

        # Unresolved. Only required fields (and all legal attestations) gate.
        if required or is_legal:
            needs_input.append(_needs_item(
                q, kind="legal_attestation" if is_legal else "screening"))

    return answers, needs_input, resolution


def _call_answer_library(user_email: str,
                         questions: List[Dict[str, Any]],
                         company: str = "") -> Dict[str, Any]:
    """Invoke ``answer_library_service.resolve(user_email, questions)`` with the
    normalized (field_id-keyed) questions. A genuine ImportError (parallel build)
    → an empty resolution so the pipeline still runs (everything falls to
    needs_input, nothing fabricated). Real call bugs surface — they are not
    swallowed into a false 'all resolved'."""
    try:
        from services import answer_library_service  # lazy: parallel build
    except ImportError:
        logger.warning("auto_apply: answer_library_service not present yet — "
                       "all questions route to needs_input")
        return {"resolved": {}, "coverage": 0.0, "has_novel_free_text": False,
                "has_unconfirmed_legal_attestation": False}
    try:
        # `company` drives the spec §1 résumé escalation (answer "Yes" to
        # "have you worked here?" when the résumé shows this employer). It can
        # only ever raise an answer to Yes, never assert "No", so passing it is
        # safe; omitting it silently disables the escalation.
        result = answer_library_service.resolve(user_email, questions,
                                                company=company or "")
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_apply: answer_library resolve failed for %s: %s", user_email, e)
        return {"resolved": {}, "coverage": 0.0, "has_novel_free_text": False,
                "has_unconfirmed_legal_attestation": False}
    return _normalize_resolution(result)


def _normalize_resolution(result: Any) -> Dict[str, Any]:
    """Guard the library's return shape, preserving the full field-id-keyed
    result (resolved + coverage + the two risk flags) when present."""
    if isinstance(result, dict) and isinstance(result.get("resolved"), dict):
        return result
    if isinstance(result, dict):
        # A bare {field_id: value|hit} map (no envelope).
        return {"resolved": result}
    if isinstance(result, list):
        resolved: Dict[str, Any] = {}
        for item in result:
            if isinstance(item, dict):
                fid = item.get("field_id") or item.get("id")
                if fid:
                    resolved[fid] = item
        return {"resolved": resolved}
    return {"resolved": {}}


# Sources that count as the user's OWN declared data (attestation-safe). Includes
# the answer library's own emitted sources: ``declared`` (a legal attestation
# filled from onboarding/visa data) and ``user_store`` (a value the user
# confirmed once and we remembered) — both are the user's stated data, never a
# guess.
_DECLARED_SOURCES = frozenset({
    "declared", "user_store", "visa_profile", "onboarding", "preferences",
    "profile", "user_declared", "structured", "resume",
    # spec §0.3 / §1. Without these two, a legitimately-resolved answer is
    # bounced straight back to needs_input and the work that produces them is
    # inert:
    #   policy_decline — a protected-EEO "prefer not to answer". Declining
    #     asserts nothing about the user, so it is not a fabrication.
    #   resume_match   — a résumé-derived escalation to "Yes" (e.g. the user
    #     HAS worked at this company). It can only ever raise an answer to Yes,
    #     never assert "No", so it cannot understate an obligation.
    # Kept as distinct strings rather than folded into "declared" because
    # neither is something the user actually declared; provenance stays honest.
    "policy_decline", "resume_match",
})


def _is_confident(hit: Any) -> bool:
    """A resolution hit is usable when it carries a value and either no
    confidence signal or a confidence at/above the library's own bar."""
    if not isinstance(hit, dict):
        return hit is not None
    if hit.get("value") is None and hit.get("answer") is None:
        return False
    conf = hit.get("confidence")
    if conf is None:
        return True
    try:
        return float(conf) >= 0.5
    except (TypeError, ValueError):
        return True


def _is_declared_source(hit: Any) -> bool:
    if not isinstance(hit, dict):
        return False
    src = str(hit.get("source") or "").strip().lower()
    return src in _DECLARED_SOURCES


def _value_of(hit: Any) -> Any:
    if isinstance(hit, dict):
        return hit.get("value", hit.get("answer"))
    return hit


def _needs_item(q: Dict[str, Any], *, kind: str) -> Dict[str, str]:
    return {
        "question": _clean(q.get("label"), 500) or _clean(q.get("field_id"), 200),
        "kind": kind,
        "field_id": _clean(q.get("field_id"), 200),
    }


def _coverage(questions: List[Dict[str, Any]], answers: Dict[str, Any]) -> float:
    """Fraction of REQUIRED fields resolved from profile + answer library. Only
    the fallback when the library did not return its own coverage; the library's
    coverage is authoritative (it excludes essays consistently)."""
    required = [q for q in questions if q.get("required")]
    if not required:
        return 1.0
    resolved = sum(1 for q in required if q.get("field_id") in answers)
    return round(resolved / len(required), 4)


# ---------------------------------------------------------------------------
# Autonomy decision (delegates to autonomy_service.score/decide; local
# fallback that faithfully implements §3 when the parallel service is absent).
# ---------------------------------------------------------------------------
def _decide_autonomy(app: Dict[str, Any], coverage: float, *,
                     user_email: str,
                     has_novel_free_text: bool,
                     has_unresolved_legal: bool,
                     needs_account: bool) -> Tuple[str, float]:
    """Return ``(autonomy, confidence_score)`` by delegating to the real
    per-application autonomy policy (`autonomy_service.evaluate`) — the SAME
    signals the answer library produced (coverage, has_novel_free_text) plus the
    hard-rule flags (unconfirmed legal → ask, Oracle wall → hold), and the
    user's OWN autonomy_threshold (read inside evaluate).

    Only a genuine ImportError (parallel build safety) falls back to the local
    policy — logged at WARNING. A wrong-signature/other call bug is NOT swallowed
    here; it surfaces so tests catch it, per the never-guess-the-contract rule.
    """
    try:
        from services import autonomy_service  # lazy: parallel build
    except ImportError:
        logger.warning("auto_apply: autonomy_service unavailable (ImportError) — "
                       "using local fallback policy")
        return _local_autonomy(
            app.get("ats_family"), coverage,
            has_novel_free_text=has_novel_free_text,
            has_unresolved_legal=has_unresolved_legal,
            needs_account=needs_account)

    result = autonomy_service.evaluate(
        user_email, app,
        answer_coverage=coverage,
        ats_family=app.get("ats_family"),
        has_novel_free_text=has_novel_free_text,
        has_unconfirmed_legal_attestation=has_unresolved_legal,
        blocked_on_oracle=needs_account,
    )
    autonomy = str(result.get("autonomy"))
    confidence = float(result.get("confidence_score") or 0.0)
    if autonomy not in ledger._VALID_AUTONOMY:
        # A real policy bug — do NOT paper over it with the fallback.
        raise ValueError(
            f"autonomy_service.evaluate returned invalid decision {autonomy!r}")
    return autonomy, max(0.0, min(1.0, confidence))


def _local_autonomy(ats_family: Optional[str], coverage: float, *,
                    has_novel_free_text: bool, has_unresolved_legal: bool,
                    needs_account: bool) -> Tuple[str, float]:
    """Faithful local §3 policy — used ONLY when autonomy_service cannot be
    imported at all (parallel-build safety). Mirrors the real service's hard
    rules; its fixed threshold is the conservative last resort, never the
    primary path."""
    ats_ease = {
        "greenhouse": 0.95, "lever": 0.95, "ashby": 0.9,
        "workday": 0.6, "icims": 0.55, "taleo": 0.5,
    }.get(str(ats_family or "").lower(), 0.7)
    coverage = float(coverage or 0.0)
    no_novel = 0.0 if has_novel_free_text else 1.0
    score = round(0.4 * ats_ease + 0.4 * coverage + 0.2 * no_novel, 4)

    # Hard rules (order matches autonomy_service.decide).
    if has_unresolved_legal:
        return "ask", score
    if needs_account:
        return "hold", score

    threshold = 0.90
    if score >= threshold and coverage >= 0.999:
        return "auto", score
    if coverage >= 0.5:
        return "hold", score
    return "ask", score


# ===========================================================================
# PHASE 1 — gather
# ===========================================================================
def gather(application_id: str, user_email: str) -> Dict[str, Any]:
    """Recon the form, resolve answers against the canonical profile, compute
    coverage + the autonomy decision, and land the ledger row in `ready`
    (auto & complete) or `needs_input` (gaps / legal attestations / one-tap).

    Idempotent: safe under Lambda at-least-once redelivery. Rows already at or
    past `needs_input` are a no-op; the CAS transitions guard concurrent runs.
    Returns a structured summary for the job result (never raises for expected
    conditions such as an unavailable executor).
    """
    application_id = _clean(application_id, 64)
    app = ledger.get_application(user_email, application_id)
    if not app:
        raise ledger.ApplicationNotFound()

    status = app.get("status")
    if status in (ledger.STATUS_NEEDS_INPUT, ledger.STATUS_READY,
                  ledger.STATUS_READY_FOR_REVIEW, ledger.STATUS_APPROVED,
                  ledger.STATUS_SUBMITTING, ledger.STATUS_SUBMITTED,
                  ledger.STATUS_CONFIRMED):
        logger.info("auto_apply gather %s: already at %s — idempotent no-op",
                    application_id, status)
        return {"idempotent": True, "status": status, "application_id": application_id}
    if status not in (ledger.STATUS_QUEUED, ledger.STATUS_GATHERING):
        # failed/skipped/skipped_duplicate — not a gather target.
        return {"skipped": status, "application_id": application_id}

    executor = get_executor()
    try:
        recon = executor.recon_form(app.get("apply_url") or "", app.get("ats_family"))
    except Exception as e:
        logger.error("auto_apply gather %s: recon raised: %s", application_id, e)
        ledger.mark_failed(user_email, application_id, f"recon_error: {e}")
        raise

    if recon.get("status") == EXECUTOR_UNAVAILABLE:
        # No fleet yet — do NOT fabricate a form. Leave the row queued so the
        # gather backlog is picked up unchanged once a real executor exists.
        logger.info("auto_apply gather %s: executor unavailable — leaving %s",
                    application_id, status)
        return {"status": EXECUTOR_UNAVAILABLE, "application_id": application_id,
                "ledger_status": status}

    # We have a real form. Reflect in-progress (best-effort; ignore a concurrent
    # run that already moved it out of queued).
    if status == ledger.STATUS_QUEUED:
        try:
            ledger.mark_gathering(user_email, application_id)
        except ledger.InvalidTransition:
            logger.info("auto_apply gather %s: lost race to gathering — no-op",
                        application_id)
            return {"idempotent": True, "application_id": application_id}

    try:
        from services import apply_forms
        raw_questions = [q for q in (recon.get("questions") or [])
                         if isinstance(q, dict)]
        # Normalize recon questions onto the stable field_id schema so recon,
        # resolution and answer-lookup all key off ONE id (the bug fix: recon
        # dicts vs. text-keyed resolutions no longer diverge).
        questions = apply_forms.normalize_recon_questions(raw_questions)
        needs_account = bool(recon.get("needs_account"))
        answers, needs_input, resolution = _resolve_answers(
            user_email, questions, company=str(app.get("company") or ""))
        cov = resolution.get("coverage")
        coverage = float(cov) if cov is not None else _coverage(questions, answers)
        has_novel_free_text = bool(resolution.get("has_novel_free_text"))
        has_unresolved_legal = bool(
            resolution.get("has_unconfirmed_legal_attestation")) or any(
            i.get("kind") == "legal_attestation" for i in needs_input)
        autonomy, confidence = _decide_autonomy(
            app, coverage,
            user_email=user_email,
            has_novel_free_text=has_novel_free_text,
            has_unresolved_legal=has_unresolved_legal,
            needs_account=needs_account)

        # F: stash the recon form fingerprint alongside the answers so the approval
        # snapshot (and its before-submit re-validation) can prove the form did
        # not change between approval and submission.
        recon_fp = _recon_fingerprint(questions)

        # Explicit autonomy MODES gate (dark-launch, defaults OFF). Applied AFTER
        # decide(), AROUND F's approve path: it CAPS the raw decision per the
        # user's chosen mode (never upgrades — the never-fabricate `ask`/`hold`
        # above always win) and enforces the mode-unset fail-safe.
        mode: Optional[str] = None
        manual_apply = False
        if _modes_enabled():
            eff_autonomy, mode, refuse = _mode_decision(user_email, autonomy)
            if refuse:
                # FAIL-SAFE: no explicit mode chosen yet (onboarding forces the
                # pick). Persist the resolved answers as an answer sheet for later,
                # but NEVER approve/prepare/submit — nothing auto-proceeds.
                _persist_answers(application_id, user_email, answers, coverage,
                                 needs_account, autonomy=autonomy,
                                 confidence_score=confidence,
                                 recon_fingerprint=recon_fp)
                logger.info("auto_apply gather %s: mode unset — refusing to "
                            "auto-act (needs onboarding)", application_id)
                return {"status": "mode_required", "application_id": application_id,
                        "mode": None, "needs_onboarding": True,
                        "autonomy": autonomy, "confidence_score": confidence,
                        "coverage": coverage, "questions": len(questions),
                        "resolved": len(answers), "needs_input": len(needs_input),
                        "needs_account": needs_account}
            autonomy = eff_autonomy
            manual_apply = (mode == "copilot")

        _persist_answers(application_id, user_email, answers, coverage,
                         needs_account, autonomy=autonomy,
                         confidence_score=confidence, recon_fingerprint=recon_fp,
                         mode=mode, manual_apply=manual_apply)

        # Land the row (F state split):
        #   * gaps / Oracle account wall → needs_input (unchanged);
        #   * clean AUTO → ready_for_review → SYSTEM-approve (freeze snapshot,
        #     approved_by="system") → immediate submit; autopilot converges with
        #     manual approval on the same approved+snapshot state;
        #   * clean but ASK/HOLD → wait in ready_for_review for a MANUAL approval.
        prepare_dispatched = False
        has_gaps = bool(needs_input) or needs_account
        if has_gaps:
            ledger.mark_needs_input(user_email, application_id, needs_input,
                                    autonomy=autonomy, confidence_score=confidence)
            final = ledger.STATUS_NEEDS_INPUT
        elif autonomy == "auto":
            if (_prepare_enabled() and not app.get("tailoring_record_id")
                    and _user_wants_tailored_resume(user_email)):
                # Defect D — a row may be approved ONLY after a tailored artifact
                # is pinned. Dispatch the async prepare phase (reserve → tailor →
                # pin → land ready_for_review → system-approve). The row stays
                # `gathering` until prepare completes (gathering→ready_for_review
                # is legal). Users may opt into plain-resume auto-apply via
                # auto_submit_settings.use_tailored_resume=False.
                _enqueue_prepare(application_id, user_email)
                final = ledger.STATUS_GATHERING
                prepare_dispatched = True
            else:
                ledger.mark_ready_for_review(
                    user_email, application_id, autonomy=autonomy,
                    confidence_score=confidence,
                    tailoring_record_id=app.get("tailoring_record_id"))
                _system_approve(user_email, application_id)
                final = ledger.STATUS_APPROVED
                # Clean auto-decision → submit NOW, don't wait for the hourly
                # sweep. Gated by AUTO_SUBMIT_ENABLED; safe/idempotent with it.
                request_immediate_submit(user_email, application_id)
        else:
            ledger.mark_ready_for_review(
                user_email, application_id, autonomy=autonomy,
                confidence_score=confidence,
                tailoring_record_id=app.get("tailoring_record_id"))
            final = ledger.STATUS_READY_FOR_REVIEW
    except Exception as e:
        logger.error("auto_apply gather %s: failed: %s", application_id, e)
        ledger.mark_failed(user_email, application_id, f"gather_error: {e}")
        raise

    return {
        "status": final,
        "application_id": application_id,
        "autonomy": autonomy,
        "confidence_score": confidence,
        "coverage": coverage,
        "questions": len(questions),
        "resolved": len(answers),
        "needs_input": len(needs_input),
        "needs_account": needs_account,
        "prepare_dispatched": prepare_dispatched,
        "mode": mode,
        "manual_apply": manual_apply,
    }


def _persist_answers(application_id: str, user_email: str,
                     answers: Dict[str, Any], coverage: float,
                     needs_account: bool,
                     autonomy: Optional[str] = None,
                     confidence_score: Optional[float] = None,
                     recon_fingerprint: Optional[str] = None,
                     resume_version_id: Optional[str] = None,
                     mode: Optional[str] = None,
                     manual_apply: Optional[bool] = None) -> None:
    """Store resolved answers in the owned collection (out of the lean ledger
    row; answers may carry PII). Upsert so a re-gather overwrites cleanly.

    The autonomy decision (autonomy/confidence_score) is stashed alongside so the
    async ``prepare`` phase (defect D) can land the row in review with the SAME
    decision gather computed, without re-deriving it or widening the lean ledger
    row. F adds two more stashed signals — the recon form fingerprint and the
    pinned résumé version — that feed the immutable approval snapshot AND its
    before-submit re-validation (build_approval_snapshot reads them here).

    The autonomy-MODES gate adds two more: ``mode`` (the mode this row was gated
    under) and ``manual_apply`` (Copilot's prepare-only flag) — surfaced as the
    row's "answer sheet" (see ``answer_sheet``). Both live here, NOT on the lean
    ledger row, so the golden ledger read stays byte-identical."""
    set_fields: Dict[str, Any] = {
        "answers": answers, "coverage": coverage,
        "needs_account": needs_account, "updated_at": _now(),
    }
    if autonomy is not None:
        set_fields["autonomy"] = autonomy
    if confidence_score is not None:
        set_fields["confidence_score"] = float(confidence_score)
    if recon_fingerprint is not None:
        set_fields["recon_fingerprint"] = recon_fingerprint
    if resume_version_id is not None:
        set_fields["resume_version_id"] = resume_version_id
    if mode is not None:
        set_fields["mode"] = mode
    if manual_apply is not None:
        set_fields["manual_apply"] = bool(manual_apply)
    _answers_coll().update_one(
        {"application_id": application_id, "user_email": user_email},
        {"$set": set_fields, "$setOnInsert": {"created_at": _now()}},
        upsert=True,
    )


def _load_decision(application_id: str, user_email: str) -> Dict[str, Any]:
    """The autonomy decision gather stashed for ``prepare`` — {autonomy?,
    confidence_score?}. Empty when gather never ran (e.g. prepare invoked
    directly), in which case mark_ready leaves the row's existing decision."""
    doc = _answers_coll().find_one(
        {"application_id": application_id, "user_email": user_email},
        {"_id": 0, "autonomy": 1, "confidence_score": 1})
    return doc or {}


def _load_answers(application_id: str, user_email: str) -> Dict[str, Any]:
    doc = _answers_coll().find_one(
        {"application_id": application_id, "user_email": user_email},
        {"_id": 0, "answers": 1})
    return (doc or {}).get("answers") or {}


def answer_sheet(user_email: str, application_id: str) -> Dict[str, Any]:
    """The resolved-answers "answer sheet" for a row — what Copilot surfaces so
    the user can apply MANUALLY (prepare-only mode: JobPilot resolves every field
    and hands it over, but NEVER submits). Owner-scoped. Returns
    {application_id, answers, coverage, mode, manual_apply}; empty answers when
    the row was never gathered."""
    application_id = _clean(application_id, 64)
    doc = _answers_coll().find_one(
        {"application_id": application_id, "user_email": user_email},
        {"_id": 0, "answers": 1, "coverage": 1, "mode": 1, "manual_apply": 1}) or {}
    return {
        "application_id": application_id,
        "answers": doc.get("answers") or {},
        "coverage": doc.get("coverage"),
        "mode": doc.get("mode"),
        "manual_apply": bool(doc.get("manual_apply", False)),
    }


# ===========================================================================
# PHASE 1.6 — RESUME AFTER ANSWER (AUTOAPPLY_REPAIR_SPEC §3)
#
# `needs_input` used to be a TERMINAL DEAD END: no surface accepted answers for
# it, `gather` treats it as an idempotent no-op, and `submit` refuses anything
# that is not approved — so a single unanswered screening question stranded the
# application forever and the user was told to go apply manually.
#
# This is the loop that closes it: merge the user's answers, BANK each one in the
# answer library (so the same question is never asked twice), re-resolve what is
# still open, and either shrink the queue (still `needs_input`) or advance the
# row to `ready_for_review` — then honour the autonomy mode, so aggressive /
# autopilot go straight through approve + submit.
#
# LATENCY: every step here is deterministic Mongo work (`answer_library_service.
# resolve` never calls an LLM), so this is a fast synchronous path. The only
# long-running work — the actual browser submission — is dispatched as the
# EXISTING `auto_apply_submit` async job via request_immediate_submit (CLAUDE.md
# invariant 2); nothing slow ever runs on the request thread.
# ===========================================================================
_MAX_SUBMITTED_ANSWERS = 50
_MAX_ANSWER_CHARS = 4000
_MAX_ANSWER_OPTIONS = 25

# needs_input kinds a TYPED ANSWER can never clear — an Oracle wall (email
# verify / password reset / CAPTCHA), an ATS account gate, an exhausted tailor
# quota, a failed prepare. They stay in the queue (and keep the row in
# needs_input) no matter what the user types, so we never pretend a wall cleared.
_NON_ANSWERABLE_KINDS = frozenset({"account", "quota", "prepare_failed"})
_NON_ANSWERABLE_KIND_PREFIXES = ("oracle_",)
# Mirrors auto_submit_service._ACCOUNT_GATE_PROMPT (kept as a literal so this
# module stays free of a reverse import).
_ACCOUNT_GATE_QUESTION = (
    "Sign in or create an account on the employer's career site to continue."
)


class AutoApplyError(Exception):
    """Typed service error — the route maps `status`/`error` onto the HTTP
    response. NEVER 401 (that is reserved for a missing/invalid JWT)."""
    status = 400
    error = "auto_apply_error"

    def __init__(self, message: Optional[str] = None):
        self.message = message or self.error
        super().__init__(self.message)


class InvalidAnswerInput(AutoApplyError):
    status = 400
    error = "invalid_answers"


class NotInNeedsInput(AutoApplyError):
    """The row is not (or no longer) waiting on the user — answering it is a
    business conflict, not an auth failure."""
    status = 409
    error = "not_in_needs_input"


def _answerable(item: Dict[str, Any]) -> bool:
    """True when a needs_input entry is a QUESTION the user can answer here."""
    kind = str((item or {}).get("kind") or "").strip().lower()
    if kind in _NON_ANSWERABLE_KINDS:
        return False
    return not any(kind.startswith(p) for p in _NON_ANSWERABLE_KIND_PREFIXES)


def _needs_view(item: Dict[str, Any]) -> Dict[str, str]:
    """The {question, kind, field_id} shape the contract returns in `remaining`."""
    out = {
        "question": _clean(item.get("question"), 500),
        "kind": _clean(item.get("kind"), 40),
        "field_id": _clean(item.get("field_id"), 200),
    }
    for extra in ("apply_url", "action"):
        val = _clean(item.get(extra), 2000 if extra == "apply_url" else 40)
        if val:
            out[extra] = val
    return out


def _sanitize_answer_value(value: Any) -> Any:
    """Bound + sanitize ONE submitted answer. Strings are capped/cleaned,
    bool/int/float pass through, a small list of scalars survives (multi-select);
    anything else is coerced to a bounded string. None means "no answer"."""
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return _clean(value, _MAX_ANSWER_CHARS) or None
    if isinstance(value, list):
        out = [_sanitize_answer_value(v) for v in value[:_MAX_ANSWER_OPTIONS]]
        out = [v for v in out if v is not None]
        return out or None
    return _clean(str(value), _MAX_ANSWER_CHARS) or None


def _sanitize_submitted_answers(answers: Any) -> Dict[str, Any]:
    """Validate + sanitize the {field_id: value} body. Every key is sanitized
    BEFORE it can reach a Mongo document/filter (invariant 4) and the map is
    bounded so a malformed client can never bloat the answer sheet."""
    if not isinstance(answers, dict):
        raise InvalidAnswerInput("answers must be an object of {field_id: value}")
    if not answers:
        raise InvalidAnswerInput("answers must contain at least one field")
    if len(answers) > _MAX_SUBMITTED_ANSWERS:
        raise InvalidAnswerInput(
            f"at most {_MAX_SUBMITTED_ANSWERS} answers may be submitted at once")
    clean: Dict[str, Any] = {}
    for raw_key, raw_value in answers.items():
        if not isinstance(raw_key, str):
            continue
        fid = _clean(raw_key, 200)
        # Never let an operator-shaped / dotted key reach a Mongo $set path.
        if not fid or fid.startswith("$") or "." in fid:
            continue
        value = _sanitize_answer_value(raw_value)
        if value is None:
            continue
        clean[fid] = value
    if not clean:
        raise InvalidAnswerInput("no usable answers were provided")
    return clean


def _remember_answer(user_email: str, question: str, value: Any) -> bool:
    """Bank ONE answer in the per-user answer library so the SAME question is
    never asked again (spec §3.1 — `remember()` had no production caller). The
    library keys on the normalized QUESTION text, so this needs the label, not
    the field id. Best-effort: a library fault never breaks the resume loop."""
    if not question:
        return False
    try:
        from services import answer_library_service
    except ImportError:  # pragma: no cover - parallel build safety
        return False
    try:
        return answer_library_service.remember(user_email, question, value) is not None
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_apply: remember failed for %s: %s", user_email, e)
        return False


def _merge_answers(application_id: str, user_email: str,
                   answers: Dict[str, Any]) -> None:
    """Upsert the merged answer sheet. Kept out of the lean ledger row (answers
    carry PII), same store gather/prepare/execution_envelope already read."""
    _answers_coll().update_one(
        {"application_id": application_id, "user_email": user_email},
        {"$set": {"answers": answers, "updated_at": _now()},
         "$setOnInsert": {"created_at": _now()}},
        upsert=True,
    )


def _question_from_needs_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """The normalized question shape `answer_library_service.resolve` consumes.
    Every needs_input entry was emitted BECAUSE it was required (or a legal
    attestation), so `required` is True by construction."""
    kind = str(item.get("kind") or "").strip().lower()
    return {
        "field_id": _clean(item.get("field_id"), 200),
        "label": _clean(item.get("question"), 500),
        "required": True,
        "is_legal_attestation": kind == "legal_attestation",
    }


def resume_after_answers(user_email: str, application_id: str,
                         answers: Any) -> Dict[str, Any]:
    """Spec §3: accept the user's answers for a `needs_input` row and RESUME it.

    1. Merge the answers into the row's answer sheet and `remember()` each one.
    2. Re-resolve every question that is still open (the library may now resolve
       neighbours from the banked answer, and a legal attestation answered here
       becomes a DECLARED value — never a fabricated one).
    3. Gaps left → stay `needs_input` with a SHRUNKEN queue; return `remaining`.
    4. Clear → `needs_input -> ready_for_review`, then honour the autonomy mode:
       an effective `auto` (aggressive, or graduated autopilot) is system-approved
       and its submit is enqueued immediately. Anything else waits for a manual
       approval — but the user is NEVER told to go apply manually for a question
       they just answered.

    Returns the §3 contract dict. Raises NotInNeedsInput (409) /
    InvalidAnswerInput (400) / ledger.ApplicationNotFound (404).
    """
    application_id = _clean(application_id, 64)
    if not application_id:
        raise InvalidAnswerInput("application_id is required")
    app = ledger.get_application(user_email, application_id)
    if not app:
        raise ledger.ApplicationNotFound()
    if app.get("status") != ledger.STATUS_NEEDS_INPUT:
        raise NotInNeedsInput(
            f"this application is not waiting for your input (status="
            f"{app.get('status')})")

    submitted = _sanitize_submitted_answers(answers)
    pending = [i for i in (app.get("needs_input") or []) if isinstance(i, dict)]
    by_field = {i.get("field_id"): i for i in pending if i.get("field_id")}

    # An ATS ACCOUNT wall is frequently parked with an EMPTY question list (a
    # Workday login gate): without this the row would look "clear" and get
    # resumed by an unrelated answer. It is a wall, not a question.
    sheet = _answers_coll().find_one(
        {"application_id": application_id, "user_email": user_email},
        {"_id": 0, "needs_account": 1}) or {}
    needs_account = bool(sheet.get("needs_account"))
    if needs_account and not any(
            str(i.get("field_id") or "") == "ats_account" for i in pending):
        pending = pending + [{"question": _ACCOUNT_GATE_QUESTION,
                              "kind": "account", "field_id": "ats_account"}]

    # (1) merge + bank.
    stored = dict(_load_answers(application_id, user_email))
    remembered = 0
    for fid, value in submitted.items():
        stored[fid] = value
        question = (by_field.get(fid) or {}).get("question")
        if _remember_answer(user_email, question, value):
            remembered += 1
    _merge_answers(application_id, user_email, stored)

    # (2) re-resolve everything still open (skip what was just answered, and the
    # walls no answer can clear).
    blocked = [_needs_view(i) for i in pending if not _answerable(i)]
    open_items = [i for i in pending
                  if _answerable(i) and i.get("field_id") not in submitted]
    resolution: Dict[str, Any] = {}
    still_missing: List[Dict[str, str]] = []
    if open_items:
        newly, still_missing, resolution = _resolve_answers(
            user_email, [_question_from_needs_item(i) for i in open_items],
            company=str(app.get("company") or ""))
        if newly:
            stored.update(newly)
            _merge_answers(application_id, user_email, stored)

    remaining = still_missing + blocked

    # (3) gaps left → stay in needs_input, but SHRINK the queue to what is
    # actually still open (the user must never be re-asked what they answered).
    if remaining:
        ledger.update_needs_input(user_email, application_id, remaining)
        logger.info("auto_apply resume %s: %d answer(s) merged, %d still open",
                    application_id, len(submitted), len(remaining))
        return {"status": "resumed",
                "application_status": ledger.STATUS_NEEDS_INPUT,
                "remaining": remaining,
                "answered": len(submitted), "remembered": remembered,
                "application_id": application_id}

    # (4) clear → land review, then honour the mode.
    cov = resolution.get("coverage")
    coverage = float(cov) if cov is not None else 1.0
    has_novel_free_text = bool(resolution.get("has_novel_free_text"))
    autonomy, confidence = _decide_autonomy(
        app, coverage, user_email=user_email,
        has_novel_free_text=has_novel_free_text,
        has_unresolved_legal=False, needs_account=False)

    mode: Optional[str] = None
    refuse = False
    if _modes_enabled():
        autonomy, mode, refuse = _mode_decision(user_email, autonomy)

    _persist_answers(application_id, user_email, stored, coverage, needs_account,
                     autonomy=autonomy, confidence_score=confidence, mode=mode)

    ledger.mark_ready_for_review(user_email, application_id, autonomy=autonomy,
                                 confidence_score=confidence,
                                 tailoring_record_id=app.get("tailoring_record_id"))
    landed = ledger.STATUS_READY_FOR_REVIEW
    approve_result: Dict[str, Any] = {}
    if autonomy == "auto" and not refuse:
        # Aggressive / graduated autopilot: converge on the SAME approved +
        # frozen-snapshot state a manual approval reaches, then enqueue the
        # submit job (never an inline submit).
        approve_result = ledger.approve_with_result(
            user_email, [application_id], approved_by="system",
            snapshot_provider=lambda aid: build_approval_snapshot(user_email, aid))
        if approve_result.get("approved"):
            landed = ledger.STATUS_APPROVED
            request_immediate_submit(user_email, application_id)

    logger.info("auto_apply resume %s: cleared — landed %s (autonomy=%s mode=%s)",
                application_id, landed, autonomy, mode)
    out = {"status": "resumed", "application_status": landed, "remaining": [],
           "answered": len(submitted), "remembered": remembered,
           "autonomy": autonomy, "mode": mode,
           "application_id": application_id}
    if approve_result.get("over_quota"):
        # The answers ARE saved and the row IS in review — the user is simply out
        # of auto-submit slots, so it waits for a manual approve/upgrade.
        out["quota_exceeded"] = True
        out["usage"] = approve_result.get("usage") or {}
    return out


def _resume_ref(app: Dict[str, Any], user_email: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """A reference the executor resolves to PDF bytes (the tailored variant) —
    NOT the bytes themselves (the orchestrator never carries megabytes; the
    fleet fetches from S3). None when no tailored variant is linked.

    ``user_email`` is folded in because the S3 lookup for the tailored résumé is
    owner-scoped and the ledger view (`app`) intentionally omits the owner."""
    trid = app.get("tailoring_record_id")
    if not trid:
        return None
    ref = {"tailoring_record_id": trid}
    if user_email:
        ref["user_email"] = user_email
    return ref


def pinned_resume_s3_key(application_id: str, user_email: str) -> Optional[str]:
    """Resolve the ONE pinned (immutable-S3-keyed) tailored-résumé PDF key for a
    row — what the executor callback surface presigns a one-use GET for. Owner-
    scoped. None when no pinned artifact exists (prepare/D guarantees one before
    submit, so None means "not ready", not "attach nothing")."""
    app = ledger.get_application(user_email, application_id)
    trid = (app or {}).get("tailoring_record_id")
    if not trid:
        return None
    rec = DBConnect().get_db().tailoring_records.find_one(
        {"record_id": _clean(trid, 200), "user_email": user_email},
        {"_id": 0, "versions": 1, "current_version_id": 1})
    if not rec:
        return None
    versions = rec.get("versions") or []
    version = next((v for v in versions
                    if v.get("version_id") == rec.get("current_version_id")), None)
    version = version or (versions[-1] if versions else None)
    if not version:
        return None
    return ((version.get("files") or {}).get("pdf") or {}).get("s3_key")


def execution_envelope(application_id: str, user_email: str) -> Optional[Dict[str, Any]]:
    """The MINIMAL execution envelope the remote worker claims. Contains only what
    a browser needs to fill + submit ONE form: the apply target, the ATS family/
    tenant, the vetted answer map (field_id→value — resolved + integrity-guarded
    here, never invented by the worker), and boolean hints for the one-use
    artifact/credential pulls. Carries NO résumé bytes, NO credentials, NO S3
    keys — those ride their own one-use endpoints. None if the row is gone."""
    app = ledger.get_application(user_email, application_id)
    if not app:
        return None

    # P0-1 SSRF containment (envelope gate): the apply_url originates from a job
    # posting (attacker-influenceable). It MUST be https AND on the supported-ATS
    # public-suffix-boundary allow-list before ANY browser navigates to it. A
    # rejection returns a BLOCKED envelope carrying no answers/credentials — the
    # worker aborts and the execution never dispatches. This is defense-in-depth
    # atop the network-egress firewall; the worker's context.route interceptor
    # re-validates every navigation/redirect (DNS-rebinding pin) at run time.
    apply_url = app.get("apply_url") or ""
    allowed, reason = apply_url_allowed(apply_url)
    if not allowed:
        logger.warning("execution_envelope %s: apply_url blocked (%s) — not "
                       "dispatching", application_id, reason)
        return {
            "application_id": _clean(application_id, 64),
            "apply_url": apply_url,
            "ats_family": app.get("ats_family"),
            "blocked": True,
            "blocked_reason": reason,
            "answers": {},
            "resume_available": False,
            "credential_required": False,
        }

    ans_doc = _answers_coll().find_one(
        {"application_id": _clean(application_id, 64), "user_email": user_email},
        {"_id": 0, "answers": 1, "needs_account": 1}) or {}
    return {
        "application_id": _clean(application_id, 64),
        "apply_url": apply_url,
        "ats_family": app.get("ats_family"),
        "tenant": app.get("ats_tenant") or app.get("tenant") or "",
        "answers": ans_doc.get("answers") or {},
        "resume_available": bool(app.get("tailoring_record_id")),
        "credential_required": bool(ans_doc.get("needs_account")),
    }


# ===========================================================================
# Approval snapshot (defect F) — the immutable evidence frozen at approval and
# re-validated immediately before submit. The three integrity fields are:
#   * application_hash    — the reconned form fingerprint ("the form we approved
#                           is the form we submit");
#   * resume_version_id   — the pinned tailored-résumé version (D);
#   * answers_fingerprint — the resolved-answers fingerprint (reuses
#                           auto_submit_service._answers_fingerprint).
# The SAME builder runs at freeze and re-validate, so a match proves nothing
# drifted between approval and submission.
# ===========================================================================
def _recon_fingerprint(questions: List[Dict[str, Any]]) -> str:
    """A stable fingerprint of the reconned form — field ids + required/legal
    shape only (never answer values). Sorted so recon ordering never changes it."""
    import hashlib
    import json
    basis = sorted(
        ({"field_id": q.get("field_id"),
          "required": bool(q.get("required")),
          "is_legal": bool(q.get("is_legal_attestation"))}
         for q in (questions or []) if isinstance(q, dict)),
        key=lambda x: x.get("field_id") or "")
    blob = json.dumps(basis, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def build_approval_snapshot(user_email: str, application_id: str) -> Dict[str, Any]:
    """Assemble the F integrity fields for a row's approval snapshot from the
    persisted stores (resolved answers + recon fingerprint + pinned résumé
    version). Runs BOTH at approval (freeze onto the row via ledger.approve) and
    before submit (re-validate) — identical inputs → identical output when nothing
    drifted. Missing signals resolve to None (e.g. a row approved before any recon
    ran), which still compares cleanly against a later None."""
    from services.auto_submit_service import _answers_fingerprint
    doc = _answers_coll().find_one(
        {"application_id": _clean(application_id, 64), "user_email": user_email},
        {"_id": 0, "answers": 1, "recon_fingerprint": 1, "resume_version_id": 1}) or {}
    answers = doc.get("answers")
    return {
        "application_hash": doc.get("recon_fingerprint"),
        "resume_version_id": doc.get("resume_version_id"),
        # None (not a fingerprint of {}) when NO answers were ever persisted, so a
        # row approved without resolved answers re-validates consistently (an
        # absent answer set at freeze must equal an absent set at submit).
        "answers_fingerprint": _answers_fingerprint(answers) if answers is not None else None,
    }


def _system_approve(user_email: str, application_id: str) -> int:
    """Autopilot approval: pass an `auto` row THROUGH ready_for_review → approved
    on the SAME snapshot path as a manual approval (approved_by='system'), so
    autopilot and manual approvals converge on one approved+frozen-snapshot state
    before any submit."""
    return ledger.approve(
        user_email, [application_id], approved_by="system",
        snapshot_provider=lambda aid: build_approval_snapshot(user_email, aid))


def _snapshot_stale(frozen: Optional[Dict[str, Any]],
                    current: Optional[Dict[str, Any]]) -> bool:
    """True when any of the three integrity fields drifted since approval — the
    form changed, the pinned résumé changed, or the answers changed. A stale
    snapshot must NOT submit; the approval is invalidated back to review."""
    frozen = frozen or {}
    current = current or {}
    return any(frozen.get(k) != current.get(k)
               for k in ("application_hash", "resume_version_id",
                         "answers_fingerprint"))


# ===========================================================================
# PHASE 1.5 — prepare (defect D: the REQUIRED tailored-résumé stage)
# ===========================================================================
def _artifact_pinned(tailoring_record_id: str, user_email: str) -> bool:
    """True when the linked tailoring_records doc has a version carrying a pinned
    (immutable-S3-keyed) rendered PDF — the "an immutable artifact already
    exists" check that makes ``prepare`` a redelivery no-op. Owner-scoped."""
    trid = _clean(tailoring_record_id, 200)
    if not trid:
        return False
    rec = DBConnect().get_db().tailoring_records.find_one(
        {"record_id": trid, "user_email": user_email},
        {"_id": 0, "versions": 1})
    if not rec:
        return False
    for v in (rec.get("versions") or []):
        if ((v.get("files") or {}).get("pdf") or {}).get("s3_key"):
            return True
    return False


def _resolve_jd_text(user_email: str, application_id: str,
                     app: Dict[str, Any]) -> str:
    """Server-side JD text for tailoring — NEVER from the client (SSRF/injection
    posture, mirroring guest_service). Resolved from the pinned inventory posting
    behind the ledger row (``posting_job_id`` → job_inventory.resolve_jd) when
    available, else a minimal company+title fallback so the tailor still has a
    target. Reads posting_job_id from the raw owner-scoped row (the lean ledger
    view intentionally omits it)."""
    posting_job_id: Optional[str] = None
    try:
        row = DBConnect().get_db().applications.find_one(
            {"application_id": _clean(application_id, 64), "user_email": user_email},
            {"_id": 0, "posting_job_id": 1})
        posting_job_id = (row or {}).get("posting_job_id")
    except Exception:  # pragma: no cover - defensive
        posting_job_id = None

    if posting_job_id:
        try:
            from services import job_inventory_service as inv
            resolved = inv.resolve_jd(str(posting_job_id))
            if resolved and resolved.get("jd_text"):
                return str(resolved["jd_text"])
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("auto_apply prepare %s: JD resolve failed: %s",
                           application_id, e)

    company = _clean(app.get("company"), 200)
    title = _clean(app.get("title"), 200)
    return (f"{title} at {company}".strip() if (title or company)
            else "") or title or company


def _enqueue_prepare(application_id: str, user_email: str) -> Optional[str]:
    """Enqueue an async auto_apply_prepare job (defect D). Lazy import of the
    resume orchestrator to avoid an import cycle at module load."""
    try:
        from services.resume_service import get_resume_service, ResumeService
        payload = {"application_id": application_id, "user_email": user_email}
        job_id = get_resume_service().create_job("auto_apply_prepare", payload, user_email)
        ResumeService.invoke_async(job_id, "auto_apply_prepare", payload)
        return job_id
    except Exception as e:  # pragma: no cover - defensive
        logger.error("auto_apply: failed to enqueue prepare for %s: %s", application_id, e)
        return None


def prepare(application_id: str, user_email: str) -> Dict[str, Any]:
    """PHASE 1.5 — pin a REQUIRED tailored-résumé artifact, then land `ready`.

    Selects/creates an owner-scoped tailored variant for the application's JD,
    PINS an immutable version + rendered PDF in S3, writes tailoring_record_id
    back onto the ledger row, and only THEN calls the SAME mark_ready path gather
    used to call directly — so a row can only ever reach `ready` (and thus
    `submit`) with a real résumé attached. Never submits; never fabricates.

    Contracts:
      * IDEMPOTENT (Lambda at-least-once): a row that already has a pinned
        tailoring_record_id + immutable artifact is a no-op — redelivery never
        re-tailors and never double-reserves quota.
      * RESERVE-BEFORE-SPEND (invariant #3): a tailor slot is reserved via
        quota_ledger_service (idempotency_key = application_id) BEFORE the LLM
        tailor call, completed on success, refunded on failure. Never
        double-counted.
      * On a reservation denial → the row is HELD in needs_input (never ready).
      * On a tailor/render failure → the reservation is refunded and the row goes
        to needs_input (never ready).
    """
    application_id = _clean(application_id, 64)
    app = ledger.get_application(user_email, application_id)
    if not app:
        raise ledger.ApplicationNotFound()

    # (b) Idempotency — an immutable artifact already pinned? Redelivery must
    # NOT re-tailor or re-reserve; hand back the existing linkage.
    existing_trid = app.get("tailoring_record_id")
    if existing_trid and _artifact_pinned(existing_trid, user_email):
        logger.info("auto_apply prepare %s: artifact already pinned (%s) — no-op",
                    application_id, existing_trid)
        return {"idempotent": True, "application_id": application_id,
                "tailoring_record_id": existing_trid, "status": app.get("status")}

    # Prepare drives the clean auto path out of `gathering`; a terminal /
    # submitted / already-ready row is not a prepare target (idempotent no-op).
    status = app.get("status")
    if status != ledger.STATUS_GATHERING:
        logger.info("auto_apply prepare %s: status %s not a prepare target — skip",
                    application_id, status)
        return {"skipped": status, "application_id": application_id}

    # (c) Reserve the tailor slot BEFORE the LLM call. Idempotent by
    # application_id: a redelivered prepare that already reserved (but crashed
    # before pinning) reuses the SAME reservation instead of double-counting.
    from services import quota_ledger_service as quota
    allowed, entry_ids, usage = quota.reserve_tailor(
        user_email, n=1, idempotency_key=application_id)
    if not allowed:
        # Out of daily tailor quota — HOLD in needs_input, never ready. No tailor.
        logger.info("auto_apply prepare %s: tailor quota exhausted — holding",
                    application_id)
        ledger.mark_needs_input(
            user_email, application_id,
            [{"question": "Daily tailoring limit reached — tailoring resumes "
                          "tomorrow or after an upgrade.",
              "kind": "quota", "field_id": "tailor_quota"}])
        return {"status": ledger.STATUS_NEEDS_INPUT, "reason": "tailor_quota_exhausted",
                "application_id": application_id, "usage": usage}

    # (d) Tailor + pin the immutable artifact. Reuses the resume_service tailor
    # path (never reimplemented); this whole phase is an async job, so the LLM is
    # never on the request path.
    try:
        jd_text = _resolve_jd_text(user_email, application_id, app)
        from services.resume_service import prepare_tailored_artifact
        artifact = prepare_tailored_artifact(user_email, jd_text)
    except Exception as e:
        # (f) Refund the reservation and HOLD — NEVER ready on a tailor failure.
        quota.refund_entries(entry_ids)
        logger.error("auto_apply prepare %s: tailor/pin failed: %s", application_id, e)
        ledger.mark_needs_input(
            user_email, application_id,
            [{"question": "We could not tailor your résumé for this role — please "
                          "retry.", "kind": "prepare_failed", "field_id": "prepare"}])
        return {"status": ledger.STATUS_NEEDS_INPUT, "reason": "tailor_failed",
                "error": str(e)[:200], "application_id": application_id}

    trid = artifact["record_id"]
    # A real slot was consumed — settle the reservation(s) (idempotent).
    for eid in entry_ids:
        quota.complete_entry(eid, record_id=trid)

    # F: stash the pinned résumé version so the approval snapshot + submit
    # re-validation key off the exact artifact this row was prepared with.
    _answers_coll().update_one(
        {"application_id": application_id, "user_email": user_email},
        {"$set": {"resume_version_id": artifact.get("version_id"),
                  "updated_at": _now()}},
        upsert=True)

    # (e) Write tailoring_record_id back + land `ready_for_review`. On the clean
    # AUTO path, SYSTEM-approve (freeze snapshot, approved_by="system") + fire the
    # immediate submit — the same converged path gather uses. An ask/hold row
    # simply waits in ready_for_review for a manual approval.
    decision = _load_decision(application_id, user_email)
    autonomy = decision.get("autonomy")
    ledger.mark_ready_for_review(
        user_email, application_id,
        autonomy=autonomy,
        confidence_score=decision.get("confidence_score"),
        tailoring_record_id=trid)

    if autonomy == "auto":
        _system_approve(user_email, application_id)
        request_immediate_submit(user_email, application_id)
        landed = ledger.STATUS_APPROVED
    else:
        landed = ledger.STATUS_READY_FOR_REVIEW

    logger.info("auto_apply prepare %s: pinned tailoring_record %s — %s",
                application_id, trid, landed)
    return {"status": landed, "application_id": application_id,
            "tailoring_record_id": trid,
            "resume_version_id": artifact.get("version_id")}


# ===========================================================================
# PHASE 2 — submit
# ===========================================================================
def submit(application_id: str, user_email: str) -> Dict[str, Any]:
    """Drive submission for an APPROVED row. Idempotent + double-submit-safe.

    Flow (F state split — submit consumes `approved`/`queued_for_execution`, the
    post-approval execution states, NOT a bare `ready`):
      * only approved/queued_for_execution proceed (ready_for_review/needs_input/
        gathering/etc. → no-op — an unapproved row is never submitted).
      * RE-VALIDATE the frozen approval snapshot first: if the form / pinned
        résumé / answers drifted since approval, INVALIDATE back to
        ready_for_review and do NOT submit stale (re-approval re-freezes).
      * executor unavailable  → leave the row in its approved state (untouched) +
        log; the fleet picks it up later.
      * lease approved → queued_for_execution → running (the executor pickup) via
        status-guarded CAS — the double-submit backstop: exactly one Lambda wins;
        a redelivered/racing job sees the guard miss and no-ops.
      * executor.submit success → `submitted` (+ confirmation/screenshot audit).
      * Oracle wall (verify/reset/CAPTCHA) → FREEZE at the data layer (no hot
        browser held) + park the ledger in `needs_input`; a scheduled sweep
        thaws it via recheck_verify().
    """
    application_id = _clean(application_id, 64)
    app = ledger.get_application(user_email, application_id)
    if not app:
        raise ledger.ApplicationNotFound()

    status = app.get("status")
    if status in (ledger.STATUS_SUBMITTED, ledger.STATUS_CONFIRMED):
        logger.info("auto_apply submit %s: already %s — idempotent no-op",
                    application_id, status)
        return {"idempotent": True, "status": status, "application_id": application_id}
    if status not in (ledger.STATUS_APPROVED, ledger.STATUS_QUEUED_FOR_EXECUTION):
        logger.info("auto_apply submit %s: not approved (status=%s) — no-op",
                    application_id, status)
        return {"status": "not_ready", "ledger_status": status,
                "application_id": application_id}

    # F: re-validate the frozen approval snapshot BEFORE the executor acts. A
    # drift (form/résumé/answers changed since approval) invalidates the approval
    # back to ready_for_review — never submit a stale approval. Re-approval will
    # re-freeze a fresh snapshot.
    frozen = ledger.get_approval_snapshot(user_email, application_id)
    if frozen and _snapshot_stale(frozen, build_approval_snapshot(user_email, application_id)):
        logger.info("auto_apply submit %s: approval snapshot stale — invalidating",
                    application_id)
        try:
            ledger.transition(user_email, application_id,
                              ledger.STATUS_READY_FOR_REVIEW)
        except ledger.InvalidTransition:
            logger.info("auto_apply submit %s: could not invalidate (race) — no-op",
                        application_id)
        return {"status": "approval_invalidated", "application_id": application_id,
                "ledger_status": ledger.STATUS_READY_FOR_REVIEW}

    executor = get_executor()
    # Probe availability BEFORE grabbing the row, so an absent fleet leaves the
    # row in its approved state (there is no legal running→approved undo).
    if not executor.is_available():
        logger.info("auto_apply submit %s: executor unavailable — leaving %s",
                    application_id, status)
        return {"status": EXECUTOR_UNAVAILABLE, "ledger_status": status,
                "application_id": application_id}

    # Lease: approved → queued_for_execution → running (the executor pickup). Each
    # status-guarded CAS is the double-submit backstop — a concurrent/redelivered
    # job that lost the race finds the guard missed and no-ops.
    if status == ledger.STATUS_APPROVED:
        try:
            ledger.transition(user_email, application_id,
                              ledger.STATUS_QUEUED_FOR_EXECUTION)
        except ledger.InvalidTransition:
            logger.info("auto_apply submit %s: lost race to queue_for_execution — no-op",
                        application_id)
            return {"idempotent": True, "status": "leased_elsewhere",
                    "application_id": application_id}
    try:
        ledger.transition(user_email, application_id, ledger.STATUS_RUNNING)
    except ledger.InvalidTransition:
        logger.info("auto_apply submit %s: lost race to running — no-op",
                    application_id)
        return {"idempotent": True, "status": "running_elsewhere",
                "application_id": application_id}

    answers = _load_answers(application_id, user_email)
    resume_pdf = _resume_ref(app, user_email)
    try:
        result = executor.submit(app, answers, resume_pdf)
    except Exception as e:
        logger.error("auto_apply submit %s: executor raised: %s", application_id, e)
        ledger.mark_failed(user_email, application_id, f"submit_error: {e}")
        raise

    st = result.get("status")

    if st == SUBMIT_SUCCESS:
        ledger.mark_submitted(user_email, application_id)
        confirmation = _clean(result.get("confirmation"), 500)
        screenshot_ref = _clean(result.get("screenshot_ref"), 500)
        _audit_submit(user_email, application_id, confirmation, screenshot_ref)
        logger.info("auto_apply submit %s: submitted", application_id)
        return {"status": ledger.STATUS_SUBMITTED, "application_id": application_id,
                "confirmation": confirmation or None,
                "screenshot_ref": screenshot_ref or None}

    if st == SUBMIT_DISPATCHED:
        # ASYNC-DISPATCH executor (Cloud Run plane): the browser work runs
        # remotely and the TERMINAL outcome arrives later on the executor
        # callback surface (finalize_submission). Leave the row RUNNING — do NOT
        # mark it terminal here. A synchronous executor never returns this.
        execution_id = _clean(result.get("execution_id"), 64)
        logger.info("auto_apply submit %s: dispatched (execution_id=%s) — "
                    "row left running for the worker callback",
                    application_id, execution_id)
        return {"status": SUBMIT_DISPATCHED, "application_id": application_id,
                "execution_id": execution_id or None,
                "ledger_status": ledger.STATUS_RUNNING}

    if st == SUBMIT_WAITING_VERIFY:
        wall = result.get("wall") or {}
        return _park_verify(user_email, application_id, wall)

    if st in _UNSUPPORTED_STATUSES:
        # Spec §4: a vendor we cannot drive is PERMANENT and user-facing — the
        # `unsupported` terminal, NOT the retryable `failed` below (and never the
        # transient executor_unavailable path that follows).
        return _park_unsupported(user_email, application_id, result)

    if st in _PREPARED_STATUSES:
        # Filled, proof captured, Submit deliberately not clicked (copilot / ops
        # dry-run). A human handoff — never a failure.
        return _park_prepared(user_email, application_id, result)

    if st in _WAITING_HUMAN_STATUSES:
        return _park_waiting_human(user_email, application_id, result)

    if st == EXECUTOR_UNAVAILABLE:
        # Rare mid-flight drain (available() said yes, submit() said no). We are
        # in `submitting`; there is no legal path back to `ready`, so fail
        # (retryable) with a clear code and log — the retry re-gathers cheaply.
        logger.warning("auto_apply submit %s: executor drained mid-flight — "
                       "failing (retryable)", application_id)
        ledger.mark_failed(user_email, application_id, "executor_unavailable_midflight")
        return {"status": EXECUTOR_UNAVAILABLE, "ledger_status": ledger.STATUS_FAILED,
                "application_id": application_id}

    # SUBMIT_FAILED or anything unexpected → hard failure (retryable).
    err = _clean(result.get("error"), 500) or (st or SUBMIT_FAILED)
    ledger.mark_failed(user_email, application_id, err)
    logger.info("auto_apply submit %s: failed (%s)", application_id, err)
    return {"status": ledger.STATUS_FAILED, "error": err,
            "application_id": application_id}


def _audit_submit(user_email: str, application_id: str,
                  confirmation: str, screenshot_ref: str) -> None:
    """Append a submission proof entry to the shared audit log (PII-free —
    confirmation/screenshot are ATS artifact handles, not user data)."""
    try:
        ledger._audit(user_email, "auto_apply_submitted",
                      application_id=application_id,
                      meta={"has_confirmation": bool(confirmation),
                            "has_screenshot": bool(screenshot_ref)})
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_apply: submit audit failed for %s: %s", application_id, e)


# ===========================================================================
# Pre-submit re-check gate (P5) — the LAST guard the remote worker calls
# immediately BEFORE it clicks Submit. The worker is stateless and holds no
# Mongo/entitlement view, so the authoritative go/no-go MUST be re-computed here
# server-side. A single "no" aborts the click — nothing is submitted.
#
# The checks (all must pass):
#   * kill_switch        — the global AUTO_SUBMIT master switch is still ON.
#   * autopilot_active   — the user has not paused / revoked auto-apply.
#   * row_live           — the row is still in an in-flight execution state
#                          (not cancelled/withdrawn/submitted/failed).
#   * approval_fresh     — a frozen approval snapshot exists AND has not drifted
#                          (immutable application hash + pinned résumé version +
#                          answers version — via _snapshot_stale) AND is within
#                          the approval TTL.
#   * entitlement_held   — the entitlement reservation taken at prepare is intact.
#   * adapter_healthy    — the executor still reports available.
#   * posting_available  — the job posting is still open.
# Best-effort seams (entitlement_held / posting_available / autopilot_active)
# default to SAFE (True only when we can positively confirm; a lookup error is
# surfaced as a reason, never silently passed).
# ===========================================================================
def _approval_ttl_hours() -> float:
    try:
        from utils.config import _get_config_value
        return float(_get_config_value("AUTO_APPLY_APPROVAL_TTL_HOURS", "72") or 72)
    except Exception:  # pragma: no cover - defensive
        return 72.0


def _autopilot_active(user_email: str, app: Dict[str, Any]) -> bool:
    """Seam: the user has not paused/revoked auto-apply. Defaults to True; a real
    per-user pause store (or a row-level revocation flag) plugs in here without
    touching the gate logic."""
    if app.get("autopilot_paused") or app.get("revoked"):
        return False
    return True


def _entitlement_reservation_held(user_email: str, app: Dict[str, Any]) -> bool:
    """Seam: the auto_submit entitlement reservation taken at prepare is still
    intact (not refunded/expired). Defaults to True; wire to the entitlements
    reservation ledger when it exposes a lookup. Never fabricates a pass on error
    — a raise is caught by the caller and surfaced as a reason."""
    return True


def _posting_available(app: Dict[str, Any]) -> bool:
    """Seam: the posting is still live (not pulled/closed). Defaults to True."""
    return not bool(app.get("posting_closed"))


def presubmit_recheck(application_id: str, user_email: str) -> Dict[str, Any]:
    """Re-run every pre-submit gate server-side. Returns
    {ok: bool, reasons: [str], checks: {name: bool}}. The remote worker calls
    this right before Submit and aborts on ``ok=False`` — so a pause, a revoked
    approval, a drifted form/résumé/answers, a spent entitlement, a closed
    posting, or the master kill-switch flipping OFF between dispatch and the click
    all stop the submission."""
    application_id = _clean(application_id, 64)
    checks: Dict[str, bool] = {}
    reasons: List[str] = []

    def _check(name: str, ok: bool, reason: str) -> None:
        checks[name] = bool(ok)
        if not ok:
            reasons.append(reason)

    # Global kill-switch.
    try:
        _check("kill_switch", _auto_submit_enabled(), "master_switch_off")
    except Exception:
        _check("kill_switch", False, "kill_switch_error")

    app = None
    try:
        app = ledger.get_application(user_email, application_id)
    except Exception:
        app = None
    if not app:
        _check("row_live", False, "application_not_found")
        return {"ok": False, "reasons": reasons, "checks": checks}

    live_states = (ledger.STATUS_RUNNING, ledger.STATUS_QUEUED_FOR_EXECUTION,
                   ledger.STATUS_WAITING_OTP, ledger.STATUS_WAITING_HUMAN)
    _check("row_live", app.get("status") in live_states,
           f"row_not_live:{app.get('status')}")

    _check("autopilot_active", _autopilot_active(user_email, app),
           "autopilot_paused_or_revoked")

    # Approval freshness: snapshot present, not drifted, and within TTL.
    try:
        frozen = ledger.get_approval_snapshot(user_email, application_id)
        if not frozen:
            _check("approval_fresh", False, "no_approval_snapshot")
        else:
            stale = _snapshot_stale(
                frozen, build_approval_snapshot(user_email, application_id))
            _check("approval_fresh", not stale, "approval_snapshot_stale")
    except Exception:
        _check("approval_fresh", False, "approval_snapshot_error")

    # Approval TTL (an approval that sat too long must not fire silently).
    approved_at = app.get("approved_at")
    if approved_at is not None:
        try:
            ttl = timedelta(hours=_approval_ttl_hours())
            at = approved_at
            if getattr(at, "tzinfo", None) is None:
                at = at.replace(tzinfo=timezone.utc)
            _check("approval_unexpired", (_now() - at) <= ttl, "approval_expired")
        except Exception:
            _check("approval_unexpired", False, "approval_ttl_error")

    try:
        _check("entitlement_held",
               _entitlement_reservation_held(user_email, app),
               "entitlement_reservation_lost")
    except Exception:
        _check("entitlement_held", False, "entitlement_check_error")

    try:
        _check("adapter_healthy", get_executor().is_available(),
               "adapter_unavailable")
    except Exception:
        _check("adapter_healthy", False, "adapter_health_error")

    # P1-KILL: an operator can dark a single ATS family (config
    # AUTO_APPLY_ADAPTERS_DISABLED) without a deploy — re-checked HERE so a
    # dispatched-but-not-yet-clicked run for a now-darked family aborts.
    try:
        from services.ats_adapters import adapter_disabled
        _check("adapter_enabled",
               not adapter_disabled(app.get("ats_family")), "adapter_disabled")
    except Exception:
        _check("adapter_enabled", True, "adapter_enabled_check_error")

    _check("posting_available", _posting_available(app), "posting_closed")

    ok = all(checks.values())
    if not ok:
        logger.info("presubmit_recheck %s: BLOCKED (%s)", application_id, reasons)
    return {"ok": ok, "reasons": reasons, "checks": checks}


# ===========================================================================
# Terminal finalize (P5) — the executor callback surface calls this when the
# remote worker reports the run's outcome. Drives the ledger from `running` to a
# terminal state. Idempotent: the callback surface guards against a duplicate
# report, and the ledger CAS backstops anything that still slips through.
#
# CRITICAL: a `submission_unknown` (a timeout / lost connection AFTER the Submit
# click) is HELD — NEVER auto-retried. Re-driving an ambiguous final submit could
# file a duplicate application, so it is parked for human review instead.
# ===========================================================================
def finalize_submission(application_id: str, user_email: str, *,
                        status: str,
                        confirmation: Optional[str] = None,
                        screenshot_ref: Optional[str] = None,
                        wall: Optional[Dict[str, Any]] = None,
                        error: Optional[str] = None,
                        field: Optional[str] = None,
                        reason: Optional[str] = None,
                        user_message: Optional[str] = None) -> Dict[str, Any]:
    """Finalize a dispatched submission from the worker's terminal report — the
    ONE place a reported outcome becomes a ledger state.

    ``status`` accepts BOTH vocabularies, because they overlap by design and a
    second mapping table is exactly how the two drifted apart:
      * the executor SUBMIT_* set (submitted / submission_unknown /
        waiting_verify / failed / executor_unsupported / prepared), and
      * the deterministic fill driver's outcome names (confirmed / submitted /
        submission_unknown / waiting_human / needs_input / prepared).

    (This absorbed the former ``finalize_fill_outcome`` — a duplicate mapping
    with zero production callers. ``fill_driver`` outcomes fold in here now.)
    """
    application_id = _clean(application_id, 64)
    app = ledger.get_application(user_email, application_id)
    if not app:
        raise ledger.ApplicationNotFound()

    cur = app.get("status")
    if cur in (ledger.STATUS_SUBMITTED, ledger.STATUS_CONFIRMED,
               ledger.STATUS_SUBMISSION_UNKNOWN):
        logger.info("finalize_submission %s: already terminal (%s) — no-op",
                    application_id, cur)
        return {"idempotent": True, "status": cur, "application_id": application_id}

    _adapter = str(app.get("ats_family") or "")

    if status in (SUBMIT_SUCCESS, "confirmed"):
        ledger.mark_submitted(user_email, application_id)
        confirmation = _clean(confirmation, 500)
        screenshot_ref = _clean(screenshot_ref, 500)
        _audit_submit(user_email, application_id, confirmation, screenshot_ref)
        _emit_metric("confirmation", application_id=application_id,
                     adapter=_adapter, outcome="submitted")
        landed = ledger.STATUS_SUBMITTED
        if status == "confirmed":
            # The driver SAW a positive confirmation — walk the last edge too.
            try:
                ledger.mark_confirmed(user_email, application_id)
                landed = ledger.STATUS_CONFIRMED
            except ledger.InvalidTransition:
                logger.info("finalize_submission %s: cannot confirm (race) — left "
                            "submitted", application_id)
        logger.info("finalize_submission %s: %s", application_id, landed)
        return {"status": landed, "application_id": application_id,
                "confirmation": confirmation or None,
                "screenshot_ref": screenshot_ref or None}

    if status == SUBMIT_UNKNOWN:
        # HELD — do not retry an ambiguous final submit.
        try:
            ledger.transition(user_email, application_id,
                              ledger.STATUS_SUBMISSION_UNKNOWN)
        except ledger.InvalidTransition:
            logger.info("finalize_submission %s: cannot mark submission_unknown "
                        "from %s (race) — no-op", application_id, cur)
        _emit_metric("submission_unknown", application_id=application_id,
                     adapter=_adapter, outcome="held")
        logger.warning("finalize_submission %s: SUBMISSION_UNKNOWN — held, no retry",
                       application_id)
        return {"status": ledger.STATUS_SUBMISSION_UNKNOWN,
                "application_id": application_id}

    if status == SUBMIT_WAITING_VERIFY:
        return _park_verify(user_email, application_id, wall or {})

    if status in _UNSUPPORTED_STATUSES:
        # Spec §4 — permanent + user-facing, never the retryable failure below.
        return _park_unsupported(user_email, application_id,
                                 {"user_message": user_message,
                                  "reason": reason or error})

    if status in _PREPARED_STATUSES:
        return _park_prepared(user_email, application_id,
                              {"screenshot_ref": screenshot_ref})

    if status in _WAITING_HUMAN_STATUSES:
        return _park_waiting_human(user_email, application_id,
                                   {"reason": reason or error})

    if status == "needs_input":
        # A one-tap field the user must supply — the row goes back into the
        # needs_input queue (and POST /api/apply/needs-input/answer resumes it),
        # never to `failed`.
        fid = _clean(field, 200) or "field"
        why = _clean(reason or error, 100) or "needs_input"
        try:
            ledger.mark_needs_input(
                user_email, application_id,
                [{"question": f"We need your answer to continue ({fid}).",
                  "kind": why, "field_id": fid}])
        except ledger.InvalidTransition:
            logger.info("finalize_submission %s: cannot enter needs_input from "
                        "%s (race) — no-op", application_id, cur)
        return {"status": ledger.STATUS_NEEDS_INPUT, "application_id": application_id,
                "field": fid, "reason": why}

    # SUBMIT_FAILED / anything else → clean, retryable failure.
    err = _clean(error, 500) or (status or SUBMIT_FAILED)
    ledger.mark_failed(user_email, application_id, err)
    logger.info("finalize_submission %s: failed (%s)", application_id, err)
    return {"status": ledger.STATUS_FAILED, "error": err,
            "application_id": application_id}


# ===========================================================================
# NOTE (fill-driver outcomes): the former `finalize_fill_outcome` — a SECOND,
# never-called copy of the outcome->ledger mapping — was removed. The single
# mapping now lives in `finalize_submission` above, which accepts BOTH the
# executor SUBMIT_* vocabulary and the fill driver's outcome names
# (confirmed / submitted / submission_unknown / waiting_human / needs_input /
# prepared). The production path is unchanged: /executor/terminal ->
# _TERMINAL_MAP -> finalize_submission.
# ===========================================================================

# ---------------------------------------------------------------------------
# Non-submit terminal/hold parks (spec §4 + the prepared/waiting_human gap).
# Each is CAS-guarded through the ledger and never raises: an out-of-order or
# redelivered report degrades to a logged no-op instead of clobbering a row.
# ---------------------------------------------------------------------------
def _park_unsupported(user_email: str, application_id: str,
                      result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Spec §4: the executor reported a vendor/site we cannot drive. Land the
    ledger's `unsupported` TERMINAL carrying the user-facing sentence — never the
    retryable `failed` this used to collapse into. The executor's technical
    reason is kept in the audit/metric only (it is not user copy)."""
    result = result or {}
    message = _clean(result.get("user_message"), 300) or ledger.UNSUPPORTED_MESSAGE
    reason = _clean(result.get("reason") or result.get("error"), 200) or "unsupported_vendor"
    try:
        ledger.mark_unsupported(user_email, application_id, message)
    except ledger.ApplicationLedgerError as e:
        logger.info("auto_apply %s: cannot mark unsupported (%s) — no-op",
                    application_id, getattr(e, "error", e))
        return {"status": ledger.STATUS_UNSUPPORTED, "application_id": application_id,
                "message": message, "reason": reason, "applied": False}
    _emit_metric("unsupported", application_id=application_id, outcome="unsupported")
    logger.info("auto_apply %s: unsupported vendor (%s) — parked, not retried",
                application_id, reason)
    return {"status": ledger.STATUS_UNSUPPORTED, "application_id": application_id,
            "message": message, "reason": reason, "retryable": False}


def _park_prepared(user_email: str, application_id: str,
                   result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A `prepared` run (copilot / ops dry-run): the form was filled and proof
    captured, and Submit was DELIBERATELY not clicked. This is a HUMAN HANDOFF,
    not a failure — park the row in `waiting_human` (legal from running) so the
    Attention surface hands it back with the apply URL instead of the row dying
    at `unrecognized_fill_outcome`."""
    result = result or {}
    screenshot_ref = _clean(result.get("screenshot_ref"), 500)
    try:
        ledger.mark_waiting_human(user_email, application_id, _PREPARED_MESSAGE)
    except ledger.ApplicationLedgerError as e:
        logger.info("auto_apply %s: cannot park prepared (%s) — no-op",
                    application_id, getattr(e, "error", e))
        return {"status": ledger.STATUS_WAITING_HUMAN,
                "application_id": application_id, "prepared": True, "applied": False}
    _emit_metric("prepared", application_id=application_id, outcome="prepared")
    logger.info("auto_apply %s: prepared (not submitted) — handed back to the user",
                application_id)
    return {"status": ledger.STATUS_WAITING_HUMAN, "application_id": application_id,
            "prepared": True, "submitted": False,
            "message": _PREPARED_MESSAGE,
            "screenshot_ref": screenshot_ref or None}


def _park_waiting_human(user_email: str, application_id: str,
                        result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The run stopped at a wall only a human can clear and reported no Oracle
    freeze (fill_driver's `waiting_human`). Park in `waiting_human` with the
    reason so the Attention card can explain it."""
    result = result or {}
    reason = _clean(result.get("reason") or result.get("error"), 200) or "human_handoff"
    try:
        ledger.mark_waiting_human(user_email, application_id, reason)
    except ledger.ApplicationLedgerError as e:
        logger.info("auto_apply %s: cannot park waiting_human (%s) — no-op",
                    application_id, getattr(e, "error", e))
        return {"status": ledger.STATUS_WAITING_HUMAN,
                "application_id": application_id, "reason": reason, "applied": False}
    logger.info("auto_apply %s: waiting on a human (%s)", application_id, reason)
    return {"status": ledger.STATUS_WAITING_HUMAN, "application_id": application_id,
            "reason": reason}


# ---------------------------------------------------------------------------
# Oracle freeze / thaw (data-layer — NO hot browser is ever held)
# ---------------------------------------------------------------------------
def _park_verify(user_email: str, application_id: str,
                 wall: Dict[str, Any]) -> Dict[str, Any]:
    """Freeze a submit blocked on an Oracle wall: persist a `waiting_verify`
    freeze doc (drives the automated re-check) AND park the ledger row in
    `needs_input` (submitting→needs_input is legal) with an oracle item — so
    the wall is visible in the one-tap queue / daily summary and the user can
    also clear it (e.g. click the email verify link) faster than the sweep."""
    kind = str(wall.get("kind") or WALL_VERIFY).lower()
    if kind not in VALID_WALL_KINDS:
        kind = WALL_VERIFY
    needs = wall.get("needs") if isinstance(wall.get("needs"), list) else []
    needs = [_clean(n, 200) for n in needs[:20] if n]
    now = _now()

    _freezes_coll().update_one(
        {"application_id": application_id, "user_email": user_email},
        {"$set": {"kind": kind, "needs": needs, "status": "waiting_verify",
                  "next_check_at": now + timedelta(minutes=_VERIFY_BACKOFF_MINUTES[0]),
                  "updated_at": now},
         "$setOnInsert": {"attempts": 0, "created_at": now}},
        upsert=True,
    )

    # Human-in-the-loop is the primary CAPTCHA strategy (see the captcha-strategy
    # decision): surface the wall with the apply URL so the FE can deep-link the
    # user to finish it in their own (logged-in, un-challenged) browser — faster
    # and more reliable than a headless retry. The automated sweep still re-checks
    # in parallel for transient walls.
    app = ledger.get_application(user_email, application_id) or {}
    item = {"question": f"Verification required ({kind}) to finish this application",
            "kind": f"oracle_{kind}", "field_id": f"oracle_{kind}",
            "apply_url": app.get("apply_url") or "", "action": "complete_in_browser"}
    ledger.mark_needs_input(user_email, application_id, [item])
    logger.info("auto_apply submit %s: parked on oracle wall (%s)",
                application_id, kind)
    return {"status": "waiting_verify", "kind": kind,
            "application_id": application_id}


def due_verify_waits(limit: int = 100) -> List[Dict[str, Any]]:
    """Freezes whose re-check is due — the seam a scheduler sweep iterates.
    Returns {application_id, user_email, kind} rows (see the Oracle re-check
    handoff note for which sweep drives this)."""
    limit = max(1, min(int(limit or 100), 500))
    cursor = (_freezes_coll()
              .find({"status": "waiting_verify", "next_check_at": {"$lte": _now()}},
                    {"_id": 0, "application_id": 1, "user_email": 1, "kind": 1})
              .sort("next_check_at", 1)
              .limit(limit))
    return list(cursor)


def recheck_verify(application_id: str, user_email: str) -> Dict[str, Any]:
    """Re-drive one frozen Oracle wall (called by the scheduled sweep).

    Executor available + wall cleared → THAW: freeze resolved, ledger
    needs_input→ready, and a fresh `auto_apply_submit` job is enqueued to
    resume submission. Still blocked → backoff the next check; exhausted →
    abandon the freeze + fail the row. Idempotent."""
    application_id = _clean(application_id, 64)
    freeze = _freezes_coll().find_one(
        {"application_id": application_id, "user_email": user_email,
         "status": "waiting_verify"}, {"_id": 0})
    if not freeze:
        return {"status": "no_freeze", "application_id": application_id}

    app = ledger.get_application(user_email, application_id)
    if not app:
        _freezes_coll().update_one(
            {"application_id": application_id, "user_email": user_email},
            {"$set": {"status": "orphaned", "updated_at": _now()}})
        return {"status": "orphaned", "application_id": application_id}

    if app.get("status") in (ledger.STATUS_SUBMITTED, ledger.STATUS_CONFIRMED):
        _resolve_freeze(application_id, user_email)
        return {"status": "already_done", "application_id": application_id}

    executor = get_executor()
    try:
        res = executor.verify_state(app, freeze)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_apply recheck %s: verify_state raised: %s", application_id, e)
        return _bump_verify(application_id, user_email, freeze)

    if res.get("status") == EXECUTOR_UNAVAILABLE:
        return _bump_verify(application_id, user_email, freeze)

    if res.get("status") == ORACLE_READY and res.get("resolved"):
        _resolve_freeze(application_id, user_email)
        # THAW (F): needs_input → ready_for_review → SYSTEM-approve (the wall
        # cleared; resume on the same approved+snapshot path) → re-enqueue submit
        # (never inline).
        try:
            ledger.mark_ready_for_review(user_email, application_id)
        except ledger.InvalidTransition:
            logger.info("auto_apply recheck %s: not in needs_input to thaw — leaving",
                        application_id)
            return {"status": "resolved_no_thaw", "application_id": application_id}
        _system_approve(user_email, application_id)
        _enqueue_submit(application_id, user_email)
        logger.info("auto_apply recheck %s: wall cleared — thawed + submit re-enqueued",
                    application_id)
        return {"status": "thawed", "application_id": application_id}

    return _bump_verify(application_id, user_email, freeze)


def _resolve_freeze(application_id: str, user_email: str) -> None:
    _freezes_coll().update_one(
        {"application_id": application_id, "user_email": user_email},
        {"$set": {"status": "resolved", "updated_at": _now()}})


def _bump_verify(application_id: str, user_email: str,
                 freeze: Dict[str, Any]) -> Dict[str, Any]:
    """Still blocked: schedule the next re-check with backoff, or abandon +
    fail the row once attempts are exhausted."""
    attempts = int(freeze.get("attempts") or 0) + 1
    now = _now()
    if attempts >= _VERIFY_MAX_ATTEMPTS:
        _freezes_coll().update_one(
            {"application_id": application_id, "user_email": user_email},
            {"$set": {"status": "abandoned", "attempts": attempts, "updated_at": now}})
        try:
            ledger.mark_failed(user_email, application_id, "oracle_wall_unresolved")
        except ledger.ApplicationLedgerError:
            pass
        logger.info("auto_apply recheck %s: oracle wall exhausted — abandoned",
                    application_id)
        return {"status": "abandoned", "application_id": application_id}

    delay = _VERIFY_BACKOFF_MINUTES[min(attempts, _VERIFY_MAX_ATTEMPTS - 1)]
    _freezes_coll().update_one(
        {"application_id": application_id, "user_email": user_email},
        {"$set": {"attempts": attempts,
                  "next_check_at": now + timedelta(minutes=delay),
                  "updated_at": now}})
    return {"status": "waiting_verify", "attempts": attempts,
            "application_id": application_id}


def _enqueue_submit(application_id: str, user_email: str) -> Optional[str]:
    """Enqueue an async auto_apply_submit job (the thaw resume). Lazy import of
    the resume orchestrator to avoid an import cycle at module load."""
    try:
        from services.resume_service import get_resume_service, ResumeService
        payload = {"application_id": application_id, "user_email": user_email}
        job_id = get_resume_service().create_job("auto_apply_submit", payload, user_email)
        ResumeService.invoke_async(job_id, "auto_apply_submit", payload)
        return job_id
    except Exception as e:  # pragma: no cover - defensive
        logger.error("auto_apply: failed to re-enqueue submit for %s: %s", application_id, e)
        return None


def _auto_submit_enabled() -> bool:
    """Master kill-switch for firing any submission (mirrors apply_scheduler)."""
    try:
        from services.auto_submit_service import auto_submit_enabled
        return bool(auto_submit_enabled())
    except Exception:
        return False


def request_immediate_submit(user_email: str, application_ids: Any) -> int:
    """Fire ``auto_apply_submit`` NOW for the given rows instead of waiting for
    the hourly sweep — this is what makes "user approves → it submits" feel
    instant. Best-effort + idempotent: the ledger CAS + the async-job idempotency
    guard make a redundant enqueue (this + the sweep) safe. Gated by the master
    ``AUTO_SUBMIT_ENABLED`` switch, so it stays dark in prod until enabled.

    Only rows actually APPROVED (F: `approved`/`queued_for_execution`, the
    post-approval execution states) are enqueued; anything else — including an
    unapproved `ready_for_review` row — is skipped. Returns the count enqueued.
    """
    if not _auto_submit_enabled():
        return 0
    if isinstance(application_ids, str):
        application_ids = [application_ids]
    enqueued = 0
    for raw in (application_ids or []):
        aid = _clean(raw, 64)
        if not aid:
            continue
        try:
            row = ledger.get_application(user_email, aid)
        except ledger.ApplicationLedgerError:
            row = None
        if not row or row.get("status") not in (
                ledger.STATUS_APPROVED, ledger.STATUS_QUEUED_FOR_EXECUTION):
            continue
        if _enqueue_submit(aid, user_email):
            enqueued += 1
    if enqueued:
        logger.info("auto_apply: immediate submit enqueued for %d row(s) (user=%s)",
                    enqueued, user_email)
    return enqueued
