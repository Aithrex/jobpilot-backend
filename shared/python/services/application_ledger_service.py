"""Applications ledger — the canonical per-user record of every job we apply to.

Auto-Apply Tranche 1 §1 (the correctness spine). One `applications` document
per (user_email, job_identity); distinct from `tailoring_records` (résumé
data) and `saved_jobs` (discovery). The ledger exists so a duplicate
application is STRUCTURALLY impossible — today or any future day:

  1. `normalized_job_identity(company, title, jd_text)` collapses the same
     posting seen via LinkedIn / Workday / Greenhouse / a company page into one
     stable sha256 key (job_ids across sources are NOT stable; company+title
     is primary and a jd_hash prefix disambiguates distinct reqs at the same
     company).
  2. `dedup_gate(user_email, job_identity)` — the fast pre-enqueue check: skip
     when a ledger row already exists in any live/handled status; only
     `failed` / `skipped_duplicate` rows may be retried.
  3. The unique index on (user_email, job_identity) — see
     scripts/migrate_indexes.py — is the race-proof backstop: two concurrent
     Lambdas enqueueing the same posting resolve to exactly one row; the
     loser's insert-conflict becomes a `skipped_duplicate` no-op.

Document shape (schema-on-read; every field defaulted defensively in _view):

    { application_id: uuid,               # PK
      user_email: str,                    # ownership scope (ALWAYS in every filter)
      job_identity: str,                  # normalized dedup key (sha256 hex)
      source: "linkedin"|"workday"|"greenhouse"|"lever"|"ashby"|...,
      ats_family: str|null, tenant: str|null,
      company: str, title: str,
      jd_hash: str,                       # sha256(normalized jd_text)
      apply_url: str,
      posting_closes_at: datetime|null,   # stop nudging after this
      tailoring_record_id: str|null,      # tailored résumé variant used
      autonomy: "auto"|"hold"|"ask",      # decided by Tranche 1 §3
      confidence_score: float,            # 0..1
      status: enum (machine below),
      needs_input: [ {question, kind, field_id} ],   # one-tap review queue (bounded)
      error: str|null,
      created_at, updated_at, submitted_at, confirmed_at,
      audit_ref: str }                    # → auto_submit_audit entries keyed by
                                          #   application_id (index application_id_at)

State machine (every transition is an atomic, status-guarded
find_one_and_update — the same compare-and-set idiom as
auto_submit_service._atomic_transition — so two racing Lambdas can never both
submit, both fail, or both requeue the same row; each transition is audited):

    queued → gathering → needs_input → ready → submitting → submitted → confirmed
                 ↘ (all answers known, auto) ────↗
       any live state → failed          needs_input ← (resume after one-tap)
       insert-conflict → skipped_duplicate
       failed / skipped_duplicate → queued   (explicit retry only)

Collections (see scripts/migrate_indexes.py for the indexes this needs):
  * applications      — the ledger rows above
  * auto_submit_audit — append-only transition log (shared with auto-submit;
                        already indexed on (user_email, at) / (application_id, at))

Nothing here calls an LLM. Entitlement/quota gating happens in the route
BEFORE a ledger row is created (same layering as auto_submit_service).
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status machine
# ---------------------------------------------------------------------------
STATUS_QUEUED = "queued"
STATUS_GATHERING = "gathering"
STATUS_NEEDS_INPUT = "needs_input"
STATUS_READY = "ready"
STATUS_SUBMITTING = "submitting"
STATUS_SUBMITTED = "submitted"
STATUS_CONFIRMED = "confirmed"
STATUS_FAILED = "failed"
STATUS_SKIPPED_DUPLICATE = "skipped_duplicate"
# User-initiated "stop nudging" terminal state (via the §2 email `skip` action).
# Distinct from skipped_duplicate (a dedup no-op): the user explicitly declined
# this posting, so dedup_gate must treat it as ALREADY-HANDLED and NOT re-offer
# it. It is NOT retryable — a skip is a deliberate, sticky decision.
STATUS_SKIPPED = "skipped"

# ---------------------------------------------------------------------------
# Canonical state machine (defect E, STEP E0 — ADDITIVE ONLY).
#
# The target machine the whole applications pipeline is converging on. E0
# introduces these names + a read-side `canonical_status()` normalizer and
# nothing else: no writer or reader is migrated here (that is E1/E3), approval
# semantics are untouched (that is F). Every legacy state above stays fully
# valid; these are added alongside them so the cutover can happen reader-by-
# reader with the normalizer as the shim.
#
#   draft → queued → preparing → needs_input → ready_for_review → approved →
#   queued_for_execution → running → (waiting_otp | waiting_human) →
#   submitted → confirmed
#   terminals: cancelled, failed_pre_submit(=failed), unsupported, withdrawn,
#              submission_unknown
# ---------------------------------------------------------------------------
STATUS_DRAFT = "draft"
STATUS_PREPARING = "preparing"                    # canonical peer of `gathering`
STATUS_READY_FOR_REVIEW = "ready_for_review"      # the `ready` split awaiting go-ahead
STATUS_APPROVED = "approved"                       # the `ready` split, approved (F)
STATUS_QUEUED_FOR_EXECUTION = "queued_for_execution"  # handed to the executor sweep
STATUS_RUNNING = "running"                         # canonical peer of `submitting`
STATUS_WAITING_OTP = "waiting_otp"                 # an OTP wall mid-run
STATUS_WAITING_HUMAN = "waiting_human"             # a human-handoff wall mid-run
STATUS_SUBMISSION_UNKNOWN = "submission_unknown"   # submit attempted, outcome unknown
STATUS_CANCELLED = "cancelled"                     # pre-submit user/system abort
STATUS_UNSUPPORTED = "unsupported"                 # ATS/site not supportable
# The user-facing sentence a row carries when it lands in `unsupported` (spec §4).
# Stored on the row (in `error`) so every reader — the fold payload, the daily
# email, the FE card — renders the SAME honest message instead of an error code.
UNSUPPORTED_MESSAGE = ("We can't auto-apply to this employer's system yet — "
                       "please apply manually.")
STATUS_WITHDRAWN = "withdrawn"                      # post-submit user withdrawal
# Canonical peer of `failed` — a canonical-only label (an OUTPUT of
# canonical_status). NOT a valid ledger status and NOT a transition target: the
# ledger still stores `failed`; this name only exists in canonical read-space.
STATUS_FAILED_PRE_SUBMIT = "failed_pre_submit"

# E1 (defect E): a create-time EXECUTION-axis terminal meaning "submitted
# OUTSIDE our executor" — a manual/mail/gmail signal that owns an OUTCOME (the
# employer-response funnel) but that JobPilot never drove to submission itself.
# ADDITIVE: it joins ALL_STATUSES (so list_applications accepts it as a filter)
# but is deliberately kept OUT of ACTIVE_STATUSES/RETRYABLE_STATUSES — dedup_gate
# must not treat an external row as "already handled" for the auto-apply queue,
# and retry must not re-queue it. It is NOT a _TRANSITION_GUARDS target either:
# no edge leads into or out of it on the execution axis. A lightweight row is
# BORN here (via set_outcome) and its whole life happens on the outcome axis.
STATUS_EXTERNAL = "external"

# Statuses that mean "this posting is already handled" — a dedup_gate hit on
# any of these SKIPS the enqueue (Tranche 1 §1 rule). Includes both terminal-good
# (confirmed) and terminal user-declined (skipped) so neither is re-offered.
# NOTE: STATUS_READY is the submit-sweep pickup state (Tranche 2 §6 auto_apply_submit
# scans for `ready` rows); the §2 email `approve` action moves rows here.
ACTIVE_STATUSES: Tuple[str, ...] = (
    STATUS_QUEUED, STATUS_GATHERING, STATUS_NEEDS_INPUT, STATUS_READY,
    STATUS_SUBMITTING, STATUS_SUBMITTED, STATUS_CONFIRMED, STATUS_SKIPPED,
)
# Only these may be retried (re-queued) — everything else is a skip.
RETRYABLE_STATUSES: Tuple[str, ...] = (STATUS_FAILED, STATUS_SKIPPED_DUPLICATE)

# E0 (defect E): the canonical states added ADDITIVELY to the valid-status set.
# Deliberately NOT folded into ACTIVE_STATUSES/RETRYABLE_STATUSES — dedup_gate
# and retry semantics read those two and must stay byte-for-byte unchanged in
# E0 (no writer emits a canonical state yet). This tuple only widens
# ALL_STATUSES so `list_applications` accepts a canonical `status` filter and the
# transition guard-key set can cover the whole target machine.
_CANONICAL_ADDED_STATUSES: Tuple[str, ...] = (
    STATUS_DRAFT, STATUS_PREPARING, STATUS_READY_FOR_REVIEW, STATUS_APPROVED,
    STATUS_QUEUED_FOR_EXECUTION, STATUS_RUNNING, STATUS_WAITING_OTP,
    STATUS_WAITING_HUMAN, STATUS_SUBMISSION_UNKNOWN, STATUS_CANCELLED,
    STATUS_UNSUPPORTED, STATUS_WITHDRAWN,
)

ALL_STATUSES: Tuple[str, ...] = (
    ACTIVE_STATUSES + RETRYABLE_STATUSES + _CANONICAL_ADDED_STATUSES
    # E1: the external terminal — valid + filterable, but NOT active/retryable.
    + (STATUS_EXTERNAL,)
)

# ---------------------------------------------------------------------------
# OUTCOME axis (defect E, E1) — the EMPLOYER-RESPONSE funnel, ORTHOGONAL to
# `status` (the execution lifecycle). While `status` answers "did JobPilot get
# this submitted?", `outcome` answers "what did the employer do after?". It is
# owned by the mail/gmail/manual signals and (until E3) MIRRORED to the coarse
# `tailoring_records.application.status` the kanban/funnel readers still read.
# Stored on the row as outcome/outcome_source/outcome_evidence/outcome_updated_at
# via set_outcome; _view is intentionally NOT widened yet (readers migrate in E3).
# ---------------------------------------------------------------------------
OUTCOME_NONE = "none"
OUTCOME_APPLIED = "applied"
OUTCOME_VIEWED = "viewed"
OUTCOME_SCREENING = "screening"
OUTCOME_INTERVIEWING = "interviewing"
OUTCOME_OFFER = "offer"
OUTCOME_REJECTED = "rejected"
OUTCOME_GHOSTED = "ghosted"
OUTCOME_WITHDRAWN = "withdrawn"

OUTCOME_STATUSES: Tuple[str, ...] = (
    OUTCOME_NONE, OUTCOME_APPLIED, OUTCOME_VIEWED, OUTCOME_SCREENING,
    OUTCOME_INTERVIEWING, OUTCOME_OFFER, OUTCOME_REJECTED, OUTCOME_GHOSTED,
    OUTCOME_WITHDRAWN,
)

# The canonical target-machine vocabulary — the set of values `canonical_status`
# may EMIT. It is the union of the additive canonical states, the legacy names
# that are already canonical (map to themselves), and `failed_pre_submit` (the
# canonical peer of `failed`). The four legacy-only aliases
# (gathering/ready/submitting/failed) are intentionally ABSENT — they normalize
# into their canonical peers below.
CANONICAL_STATUSES: Tuple[str, ...] = _CANONICAL_ADDED_STATUSES + (
    STATUS_QUEUED, STATUS_NEEDS_INPUT, STATUS_SUBMITTED, STATUS_CONFIRMED,
    STATUS_SKIPPED, STATUS_SKIPPED_DUPLICATE, STATUS_FAILED_PRE_SUBMIT,
)

# Legacy → canonical remap for the four names whose spelling changes. Read-side
# only (E3 will call `canonical_status`); no writer uses this in E0.
_LEGACY_TO_CANONICAL: Dict[str, str] = {
    STATUS_GATHERING: STATUS_PREPARING,
    STATUS_READY: STATUS_READY_FOR_REVIEW,
    STATUS_SUBMITTING: STATUS_RUNNING,
    STATUS_FAILED: STATUS_FAILED_PRE_SUBMIT,
}


def canonical_status(status: str) -> str:
    """Normalize a (legacy or canonical) status onto the canonical vocabulary.

    E0 read-side shim for defect E. Maps the four renamed legacy states
    (gathering→preparing, ready→ready_for_review, submitting→running,
    failed→failed_pre_submit); every other legacy name and every canonical name
    is already canonical and returns unchanged, so the function is idempotent
    (`canonical_status(canonical_status(x)) == canonical_status(x)`). An unknown
    or non-string input is returned as-is and never raises — this is a
    tolerant normalizer, not a validator. NOT called by any writer/reader yet.
    """
    if not isinstance(status, str):
        return status
    return _LEGACY_TO_CANONICAL.get(status, status)


# ---------------------------------------------------------------------------
# E3 (defect E) — the read-side DUAL-READ shim. Collapses a canonical
# `applications` row's TWO axes (outcome + canonical_status(status)) onto the
# SINGLE coarse `tailoring_records.application.status` vocabulary the funnel /
# kanban / today_inbox readers consume today, so a reader can PREFER the
# canonical row when one exists and FALL BACK to the tailoring_records mirror
# otherwise. Writers dual-write the mirror in E1 and the mirror is KEPT (its drop
# is a DEFERRED cleanup, see docs/specs/auto-apply-defects-plan.md), so a reader
# seeded from tailoring_records only (the char goldens) is unaffected.
# ---------------------------------------------------------------------------
# outcome (employer-response funnel) -> coarse tracker/funnel bucket.
_OUTCOME_TO_BUCKET: Dict[str, str] = {
    OUTCOME_NONE: "draft",
    OUTCOME_APPLIED: "applied",
    OUTCOME_VIEWED: "applied",
    OUTCOME_SCREENING: "interviewing",
    OUTCOME_INTERVIEWING: "interviewing",
    OUTCOME_OFFER: "offer",
    OUTCOME_REJECTED: "rejected",
    OUTCOME_GHOSTED: "ghosted",
    OUTCOME_WITHDRAWN: "withdrawn",
}
# execution status -> coarse bucket, for a row whose outcome axis is still `none`
# (no employer response yet) but that JobPilot DID submit — it must count as
# `applied` on the funnel exactly as a submitted legacy row does.
_EXEC_STATUS_TO_BUCKET: Dict[str, str] = {
    STATUS_SUBMITTED: "applied",
    STATUS_CONFIRMED: "applied",
    STATUS_WITHDRAWN: "withdrawn",
}


def outcome_view(row: Dict[str, Any]) -> str:
    """Collapse a canonical `applications` row onto the coarse
    `tailoring_records.application.status` bucket the funnel / kanban /
    today_inbox readers use today (E3 dual-read).

    The OUTCOME axis (what the employer did) WINS when it carries a real signal;
    a row with no employer response yet falls back to its EXECUTION status (a
    submitted/confirmed row counts as `applied`), else `draft`. Tolerant: a
    non-dict / unknown input collapses to `draft` and never raises.
    """
    if not isinstance(row, dict):
        return "draft"
    bucket = _OUTCOME_TO_BUCKET.get(row.get("outcome"))
    if bucket and bucket != "draft":
        return bucket
    exec_bucket = _EXEC_STATUS_TO_BUCKET.get(canonical_status(row.get("status")))
    if exec_bucket:
        return exec_bucket
    return bucket or "draft"


# Live (non-terminal) states — the only ones that may fall to `failed`.
_LIVE_STATUSES: Tuple[str, ...] = (
    STATUS_QUEUED, STATUS_GATHERING, STATUS_NEEDS_INPUT, STATUS_READY,
    STATUS_SUBMITTING, STATUS_SUBMITTED,
)

# E0: the canonical in-flight (pre-terminal) states — the canonical peers of
# _LIVE_STATUSES. Unioned into the `failed` guard so a canonical live row can
# still fall to `failed` exactly as a legacy live row can.
_CANONICAL_LIVE_STATUSES: Tuple[str, ...] = (
    STATUS_DRAFT, STATUS_PREPARING, STATUS_READY_FOR_REVIEW, STATUS_APPROVED,
    STATUS_QUEUED_FOR_EXECUTION, STATUS_RUNNING, STATUS_WAITING_OTP,
    STATUS_WAITING_HUMAN,
)

# target status -> statuses it may be entered FROM (the compare-and-set guard).
#
# E0 (defect E): the canonical machine is unioned in ADDITIVELY. No legacy edge
# below is removed or re-parented — canonical sources are only ADDED to existing
# targets, and the new canonical targets are appended. Every from->to pair that
# is legal today stays legal; a diff reviewer should see only additions.
_TRANSITION_GUARDS: Dict[str, Tuple[str, ...]] = {
    STATUS_GATHERING: (STATUS_QUEUED,),
    # entered from gather (novel question), or re-entered when a submit run
    # blocks on something only the user can answer (one-tap resume loop).
    # E0: preparing → needs_input (canonical peer of gathering → needs_input).
    # F: running → needs_input — an executor run that hits an Oracle wall
    # (verify/CAPTCHA) parks the row in the one-tap queue (see auto_apply._park_verify).
    STATUS_NEEDS_INPUT: (
        STATUS_GATHERING, STATUS_READY, STATUS_SUBMITTING, STATUS_PREPARING,
        STATUS_RUNNING),
    # gathering → ready directly when all answers are known (auto path).
    STATUS_READY: (STATUS_GATHERING, STATUS_NEEDS_INPUT),
    STATUS_SUBMITTING: (STATUS_READY,),
    # E0: a run submits directly (running), or after an OTP / human-handoff wall
    # clears — canonical peers of submitting → submitted.
    STATUS_SUBMITTED: (
        STATUS_SUBMITTING, STATUS_RUNNING, STATUS_WAITING_OTP,
        STATUS_WAITING_HUMAN),
    STATUS_CONFIRMED: (STATUS_SUBMITTED,),
    # any live state → failed. E0: canonical live states join the legacy ones.
    STATUS_FAILED: _LIVE_STATUSES + _CANONICAL_LIVE_STATUSES,
    # a row discovered mid-gather to collide with another ledger row.
    STATUS_SKIPPED_DUPLICATE: (STATUS_QUEUED, STATUS_GATHERING),
    # user-initiated skip (§2 email `skip`): from any pre-submit live state.
    STATUS_SKIPPED: (STATUS_QUEUED, STATUS_GATHERING, STATUS_NEEDS_INPUT, STATUS_READY),
    # explicit retry only. E0: canonical draft → queued (the entry hand-off).
    STATUS_QUEUED: RETRYABLE_STATUSES + (STATUS_DRAFT,),

    # --- E0: canonical machine targets (defect E), additive ---
    # draft is the canonical entry state; a row is CREATED here, nothing
    # transitions INTO it yet (empty guard = no legal inbound edge in E0).
    STATUS_DRAFT: (),
    # queued → preparing (canonical peer of queued → gathering).
    STATUS_PREPARING: (STATUS_QUEUED,),
    # needs_input → ready_for_review (answers resolved); or preparing →
    # ready_for_review directly when all answers are known (canonical peer of
    # the gathering → ready auto path).
    # F: gathering → ready_for_review is the live auto/prepare landing edge (the
    # split of the legacy gathering → ready). approved/queued_for_execution →
    # ready_for_review is the approval-INVALIDATION edge — a stale snapshot at
    # submit re-validation kicks the row back to review rather than submitting.
    STATUS_READY_FOR_REVIEW: (
        STATUS_NEEDS_INPUT, STATUS_PREPARING, STATUS_GATHERING,
        STATUS_APPROVED, STATUS_QUEUED_FOR_EXECUTION),
    # ready_for_review → approved. This is the F approval edge; F will own the
    # approval SEMANTICS/snapshot — E0 only makes the edge legal.
    STATUS_APPROVED: (STATUS_READY_FOR_REVIEW,),
    # approved → queued_for_execution (picked up by the executor sweep).
    STATUS_QUEUED_FOR_EXECUTION: (STATUS_APPROVED,),
    # queued_for_execution → running (canonical peer of ready → submitting).
    STATUS_RUNNING: (STATUS_QUEUED_FOR_EXECUTION,),
    # running → waiting_otp / waiting_human (an OTP or human wall mid-run).
    STATUS_WAITING_OTP: (STATUS_RUNNING,),
    STATUS_WAITING_HUMAN: (STATUS_RUNNING,),
    # --- E0: canonical terminals, from the appropriate in-flight states ---
    # cancel: a pre-submit abort of the whole application (never after submit).
    STATUS_CANCELLED: (
        STATUS_DRAFT, STATUS_QUEUED, STATUS_PREPARING, STATUS_NEEDS_INPUT,
        STATUS_READY_FOR_REVIEW, STATUS_APPROVED, STATUS_QUEUED_FOR_EXECUTION),
    # the ATS/site turned out to be unsupportable during prep or execution.
    STATUS_UNSUPPORTED: (
        STATUS_PREPARING, STATUS_QUEUED_FOR_EXECUTION, STATUS_RUNNING),
    # user withdraws after we submitted (post-submit terminal; confirmed stays a
    # dead-end so the "no legal forward move from confirmed" invariant holds).
    STATUS_WITHDRAWN: (STATUS_SUBMITTED,),
    # a submit attempt whose outcome we could not confirm — HELD (distinct from
    # failed), so reached only from the execution states that attempt a submit.
    STATUS_SUBMISSION_UNKNOWN: (
        STATUS_RUNNING, STATUS_WAITING_OTP, STATUS_WAITING_HUMAN),
}

# F: approval is TIGHTENED to only `ready_for_review` — the review/wait state a
# row reaches once gather/prepare have resolved every answer (and, with D, pinned
# a résumé). A queued/gathering/needs_input row is NOT yet reviewable, so an
# email-approve on it is a no-op (belt-and-suspenders: never approve — and, with
# G, never charge — a row that isn't ready). This replaces the pre-F set
# (queued/gathering/needs_input), which let an approval race a still-gathering row.
_APPROVABLE_STATUSES: Tuple[str, ...] = (STATUS_READY_FOR_REVIEW,)
# Legacy `ready` rows predate the split; tolerate them as their canonical peer
# (canonical_status(ready) == ready_for_review) so an in-flight legacy row can
# still be approved during the cutover.
_APPROVABLE_LEGACY: Tuple[str, ...] = (STATUS_READY,)

# F: the approval-policy + consent-copy schema versions frozen into every
# approval snapshot. Bump when the approval policy or the consent text materially
# changes, so a snapshot minted under an older policy is detectable (and can be
# forced through a fresh re-approval).
_APPROVAL_POLICY_VERSION = "1"
_APPROVAL_CONSENT_VERSION = "1"
# Pre-submit live states a user `skip` may terminate (never touch an already
# submitting/submitted/confirmed row).
_SKIPPABLE_STATUSES: Tuple[str, ...] = (
    STATUS_QUEUED, STATUS_GATHERING, STATUS_NEEDS_INPUT, STATUS_READY,
)

_VALID_AUTONOMY = ("auto", "hold", "ask")

# Bounded needs_input so the one-tap queue can never grow a document without
# limit (no unbounded arrays — same spirit as apply.py::_MAX_VAULT_BYTES).
_MAX_NEEDS_INPUT_ITEMS = 50

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# Typed errors — the route maps each to its HTTP status. Never 401 (that is
# reserved for missing/invalid JWT); business failures are 400/404/409.
# ---------------------------------------------------------------------------
class ApplicationLedgerError(Exception):
    status = 400
    error = "application_ledger_error"

    def __init__(self, message: Optional[str] = None):
        self.message = message or self.error
        super().__init__(self.message)


class InvalidApplicationInput(ApplicationLedgerError):
    status = 400
    error = "invalid_application_input"


class ApplicationNotFound(ApplicationLedgerError):
    status = 404
    error = "application_not_found"


class InvalidTransition(ApplicationLedgerError):
    """Row exists but is not in a state this transition may leave from — either
    the caller is out of order or another Lambda finalized it first (the CAS
    guard did not match). A conflict, never a silent overwrite."""
    status = 409
    error = "invalid_transition"


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------
def _applications():
    return DBConnect().get_db().applications


def _audit_coll():
    # Shared with auto_submit_service — already indexed (user_email, at) and
    # (application_id, at) in scripts/migrate_indexes.py. The ledger's
    # `audit_ref` (= application_id) resolves to entries here.
    return DBConnect().get_db().auto_submit_audit


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """mongomock/pymongo may hand back naive UTC datetimes — normalize so
    comparisons never raise on offset-naive vs offset-aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(dt: Optional[datetime]) -> Optional[str]:
    dt = _as_aware(dt)
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Audit log (append-only, PII-redacted — meta carries counts/prefixes only)
# ---------------------------------------------------------------------------
def _audit(user_email: str, action: str, *, application_id: Optional[str] = None,
           meta: Optional[Dict[str, Any]] = None) -> None:
    """Record one ledger transition. Soft-fail: an audit write must never break
    the user-facing action, but we log loudly if it does."""
    try:
        _audit_coll().insert_one({
            "user_email": user_email,
            "action": action,            # application_created|application_requeued|
                                         # application_skipped_duplicate|
                                         # application_transition
            "approval_id": None,
            "application_id": application_id,
            "source": "application_ledger",
            "at": _now(),
            "meta": meta or {},          # redacted only
        })
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("application_ledger audit write failed (%s): %s", action, e)


