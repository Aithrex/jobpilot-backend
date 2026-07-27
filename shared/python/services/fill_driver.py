"""Deterministic FILL DRIVER — the piece that actually drives an ATS adapter to
"fill the whole form correctly and submit" (auto-apply capstone).

Given an instantiated ``services.ats_adapters.AtsAdapter`` (bound to a live
Playwright page OR the offline ``tests/ats_dom.FakePage``) and a VETTED answer map
(``{field_id: value}`` — resolved + integrity-guarded upstream by
``answer_library_service`` / ``auto_apply_service``, with any inverse-polarity
already applied), this driver:

  recon (per step) → for each field: option-match selects/radios against the
  form's LIVE options → deterministic fill → advance multi-step (Workday) until
  Review → review snapshot → submit (per MODE) → detect confirmation → receipt.

HARD RULES (never bypassed):
  * NEVER invents a value. A field with no vetted answer is NOT filled.
  * A REQUIRED field that is unresolved, has no confident option match, or is a
    sensitive/legal attestation with no declared value STOPS the run BEFORE any
    submit — ``needs_input`` (a person supplies the value) for a plain field, or
    ``waiting_human`` for a sensitive/legal one — naming the exact field. Never a
    guess.
  * The LLM proposes answers UPSTREAM (answer library / grounded generation); this
    driver only fills + clicks. No model call here.
  * Copilot mode PREPARES only (fills, snapshots) and never submits; approval /
    autopilot submit (the approval gate + presubmit re-check already ran upstream).

The adapter owns the anti-bot / takeover boundary (CAPTCHA / MFA / account gate /
form-drift / operator kill-switch all resolve to ``waiting_human`` inside
``adapter.submit()``); this driver adds the per-field answer-gap gate on top.

The outcome vocabulary maps 1:1 onto the ledger via
``auto_apply_service.finalize_fill_outcome`` (submitted→submitted,
confirmed→confirmed, submission_unknown→HELD, waiting_human→waiting_human,
needs_input→needs_input).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from services.ats_adapters import (
    ADAPTER_SUBMITTED, ADAPTER_SUBMISSION_UNKNOWN, ADAPTER_WAITING_HUMAN,
    ADAPTER_VALIDATION_FAILED,
)
from services.executor_adapter import QTYPE_SELECT, QTYPE_RADIO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Driver outcome vocabulary (maps onto the ledger via auto_apply_service).
# ---------------------------------------------------------------------------
FILL_CONFIRMED = "confirmed"                     # submitted AND confirmation seen
FILL_SUBMITTED = "submitted"                     # submitted, confirmation not (yet) seen
FILL_SUBMISSION_UNKNOWN = "submission_unknown"   # clicked, ambiguous → HELD, no retry
FILL_WAITING_HUMAN = "waiting_human"             # anti-bot / sensitive-gap takeover
FILL_NEEDS_INPUT = "needs_input"                 # a plain field the user must supply
FILL_PREPARED = "prepared"                       # copilot: filled, deliberately not submitted

# Autonomy modes (mirror autonomy_service; kept as plain strings so this module
# needs no import of that service on the hot path).
MODE_COPILOT = "copilot"
MODE_APPROVAL = "approval"
MODE_AUTOPILOT = "autopilot"

_MAX_STEPS = 10          # hard bound on the wizard advance loop (defends selector drift)
_OPTION_WIDGETS = frozenset({QTYPE_SELECT, QTYPE_RADIO})


# ---------------------------------------------------------------------------
# Answer resolution helper (in-process convenience — the worker path passes the
# envelope's pre-resolved answers directly and does NOT call this).
# ---------------------------------------------------------------------------
def flatten_resolved(resolution: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten an ``answer_library_service.resolve`` result's ``resolved`` map
    ({field_id: {value, ...}}) to the flat ``{field_id: value}`` shape the driver
    fills from — the same flattening ``auto_apply_service`` persists into the
    execution envelope. Values already carry any inverse-polarity flip."""
    out: Dict[str, Any] = {}
    for fid, hit in (resolution.get("resolved") or {}).items():
        if isinstance(hit, dict):
            v = hit.get("value", hit.get("answer"))
        else:
            v = hit
        if v is not None:
            out[fid] = v
    return out


