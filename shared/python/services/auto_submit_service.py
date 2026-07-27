"""Opt-in auto-submit — the guardrailed, per-application approval backend.

The SHIPPED default is the never-submit Review Card: the extension fills a form
and stops. Auto-submit only ever happens when BOTH of these are true:

  1. A master kill-switch `AUTO_SUBMIT_ENABLED` is on (operator flip; OFF in prod
     by default, exactly like `VOICE_GRAPH_ENABLED` / `MAIL_INGRESS_ENABLED` /
     `GMAIL_OAUTH_ENABLED`). When off, every auto-submit route 404s / disables.
  2. The individual user has opted in (`auto_submit_settings.auto_submit_enabled`,
     default False). This is a separate, per-user consent from the master flag.

Even then, a submit is never implicit. Every submit is backed by a single-use,
short-lived **approval record** the user (or an approved WhatsApp reply) created
for that exact application:

    { approval_id, user_email, application_id, answers_snapshot,
      status: pending|approved|submitted|cancelled|expired,
      source: in_app|whatsapp,
      created_at, approved_at, undo_until, expires_at,
      cancelled_at, submitted_at }

State machine (each transition is written to an audit log, PII-redacted):

    (create) --> approved --(undo elapses)--> eligible --(extension clicks)--> submitted
                    |                                   \
                    | cancel (while approved)            (expiry passes) --> expired
                    v
                 cancelled

  * UNDO WINDOW (`undo_until` = approved_at + UNDO_WINDOW_SECONDS): after approval
    the record is `approved` but NOT yet eligible to submit. A cancel during this
    buffer flips it to `cancelled`. Only once `undo_until` has passed (and before
    `expires_at`) is the record eligible for the extension to submit.
  * EXPIRY (`expires_at`): an approval is single-use and short-lived; a stale one
    can never be submitted.
  * IDEMPOTENCY: re-approving an application with a live approval is a no-op
    (returns the existing record); double-submitting is a 409. Every mutating
    transition is an atomic, status-guarded `find_one_and_update`, so two racing
    Lambdas can never both submit or both cancel the same record.

The extension NEVER submits without first seeing an eligible `approved` record
(past undo, unexpired) for that exact application via GET /api/apply/auto-submit/
pending, then reporting the click via POST .../mark-submitted.

Collections (see scripts/migrate_indexes.py for the indexes this needs):
  * auto_submit_settings   — one doc per user (the opt-in toggle)
  * auto_submit_approvals  — the approval records above
  * auto_submit_audit      — append-only transition log (redacted of answer text)

Nothing here calls an LLM; the per-plan `auto_submit` metric (entitlements_service)
is a daily abuse/safety cap on how many auto-submits a user may queue, enforced by
the route BEFORE an approval is created.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from utils.config import _get_config_value
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Feature flag — master kill-switch (mirrors the other flag readers exactly)
# ---------------------------------------------------------------------------
_FLAG_TRUE = {"1", "true", "yes", "on"}
_FLAG_FALSE = {"0", "false", "no", "off"}


def auto_submit_enabled() -> bool:
    """`AUTO_SUBMIT_ENABLED` master kill-switch for the opt-in auto-submit feature.

    Unset → ON in dev, OFF in prod — identical resolution to
    `voice_graph_enabled()` / `mail_ingress_enabled()` / `gmail_oauth_enabled()`.
    When off, every auto-submit endpoint 404s and no approval can be created, so
    the feature is inert until an operator flips it in the jobs Lambda env/SSM.
    """
    raw = (_get_config_value("AUTO_SUBMIT_ENABLED", "") or "").strip().lower()
    if raw in _FLAG_TRUE:
        return True
    if raw in _FLAG_FALSE:
        return False
    return os.getenv("ENVIRONMENT", "development").strip().lower() == "development"


# ---------------------------------------------------------------------------
# Timing knobs
# ---------------------------------------------------------------------------
UNDO_WINDOW_SECONDS = 60        # cancel-buffer before an approval becomes eligible
APPROVAL_TTL_SECONDS = 15 * 60  # single-use approvals expire 15m after approval

_VALID_SOURCES = ("in_app", "whatsapp")
_ACTIVE_STATUSES = ("pending", "approved")  # states that block a duplicate approval

# Cap the reviewed-answers snapshot so a malformed/oversized client payload can't
# bloat Mongo (same spirit as apply.py::_MAX_VAULT_BYTES).
_MAX_ANSWERS_BYTES = 128 * 1024


# ---------------------------------------------------------------------------
# Typed errors — the route maps each to its HTTP status. Never 401 (that is
# reserved for missing/invalid JWT); auth/business failures are 400/403/404/409.
# ---------------------------------------------------------------------------
class AutoSubmitError(Exception):
    status = 400
    error = "auto_submit_error"

    def __init__(self, message: Optional[str] = None):
        self.message = message or self.error
        super().__init__(self.message)


class AutoSubmitDisabled(AutoSubmitError):
    status = 404
    error = "not_found"


class OptInRequired(AutoSubmitError):
    status = 403
    error = "auto_submit_opt_in_required"


class InvalidAnswers(AutoSubmitError):
    status = 400
    error = "invalid_answers"


class ApprovalNotFound(AutoSubmitError):
    status = 404
    error = "approval_not_found"


class NotEligible(AutoSubmitError):
    """Approval exists but cannot be submitted/cancelled right now (undo window
    still active, or already expired)."""
    status = 409
    error = "not_eligible"


class AlreadyFinal(AutoSubmitError):
    """Approval is already in a terminal state (submitted/cancelled/expired) — a
    second transition is a no-op conflict, not a new action."""
    status = 409
    error = "already_final"


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------
def _settings():
    return DBConnect().get_db().auto_submit_settings


def _approvals():
    return DBConnect().get_db().auto_submit_approvals


def _audit_coll():
    return DBConnect().get_db().auto_submit_audit


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """mongomock/pymongo may hand back naive UTC datetimes — normalize so the
    comparisons below never raise on offset-naive vs offset-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _as_aware(dt)
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Audit log (append-only, PII-redacted)
# ---------------------------------------------------------------------------
def _answers_fingerprint(answers: Any) -> str:
    """A stable, content-derived fingerprint so the audit can prove WHICH answer
    set was approved/submitted WITHOUT storing the answer text itself."""
    try:
        blob = json.dumps(answers, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        blob = str(answers)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _answers_count(answers: Any) -> int:
    if isinstance(answers, (list, dict)):
        return len(answers)
    return 0


def _audit(user_email: str, action: str, *, approval_id: Optional[str] = None,
           application_id: Optional[str] = None, source: Optional[str] = None,
           meta: Optional[Dict[str, Any]] = None) -> None:
    """Record one state transition. `meta` must carry only redacted, non-PII
    signal (counts / fingerprints) — never answer text. Soft-fail: an audit
    write must never break the user-facing action, but we log loudly if it does.
    """
    try:
        _audit_coll().insert_one({
            "user_email": user_email,      # the actor (necessary for who/what/when)
            "action": action,             # settings_enabled|settings_disabled|approved|
                                          # cancelled|submitted|expired
            "approval_id": approval_id,
            "application_id": application_id,
            "source": source,
            "at": _now(),
            "meta": meta or {},           # redacted only
        })
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_submit audit write failed (%s): %s", action, e)


# ---------------------------------------------------------------------------
# Public record view (safe to return to the owning user over an authed channel)
# ---------------------------------------------------------------------------
def _view(rec: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or _now()
    undo_until = _as_aware(rec.get("undo_until"))
    expires_at = _as_aware(rec.get("expires_at"))
    eligible = (
        rec.get("status") == "approved"
        and undo_until is not None and now >= undo_until
        and expires_at is not None and now < expires_at
    )
    return {
        "approval_id": rec.get("approval_id"),
        "application_id": rec.get("application_id"),
        "url": rec.get("url"),
        "application_url": rec.get("url"),
        "status": rec.get("status"),
        "source": rec.get("source"),
        "answers_snapshot": rec.get("answers_snapshot"),
        "created_at": _iso(rec.get("created_at")),
        "approved_at": _iso(rec.get("approved_at")),
        "undo_until": _iso(rec.get("undo_until")),
        "expires_at": _iso(rec.get("expires_at")),
        "cancelled_at": _iso(rec.get("cancelled_at")),
        "submitted_at": _iso(rec.get("submitted_at")),
        "eligible": eligible,
    }


# ---------------------------------------------------------------------------
# Per-user opt-in settings
# ---------------------------------------------------------------------------
def get_settings(user_email: str) -> Dict[str, Any]:
    """Read the user's opt-in. Default False (never auto-submit).

    ``use_tailored_resume`` defaults True — auto-apply pins a tailored artifact
    when prepare is enabled. Users may opt into plain-resume submits.
    """
    doc = None
    try:
        doc = _settings().find_one({"user_email": user_email}, {"_id": 0})
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto_submit get_settings failed for %s: %s", user_email, e)
    enabled = bool(doc and doc.get("auto_submit_enabled"))
    updated_at = _iso(doc.get("updated_at")) if doc else None
    # Default ON: safer public default is tailor-then-apply. Explicit False skips.
    use_tailored = True if not doc or "use_tailored_resume" not in doc else bool(
        doc.get("use_tailored_resume")
    )
    return {
        "auto_submit_enabled": enabled,
        "use_tailored_resume": use_tailored,
        "updated_at": updated_at,
    }


def set_settings(user_email: str, enabled: bool,
                 use_tailored_resume: Optional[bool] = None) -> Dict[str, Any]:
    """Set the user's opt-in (and optional tailor preference) and audit."""
    enabled = bool(enabled)
    fields: Dict[str, Any] = {
        "user_email": user_email,
        "auto_submit_enabled": enabled,
        "updated_at": _now(),
    }
    if use_tailored_resume is not None:
        fields["use_tailored_resume"] = bool(use_tailored_resume)
    _settings().update_one(
        {"user_email": user_email},
        {"$set": fields},
        upsert=True,
    )
    _audit(user_email, "settings_enabled" if enabled else "settings_disabled")
    return get_settings(user_email)


def use_tailored_resume(user_email: str) -> bool:
    """True when auto-apply should pin a tailored résumé before submit."""
    return bool(get_settings(user_email).get("use_tailored_resume", True))


def user_opted_in(user_email: str) -> bool:
    return bool(get_settings(user_email).get("auto_submit_enabled"))


# ---------------------------------------------------------------------------
# Approval lifecycle
# ---------------------------------------------------------------------------
def _require_enabled() -> None:
    if not auto_submit_enabled():
        raise AutoSubmitDisabled()


def _validate_answers(answers: Any) -> Any:
    """Answers must be a JSON object/array (the reviewed field->value set the
    extension will submit) and within the size cap."""
    if not isinstance(answers, (list, dict)):
        raise InvalidAnswers("answers must be an object or array of reviewed values")
    try:
        if len(json.dumps(answers, default=str)) > _MAX_ANSWERS_BYTES:
            raise InvalidAnswers("answers payload too large")
    except (TypeError, ValueError):
        raise InvalidAnswers("answers is not serializable")
    return answers


def get_active_approval(user_email: str, application_id: str) -> Optional[Dict[str, Any]]:
    """The live (pending/approved, unexpired) approval for this exact application,
    if any — scoped to the caller so cross-user records are invisible. Lazily
    expires a stale `approved` record instead of returning it."""
    rec = _approvals().find_one({
        "user_email": user_email,
        "application_id": application_id,
        "status": {"$in": list(_ACTIVE_STATUSES)},
    })
    if not rec:
        return None
    expires_at = _as_aware(rec.get("expires_at"))
    if expires_at is not None and _now() >= expires_at:
        _expire(rec)
        return None
    return _view(rec)


def create_approval(user_email: str, application_id: str, answers: Any,
                    source: str = "in_app",
                    url: Optional[str] = None,
                    undo_seconds: Optional[int] = None,
                    ttl_seconds: Optional[int] = None) -> Tuple[Dict[str, Any], bool]:
    """Create (or return the existing live) approval for `application_id`.

    Requires the master flag AND the user's opt-in — both, always. Returns
    ``(record_view, created)``; ``created`` is False when a live approval already
    existed (idempotent no-op so the client can safely retry).
    """
    _require_enabled()
    if source not in _VALID_SOURCES:
        raise InvalidAnswers(f"invalid source: {source}")
    if not application_id:
        raise InvalidAnswers("application_id is required")
    if not user_opted_in(user_email):
        raise OptInRequired("Enable auto-submit in settings before approving an application.")
    _validate_answers(answers)

    existing = get_active_approval(user_email, application_id)
    if existing is not None:
        # Idempotent: a live approval already covers this application.
        return existing, False

    now = _now()
    undo = UNDO_WINDOW_SECONDS if undo_seconds is None else int(undo_seconds)
    ttl = APPROVAL_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    rec = {
        "approval_id": str(uuid.uuid4()),
        "user_email": user_email,
        "application_id": application_id,
        "url": (url or None),
        "answers_snapshot": answers,
        "status": "approved",
        "source": source,
        "created_at": now,
        "approved_at": now,
        "undo_until": now + timedelta(seconds=undo),
        "expires_at": now + timedelta(seconds=ttl),
        "cancelled_at": None,
        "submitted_at": None,
    }
    _approvals().insert_one(rec)
    _audit(user_email, "approved", approval_id=rec["approval_id"],
           application_id=application_id, source=source,
           meta={"answers_count": _answers_count(answers),
                 "answers_fingerprint": _answers_fingerprint(answers),
                 "undo_seconds": undo, "ttl_seconds": ttl})
    return _view(rec, now=now), True


def create_whatsapp_approval(user_email: str, application_id: str,
                             answers: Any) -> Dict[str, Any]:
    """Internal hook: create an `approved` record whose source is WhatsApp.

    This is the plumbing a (not-yet-built) WhatsApp inbound handler calls once it
    has verified an approving reply from the user. It carries the SAME guardrails
    as the in-app path — master flag + per-user opt-in + undo window + single-use
    expiry — so a WhatsApp "YES" is exactly as safe as an in-app approval.

    The Twilio/WhatsApp Cloud API send+receive itself is a documented HUMAN GATE
    (see MANUAL_STEPS.md) and the inbound webhook is a new top-level prefix that
    needs an external Terraform route — neither is created here.
    """
    rec, _created = create_approval(user_email, application_id, answers,
                                    source="whatsapp")
    return rec


def cancel_approval(user_email: str, application_id: str) -> Dict[str, Any]:
    """Cancel the live approval for this application (the undo action).

    Succeeds while the record is still `approved` (not yet submitted) — the undo
    window is the guaranteed pre-eligibility buffer. Atomic + status-guarded so a
    cancel racing a submit resolves to exactly one winner.
    """
    _require_enabled()
    rec = _approvals().find_one({
        "user_email": user_email,
        "application_id": application_id,
        "status": "approved",
    })
    if not rec:
        # Distinguish "already terminal" from "never existed" for a clear 409/404.
        terminal = _approvals().find_one({
            "user_email": user_email,
            "application_id": application_id,
            "status": {"$in": ["submitted", "cancelled", "expired"]},
        })
        if terminal:
            raise AlreadyFinal(f"approval already {terminal.get('status')}")
        raise ApprovalNotFound()

    now = _now()
    if _as_aware(rec.get("expires_at")) is not None and now >= _as_aware(rec["expires_at"]):
        _expire(rec)
        raise NotEligible("approval expired")

    updated = _atomic_transition(rec["approval_id"], guard=("approved",),
                                 set_fields={"status": "cancelled", "cancelled_at": now})
    if not updated:
        # Lost the race — someone submitted/cancelled first.
        raise AlreadyFinal("approval is no longer cancellable")
    _audit(user_email, "cancelled", approval_id=rec["approval_id"],
           application_id=application_id, source=rec.get("source"))
    return _view(updated, now=now)


def mark_submitted(user_email: str, application_id: str) -> Dict[str, Any]:
    """Transition an eligible `approved` record to `submitted`.

    Called by the extension AFTER it clicked submit. Eligible means: status is
    `approved`, the undo window has elapsed, and the approval has not expired.
    Single-use + atomic: a double-submit is a 409 (the second caller finds the
    record already submitted), never a second real submission.
    """
    _require_enabled()
    rec = _approvals().find_one({
        "user_email": user_email,
        "application_id": application_id,
        "status": "approved",
    })
    if not rec:
        terminal = _approvals().find_one({
            "user_email": user_email,
            "application_id": application_id,
            "status": {"$in": ["submitted", "cancelled", "expired"]},
        })
        if terminal:
            raise AlreadyFinal(f"approval already {terminal.get('status')}")
        raise ApprovalNotFound()

    now = _now()
    undo_until = _as_aware(rec.get("undo_until"))
    expires_at = _as_aware(rec.get("expires_at"))
    if expires_at is not None and now >= expires_at:
        _expire(rec)
        raise NotEligible("approval expired")
    if undo_until is not None and now < undo_until:
        raise NotEligible("undo window still active; not yet eligible to submit")

    updated = _atomic_transition(rec["approval_id"], guard=("approved",),
                                 set_fields={"status": "submitted", "submitted_at": now})
    if not updated:
        raise AlreadyFinal("approval already submitted")
    _audit(user_email, "submitted", approval_id=rec["approval_id"],
           application_id=application_id, source=rec.get("source"),
           meta={"answers_fingerprint": _answers_fingerprint(rec.get("answers_snapshot"))})
    return _view(updated, now=now)


def list_pending_eligible(user_email: str) -> Dict[str, Any]:
    """The queue the extension/app polls.

    Returns `pending`: `approved` records that are eligible to submit NOW (undo
    elapsed, unexpired) — these are the ONLY records the extension may act on.
    Also returns `waiting`: `approved` records still inside their undo window (for
    UI countdowns). Stale records are lazily expired as they are scanned.
    """
    now = _now()
    eligible: List[Dict[str, Any]] = []
    waiting: List[Dict[str, Any]] = []
    cursor = _approvals().find({"user_email": user_email, "status": "approved"})
    for rec in cursor:
        expires_at = _as_aware(rec.get("expires_at"))
        undo_until = _as_aware(rec.get("undo_until"))
        if expires_at is not None and now >= expires_at:
            _expire(rec)
            continue
        if undo_until is not None and now >= undo_until:
            eligible.append(_view(rec, now=now))
        else:
            waiting.append(_view(rec, now=now))
    return {"pending": eligible, "waiting": waiting}


# ---------------------------------------------------------------------------
# E2 (defect E) — fold `auto_submit_approvals` INTO the applications ledger.
#
# A THIN ADAPTER over the ledger's F approval states (ready_for_review / approved
# / queued_for_execution) + the frozen approval_snapshot. New approvals go through
# `ledger.approve()`; the pending queue reads canonical ledger rows LABELED with
# their `status` + an `already_approved` flag (so defect H's FE renders an
# already-approved row read-only instead of re-approvable). Legacy
# `auto_submit_approvals` docs are STILL read (via list_pending_eligible) and are
# NOT deleted during the transition. Dark-launch gated (AUTO_APPLY_APPROVAL_FOLD,
# default OFF everywhere — dark until an operator flips it), mirroring the
# AUTO_APPLY_RESERVE / AUTO_APPLY_PREPARE / AUTO_APPLY_MODES posture, so the live
# queue is byte-identical until the flag is on.
# ---------------------------------------------------------------------------
# ready_for_review still AWAITS a go-ahead (already_approved False); approved /
# queued_for_execution are cleared to submit + snapshot-frozen (F), surfaced
# read-only (already_approved True). Attention statuses hand the row BACK to the
# human (never approvable) — without them the Auto-Apply tab looks empty while
# gather has correctly parked rows in needs_input / waiting_human / etc.
_FOLD_AWAITING: Tuple[str, ...] = ("ready_for_review",)
_FOLD_APPROVED: Tuple[str, ...] = ("approved", "queued_for_execution")
_FOLD_ATTENTION: Tuple[str, ...] = (
    "needs_input", "waiting_human", "submission_unknown",
)
_ACCOUNT_GATE_PROMPT = (
    "Sign in or create an account on the employer's career site to continue."
)


def approval_fold_enabled() -> bool:
    """Dark-launch gate for E2's approval fold. Default OFF in ALL environments
    (unlike the master `auto_submit_enabled`, which is dev-ON) so the pending
    queue stays byte-identical until an operator flips AUTO_APPLY_APPROVAL_FOLD."""
    raw = (_get_config_value("AUTO_APPLY_APPROVAL_FOLD", "") or "").strip().lower()
    if raw in _FLAG_TRUE:
        return True
    if raw in _FLAG_FALSE:
        return False
    return False


def _fold_needs_input(user_email: str, row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Ledger needs_input list, plus a synthetic account-gate prompt when gather
    parked the row on ``needs_account`` with an empty question list (common on
    Workday login walls) so the FE Attention card has something to show."""
    items = [i for i in (row.get("needs_input") or []) if isinstance(i, dict)]
    if items or row.get("status") != "needs_input":
        return items
    aid = row.get("application_id")
    if not aid:
        return items
    try:
        from services.auto_apply_service import _answers_coll
        ans = _answers_coll().find_one(
            {"application_id": aid, "user_email": user_email},
            {"_id": 0, "needs_account": 1},
        ) or {}
    except Exception:  # pragma: no cover - best-effort enrichment
        return items
    if ans.get("needs_account"):
        return [{
            "question": _ACCOUNT_GATE_PROMPT,
            "kind": "account",
            "field_id": "ats_account",
        }]
    return items


def list_canonical_approvals(user_email: str) -> List[Dict[str, Any]]:
    """E2 thin ADAPTER: the auto-apply LEDGER rows that belong in the approval
    queue, in the extension's pending-record shape, LABELED with the canonical
    execution `status` + an `already_approved` flag.

      * ready_for_review          -> already_approved False, eligible False
                                     (awaits the user's go-ahead — H renders it
                                      as an actionable review card).
      * approved / queued_for_exec -> already_approved True, eligible True
                                     (cleared to submit, snapshot frozen — H
                                      renders it read-only, no second approve).
      * needs_input / waiting_human / submission_unknown
                                  -> already_approved False, eligible False
                                     (attention holds — H's FE Attention section).

    Dark-launch gated: returns [] until AUTO_APPLY_APPROVAL_FOLD is on, so the
    live queue is unchanged. Owner-scoped; best-effort (never raises past here).
    """
    if not approval_fold_enabled():
        return []
    out: List[Dict[str, Any]] = []
    try:
        from services import application_ledger_service as als
        for status in _FOLD_AWAITING + _FOLD_APPROVED + _FOLD_ATTENTION:
            already = status in _FOLD_APPROVED
            for r in als.list_applications(user_email, status=status, limit=100):
                url = r.get("apply_url")
                payload = {
                    "application_id": r.get("application_id"),
                    "apply_url": url, "url": url,
                    "status": status,
                    "already_approved": already,
                    # only an approved/queued row is cleared for the extension.
                    "eligible": already,
                    "ats": r.get("ats_family"),
                    "title": r.get("title"), "company": r.get("company"),
                    "source": "ledger",
                }
                if status in _FOLD_ATTENTION:
                    payload["needs_input"] = _fold_needs_input(user_email, r)
                    reason = r.get("error") or r.get("reason")
                    if reason:
                        payload["reason"] = reason
                out.append(payload)
    except Exception as e:  # pragma: no cover - fold is best-effort
        logger.warning("auto_submit canonical fold failed for %s: %s", user_email, e)
    return out


def approve_ledger(user_email: str, application_ids: Any, *,
                   approved_by: str = "user", snapshot_provider: Any = None) -> int:
    """E2 thin ADAPTER: route a NEW approval through the ledger's F approval edge
    (`ready_for_review -> approved`, snapshot frozen) instead of minting a legacy
    `auto_submit_approvals` doc. Idempotent + single-writer (see
    application_ledger_service.approve): a row not yet in ready_for_review is left
    untouched, and a replay never double-approves. Returns the count moved."""
    from services import application_ledger_service as als
    return als.approve(user_email, application_ids, approved_by=approved_by,
                       snapshot_provider=snapshot_provider)


# ---------------------------------------------------------------------------
# Internal transition helpers
# ---------------------------------------------------------------------------
def _atomic_transition(approval_id: str, guard: Tuple[str, ...],
                       set_fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compare-and-set a status transition: only mutate if the record is still in
    one of `guard` states, and return the post-update doc (None if the guard did
    not match, i.e. another caller finalized it first). This is the single-writer
    guarantee that makes cancel/submit/expire safe under concurrent Lambdas."""
    from pymongo import ReturnDocument
    return _approvals().find_one_and_update(
        {"approval_id": approval_id, "status": {"$in": list(guard)}},
        {"$set": set_fields},
        return_document=ReturnDocument.AFTER,
    )


def _expire(rec: Dict[str, Any]) -> None:
    """Atomically flip a stale `approved`/`pending` record to `expired` + audit.
    Guarded so it never clobbers a record another caller already finalized."""
    now = _now()
    updated = _atomic_transition(rec["approval_id"], guard=_ACTIVE_STATUSES,
                                 set_fields={"status": "expired", "expired_at": now})
    if updated:
        _audit(rec.get("user_email"), "expired", approval_id=rec.get("approval_id"),
               application_id=rec.get("application_id"), source=rec.get("source"))