# ---------------------------------------------------------------------------
# Normalized job identity (the dedup key)
# ---------------------------------------------------------------------------
# Tokens that vary between postings of the SAME role: seniority/level noise,
# roman-numeral levels, and work-arrangement/location fillers.
_TITLE_NOISE_TOKENS = frozenset({
    # seniority / level
    "senior", "sr", "jr", "junior", "staff", "principal", "lead", "associate",
    "entry", "mid", "level", "intern", "internship", "new", "grad", "graduate",
    "i", "ii", "iii", "iv", "v",
    # work arrangement / location fillers
    "remote", "hybrid", "onsite", "on-site", "us", "usa", "united", "states",
})

# Req-id shapes: "Req ID 12345", "Requisition #: 9-88", "Job No. 4711",
# "JR-102938" (Workday), "R-0045678", trailing "#12345".
_REQ_ID_PATTERN = re.compile(
    r"\b(?:req|requisition|job|jr|jo)[\s#:._-]*(?:id|no|number)?[\s#:._-]*\d[\w-]*\b"
    r"|\br-?\d{4,}\b"
    r"|#\d+\b",
    re.IGNORECASE,
)


def canonical_title(title: str) -> str:
    """Reduce a job title to its role stem, deterministically.

    Strips parenthesized/bracketed segments, pipe/comma-suffixed location and
    department tails, req-ids, seniority/level tokens, and bare level numbers —
    so "Sr. Software Engineer II (Remote) - REQ-4711" and
    "Software Engineer, Seattle, WA" both stem to "software engineer".
    Distinct reqs at the same company stay distinct via the jd_hash prefix in
    `normalized_job_identity`, so over-stemming here is safe by design.
    """
    text = (title or "").strip().lower()
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", text)  # "(Remote)", "[Hybrid]"
    text = text.split("|")[0]                          # "Title | Dept | Location"
    text = text.split(",")[0]                          # "Title, Seattle, WA"
    text = _REQ_ID_PATTERN.sub(" ", text)
    # Keep +/# so "c++" / "c#" survive tokenization.
    tokens = re.split(r"[^a-z0-9+#]+", text)
    kept = [t for t in tokens
            if t and t not in _TITLE_NOISE_TOKENS and not t.isdigit()]
    return " ".join(kept) or text.strip()


