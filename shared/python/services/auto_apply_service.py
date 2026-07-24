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
from services.executor_adapter import (
    EXECUTOR_UNAVAILABLE,
    ExecutorAdapter,
    ORACLE_READY,
    StubExecutor,
    SUBMIT_FAILED,
    SUBMIT_SUCCESS,
    SUBMIT_WAITING_VERIFY,
    VALID_WALL_KINDS,
    WALL_VERIFY,
)

logger = logging.getLogger(__name__)

# Re-check backoff for frozen Oracle walls (verify/reset/CAPTCHA).
_VERIFY_BACKOFF_MINUTES = (5, 15, 30, 60, 120, 240)
_VERIFY_MAX_ATTEMPTS = len(_VERIFY_BACKOFF_MINUTES)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: Any, max_length: int = 200) -> str:
    return InputSanitizer.sanitize_string(str(value or ""), max_length=max_length)


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


# ---------------------------------------------------------------------------
# Owned collections
# ---------------------------------------------------------------------------
def _answers_coll():
    return DBConnect().get_db().auto_apply_answers


def _freezes_coll():
    return DBConnect().get_db().auto_apply_freezes


# ---------------------------------------------------------------------------
# Canonical profile — the declared source of truth for answers/attestations.
# (Handed to the answer library; also the ONLY permissible source for legal
# attestations — never an LLM, never a heuristic.)
# ---------------------------------------------------------------------------
def _load_profile(user_email: str) -> Dict[str, Any]:
    """Assemble the canonical profile: structured résumé + preferences +
    visa/onboarding declarations. Best-effort per source (a missing source
    just narrows coverage — it never fabricates)."""
    db = DBConnect().get_db()
    profile: Dict[str, Any] = {"user_email": user_email}
    try:
        # Resume Platform v3: canonical read path (users.active_resume_id ->
        # resume_assets) — never a direct user_resumes scan.
        from services import resume_access
        resume = resume_access.get_active_structured(user_email)
        profile["structured"] = (resume or {}).get("structured") or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_apply: structured résumé load failed for %s: %s", user_email, e)
        profile["structured"] = {}
    try:
        profile["preferences"] = db.user_preferences.find_one(
            {"user_email": user_email}, {"_id": 0}) or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_apply: preferences load failed for %s: %s", user_email, e)
        profile["preferences"] = {}
    try:
        # Declared work-authorization / sponsorship / EEO facts — the ONLY
        # source legal attestations may draw from.
        profile["visa_profile"] = db.visa_profiles.find_one(
            {"user_email": user_email}, {"_id": 0}) or {}
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_apply: visa profile load failed for %s: %s", user_email, e)
        profile["visa_profile"] = {}
    return profile


