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

ALL_STATUSES: Tuple[str, ...] = ACTIVE_STATUSES + RETRYABLE_STATUSES

# Live (non-terminal) states — the only ones that may fall to `failed`.
_LIVE_STATUSES: Tuple[str, ...] = (
    STATUS_QUEUED, STATUS_GATHERING, STATUS_NEEDS_INPUT, STATUS_READY,
    STATUS_SUBMITTING, STATUS_SUBMITTED,
)

# target status -> statuses it may be entered FROM (the compare-and-set guard).
_TRANSITION_GUARDS: Dict[str, Tuple[str, ...]] = {
    STATUS_GATHERING: (STATUS_QUEUED,),
    # entered from gather (novel question), or re-entered when a submit run
    # blocks on something only the user can answer (one-tap resume loop).
    STATUS_NEEDS_INPUT: (STATUS_GATHERING, STATUS_READY, STATUS_SUBMITTING),
    # gathering → ready directly when all answers are known (auto path).
    STATUS_READY: (STATUS_GATHERING, STATUS_NEEDS_INPUT),
    STATUS_SUBMITTING: (STATUS_READY,),
    STATUS_SUBMITTED: (STATUS_SUBMITTING,),
    STATUS_CONFIRMED: (STATUS_SUBMITTED,),
    STATUS_FAILED: _LIVE_STATUSES,          # any live state → failed
    # a row discovered mid-gather to collide with another ledger row.
    STATUS_SKIPPED_DUPLICATE: (STATUS_QUEUED, STATUS_GATHERING),
    # user-initiated skip (§2 email `skip`): from any pre-submit live state.
    STATUS_SKIPPED: (STATUS_QUEUED, STATUS_GATHERING, STATUS_NEEDS_INPUT, STATUS_READY),
    STATUS_QUEUED: RETRYABLE_STATUSES,      # explicit retry only
}

# States the §2 email approve loop may move to `ready` (the submit-sweep pickup
# state). In Tranche 1 rows sit in `queued` (no gather job yet), so `queued` is
# included alongside gathering/needs_input.
_APPROVABLE_STATUSES: Tuple[str, ...] = (
    STATUS_QUEUED, STATUS_GATHERING, STATUS_NEEDS_INPUT,
)
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
    elif to_status == STATUS_NEEDS_INPUT:
        set_fields["needs_input"] = _sanitize_needs_input(needs_input)
    elif to_status == STATUS_READY:
        set_fields["needs_input"] = []   # resolved by the one-tap loop
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


def mark_ready(user_email: str, application_id: str, *,
               autonomy: Optional[str] = None,
               confidence_score: Optional[float] = None,
               tailoring_record_id: Optional[str] = None) -> Dict[str, Any]:
    return transition(user_email, application_id, STATUS_READY,
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


def requeue(user_email: str, application_id: str) -> Dict[str, Any]:
    """Explicit retry: failed/skipped_duplicate → queued (the ONLY way back in)."""
    return transition(user_email, application_id, STATUS_QUEUED)


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


def approve_to_ready(user_email: str, application_ids: Any) -> int:
    """§2 `approve` / `approve_all` / `approve_one`: move the user's listed rows
    still awaiting go-ahead (queued/gathering/needs_input) to `ready` — the
    state the Tranche 2 submit sweep picks up. Idempotent (rows already
    ready/submitted/skipped are left untouched). Returns the number moved."""
    user_email = _require_email(user_email)
    ids = _clean_id_list(application_ids)
    if not ids:
        return 0
    res = _applications().update_many(
        {"user_email": user_email, "application_id": {"$in": ids},
         "status": {"$in": list(_APPROVABLE_STATUSES)}},
        {"$set": {"status": STATUS_READY, "needs_input": [], "updated_at": _now()}},
    )
    moved = getattr(res, "modified_count", 0) or 0
    if moved:
        _audit(user_email, "application_approved_ready",
               meta={"requested": len(ids), "moved": moved})
    return moved


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