def resolve_answers(user_email: str, questions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """In-process: resolve normalized questions to a flat vetted answer map via
    the answer library (declared-data-only for legal attestations, never a
    guess). Used when the driver runs where Mongo is reachable; the stateless
    remote worker instead fills from the envelope's already-resolved answers."""
    from services import answer_library_service as lib
    resolution = lib.resolve(user_email, questions)
    return flatten_resolved(resolution)


# ---------------------------------------------------------------------------
# The driver.
# ---------------------------------------------------------------------------
def _out(outcome: str, *, adapter_family: str, steps: int,
         review_snapshot: Optional[Dict[str, Any]] = None,
         **extra) -> Dict[str, Any]:
    result: Dict[str, Any] = {"outcome": outcome, "adapter": adapter_family,
                              "steps": steps}
    if review_snapshot is not None:
        result["review_snapshot"] = review_snapshot
    result.update({k: v for k, v in extra.items() if v is not None})
    return result


def _is_sensitive(question: Dict[str, Any], field_id: str) -> bool:
    if question.get("is_legal_attestation"):
        return True
    try:
        from services import application_question_bank as qb
        return qb.is_gated(field_id)
    except Exception:  # pragma: no cover - defensive
        return False


def _match_value(adapter, question: Dict[str, Any], field_id: str,
                 value: Any) -> Any:
    """Return the exact form value to fill. For a select/radio, option-match the
    canonical (already-inverted) value against the field's LIVE options; a marker
    (``__NO_MATCH__``) is returned when a REQUIRED option field has no confident
    match, so the caller can stop instead of guessing. Text fields fill raw."""
    if question.get("type") not in _OPTION_WIDGETS:
        return value
    options = adapter.options_for(field_id)
    if not options:
        # Custom widget with no enumerable <option>s (e.g. a Workday listbox
        # button) — the live driver actuates it by handle; fill the raw value.
        return value
    try:
        from services import application_question_bank as qb
        match = qb.match_option(field_id, value, options)
    except Exception:  # pragma: no cover - defensive
        match = None
    if match is None:
        return _NO_MATCH
    return match["option"]


_NO_MATCH = object()


def drive(adapter, answers: Dict[str, Any], *,
          mode: str = MODE_APPROVAL) -> Dict[str, Any]:
    """Fill (+ per mode submit) an ATS form deterministically.

    ``adapter``  an instantiated ``AtsAdapter`` bound to a page.
    ``answers``  the VETTED ``{field_id: value}`` map (never extended here).
    ``mode``     copilot → prepare-only (no submit); approval/autopilot → submit.

    Returns a structured outcome (see the FILL_* vocabulary) carrying the review
    snapshot, receipt, and — on a stop — the exact field/reason that blocked."""
    answers = answers or {}
    family = getattr(adapter, "family", "unknown")
    steps = 0

    for _ in range(_MAX_STEPS):
        steps += 1
        questions = adapter.recon() or []

        for q in questions:
            fid = q.get("field_id")
            if not fid:
                continue
            required = bool(q.get("required"))
            sensitive = _is_sensitive(q, fid)
            has_answer = fid in answers and answers[fid] not in (None, "")

            if not has_answer:
                # No vetted value — NEVER guess. A required gap stops the run and
                # names the field; an optional gap is simply left blank.
                if not required:
                    continue
                if sensitive:
                    return _out(FILL_WAITING_HUMAN, adapter_family=family,
                                steps=steps, field=fid,
                                reason="sensitive_unconfirmed",
                                review_snapshot=adapter.review_snapshot())
                return _out(FILL_NEEDS_INPUT, adapter_family=family, steps=steps,
                            field=fid, reason="unresolved",
                            review_snapshot=adapter.review_snapshot())

            fill_value = _match_value(adapter, q, fid, answers[fid])
            if fill_value is _NO_MATCH:
                # No confident option match for a select/radio.
                if not required:
                    continue
                return _out(FILL_NEEDS_INPUT, adapter_family=family, steps=steps,
                            field=fid, reason="no_option_match",
                            review_snapshot=adapter.review_snapshot())

            res = adapter.fill(fid, fill_value)
            if res.get("found") is False and required:
                # A required handle recon reported but fill could not resolve on
                # the page (renamed/missing hook) — never silently proceed.
                return _out(FILL_NEEDS_INPUT, adapter_family=family, steps=steps,
                            field=fid, reason="field_not_on_page",
                            review_snapshot=adapter.review_snapshot())
            if res.get("status") == "needs_input" and required:
                return _out(FILL_NEEDS_INPUT, adapter_family=family, steps=steps,
                            field=fid, reason=res.get("reason") or "fill_failed",
                            review_snapshot=adapter.review_snapshot())

        # Multi-step wizard: advance to the next step until the terminal Review.
        if adapter.has_next_step() and adapter.current_step() != "review":
            if not adapter.advance_step():
                break
            # Mid-wizard wall (Workday MFA / account overlay after Next).
            check = getattr(adapter, "_check_takeover", None)
            if callable(check):
                try:
                    wall = check()
                except Exception:
                    wall = None
                if wall:
                    return _out(
                        FILL_WAITING_HUMAN,
                        adapter_family=family,
                        steps=steps,
                        reason=wall.get("reason"),
                        takeover=wall,
                        pending_action={"action": wall.get("reason"),
                                        "ats": family},
                        review_snapshot=adapter.review_snapshot(),
                    )
            # If validation blocked advance, surface it instead of spinning.
            # Only treat explicit Workday "Errors Found" banners as blocking —
            # generic role=alert nodes are too noisy mid-wizard.
            try:
                body = ""
                if getattr(adapter, "_page", None) is not None:
                    body = (adapter._page.inner_text("body") or "")[:3000]
                has_banner = "errors found" in body.lower()
                errs = adapter.capture_validation_errors() or [] if has_banner else []
            except Exception:
                errs = []
            if errs:
                return _out(FILL_NEEDS_INPUT, adapter_family=family, steps=steps,
                            reason="validation_failed", errors=errs,
                            field=(errs[0] or {}).get("field"),
                            review_snapshot=adapter.review_snapshot())
            continue
        break

    review_snapshot = adapter.review_snapshot()

    # Copilot PREPARES only — JobPilot fills every field and hands it over; the
    # user submits themselves. Never clicks submit.
    if mode == MODE_COPILOT:
        return _out(FILL_PREPARED, adapter_family=family, steps=steps,
                    review_snapshot=review_snapshot,
                    current_step=adapter.current_step())

    # Refuse to submit while still mid-wizard (Next still present).
    if adapter.has_next_step() and adapter.current_step() != "review":
        # Prefer surfacing an Errors Found banner over a generic stuck reason.
        try:
            body = ""
            if getattr(adapter, "_page", None) is not None:
                body = (adapter._page.inner_text("body") or "")[:4000]
            errs = []
            if "errors found" in body.lower():
                errs = adapter.capture_validation_errors() or []
                if not errs:
                    # Pull the first numbered error line from the banner text.
                    for line in body.splitlines():
                        ls = line.strip()
                        if ls.lower().startswith("error") or (
                                len(ls) > 20 and "required" in ls.lower()):
                            errs.append({"field": None, "message": ls[:240],
                                         "evidence": "errors_found_banner"})
                            break
            if errs:
                return _out(FILL_NEEDS_INPUT, adapter_family=family, steps=steps,
                            reason="validation_failed", errors=errs,
                            field=(errs[0] or {}).get("field") or "validation",
                            review_snapshot=review_snapshot)
        except Exception:
            pass
        return _out(FILL_NEEDS_INPUT, adapter_family=family, steps=steps,
                    reason="stuck_mid_wizard",
                    field=adapter.current_step(),
                    review_snapshot=review_snapshot)

    # approval / autopilot: the approval gate + presubmit re-check already ran
    # upstream (the worker calls /precheck immediately before this). Submit.
    result = adapter.submit()
    return _classify_submit(result, family, steps, review_snapshot)


def _classify_submit(result: Dict[str, Any], family: str, steps: int,
                     review_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    st = (result or {}).get("status")
    if st == ADAPTER_SUBMITTED:
        # A confirmation node/text was detected → positively confirmed.
        return _out(FILL_CONFIRMED, adapter_family=family, steps=steps,
                    review_snapshot=result.get("review_snapshot") or review_snapshot,
                    confirmation=result.get("confirmation"),
                    receipt=result.get("receipt"))
    if st == ADAPTER_SUBMISSION_UNKNOWN:
        # Clicked, no confirmation and no error → HELD, never auto-retried.
        return _out(FILL_SUBMISSION_UNKNOWN, adapter_family=family, steps=steps,
                    review_snapshot=result.get("review_snapshot") or review_snapshot,
                    receipt=result.get("receipt"))
    if st == ADAPTER_WAITING_HUMAN:
        # CAPTCHA / MFA / account gate / form-drift / operator kill-switch.
        takeover = result.get("takeover") or {}
        return _out(FILL_WAITING_HUMAN, adapter_family=family, steps=steps,
                    review_snapshot=result.get("review_snapshot") or review_snapshot,
                    takeover=takeover, reason=takeover.get("reason"),
                    pending_action=result.get("pending_action"))
    if st == ADAPTER_VALIDATION_FAILED:
        # The form blocked submit with visible field errors → one-tap fix queue.
        return _out(FILL_NEEDS_INPUT, adapter_family=family, steps=steps,
                    review_snapshot=result.get("review_snapshot") or review_snapshot,
                    reason="validation_failed", errors=result.get("errors"))
    # An adapter that returned something unexpected — treat as a held unknown
    # rather than a false success (never claim a submit we cannot vouch for).
    logger.warning("fill_driver: unexpected adapter submit status %r", st)
    return _out(FILL_SUBMISSION_UNKNOWN, adapter_family=family, steps=steps,
                review_snapshot=review_snapshot, reason="unexpected_status")