def jd_hash(jd_text: str) -> str:
    """sha256 of the whitespace-collapsed, lowercased JD text — the same
    posting fetched twice (different trailing whitespace / casing) hashes
    identically; a genuinely different req at the same company does not."""
    normalized = re.sub(r"\s+", " ", (jd_text or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalized_job_identity(company: str, title: str, jd_text: str) -> str:
    """The dedup key (Tranche 1 §1, exact formula):

        sha256( lower(strip(company)) + "|" + lower(canonical_title(title))
                + "|" + jd_hash[:16] )

    Stable across heterogeneous sources whose native job_ids are not.
    Company+title is primary; the jd_hash prefix disambiguates distinct reqs
    at the same company.
    """
    company_norm = (company or "").strip().lower()
    title_norm = canonical_title(title).lower()
    basis = f"{company_norm}|{title_norm}|{jd_hash(jd_text)[:16]}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public record view (safe to return to the owning user over an authed channel)
# ---------------------------------------------------------------------------
def _view(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "application_id": rec.get("application_id"),
        "job_identity": rec.get("job_identity"),
        "source": rec.get("source"),
        "ats_family": rec.get("ats_family"),
        "tenant": rec.get("tenant"),
        "company": rec.get("company"),
        "title": rec.get("title"),
        "jd_hash": rec.get("jd_hash"),
        "apply_url": rec.get("apply_url"),
        "posting_closes_at": _iso(rec.get("posting_closes_at")),
        "tailoring_record_id": rec.get("tailoring_record_id"),
        "autonomy": rec.get("autonomy") or "ask",
        "confidence_score": float(rec.get("confidence_score") or 0.0),
        "status": rec.get("status"),
        "needs_input": rec.get("needs_input") or [],
        "error": rec.get("error"),
        "created_at": _iso(rec.get("created_at")),
        "updated_at": _iso(rec.get("updated_at")),
        "submitted_at": _iso(rec.get("submitted_at")),
        "confirmed_at": _iso(rec.get("confirmed_at")),
        "audit_ref": rec.get("audit_ref"),
    }


# ---------------------------------------------------------------------------
# Input validation / sanitization (before ANY value reaches a Mongo filter
# or document — user_email itself comes from the verified JWT identity)
# ---------------------------------------------------------------------------
def _require_email(user_email: str) -> str:
    if not user_email or not isinstance(user_email, str):
        raise InvalidApplicationInput("user_email is required")
    return user_email


def _require_identity(job_identity: str) -> str:
    """job_identity must be exactly what normalized_job_identity emits — a
    64-char lowercase sha256 hex string. Anything else (including an
    injection-shaped dict in an operator position) never reaches a filter."""
    if not isinstance(job_identity, str):
        raise InvalidApplicationInput("job_identity must be a sha256 hex string")
    value = job_identity.strip().lower()
    if not _HEX64.match(value):
        raise InvalidApplicationInput("job_identity must be a sha256 hex string")
    return value


def _clean_str(value: Any, max_length: int) -> str:
    return InputSanitizer.sanitize_string(str(value or ""), max_length=max_length)


def _parse_closes_at(value: Any) -> Optional[datetime]:
    """Accept a datetime or ISO-8601 string; store an aware datetime so the
    sparse posting_closes_at index supports the §5 sweep's range filter."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _as_aware(value)
    if isinstance(value, str):
        try:
            return _as_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            raise InvalidApplicationInput("posting_closes_at must be ISO-8601")
    raise InvalidApplicationInput("posting_closes_at must be ISO-8601")


def _sanitize_needs_input(items: Any) -> List[Dict[str, str]]:
    """Bound + sanitize the one-tap review queue items ({question, kind,
    field_id}). Silently drops malformed entries; caps the list."""
    out: List[Dict[str, str]] = []
    if not isinstance(items, list):
        return out
    for item in items[:_MAX_NEEDS_INPUT_ITEMS]:
        if not isinstance(item, dict):
            continue
        question = _clean_str(item.get("question"), 500)
        if not question:
            continue
        entry = {
            "question": question,
            "kind": _clean_str(item.get("kind"), 40),
            "field_id": _clean_str(item.get("field_id"), 200),
        }
        # Optional human-handoff fields (e.g. an oracle/captcha wall the user can
        # finish in their own browser): keep the apply URL + action when present.
        apply_url = _clean_str(item.get("apply_url"), 2000)
        if apply_url:
            entry["apply_url"] = apply_url
        action = _clean_str(item.get("action"), 40)
        if action:
            entry["action"] = action
        out.append(entry)
    return out


def _clamp_score(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Dedup gate — the fast pre-enqueue check (the unique index is the backstop)
# ---------------------------------------------------------------------------
def dedup_gate(user_email: str, job_identity: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Should this (user, posting) enqueue be SKIPPED?

    Returns ``(skip, existing_view)``:
      * ``(True, view)``  — a ledger row exists in a handled status
        (queued/gathering/needs_input/ready/submitting/submitted/confirmed):
        already handled, do not enqueue again.
      * ``(False, view)`` — a row exists but is retryable
        (failed/skipped_duplicate): the caller may retry via
        ``create_application`` (which re-queues it atomically).
      * ``(False, None)`` — no row: enqueue freely.

    Always folds in user_email — cross-user rows are invisible. This is the
    cheap check; the unique (user_email, job_identity) index makes the race
    window between gate and insert harmless (loser → skipped_duplicate).
    """
    user_email = _require_email(user_email)
    job_identity = _require_identity(job_identity)
    rec = _applications().find_one(
        {"user_email": user_email, "job_identity": job_identity},
        {"_id": 0},
    )
    if not rec:
        return False, None
    if rec.get("status") in ACTIVE_STATUSES:
        return True, _view(rec)
    return False, _view(rec)


# ---------------------------------------------------------------------------
# Create (insert-first; unique-index conflict → skipped_duplicate no-op)
# ---------------------------------------------------------------------------
def _pin_source_posting(posting_job_id: Optional[str]) -> None:
    """Best-effort pin of the inventory row behind an application.

    Never raises. If the pin fails the posting simply stays subject to normal
    retention and the archive is retried by the next sweep that reaches it;
    losing the pin is a degraded history link, not a lost application.
    """
    if not posting_job_id:
        return
    try:
        from services import job_inventory_service as inv

        inv.pin_posting(str(posting_job_id), reason="applied")
    except Exception as exc:  # pragma: no cover - pinning must never block apply
        logger.warning("pin_posting failed for %s: %s",
                       str(posting_job_id)[:12], type(exc).__name__)


def create_application(user_email: str, *, company: str, title: str,
                       jd_text: str, source: str, apply_url: str = "",
                       ats_family: Optional[str] = None,
                       tenant: Optional[str] = None,
                       posting_closes_at: Any = None,
                       tailoring_record_id: Optional[str] = None,
                       posting_job_id: Optional[str] = None,
                       autonomy: str = "ask",
                       confidence_score: float = 0.0) -> Tuple[Dict[str, Any], str]:
    """Create a ledger row in `queued`, or resolve against the existing one.

    Returns ``(record_view, outcome)`` where outcome is one of:
      * ``"created"``            — new row inserted (status `queued`).
      * ``"requeued"``           — an existing failed/skipped_duplicate row was
                                   atomically reset to `queued` (the ONLY retry
                                   path, per the §1 status rule).
      * ``"skipped_duplicate"``  — a row in a handled status already covers
                                   this (user, posting); no-op, nothing written.

    Insert-first: we do NOT check-then-insert. The unique
    (user_email, job_identity) index turns the concurrent-Lambda race into a
    DuplicateKeyError we resolve deterministically — exactly one winner ever
    holds the row. (Run scripts/migrate_indexes.py before first use; without
    the index the DuplicateKeyError backstop cannot fire.)
    """
    from pymongo.errors import DuplicateKeyError

    user_email = _require_email(user_email)
    company = _clean_str(company, 300)
    title = _clean_str(title, 300)
    if not company or not title:
        raise InvalidApplicationInput("company and title are required")
    source = _clean_str(source, 40).lower()
    if not source:
        raise InvalidApplicationInput("source is required")
    if autonomy not in _VALID_AUTONOMY:
        raise InvalidApplicationInput(f"autonomy must be one of {_VALID_AUTONOMY}")

    now = _now()
    identity = normalized_job_identity(company, title, jd_text)
    application_id = str(uuid.uuid4())
    fresh_fields = {
        "source": source,
        "ats_family": _clean_str(ats_family, 40).lower() or None,
        "tenant": _clean_str(tenant, 200).lower() or None,
        "company": company,
        "title": title,
        "jd_hash": jd_hash(jd_text),
        "apply_url": _clean_str(apply_url, 2000),
        "posting_closes_at": _parse_closes_at(posting_closes_at),
        "tailoring_record_id": _clean_str(tailoring_record_id, 200) or None,
        # Link back to the canonical inventory row, when the posting came from
        # there. This is what `pin_posting` below uses to make the job outlive
        # the 7-day retention window.
        "posting_job_id": _clean_str(posting_job_id, 64) or None,
        "autonomy": autonomy,
        "confidence_score": _clamp_score(confidence_score),
    }
    rec = {
        "application_id": application_id,
        "user_email": user_email,
        "job_identity": identity,
        **fresh_fields,
        "status": STATUS_QUEUED,
        "needs_input": [],
        "error": None,
        "created_at": now,
        "updated_at": now,
        "submitted_at": None,
        "confirmed_at": None,
        # Audit entries live in auto_submit_audit keyed by application_id
        # (index application_id_at) — audit_ref is that grouping key.
        "audit_ref": application_id,
    }

    try:
        _applications().insert_one(rec)
    except DuplicateKeyError:
        _pin_source_posting(posting_job_id)
        return _resolve_insert_conflict(user_email, identity, fresh_fields, now)

    # A posting a user has applied to must outlive the inventory's rolling
    # 7-day window, or their own application history renders as a dead
    # reference. Pinning archives the full posting (body included) to S3 and
    # flags the Mongo row so retention verifies that archive before deleting.
    # Done AFTER the successful insert so a failed create cannot pin, and
    # deliberately non-fatal: an application that exists with an unpinned
    # posting is recoverable, an application that failed to record is not.
    _pin_source_posting(posting_job_id)

    _audit(user_email, "application_created", application_id=application_id,
           meta={"job_identity_prefix": identity[:12], "source": source,
                 "autonomy": autonomy,
                 "confidence_score": fresh_fields["confidence_score"]})
    rec.pop("_id", None)  # insert_one mutates rec with the ObjectId
    return _view(rec), "created"


def _resolve_insert_conflict(user_email: str, job_identity: str,
                             fresh_fields: Dict[str, Any],
                             now: datetime) -> Tuple[Dict[str, Any], str]:
    """The unique index rejected our insert: exactly one row already exists for
    this (user, posting). Retryable (failed/skipped_duplicate) → CAS it back to
    `queued` with the fresh posting fields; anything else → skipped_duplicate
    no-op (the existing row IS the application of record)."""
    from pymongo import ReturnDocument

    requeued = _applications().find_one_and_update(
        {"user_email": user_email, "job_identity": job_identity,
         "status": {"$in": list(RETRYABLE_STATUSES)}},
        {"$set": {**fresh_fields, "status": STATUS_QUEUED, "error": None,
                  "needs_input": [], "updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if requeued is not None:
        _audit(user_email, "application_requeued",
               application_id=requeued.get("application_id"),
               meta={"job_identity_prefix": job_identity[:12]})
        return _view(requeued), "requeued"

    existing = _applications().find_one(
        {"user_email": user_email, "job_identity": job_identity}, {"_id": 0}
    )
    if existing is None:  # pragma: no cover — row deleted between conflict and read
        raise ApplicationNotFound("application vanished during dedup resolution")
    _audit(user_email, "application_skipped_duplicate",
           application_id=existing.get("application_id"),
           meta={"job_identity_prefix": job_identity[:12],
                 "existing_status": existing.get("status")})
    return _view(existing), "skipped_duplicate"


# ---------------------------------------------------------------------------
# Status transitions (atomic compare-and-set — the auto_submit idiom)
# ---------------------------------------------------------------------------
def _atomic_transition(user_email: str, application_id: str,
                       guard: Tuple[str, ...],
                       set_fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Compare-and-set a status transition: only mutate if the record is still
    in one of `guard` states, and return the post-update doc (None if the guard
    did not match, i.e. another caller finalized it first). Folds in user_email
    so a row can only ever be transitioned by its owner. This is the
    single-writer guarantee that makes the ledger safe under concurrent
    Lambdas."""
    from pymongo import ReturnDocument
    return _applications().find_one_and_update(
        {"application_id": application_id, "user_email": user_email,
         "status": {"$in": list(guard)}},
        {"$set": {**set_fields, "updated_at": _now()}},
        return_document=ReturnDocument.AFTER,
    )


# ---------------------------------------------------------------------------
# Exactly-once entitlement RESERVATION for auto_submit (defect G).
#
# Dark-launch gated (AUTO_APPLY_RESERVE, default OFF) so every existing
# approval/transition path stays byte-identical until flipped on — mirroring the
# AUTO_APPLY_PREPARE / AUTO_APPLY_MODES posture. When ON:
#   * approve() RESERVES one auto_submit slot per row (atomic, idempotent on
#     application_id + snapshot fingerprint) BEFORE the CAS — an over-cap row is
#     NOT approved (the route surfaces 429, never 401).
#   * transition() SETTLES that reservation at the terminal execution state:
#       - confirmed                                   → FINALIZE (slot consumed)
#       - failed / unsupported / cancelled / withdrawn → RELEASE (slot returned)
#       - submission_unknown                          → HOLD  (stays reserved)
#       - ready_for_review (approval invalidated)     → RELEASE + clear (a fresh
#         approval re-reserves against the fresh snapshot)
# All settlement is idempotent (quota_ledger complete/refund guard on entry
# state), so a redelivered transition never double-settles.
# ---------------------------------------------------------------------------
_AUTO_SUBMIT_FEATURE = "auto_submit"

# SAFE pre-submit terminals whose reservation is RELEASED (never charge for an
# application that never submitted).
_RESERVATION_REFUND_STATUSES: Tuple[str, ...] = (
    STATUS_FAILED, STATUS_UNSUPPORTED, STATUS_CANCELLED, STATUS_WITHDRAWN,
)


def _reserve_enabled() -> bool:
    """Dark-launch gate for defect G's atomic auto_submit reservation. Default
    OFF — a misconfig can only leave today's (non-reserving) behavior in place."""
    try:
        from utils.config import _get_config_value
        val = _get_config_value("AUTO_APPLY_RESERVE", "false") or "false"
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # pragma: no cover - defensive
        return False


def reserve_enabled() -> bool:
    """PUBLIC read of the defect-G reservation gate, for callers that must know
    WHO charges the auto_submit slot. When True, `approve()` reserves the slot
    itself (atomic, idempotency-keyed), so a route that also metered the feature
    would DOUBLE-CHARGE — the route must skip its own charge for rows it advances
    through `approve()`. When False, `approve()` never touches quota and the
    legacy route-level metering remains the single charge."""
    return _reserve_enabled()


def _auto_submit_idempotency_key(application_id: str,
                                 snapshot: Optional[Dict[str, Any]]) -> str:
    """The reservation idempotency key = application_id + a fingerprint of the
    STABLE approval integrity fields (never approved_at/approved_by, which differ
    per click). So two concurrent/replayed approvals of the SAME snapshot collapse
    onto ONE reservation, and a re-approval after a snapshot drift mints a fresh
    one."""
    snapshot = snapshot or {}
    basis = "|".join(
        str(snapshot.get(k) or "")
        for k in ("application_hash", "resume_version_id", "answers_fingerprint"))
    fp = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"{_AUTO_SUBMIT_FEATURE}:{application_id}:{fp}"


def _settle_reservation(user_email: str, rec: Dict[str, Any],
                        to_status: str) -> None:
    """Settle the auto_submit reservation frozen at approval when a row reaches a
    terminal execution state (defect G). Inert when the row carries no
    reservation (e.g. a manual/mail row, or reservation gated OFF at approval),
    so it is safe to call on EVERY transition. Idempotent."""
    reservation = rec.get("auto_submit_reservation") or {}
    entry_ids = reservation.get("entry_ids") or []
    if not entry_ids:
        return
    try:
        from services import quota_ledger_service as quota
        if to_status == STATUS_CONFIRMED:
            for eid in entry_ids:
                quota.complete_entry(eid)
        elif to_status in _RESERVATION_REFUND_STATUSES:
            quota.refund_entries(entry_ids)
        elif to_status == STATUS_READY_FOR_REVIEW:
            # Approval invalidated back to review — release the slot and drop the
            # linkage so a fresh approval reserves cleanly against the new snapshot.
            quota.refund_entries(entry_ids)
            _applications().update_one(
                {"application_id": rec.get("application_id"), "user_email": user_email},
                {"$unset": {"auto_submit_reservation": ""}})
        # STATUS_SUBMISSION_UNKNOWN → HOLD: leave the reservation reserved.
    except Exception as e:  # pragma: no cover - settlement must never break the txn
        logger.warning("auto_submit reservation settle (%s) failed for %s: %s",
                       to_status, rec.get("application_id"), e)


def transition(user_email: str, application_id: str, to_status: str, *,
               error: Optional[str] = None,
               needs_input: Any = None,
               autonomy: Optional[str] = None,
               confidence_score: Optional[float] = None,
               tailoring_record_id: Optional[str] = None) -> Dict[str, Any]:
    """Move an application to `to_status` via a status-guarded CAS.

    The guard (which states `to_status` may be entered from) comes from
    _TRANSITION_GUARDS — an out-of-order call or a lost race is a 409
    InvalidTransition, never a silent overwrite. Optional keyword fields are
    validated/sanitized and set atomically WITH the status flip (one write,
    no read-modify-write window):

      * error               — stored on → failed (sanitized, capped)
      * needs_input         — stored on → needs_input (bounded list); cleared
                              automatically on → ready (questions resolved)
      * autonomy / confidence_score / tailoring_record_id — the §3 decision,
                              typically stamped at the gathering→ready|needs_input
                              boundary
    """
    user_email = _require_email(user_email)
    application_id = _clean_str(application_id, 64)
    if not application_id:
        raise InvalidApplicationInput("application_id is required")
    guard = _TRANSITION_GUARDS.get(to_status)
    if guard is None:
        raise InvalidApplicationInput(f"unknown status: {to_status}")

    now = _now()
    set_fields: Dict[str, Any] = {"status": to_status}
    if to_status == STATUS_SUBMITTED:
        set_fields["submitted_at"] = now
    elif to_status == STATUS_CONFIRMED:
        set_fields["confirmed_at"] = now
    elif to_status == STATUS_FAILED:
        set_fields["error"] = _clean_str(error, 2000) or "unknown_error"
    elif to_status == STATUS_WAITING_HUMAN and error:
        # A human-handoff hold carries its REASON in the same field every reader
        # already surfaces (the fold payload reads `error` as the card's reason).
        # Only set when a reason was passed, so existing callers are unchanged.
        set_fields["error"] = _clean_str(error, 2000)
    elif to_status == STATUS_UNSUPPORTED:
        # Spec §4: an unsupported ATS is surfaced HONESTLY, not as an error — the
        # row carries the user-facing sentence the FE renders verbatim. Distinct
        # from `failed` (retryable): this posting will never be automatable by us.
        set_fields["error"] = _clean_str(error, 2000) or UNSUPPORTED_MESSAGE
    elif to_status == STATUS_NEEDS_INPUT:
        set_fields["needs_input"] = _sanitize_needs_input(needs_input)
    elif to_status == STATUS_READY:
        set_fields["needs_input"] = []   # resolved by the one-tap loop
    elif to_status == STATUS_READY_FOR_REVIEW:
        set_fields["needs_input"] = []          # answers resolved before review
        # F: entering (or re-entering, on invalidation) review VOIDS any prior
        # approval — a fresh approval must re-freeze a fresh snapshot.
        set_fields["approval_snapshot"] = None
    elif to_status == STATUS_QUEUED:
        set_fields["error"] = None       # retry wipes the stale failure
        set_fields["needs_input"] = []

    if autonomy is not None:
        if autonomy not in _VALID_AUTONOMY:
            raise InvalidApplicationInput(f"autonomy must be one of {_VALID_AUTONOMY}")
        set_fields["autonomy"] = autonomy
    if confidence_score is not None:
        set_fields["confidence_score"] = _clamp_score(confidence_score)
    if tailoring_record_id is not None:
        set_fields["tailoring_record_id"] = _clean_str(tailoring_record_id, 200) or None

    updated = _atomic_transition(user_email, application_id, guard, set_fields)
    if updated is not None:
        # Defect G: settle the auto_submit reservation at the terminal execution
        # state (finalize on confirmed / release on safe pre-submit terminal /
        # hold on submission_unknown). Inert + idempotent when no reservation.
        _settle_reservation(user_email, updated, to_status)
        _audit(user_email, "application_transition", application_id=application_id,
               meta={"to": to_status,
                     "needs_input_count": len(set_fields.get("needs_input") or [])
                     if to_status == STATUS_NEEDS_INPUT else 0})
        return _view(updated)

    # Guard missed — distinguish "wrong state / lost race" (409) from
    # "never existed for this user" (404) for a clear route mapping.
    current = _applications().find_one(
        {"application_id": application_id, "user_email": user_email},
        {"_id": 0, "status": 1},
    )
    if current is None:
        raise ApplicationNotFound()
    raise InvalidTransition(
        f"cannot enter '{to_status}' from '{current.get('status')}'"
    )


# Named helpers — the vocabulary the gather/submit job branches (§6) speak.
def mark_gathering(user_email: str, application_id: str) -> Dict[str, Any]:
    return transition(user_email, application_id, STATUS_GATHERING)


def mark_needs_input(user_email: str, application_id: str, questions: Any, *,
                     autonomy: Optional[str] = None,
                     confidence_score: Optional[float] = None) -> Dict[str, Any]:
    return transition(user_email, application_id, STATUS_NEEDS_INPUT,
                      needs_input=questions, autonomy=autonomy,
                      confidence_score=confidence_score)


def update_needs_input(user_email: str, application_id: str,
                       questions: Any) -> Dict[str, Any]:
    """Rewrite the one-tap queue of a row that STAYS in `needs_input`.

    The answer loop (spec §3) shrinks the queue as questions get answered, which
    is NOT a transition (needs_input -> needs_input is not — and must not become —
    a legal edge). This is the only sanctioned way to edit the list in place: a
    status-guarded CAS on `needs_input`, so it can never touch a row that already
    advanced (a lost race raises InvalidTransition/404 exactly like `transition`).
    """
    user_email = _require_email(user_email)
    application_id = _clean_str(application_id, 64)
    if not application_id:
        raise InvalidApplicationInput("application_id is required")
    items = _sanitize_needs_input(questions)
    updated = _atomic_transition(user_email, application_id,
                                 (STATUS_NEEDS_INPUT,), {"needs_input": items})
    if updated is not None:
        _audit(user_email, "application_needs_input_updated",
               application_id=application_id,
               meta={"needs_input_count": len(items)})
        return _view(updated)
    current = _applications().find_one(
        {"application_id": application_id, "user_email": user_email},
        {"_id": 0, "status": 1},
    )
    if current is None:
        raise ApplicationNotFound()
    raise InvalidTransition(
        f"cannot update needs_input from '{current.get('status')}'")


def mark_ready(user_email: str, application_id: str, *,
               autonomy: Optional[str] = None,
               confidence_score: Optional[float] = None,
               tailoring_record_id: Optional[str] = None) -> Dict[str, Any]:
    return transition(user_email, application_id, STATUS_READY,
                      autonomy=autonomy, confidence_score=confidence_score,
                      tailoring_record_id=tailoring_record_id)


def mark_ready_for_review(user_email: str, application_id: str, *,
                          autonomy: Optional[str] = None,
                          confidence_score: Optional[float] = None,
                          tailoring_record_id: Optional[str] = None) -> Dict[str, Any]:
    """F: land a row in `ready_for_review` — the split of legacy `ready` that
    AWAITS an approval (a manual go-ahead, or the automatic system-approval on the
    clean-auto path). Approval (→ `approved`) freezes the immutable snapshot
    separately; see `approve`. Canonical peer of the legacy `mark_ready`."""
    return transition(user_email, application_id, STATUS_READY_FOR_REVIEW,
                      autonomy=autonomy, confidence_score=confidence_score,
                      tailoring_record_id=tailoring_record_id)


def mark_submitting(user_email: str, application_id: str) -> Dict[str, Any]:
    return transition(user_email, application_id, STATUS_SUBMITTING)


def mark_submitted(user_email: str, application_id: str) -> Dict[str, Any]:
    return transition(user_email, application_id, STATUS_SUBMITTED)


def mark_confirmed(user_email: str, application_id: str) -> Dict[str, Any]:
    return transition(user_email, application_id, STATUS_CONFIRMED)


def mark_failed(user_email: str, application_id: str, error: str) -> Dict[str, Any]:
    return transition(user_email, application_id, STATUS_FAILED, error=error)


def mark_waiting_human(user_email: str, application_id: str,
                       reason: Optional[str] = None) -> Dict[str, Any]:
    """Hand a RUNNING row back to the human (a `prepared` copilot fill, or a wall
    the executor cannot clear). `reason` is the user-facing sentence the Attention
    card renders. Not a failure — the row stays live and re-approvable."""
    return transition(user_email, application_id, STATUS_WAITING_HUMAN,
                      error=reason)


def mark_unsupported(user_email: str, application_id: str,
                     message: Optional[str] = None) -> Dict[str, Any]:
    """Spec §4: this employer's ATS/site is not automatable by us — a HONEST,
    non-retryable terminal (never a `failed` we would silently re-drive). Legal
    from preparing / queued_for_execution / running (see _TRANSITION_GUARDS).
    `message` defaults to the user-facing UNSUPPORTED_MESSAGE."""
    return transition(user_email, application_id, STATUS_UNSUPPORTED,
                      error=message or UNSUPPORTED_MESSAGE)


def requeue(user_email: str, application_id: str) -> Dict[str, Any]:
    """Explicit retry: failed/skipped_duplicate → queued (the ONLY way back in)."""
    return transition(user_email, application_id, STATUS_QUEUED)


# ---------------------------------------------------------------------------
# OUTCOME axis writer (defect E, E1) — the employer-response funnel write-through
# ---------------------------------------------------------------------------
def _outcome_locator(user_email: str, record_id: Optional[str],
                     job_ref: Optional[str]) -> Optional[Dict[str, Any]]:
    """The owner-scoped filter that finds the canonical row an outcome attaches
    to. Prefer the tailored-résumé link (`tailoring_record_id`), else a job
    reference (a 64-hex `job_identity`, else a `posting_job_id`). None means
    "no locator" → a standalone lightweight row is created. Every branch folds
    in user_email so an outcome can only ever touch the caller's OWN row."""
    if record_id:
        return {"user_email": user_email, "tailoring_record_id": record_id}
    if job_ref:
        jr = _clean_str(job_ref, 200)
        if jr:
            low = jr.lower()
            if _HEX64.match(low):
                return {"user_email": user_email, "job_identity": low}
            return {"user_email": user_email, "posting_job_id": jr}
    return None


def set_outcome(user_email: str, *, record_id: Optional[str] = None,
                job_ref: Optional[str] = None, outcome: str, source: str,
                evidence: Optional[str] = None) -> Dict[str, Any]:
    """Stamp the OUTCOME axis (employer-response funnel) on the canonical row for
    a (user, application), find-or-creating that row.

    This is the E1 write-through: mail/gmail/manual signals call it to record
    what the employer did (`outcome` ∈ OUTCOME_STATUSES), independent of the
    execution `status`. Resolution order (owner-scoped throughout):
      1. the row whose `tailoring_record_id == record_id`, else
      2. the row matched by `job_ref` (job_identity or posting_job_id), else
      3. a NEW lightweight row — `{origin: source, status: "external", ...}` —
         so a mail-only / manually-tracked application becomes a real canonical
         row WITHOUT ever entering ACTIVE_STATUSES/dedup_gate/retry (external is
         an execution-axis terminal; this row lives entirely on the outcome axis).

    The execution `status` of an EXISTING row is never touched here. Returns a
    small result dict {application_id, outcome, created}. Raises
    InvalidApplicationInput on an outcome outside OUTCOME_STATUSES.
    """
    from pymongo import ReturnDocument

    user_email = _require_email(user_email)
    if outcome not in OUTCOME_STATUSES:
        raise InvalidApplicationInput(f"unknown outcome: {outcome}")
    source_clean = _clean_str(source, 40).lower() or "unknown"
    evidence_clean = _clean_str(evidence, 500) or None
    record_id_clean = _clean_str(record_id, 200) or None
    now = _now()

    set_fields: Dict[str, Any] = {
        "outcome": outcome,
        "outcome_source": source_clean,
        "outcome_evidence": evidence_clean,
        "outcome_updated_at": now,
        "updated_at": now,
    }

    locator = _outcome_locator(user_email, record_id_clean, job_ref)

    new_application_id = str(uuid.uuid4())
    on_insert: Dict[str, Any] = {
        "application_id": new_application_id,
        "user_email": user_email,
        "origin": source_clean,
        "status": STATUS_EXTERNAL,
        "needs_input": [],
        "error": None,
        "created_at": now,
        "submitted_at": None,
        "confirmed_at": None,
        "audit_ref": new_application_id,
    }
    if record_id_clean:
        on_insert["tailoring_record_id"] = record_id_clean

    if locator is None:
        # No locator to upsert against — a standalone lightweight row.
        doc = {**on_insert, **set_fields}
        _applications().insert_one(doc)
        doc.pop("_id", None)
        rec = doc
    else:
        # A locator field seeds the upserted doc from the query equality, so it
        # must NOT also appear in $setOnInsert (that is a Mongo path conflict).
        insert_doc = {k: v for k, v in on_insert.items() if k not in locator}
        rec = _applications().find_one_and_update(
            locator,
            {"$set": set_fields, "$setOnInsert": insert_doc},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    created = bool(rec) and rec.get("application_id") == new_application_id
    _audit(user_email, "application_outcome_set",
           application_id=(rec or {}).get("application_id"),
           meta={"outcome": outcome, "source": source_clean, "created": created})
    return {
        "application_id": (rec or {}).get("application_id"),
        "outcome": outcome,
        "created": created,
    }


# ---------------------------------------------------------------------------
# Email approval loop (§2) — bulk, user-scoped, idempotent set operations over
# the exact application_ids a signed magic-link authorized. Both fold in
# user_email and guard on the source status ($in), so a stale/replayed link can
# only ever move the caller's OWN pre-submit rows and can never disturb a row
# that already advanced (no lost-update, no cross-user reach).
# ---------------------------------------------------------------------------
def _clean_id_list(application_ids: Any) -> List[str]:
    """Sanitize the id list before it reaches a Mongo $in filter — plain bounded
    strings only (the token payload is already validated; this is defense in
    depth so nothing operator-shaped can occupy a filter position)."""
    if not isinstance(application_ids, list):
        return []
    out: List[str] = []
    for aid in application_ids[:200]:
        aid = _clean_str(aid, 64)
        if aid:
            out.append(aid)
    return out


def _build_approval_snapshot(fields: Optional[Dict[str, Any]],
                             approved_by: str) -> Dict[str, Any]:
    """The immutable approval snapshot frozen onto a row at approval (F). Every
    field is sanitized/defaulted so nothing user-derived reaches the row raw. The
    three integrity fields (application_hash / resume_version_id /
    answers_fingerprint) are re-validated before the executor submits — a mismatch
    invalidates the approval instead of submitting a stale application. policy_/
    consent_version pin the approval semantics; approved_at/approved_by are the
    who/when audit anchor (approved_by == 'system' on the autopilot path)."""
    fields = fields or {}
    return {
        "application_hash": _clean_str(fields.get("application_hash"), 128) or None,
        "resume_version_id": _clean_str(fields.get("resume_version_id"), 128) or None,
        "answers_fingerprint": _clean_str(fields.get("answers_fingerprint"), 128) or None,
        "policy_version": _clean_str(fields.get("policy_version"), 40) or _APPROVAL_POLICY_VERSION,
        "consent_version": _clean_str(fields.get("consent_version"), 40) or _APPROVAL_CONSENT_VERSION,
        "approved_at": _now(),
        "approved_by": _clean_str(approved_by, 40) or "user",
    }


def approve_with_result(user_email: str, application_ids: Any, *,
                        approved_by: str = "user",
                        snapshot_provider: Any = None) -> Dict[str, Any]:
    """F: approve the user's listed `ready_for_review` rows (tolerating a legacy
    `ready`) → `approved`, FREEZING an immutable snapshot on each.

    Tightened from the pre-F `approve_to_ready` (which accepted queued/gathering/
    needs_input and moved to `ready`): a row that has NOT reached `ready_for_review`
    is left untouched — an approval can never race a still-gathering row. Idempotent
    + single-writer: a row already `approved` (or further) is a no-op — the
    status-guarded find_one_and_update never re-transitions or re-freezes it, so a
    replayed magic-link / duplicate click can never double-approve (and, once G
    lands its reservation call here, never double-charge). Autopilot converges on
    this same path with approved_by="system".

    `snapshot_provider(application_id) -> dict` optionally supplies the per-row
    integrity fields (application_hash / resume_version_id / answers_fingerprint /
    policy_version / consent_version); a build failure degrades to an empty
    snapshot (the approval still records who/when).

    Returns the DETAILED outcome so a caller (the in-app approve route) can tell
    "approved" from "over quota" from "not approvable" — three outcomes the bare
    count collapses into 0:

        {"approved": int, "approved_ids": [...],
         "over_quota": [...], "usage": {...},     # G reservation denial
         "not_approvable": [...]}                 # wrong state / lost race

    (G: the auto_submit entitlement is reserved here, right before the CAS, keyed
    on application_id + snapshot, so a redelivery reuses the reservation.)
    """
    from pymongo import ReturnDocument

    user_email = _require_email(user_email)
    ids = _clean_id_list(application_ids)
    result: Dict[str, Any] = {"approved": 0, "approved_ids": [],
                              "over_quota": [], "usage": {}, "not_approvable": []}
    if not ids:
        return result
    approvable = list(_APPROVABLE_STATUSES) + list(_APPROVABLE_LEGACY)
    moved = 0
    for aid in ids:
        snap_fields: Dict[str, Any] = {}
        if snapshot_provider is not None:
            try:
                snap_fields = snapshot_provider(aid) or {}
            except Exception as e:  # never let snapshot-building block approval
                logger.warning("approval snapshot build failed for %s: %s",
                               str(aid)[:12], type(e).__name__)
                snap_fields = {}
        snapshot = _build_approval_snapshot(snap_fields, approved_by)

        # G HOOK: reserve the auto_submit entitlement BEFORE the CAS, keyed on
        # application_id + snapshot fingerprint (a redelivery/duplicate approve of
        # the SAME snapshot reuses the reservation — never double-reserves). An
        # over-cap reservation means this row is NOT approved (the route maps the
        # miss to 429, never 401). Dark-launch gated (AUTO_APPLY_RESERVE, OFF by
        # default) so existing approval flows are byte-identical until flipped.
        reservation: Optional[Dict[str, Any]] = None
        if _reserve_enabled():
            try:
                from services import quota_ledger_service as quota
                res_key = _auto_submit_idempotency_key(aid, snapshot)
                res = quota.reserve(user_email, _AUTO_SUBMIT_FEATURE, n=1,
                                    idempotency_key=res_key)
            except Exception as e:  # pragma: no cover - reservation infra failure
                logger.error("auto_submit reserve failed for %s: %s",
                             str(aid)[:12], type(e).__name__)
                res = {"reserved": False}
            if not res.get("reserved"):
                # Over the daily auto_submit cap — do NOT approve (and do NOT
                # transition) this row.
                _audit(user_email, "application_approve_over_quota",
                       application_id=aid, meta={"feature": _AUTO_SUBMIT_FEATURE})
                result["over_quota"].append(aid)
                result["usage"] = res.get("usage") or result["usage"]
                continue
            reservation = {"feature": _AUTO_SUBMIT_FEATURE,
                           "entry_ids": res.get("entry_ids") or [],
                           "idempotency_key": res_key}

        set_doc: Dict[str, Any] = {
            "status": STATUS_APPROVED, "needs_input": [],
            "approval_snapshot": snapshot, "updated_at": _now(),
        }
        if reservation is not None:
            set_doc["auto_submit_reservation"] = reservation
        updated = _applications().find_one_and_update(
            {"user_email": user_email, "application_id": aid,
             "status": {"$in": approvable}},
            {"$set": set_doc},
            return_document=ReturnDocument.AFTER,
        )
        # CAS miss with a fresh reservation in hand: the row was already approved
        # (a duplicate — the live row holds the SAME idempotency_key'd entries, so
        # we must NOT refund) or slipped out of an approvable state. Either way the
        # reservation is idempotency-keyed, so a later approve of the same snapshot
        # reuses it — no double charge; we intentionally do not refund here.
        if updated is not None:
            moved += 1
            result["approved_ids"].append(aid)
            _audit(user_email, "application_approved", application_id=aid,
                   meta={"approved_by": snapshot["approved_by"],
                         "has_snapshot": any(
                             snapshot.get(k) for k in
                             ("application_hash", "resume_version_id",
                              "answers_fingerprint"))})
        else:
            result["not_approvable"].append(aid)
    result["approved"] = moved
    return result


def approve(user_email: str, application_ids: Any, *, approved_by: str = "user",
            snapshot_provider: Any = None) -> int:
    """Count-returning face of `approve_with_result` (the historical signature —
    every existing caller/test uses this). Semantics are unchanged: the number of
    rows moved `ready_for_review` → `approved` with a frozen snapshot."""
    return approve_with_result(user_email, application_ids,
                               approved_by=approved_by,
                               snapshot_provider=snapshot_provider)["approved"]


# Back-compat alias for the pre-F name. NOTE the semantics changed under F: it now
# approves ONLY ready_for_review rows (→ approved, snapshot frozen), not the old
# queued/gathering/needs_input → ready. Retained so any straggling caller keeps
# resolving; new code calls `approve`.
def approve_to_ready(user_email: str, application_ids: Any) -> int:
    return approve(user_email, application_ids, approved_by="user")


def get_approval_snapshot(user_email: str,
                          application_id: str) -> Optional[Dict[str, Any]]:
    """The frozen approval snapshot for a row (owner-scoped), or None. Read at
    submit time to re-validate the approval before the executor acts (F)."""
    user_email = _require_email(user_email)
    application_id = _clean_str(application_id, 64)
    if not application_id:
        return None
    rec = _applications().find_one(
        {"application_id": application_id, "user_email": user_email},
        {"_id": 0, "approval_snapshot": 1})
    return (rec or {}).get("approval_snapshot")


def approval_snapshots(user_email: str,
                       application_ids: Any) -> Dict[str, Dict[str, Any]]:
    """Bulk peer of `get_approval_snapshot`: {application_id -> frozen snapshot}
    for the listed rows, owner-scoped, in ONE query. Rows without a snapshot are
    absent from the map. Used by the approval-fold payload (which needs
    `approved_at` to derive the undo window for a whole page of rows without N
    round trips). Best-effort: any read failure degrades to an empty map."""
    user_email = _require_email(user_email)
    ids = _clean_id_list(application_ids)
    if not ids:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    try:
        cursor = _applications().find(
            {"user_email": user_email, "application_id": {"$in": ids}},
            {"_id": 0, "application_id": 1, "approval_snapshot": 1})
        for rec in cursor:
            snap = rec.get("approval_snapshot")
            if isinstance(snap, dict) and rec.get("application_id"):
                out[rec["application_id"]] = snap
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("approval_snapshots read failed for %s: %s", user_email, e)
    return out


def user_skip(user_email: str, application_ids: Any) -> int:
    """§2 `skip`: move the user's listed pre-submit rows to `skipped` — the
    user-initiated "stop nudging" terminal state (dedup_gate treats `skipped` as
    already-handled, so the posting is not re-offered). Idempotent. Returns the
    number moved."""
    user_email = _require_email(user_email)
    ids = _clean_id_list(application_ids)
    if not ids:
        return 0
    res = _applications().update_many(
        {"user_email": user_email, "application_id": {"$in": ids},
         "status": {"$in": list(_SKIPPABLE_STATUSES)}},
        {"$set": {"status": STATUS_SKIPPED, "updated_at": _now()}},
    )
    moved = getattr(res, "modified_count", 0) or 0
    if moved:
        _audit(user_email, "application_user_skipped",
               meta={"requested": len(ids), "moved": moved})
    return moved


# ---------------------------------------------------------------------------
# Reads (always user-scoped, always projected)
# ---------------------------------------------------------------------------
def get_application(user_email: str, application_id: str) -> Optional[Dict[str, Any]]:
    """One row, owner-scoped. None when it doesn't exist for this user."""
    user_email = _require_email(user_email)
    application_id = _clean_str(application_id, 64)
    if not application_id:
        return None
    rec = _applications().find_one(
        {"application_id": application_id, "user_email": user_email}, {"_id": 0}
    )
    return _view(rec) if rec else None


def list_applications(user_email: str, *, status: Optional[str] = None,
                      limit: int = 50) -> List[Dict[str, Any]]:
    """The user's ledger, newest first. `status` (validated against the enum)
    narrows to one state — served by the (user_email, status) index; the
    unfiltered list is served by the unique (user_email, job_identity) index's
    user_email prefix."""
    user_email = _require_email(user_email)
    query: Dict[str, Any] = {"user_email": user_email}
    if status is not None:
        if status not in ALL_STATUSES:
            raise InvalidApplicationInput(f"unknown status: {status}")
        query["status"] = status
    limit = max(1, min(int(limit or 50), 200))
    cursor = (
        _applications().find(query, {"_id": 0})
        .sort("created_at", -1)
        .limit(limit)
    )
    return [_view(rec) for rec in cursor]


# ---------------------------------------------------------------------------
# E3 (defect E) — the canonical-preferred DUAL-READ map for the funnel/kanban/
# today_inbox readers. {tailoring_record_id -> outcome_view bucket} for every
# canonical `applications` row this user owns that links a tailoring record. A
# reader PREFERS this bucket over `tailoring_records.application.status` when a
# canonical row exists for the record, and falls back to the tailoring_records
# mirror otherwise.
# ---------------------------------------------------------------------------
def outcome_buckets_for_records(user_email: str) -> Dict[str, str]:
    """Owner-scoped {tailoring_record_id -> coarse funnel bucket} projection of
    the canonical ledger, for the E3 dual-read. Best-effort: an empty map (any
    error, or no canonical rows) degrades cleanly to the tailoring_records
    fallback, so a read surface never 500s on the dual-read.
    """
    try:
        user_email = _require_email(user_email)
    except InvalidApplicationInput:
        return {}
    out: Dict[str, str] = {}
    try:
        cursor = _applications().find(
            {"user_email": user_email,
             "tailoring_record_id": {"$nin": [None, ""]}},
            {"_id": 0, "tailoring_record_id": 1, "outcome": 1, "status": 1},
        )
        for row in cursor:
            rid = row.get("tailoring_record_id")
            if rid:
                out[rid] = outcome_view(row)
    except Exception as e:  # pragma: no cover - dual-read is best-effort
        logger.warning("outcome_buckets_for_records failed for %s: %s",
                       user_email, e)
    return out


# ---------------------------------------------------------------------------
# Index management — call from scripts/migrate_indexes.py, NEVER at import time
# (keeps cold starts lean). Idempotent (create_index is a no-op by name).
# ---------------------------------------------------------------------------
def ensure_indexes() -> None:
    """Create the `applications` ledger indexes. Mirrors the specs in
    scripts/migrate_indexes.py (matching names/options) so a runtime ensure and
    the migration are a clean no-op re-assert of each other. Safe to run
    repeatedly.
    """
    coll = _applications()
    coll.create_index([("user_email", 1), ("job_identity", 1)],
                      unique=True, name="user_email_job_identity_unique")
    coll.create_index([("user_email", 1), ("status", 1)], name="user_email_status")
    coll.create_index([("posting_closes_at", 1)], sparse=True,
                      name="posting_closes_at_sparse")
    coll.create_index([("application_id", 1)], unique=True,
                      name="application_id_unique")
    # E1 upsert race guard (defect E): set_outcome find-or-creates the canonical
    # row on (user_email, tailoring_record_id). Unique PARTIAL (NOT plain sparse):
    # a compound sparse index still indexes every doc carrying ANY keyed field
    # (all rows have user_email), so a null tailoring_record_id would collide — the
    # partial filter limits uniqueness to rows that actually link a tailored
    # résumé, which is exactly the set two concurrent signals could duplicate.
    coll.create_index([("user_email", 1), ("tailoring_record_id", 1)],
                      unique=True, name="user_email_tailoring_record_id_unique",
                      partialFilterExpression={"tailoring_record_id": {"$type": "string"}})