# ---------------------------------------------------------------------------
# Answer resolution (delegates to answer_library_service; defensive shape
# normalization + a hard legal-attestation guard on top).
# ---------------------------------------------------------------------------
def _resolve_answers(user_email: str, questions: List[Dict[str, Any]],
                     profile: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Resolve reconned questions against the canonical profile via the answer
    library. Returns ``(answers, needs_input_items)``:

      * answers          {field_id: value} for every confidently-resolved field.
      * needs_input_items the one-tap queue for everything unresolved (and every
                         UNresolved legal attestation — never guessed).

    Hard integrity guard (independent of the library): a required legal
    attestation is only accepted if the library resolved it FROM DECLARED data;
    otherwise it is forced into needs_input. We never emit an attestation value
    this service invented.
    """
    answers: Dict[str, Any] = {}
    needs_input: List[Dict[str, str]] = []

    resolution = _call_answer_library(user_email, questions, profile)
    resolved_map = resolution.get("resolved") or {}

    for q in questions:
        qid = q.get("id")
        if not qid:
            continue
        is_legal = bool(q.get("is_legal_attestation"))
        required = bool(q.get("required"))
        hit = resolved_map.get(qid)

        if hit is not None and _is_confident(hit):
            # Legal attestations must be sourced from the user's DECLARED data.
            if is_legal and not _is_declared_source(hit):
                needs_input.append(_needs_item(q, kind="legal_attestation"))
                continue
            answers[qid] = _value_of(hit)
            continue

        # Unresolved. Only required fields (and all legal attestations) gate.
        if required or is_legal:
            needs_input.append(_needs_item(
                q, kind="legal_attestation" if is_legal else "screening"))

    return answers, needs_input


def _call_answer_library(user_email: str, questions: List[Dict[str, Any]],
                         profile: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke answer_library_service.resolve(user_email, questions) if present
    (built in parallel), tolerating a couple of return shapes. Absent → an
    empty resolution so the pipeline still runs (everything falls to
    needs_input, nothing is fabricated)."""
    try:
        from services import answer_library_service  # lazy: parallel build
    except ImportError:
        logger.info("auto_apply: answer_library_service not present yet — "
                    "all questions route to needs_input")
        return {"resolved": {}}
    try:
        try:
            result = answer_library_service.resolve(
                user_email, questions, profile=profile)
        except TypeError:
            # Documented interface: resolve(user_email, questions).
            result = answer_library_service.resolve(user_email, questions)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_apply: answer_library resolve failed for %s: %s", user_email, e)
        return {"resolved": {}}
    return _normalize_resolution(result)


def _normalize_resolution(result: Any) -> Dict[str, Any]:
    """Coerce the answer library's return into {resolved: {qid: hit}}."""
    if isinstance(result, dict):
        if isinstance(result.get("resolved"), dict):
            return {"resolved": result["resolved"]}
        # A bare {qid: value|hit} map.
        return {"resolved": result}
    if isinstance(result, list):
        # A list of hit dicts each carrying an id/field_id.
        resolved: Dict[str, Any] = {}
        for item in result:
            if isinstance(item, dict):
                qid = item.get("id") or item.get("field_id")
                if qid:
                    resolved[qid] = item
        return {"resolved": resolved}
    return {"resolved": {}}


# Sources that count as the user's OWN declared data (attestation-safe).
_DECLARED_SOURCES = frozenset({
    "visa_profile", "onboarding", "preferences", "profile", "user_declared",
    "structured", "resume",
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
        "question": _clean(q.get("text"), 500) or _clean(q.get("id"), 200),
        "kind": kind,
        "field_id": _clean(q.get("id"), 200),
    }


def _coverage(questions: List[Dict[str, Any]], answers: Dict[str, Any]) -> float:
    """Fraction of REQUIRED fields resolved from profile + answer library."""
    required = [q for q in questions if q.get("required")]
    if not required:
        return 1.0
    resolved = sum(1 for q in required if q.get("id") in answers)
    return round(resolved / len(required), 4)


# ---------------------------------------------------------------------------
# Autonomy decision (delegates to autonomy_service.score/decide; local
# fallback that faithfully implements §3 when the parallel service is absent).
# ---------------------------------------------------------------------------
def _decide_autonomy(app: Dict[str, Any], questions: List[Dict[str, Any]],
                     answers: Dict[str, Any], coverage: float, *,
                     has_unresolved_legal: bool,
                     needs_account: bool) -> Tuple[str, float]:
    """Return ``(autonomy, confidence_score)``.

    Hard rules (override the score, per §3):
      * any unconfirmed LEGAL ATTESTATION            → 'ask'  (NEVER auto)
      * blocked on account/verify/reset/CAPTCHA      → 'hold' (one-tap)
    """
    no_novel_free_text = not any(
        q.get("type") in ("textarea",) and not q.get("is_legal_attestation")
        and q.get("id") not in answers
        for q in questions
    )
    signals = {
        "ats_family": app.get("ats_family"),
        "answer_coverage": coverage,
        "no_novel_free_text": no_novel_free_text,
        "has_unconfirmed_legal_attestation": has_unresolved_legal,
        "blocked_on_oracle": needs_account,
    }
    try:
        from services import autonomy_service  # lazy: parallel build
        score = float(autonomy_service.score(**signals))
        autonomy = str(autonomy_service.decide(score, **signals))
        if autonomy in ledger._VALID_AUTONOMY:
            return autonomy, max(0.0, min(1.0, score))
        logger.warning("auto_apply: autonomy_service returned invalid decision %r; "
                       "using local fallback", autonomy)
    except ImportError:
        pass
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_apply: autonomy_service failed (%s); local fallback", e)
    return _local_autonomy(signals)


def _local_autonomy(signals: Dict[str, Any]) -> Tuple[str, float]:
    """Faithful local implementation of §3 (used until autonomy_service ships)."""
    ats_ease = {
        "greenhouse": 0.95, "lever": 0.95, "ashby": 0.9,
        "workday": 0.6, "icims": 0.55, "taleo": 0.5,
    }.get(str(signals.get("ats_family") or "").lower(), 0.7)
    coverage = float(signals.get("answer_coverage") or 0.0)
    no_novel = 1.0 if signals.get("no_novel_free_text") else 0.0
    score = round(0.4 * ats_ease + 0.4 * coverage + 0.2 * no_novel, 4)

    # Hard rules.
    if signals.get("has_unconfirmed_legal_attestation"):
        return "ask", score
    if signals.get("blocked_on_oracle"):
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
        questions = [q for q in (recon.get("questions") or []) if isinstance(q, dict)]
        needs_account = bool(recon.get("needs_account"))
        profile = _load_profile(user_email)
        answers, needs_input = _resolve_answers(user_email, questions, profile)
        coverage = _coverage(questions, answers)
        has_unresolved_legal = any(
            i.get("kind") == "legal_attestation" for i in needs_input)
        autonomy, confidence = _decide_autonomy(
            app, questions, answers, coverage,
            has_unresolved_legal=has_unresolved_legal,
            needs_account=needs_account)

        _persist_answers(application_id, user_email, answers, coverage, needs_account)

        # Land the row. needs_input when there are gaps, an Oracle account wall,
        # or the decision isn't a clean auto; ready only when auto + complete.
        gated = bool(needs_input) or needs_account or autonomy != "auto"
        if gated:
            ledger.mark_needs_input(user_email, application_id, needs_input,
                                    autonomy=autonomy, confidence_score=confidence)
            final = ledger.STATUS_NEEDS_INPUT
        else:
            ledger.mark_ready(user_email, application_id, autonomy=autonomy,
                              confidence_score=confidence,
                              tailoring_record_id=app.get("tailoring_record_id"))
            final = ledger.STATUS_READY
            # Clean auto-decision → submit NOW, don't wait for the hourly sweep.
            # Gated by AUTO_SUBMIT_ENABLED; safe/idempotent with the sweep.
            request_immediate_submit(user_email, application_id)
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
    }


def _persist_answers(application_id: str, user_email: str,
                     answers: Dict[str, Any], coverage: float,
                     needs_account: bool) -> None:
    """Store resolved answers in the owned collection (out of the lean ledger
    row; answers may carry PII). Upsert so a re-gather overwrites cleanly."""
    _answers_coll().update_one(
        {"application_id": application_id, "user_email": user_email},
        {"$set": {"answers": answers, "coverage": coverage,
                  "needs_account": needs_account, "updated_at": _now()},
         "$setOnInsert": {"created_at": _now()}},
        upsert=True,
    )


def _load_answers(application_id: str, user_email: str) -> Dict[str, Any]:
    doc = _answers_coll().find_one(
        {"application_id": application_id, "user_email": user_email},
        {"_id": 0, "answers": 1})
    return (doc or {}).get("answers") or {}


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


# ===========================================================================
# PHASE 2 — submit
# ===========================================================================
def submit(application_id: str, user_email: str) -> Dict[str, Any]:
    """Drive submission for a `ready` row. Idempotent + double-submit-safe.

    Flow:
      * only `ready` proceeds (needs_input/gathering/etc. → no-op).
      * executor unavailable  → leave the row `ready` (untouched) + log; the
        future fleet picks it up. (No ledger churn — we never grab `submitting`
        for a fleet that can't act.)
      * CAS ready→submitting  → the double-submit backstop: exactly one Lambda
        wins; a redelivered/racing job sees the guard miss and no-ops.
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
    if status != ledger.STATUS_READY:
        logger.info("auto_apply submit %s: not ready (status=%s) — no-op",
                    application_id, status)
        return {"status": "not_ready", "ledger_status": status,
                "application_id": application_id}

    executor = get_executor()
    # Probe availability BEFORE grabbing the row, so an absent fleet leaves the
    # row `ready` (there is no legal submitting→ready transition to undo with).
    if not executor.is_available():
        logger.info("auto_apply submit %s: executor unavailable — leaving ready",
                    application_id)
        return {"status": EXECUTOR_UNAVAILABLE, "ledger_status": ledger.STATUS_READY,
                "application_id": application_id}

    # Double-submit backstop: CAS ready→submitting. A concurrent/redelivered
    # job that lost the race finds the guard missed and no-ops.
    try:
        ledger.mark_submitting(user_email, application_id)
    except ledger.InvalidTransition:
        logger.info("auto_apply submit %s: lost race to submitting — no-op",
                    application_id)
        return {"idempotent": True, "status": "submitting_elsewhere",
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

    if st == SUBMIT_WAITING_VERIFY:
        wall = result.get("wall") or {}
        return _park_verify(user_email, application_id, wall)

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
        # THAW: needs_input→ready, then re-enqueue submit (never inline).
        try:
            ledger.mark_ready(user_email, application_id)
        except ledger.InvalidTransition:
            logger.info("auto_apply recheck %s: not in needs_input to thaw — leaving",
                        application_id)
            return {"status": "resolved_no_thaw", "application_id": application_id}
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

    Only rows actually in ``ready`` are enqueued (approval flips them to ready
    first); anything else is skipped. Returns the count enqueued.
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
        if not row or row.get("status") != ledger.STATUS_READY:
            continue
        if _enqueue_submit(aid, user_email):
            enqueued += 1
    if enqueued:
        logger.info("auto_apply: immediate submit enqueued for %d row(s) (user=%s)",
                    enqueued, user_email)
    return enqueued
