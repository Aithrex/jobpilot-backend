"""Guest Onboarding service v2 — sessions, quota subjects, matching, downloads, claim.

Backs the guest blueprints (resume Lambda `/api/resume/guest/*`, jobs Lambda
`/api/jobs/guest/matches`) and the guest async-job branches in
services.resume_service._process_job. Lives in the shared layer because the
guest domain spans BOTH the resume and jobs Lambdas (same rationale as
job_service/resume_service — see the coupling note in CLAUDE.md).

v2 key properties (docs/specs/guest-onboarding.md v2):
  * guest_id — uuid4 CAPABILITY token: validated as UUID before ANY Mongo
    filter; generic 404 on mismatch (no existence oracle). Grants artifact
    access only.
  * quota_subject_id — server-only pseudonymous uuid per abuse/quota cluster
    (collection `guest_quota_subjects`). Used ONLY for quota counters, the
    single-trial reservation slot, the same-day match cache, and claim
    carry-in. NEVER returned to a client; grants no artifact access.
  * The single guest tailor is an ATOMIC single-reservation slot on the
    subject (find_one_and_update) — two concurrent tailors → one reservation;
    a same-match retry is idempotent ({existing:true}); a failed tailor
    releases the slot so infra errors never cheat the guest.
  * Download capability tokens: versioned HMAC over record_id|guest_id|format|exp
    with the dedicated GUEST_DOWNLOAD_TOKEN_SECRET (fail closed; never a 401).
  * Global per-day circuit breakers on anonymous spend (atomic $inc).
  * No resume PII or raw tokens in logs — guest_ids/counters only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import inspect
import logging
import os
import re
import secrets as _pysecrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from utils.config import _get_config_value
from utils.db_connect import DBConnect
from utils.security import InputSanitizer

from services import guest_identity_service as identity
from services import platform_config_service as pconfig

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_MATCH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DEVICE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")

_FLAG_TRUE = {"1", "true", "yes", "on"}
_FLAG_FALSE = {"0", "false", "no", "off"}

DOWNLOAD_TOKEN_TTL_SECONDS = 15 * 60
DOWNLOAD_FORMATS = ("pdf", "docx")
_DOWNLOAD_TOKEN_VERSION = "v1"

# Whitelisted job-card fields returned to guests (no internal scoring debris).
# Card shape per resume-platform-v3 spec 1.4: title/company/location/source/
# posted/apply_link/url/logo_url (+ match_score/matched_skills/gaps/why/
# sponsorship/match_id stamped below; days_ago/posted_text/seniority are
# additive v4 fields from the authed-parity scan).
_JOB_CARD_FIELDS = (
    "title", "company", "location", "apply_link", "url", "source",
    "posted", "posted_text", "days_ago", "logo_url",
)

# Matching latency budgets (v4: the guest scan runs the SAME three sources as
# the authed Job Opportunities feed — Workday catalog + career pages + MNC —
# in parallel inside ONE resume-Lambda invocation, so the wall clock is the
# slowest source, not the sum. 300s Lambda − scoring/enrich/store headroom.)
GUEST_MATCH_BUDGET_SECONDS = 210
GUEST_FETCH_BUDGET_SECONDS = 165
GUEST_WORKDAY_TIMEOUT = 110
GUEST_SIDE_SOURCE_TIMEOUT = 90
# Post-fetch grace: scoring/ranking is pure CPU (regex) — it must NEVER be
# starved by a fetch that ate the wall clock (live incident: fetch 230s,
# scored=0, honest-but-wrong empty day). The deadline extends to at least
# fetch-end + this many seconds.
GUEST_SCORE_GRACE_SECONDS = 25
# Detail-enrichment cap passed to each feed service (their tab defaults
# fetch 150-700 JD pages — far beyond a guest scan's budget).
GUEST_ENRICH_TOP = 50
_ENRICH_MAX_CANDIDATES = 15
_ENRICH_PER_REQUEST_TIMEOUT = 6
_ENRICH_OVERALL_BUDGET = 12

# The scored pool persisted per snapshot: guests SEE the top guest_jobs_max
# (5); the rest exist as an honest locked count + power the instant seniority
# re-rank (no re-scan). jd_sources are stored for the top slice only so
# snapshot docs stay small (the Atlas 512MB incident taught us).
GUEST_POOL_MAX = 60
GUEST_JD_SOURCES_MAX = 25
_JD_TEXT_MAX = 12000

# Seniority views over the pool. A title with no seniority marker is
# "neutral" and shows in every view; markers EXCLUDE mismatched views.
GUEST_SENIORITY_LEVELS = ("entry", "mid", "senior")
_SENIOR_TITLE_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|architect|director|"
    r"head of|vp|vice president|distinguished|iii|iv)\b", re.IGNORECASE)
_ENTRY_TITLE_RE = re.compile(
    r"\b(intern(ship)?|co[- ]?op|new ?grad(uate)?|entry[- ]?level|junior|"
    r"jr\.?|graduate|early career|apprentice)\b|\bi\b$", re.IGNORECASE)
_MID_TITLE_RE = re.compile(r"\b(mid[- ]?level|ii)\b", re.IGNORECASE)

# Unclaimed snapshots are purged 7 days out (claim extends to null).
SNAPSHOT_RETENTION_DAYS = 7


# ---------------------------------------------------------------------------
# Guest freshness contract (server-authoritative)
# ---------------------------------------------------------------------------
# The feeds run a 30-day window (workday_jobs_service.WINDOW_DAYS). That is the
# right window for an authed user browsing a whole tab; it is the WRONG window
# for a guest's first impression, where a 26-day-old req reads as a dead lead
# (live incident: a 26-day-old Workday row was served to an anonymous guest).
#
# The guest window is enforced HERE, once, so it holds no matter which feed a
# row came from and no matter how a feed derived its date. All three rules
# fail CLOSED:
#   * unknown posted date -> DROP. We never substitute now() for a missing
#     date; that would turn "we do not know" into "posted today".
#   * age > GUEST_MAX_AGE_DAYS -> DROP.
#   * an explicit closed/inactive marker -> DROP.
# Applied immediately after fetch (before scoring) AND on every snapshot read,
# so a snapshot written this morning can never serve a row that has since aged
# out. Fewer than guest_jobs_max survivors is a SUCCESS, never padded.


def _env_int(name: str, default: int) -> int:
    """Optional env override for a guest knob (same contract as
    workday_jobs_service._env_int; redefined locally so importing
    guest_service never drags in the workday fan-out module at import time)."""
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


GUEST_MAX_AGE_DAYS = _env_int("GUEST_MAX_AGE_DAYS", 7)

# Explicit "this req is not live" markers seen across the feeds. Only an
# EXPLICIT marker rejects: an absent status field is not evidence of closure.
_CLOSED_STATUS_VALUES = frozenset({
    "closed", "close", "inactive", "expired", "filled", "archived",
    "cancelled", "canceled", "removed", "withdrawn", "draft", "unpublished",
    "not_active", "notactive", "deleted",
})
_STATUS_FIELDS = ("status", "job_status", "state", "posting_status")
# Truthy => open. A present-but-falsey value rejects.
_OPEN_FLAG_FIELDS = ("active", "is_active", "is_open", "is_live")
# Truthy => closed.
_CLOSED_FLAG_FIELDS = ("closed", "is_closed", "expired", "is_expired",
                       "archived", "is_archived")


def _row_days_ago(row: Dict[str, Any]) -> Optional[int]:
    """The row's own calendar age in days, or None when it is UNKNOWN.

    Only the feed-computed `days_ago` counts. A missing/unparseable value is
    unknown — it is never derived from now(), and never defaulted to 0."""
    value = row.get("days_ago")
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_is_open(row: Dict[str, Any]) -> bool:
    """False only when the row carries an EXPLICIT closed/inactive marker."""
    for field in _OPEN_FLAG_FIELDS:
        if field in row and row.get(field) is not None and not row.get(field):
            return False
    for field in _CLOSED_FLAG_FIELDS:
        if row.get(field):
            return False
    for field in _STATUS_FIELDS:
        value = row.get(field)
        if isinstance(value, str) and value.strip().lower().replace(
            " ", "_"
        ) in _CLOSED_STATUS_VALUES:
            return False
    return True


def filter_fresh_for_guest(
    rows: Optional[List[Dict[str, Any]]],
    *,
    max_age_days: Optional[int] = None,
    extra_age_days: int = 0,
) -> List[Dict[str, Any]]:
    """Drop every row a guest must not see: unknown date, too old, or closed.

    ``extra_age_days`` ages rows that were dated at some point in the PAST
    (a stored snapshot): a card that was 6 days old when the snapshot was
    written is 8 days old two days later and must stop being served."""
    try:
        limit = int(GUEST_MAX_AGE_DAYS if max_age_days is None else max_age_days)
    except (TypeError, ValueError):
        limit = GUEST_MAX_AGE_DAYS
    try:
        extra = max(0, int(extra_age_days))
    except (TypeError, ValueError):
        extra = 0
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        days = _row_days_ago(row)
        if days is None:            # unknown posted date — never guessed
            continue
        if days + extra > limit:
            continue
        if not _row_is_open(row):
            continue
        out.append(row)
    return out


def _snapshot_extra_age_days(snapshot: Dict[str, Any]) -> int:
    """Calendar days elapsed since the snapshot's own date — the amount every
    stored card has aged since it was scored."""
    raw = str((snapshot or {}).get("date") or "")
    try:
        snap_date = datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return 0
    return max(0, (_now().date() - snap_date).days)


def _freshen_snapshot(snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Re-apply the guest freshness contract to a STORED snapshot on read.

    A snapshot is a cache, not a licence: rows that have aged out (or were
    written by an older, laxer code path) are dropped here, so a cache hit can
    never serve something a fresh scan would reject."""
    if not snapshot:
        return snapshot
    extra = _snapshot_extra_age_days(snapshot)
    out = dict(snapshot)
    for key in ("jobs", "pool"):
        rows = out.get(key)
        if isinstance(rows, list):
            out[key] = filter_fresh_for_guest(rows, extra_age_days=extra)
    return out


def _clock() -> float:
    """Monotonic clock seam — tests monkeypatch this to drive the 75s
    wall-clock budget deterministically."""
    return time.monotonic()


def _db():
    return DBConnect().get_db()


def _sessions():
    return _db().guest_sessions


def _subjects():
    return _db().guest_quota_subjects


def _snapshots():
    """job_match_snapshots — guest matches live here (spec 1.4), OUT of
    guest_sessions/guest_quota_subjects."""
    return _db().job_match_snapshots


def _now():
    return datetime.now(timezone.utc)


def _today_key() -> str:
    return _now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Feature flag (env kill-switch AND runtime admin knob — both must be on)
# ---------------------------------------------------------------------------

def guest_onboarding_env_enabled() -> bool:
    """`GUEST_ONBOARDING_ENABLED` — same resolution pattern as
    `GMAIL_OAUTH_ENABLED` (gmail_service.gmail_oauth_enabled): explicit value
    wins; unset → ON in dev, OFF in prod."""
    raw = (_get_config_value("GUEST_ONBOARDING_ENABLED", "") or "").strip().lower()
    if raw in _FLAG_TRUE:
        return True
    if raw in _FLAG_FALSE:
        return False
    return os.getenv("ENVIRONMENT", "development").strip().lower() == "development"


def guest_onboarding_enabled() -> bool:
    """Effective gate: env flag AND the admin runtime `enabled` knob."""
    if not guest_onboarding_env_enabled():
        return False
    return pconfig.get_bool("enabled", True)


# ---------------------------------------------------------------------------
# Validation / hashing helpers
# ---------------------------------------------------------------------------

def validate_guest_id(guest_id: Any) -> Optional[str]:
    """Return the normalized (lowercase) uuid4 string, or None if invalid.

    MUST be called before a guest_id reaches any Mongo filter (spec §Security 1).
    """
    if not guest_id or not isinstance(guest_id, str):
        return None
    candidate = InputSanitizer.sanitize_string(guest_id, max_length=64).lower()
    return candidate if _UUID_RE.match(candidate) else None


def validate_match_id(match_id: Any) -> Optional[str]:
    """match_id is an opaque server-minted uuid4().hex — 32 lowercase hex chars.
    Format-validated before it reaches any Mongo filter or cache lookup."""
    if not match_id or not isinstance(match_id, str):
        return None
    candidate = InputSanitizer.sanitize_string(match_id, max_length=64).lower()
    return candidate if _MATCH_ID_RE.match(candidate) else None


def validate_device_token(token: Any) -> Optional[str]:
    """Server-issued device tokens are urlsafe-base64 strings. Charset/length
    validated; anything else is dropped (never an error — it's a weak signal)."""
    if not token or not isinstance(token, str):
        return None
    candidate = InputSanitizer.sanitize_string(token, max_length=256)
    return candidate if _DEVICE_TOKEN_RE.match(candidate) else None


def mint_device_token() -> str:
    """Opaque random device token (>=32 bytes, urlsafe). The server stores
    ONLY sha256(token) — an anti-abuse signal, never a capability."""
    return _pysecrets.token_urlsafe(32)


def hash_device_token(token: Any) -> Optional[str]:
    t = validate_device_token(token)
    if not t:
        return None
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


_ip_salt_warned = False


def hash_ip(ip: str) -> Optional[str]:
    """sha256(ip + server salt) — stored for abuse correlation, never raw IP.

    The salt MUST come from config (`GUEST_IP_HASH_SALT` — SSM in prod, .env
    locally; see docs/ops/manual-steps.md). With no salt configured we fail CLOSED for
    storage: return None (ip_hash stored as null) rather than hash with a
    known constant an attacker could brute-force the IPv4 space against.
    Logged once per process, no PII."""
    global _ip_salt_warned
    salt = (_get_config_value("GUEST_IP_HASH_SALT", "") or "").strip()
    if not salt:
        if not _ip_salt_warned:
            logger.warning(
                "GUEST_IP_HASH_SALT not configured — guest ip_hash storage "
                "disabled (set the secret to enable IP-abuse correlation)"
            )
            _ip_salt_warned = True
        return None
    return hashlib.sha256(f"{ip or ''}|{salt}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

def get_session(guest_id: str) -> Optional[Dict[str, Any]]:
    """Load the session doc, hydrating the in-memory ``resume`` view from the
    referenced resume_assets doc (v3: sessions store only the resume_id ref;
    readers keep seeing the legacy embedded shape until Phase C rewires them).
    """
    gid = validate_guest_id(guest_id)
    if not gid:
        return None
    doc = _sessions().find_one({"guest_id": gid})
    if doc and not doc.get("resume") and doc.get("resume_id"):
        try:
            from services import resume_access

            asset = resume_access.get_asset(doc["resume_id"])
            if asset and asset.get("structured"):
                doc["resume"] = {
                    "filename": asset.get("filename") or "",
                    "structured": asset["structured"],
                    "raw_text": asset.get("raw_text") or "",
                    "uploaded_at": asset.get("parsed_at") or asset.get("created_at"),
                }
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("guest session resume hydration failed: %s", type(e).__name__)
    return doc


def create_or_get_session(
    guest_id: Optional[str],
    fingerprint_hash: Optional[str],
    ip_hash: Optional[str],
    device_token_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the existing session for a valid guest_id, else create a new one.

    An unknown-but-valid-format guest_id gets a FRESH id (capability tokens
    are minted server-side only; we never persist client-invented ids).
    """
    fp = identity.sanitize_fingerprint(fingerprint_hash)
    gid = validate_guest_id(guest_id or "")
    if gid:
        existing = _sessions().find_one({"guest_id": gid})
        if existing:
            update: Dict[str, Any] = {"$set": {"last_seen_at": _now()}}
            if fp and not existing.get("fingerprint_hash"):
                update["$set"]["fingerprint_hash"] = fp
            if device_token_hash and not existing.get("device_token_hash"):
                update["$set"]["device_token_hash"] = str(device_token_hash)
            _sessions().update_one({"guest_id": gid}, update)
            existing.update(update["$set"])
            return existing

    new_id = str(uuid.uuid4())
    doc = {
        "guest_id": new_id,
        "created_at": _now(),
        "last_seen_at": _now(),
        "quota_subject_id": str(uuid.uuid4()),  # server-only; never sent to clients
        "fingerprint_hash": fp or None,
        "device_token_hash": device_token_hash or None,
        "ip_hash": ip_hash or None,  # null when GUEST_IP_HASH_SALT is unset
        "identity": {
            "name_normalized": "",
            "email_hmacs": [],
            "phone_hmacs": [],
            "resume_sig_hmacs": [],
            "sig_v": identity.SIGNAL_VERSION,
        },
        "resume": None,
        "resume_id": None,        # ref -> resume_assets (spec 1.7)
        "snapshot_id": None,      # ref -> job_match_snapshots (spec 1.4/1.7)
        "tailor_record_ids": [],
        "resume_file_s3_keys": [],  # legacy field kept for retention
        "merged_into": None,
        "claimed_by": None,
        "claimed_at": None,
        # Claim state machine (spec 1.7): unclaimed -> claim_pending ->
        # transferring -> completed; steps tick as each transfer lands.
        "claim": {
            "state": "unclaimed",
            "email": None,
            "started_at": None,
            "completed_at": None,
            "steps": {
                "records": False,
                "resume": False,
                "subject_link": False,
                "carryin": False,
            },
        },
    }
    _sessions().insert_one(doc)
    doc.pop("_id", None)
    logger.info("guest session created: %s", new_id)
    return doc


# ---------------------------------------------------------------------------
# Quota subjects (server-only; quota + trial + cache + claim carry-in)
# ---------------------------------------------------------------------------

def ensure_subject(subject_id: str) -> None:
    """Upsert the guest_quota_subjects doc for subject_id (atomic)."""
    _subjects().update_one(
        {"subject_id": str(subject_id)},
        {
            "$setOnInsert": {
                "created_at": _now(),
                "tailors_used": 0,
                "reservation": None,
                "linked_accounts": [],
            },
            "$set": {"last_seen_at": _now()},
        },
        upsert=True,
    )


def subject_for_guest(guest_id: str) -> Optional[str]:
    """Resolve the session's quota_subject_id (minting one for legacy docs),
    ensuring the subject doc exists. None for unknown sessions."""
    doc = get_session(guest_id)
    if not doc:
        return None
    sid = doc.get("quota_subject_id")
    if not sid:
        sid = str(uuid.uuid4())
        _sessions().update_one(
            {"guest_id": doc["guest_id"], "quota_subject_id": None},
            {"$set": {"quota_subject_id": sid}},
        )
        refreshed = _sessions().find_one({"guest_id": doc["guest_id"]}, {"quota_subject_id": 1})
        sid = (refreshed or {}).get("quota_subject_id") or sid
    ensure_subject(sid)
    return str(sid)


def unify_subjects(canonical_guest_id: str, newer_guest_id: str) -> None:
    """Merge the newer session's quota subject into the canonical session's
    subject (called from guest_identity_service.merge_sessions).

    Counters AGGREGATE onto the canonical subject ($inc, never negative) so a
    re-upload under a fresh guest_id can never reset the trial.
    """
    canonical_sid = subject_for_guest(canonical_guest_id)
    newer = get_session(newer_guest_id)
    if not canonical_sid or not newer:
        return
    newer_sid = newer.get("quota_subject_id")
    if not newer_sid or str(newer_sid) == canonical_sid:
        return
    newer_sid = str(newer_sid)

    # Re-point every session on the old subject at the canonical subject.
    _sessions().update_many(
        {"quota_subject_id": newer_sid},
        {"$set": {"quota_subject_id": canonical_sid}},
    )
    old = _subjects().find_one({"subject_id": newer_sid})
    if old:
        update: Dict[str, Any] = {"$set": {"last_seen_at": _now()}}
        carried = max(0, int(old.get("tailors_used") or 0))
        if carried:
            update["$inc"] = {"tailors_used": carried}
        linked = [e for e in (old.get("linked_accounts") or []) if isinstance(e, str) and e]
        if linked:
            update["$addToSet"] = {"linked_accounts": {"$each": linked}}
        _subjects().update_one({"subject_id": canonical_sid}, update)
        _subjects().update_one(
            {"subject_id": newer_sid},
            {"$set": {"merged_into": canonical_sid, "last_seen_at": _now()}},
        )


def attach_parsed_resume(
    guest_id: str,
    structured: Dict[str, Any],
    raw_text: str,
    filename: str,
    fingerprint_hash: Optional[str] = None,
    resume_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the parsed resume INTO the guest-owned resume_assets doc, store
    only the ``resume_id`` ref on the session, extract v2 HMAC identity
    signals and run strong-signal resolution/merge. Returns the recognition
    summary (this session's OWN parsed name only — never another session's).

    When ``resume_id`` is omitted (legacy/direct callers), a fileless
    guest-owned asset is created on the fly so the asset model stays uniform.
    """
    gid = validate_guest_id(guest_id)
    if not gid:
        raise ValueError("invalid guest_id")

    session = _sessions().find_one({"guest_id": gid})
    if not session:
        raise ValueError("guest session not found")

    from services import resume_access

    if not resume_id:
        resume_id = resume_access.create_pending_asset(
            owner_type="guest",
            guest_id=gid,
            quota_subject_id=session.get("quota_subject_id"),
            filename=filename or "",
            purge_after=_now() + timedelta(days=30),
        )
    resume_access.save_parsed_fields(
        resume_id, resume_access.build_parsed_fields(structured, raw_text)
    )

    signals = identity.extract_signals(structured, fingerprint_hash, raw_text)
    set_fields: Dict[str, Any] = {
        "last_seen_at": _now(),
        # v3: the session carries only the asset ref — parsed content lives
        # on resume_assets (get_session hydrates the legacy view for readers).
        "resume_id": resume_id,
        "identity.name_normalized": signals["name_normalized"],
        "identity.sig_v": identity.SIGNAL_VERSION,
    }
    if signals["fingerprint"]:
        set_fields["fingerprint_hash"] = signals["fingerprint"]
    add_to_set: Dict[str, Any] = {}
    if signals["email_hmacs"]:
        add_to_set["identity.email_hmacs"] = {"$each": signals["email_hmacs"]}
    if signals["phone_hmacs"]:
        add_to_set["identity.phone_hmacs"] = {"$each": signals["phone_hmacs"]}
    if signals["resume_sig_hmac"]:
        add_to_set["identity.resume_sig_hmacs"] = {"$each": [signals["resume_sig_hmac"]]}
    update: Dict[str, Any] = {"$set": set_fields}
    if add_to_set:
        update["$addToSet"] = add_to_set
    _sessions().update_one({"guest_id": gid}, update)

    recognition = identity.resolve_and_merge(
        gid, signals, device_token_hash=session.get("device_token_hash")
    )
    state = tailor_quota_state(gid)
    return {
        "recognized": bool(recognition.get("recognized")),
        "recognized_name": signals["name_normalized"],  # this session's own name
        "tailors_used": state["tailors_used"],
        "tailors_limit": state["tailors_limit"],
        "resume_id": resume_id,
    }


def session_summary(
    guest_id: Optional[str],
    fingerprint_hash: Optional[str],
    device_token: Optional[str] = None,
) -> Dict[str, Any]:
    """GET /guest/session payload (v2).

    * guest_id capability holder: full summary; `recognized_name` ONLY when
      the session has a parsed resume (and a name) — the owning-guest_id rule.
    * device/fingerprint-only hit: `{exists:false, recognized:true,
      claimed:false}` with NO name key (a guessable signal must never be a
      name or account-state disclosure oracle).
    """
    doc = get_session(guest_id) if guest_id else None
    if doc:
        state = tailor_quota_state(doc["guest_id"])
        cached = get_cached_matches(doc["guest_id"]) or {}
        matched_today = len(cached.get("jobs") or [])
        has_resume = bool((doc.get("resume") or {}).get("structured"))
        out: Dict[str, Any] = {
            "exists": True,
            "recognized": False,
            "tailors_used": state["tailors_used"],
            "tailors_limit": state["tailors_limit"],
            "has_resume": has_resume,
            "matched_jobs_today": matched_today,
            "claimed": bool(doc.get("claimed_by")),
        }
        if has_resume:
            name = (doc.get("identity") or {}).get("name_normalized") or ""
            if name:
                out["recognized_name"] = name
        return out

    # Unknown guest_id — device-signal-only recognition ("welcome back"
    # banner). Never links quota, never reveals a name or claim state.
    ors: List[Dict[str, Any]] = []
    fp = identity.sanitize_fingerprint(fingerprint_hash)
    if fp:
        ors.append({"fingerprint_hash": fp})
    dth = hash_device_token(device_token)
    if dth:
        ors.append({"device_token_hash": dth})
    if ors:
        query: Dict[str, Any] = ors[0] if len(ors) == 1 else {"$or": ors}
        match = _sessions().find_one(query, {"guest_id": 1})
        if match:
            return {"exists": False, "recognized": True, "claimed": False}
    return {"exists": False, "recognized": False}


# ---------------------------------------------------------------------------
# Quota — the atomic single-reservation slot on the quota subject
# ---------------------------------------------------------------------------

def tailor_quota_state(guest_id: str) -> Dict[str, Any]:
    limit = pconfig.get_int("guest_tailor_limit")
    used = 0
    sid = subject_for_guest(guest_id)
    if sid:
        doc = _subjects().find_one({"subject_id": sid}, {"tailors_used": 1}) or {}
        try:
            used = max(0, int(doc.get("tailors_used") or 0))
        except (TypeError, ValueError):
            used = 0
    return {
        "tailors_used": used,
        "tailors_limit": limit,
        "remaining": max(0, limit - used),
    }


def try_reserve_tailor(guest_id: str, match_id: str) -> Dict[str, Any]:
    """Atomically reserve the subject's single tailor slot for match_id.

    Exactly the spec'd find_one_and_update: filter requires an empty-or-failed
    reservation AND tailors_used < limit; the update installs the reservation
    and $incs the counter in the same atomic op. Two concurrent tailor calls →
    exactly one reservation.

    Returns one of:
      {"reserved": True,  "state": {...}}
      {"existing": True,  "job_id": str, "state": {...}}   (same-match retry)
      {"exhausted": True, "state": {...}}
    """
    from pymongo import ReturnDocument

    gid = validate_guest_id(guest_id)
    mid = validate_match_id(match_id)
    limit = pconfig.get_int("guest_tailor_limit")
    if not gid or not mid:
        return {"exhausted": True, "state": {"tailors_used": 0, "tailors_limit": limit, "remaining": 0}}
    sid = subject_for_guest(gid)
    if not sid:
        return {"exhausted": True, "state": {"tailors_used": 0, "tailors_limit": limit, "remaining": 0}}
    if limit <= 0:
        return {"exhausted": True, "state": tailor_quota_state(gid)}

    reserved = _subjects().find_one_and_update(
        {
            "subject_id": sid,
            "$or": [{"reservation": None}, {"reservation.status": "failed"}],
            "tailors_used": {"$lt": limit},
        },
        {
            "$set": {
                "reservation": {
                    "match_id": mid,
                    "guest_id": gid,
                    "job_id": None,
                    "record_id": None,
                    "status": "reserved",
                    "reserved_at": _now(),
                },
                "last_seen_at": _now(),
            },
            "$inc": {"tailors_used": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if reserved is not None:
        used = max(0, int(reserved.get("tailors_used") or 0))
        return {
            "reserved": True,
            "state": {
                "tailors_used": used,
                "tailors_limit": limit,
                "remaining": max(0, limit - used),
            },
        }

    # Lost the slot. Same-match retry against a live/completed reservation is
    # idempotent — hand back the existing job_id.
    doc = _subjects().find_one({"subject_id": sid}) or {}
    resv = doc.get("reservation") or {}
    if (
        resv.get("match_id") == mid
        and resv.get("status") in ("reserved", "completed")
        and resv.get("job_id")
    ):
        return {"existing": True, "job_id": str(resv["job_id"]), "state": tailor_quota_state(gid)}
    return {"exhausted": True, "state": tailor_quota_state(gid)}


def attach_reservation_job(guest_id: str, match_id: str, job_id: str) -> None:
    """Stamp the dispatched job_id onto the live reservation."""
    sid = subject_for_guest(guest_id)
    mid = validate_match_id(match_id)
    if not sid or not mid:
        return
    _subjects().update_one(
        {"subject_id": sid, "reservation.match_id": mid, "reservation.status": "reserved"},
        {"$set": {"reservation.job_id": str(job_id)}},
    )


def release_reservation(guest_id: str) -> None:
    """Pre-dispatch rollback: the job never left the building — clear the
    reservation and refund the counter (status-guarded so a double release
    can never drive the counter negative)."""
    sid = subject_for_guest(guest_id)
    if not sid:
        return
    _subjects().update_one(
        {"subject_id": sid, "reservation.status": "reserved"},
        {"$set": {"reservation": None}, "$inc": {"tailors_used": -1}},
    )


def complete_reservation(guest_id: str, job_id: str, record_id: Optional[str]) -> None:
    """Worker success: mark the reservation completed and stamp the record."""
    sid = subject_for_guest(guest_id)
    if not sid or not job_id:
        return
    _subjects().update_one(
        {"subject_id": sid, "reservation.job_id": str(job_id)},
        {"$set": {"reservation.status": "completed", "reservation.record_id": record_id}},
    )


def fail_reservation(guest_id: str, job_id: str) -> None:
    """Worker failure: keep the reservation object (status=failed, so the
    $or re-reserve clause matches) and refund the counter — an infra error
    must never consume the guest's single trial. Status-guarded: idempotent."""
    sid = subject_for_guest(guest_id)
    if not sid or not job_id:
        return
    _subjects().update_one(
        {"subject_id": sid, "reservation.job_id": str(job_id), "reservation.status": "reserved"},
        {"$set": {"reservation.status": "failed"}, "$inc": {"tailors_used": -1}},
    )


# ---------------------------------------------------------------------------
# Global per-day circuit breakers on anonymous spend
# ---------------------------------------------------------------------------

def _try_reserve_global(kind: str, limit_knob: str) -> bool:
    """Platform-wide per-day circuit breaker on anonymous spend (shared by
    tailor / parse / matches — one counter doc per kind per day).

    Atomic $inc on a per-day counter doc (concurrent-Lambda safe). Over the
    limit → the reservation is rolled back and False is returned. A limit of
    0 disables that guest activity outright.
    """
    limit = pconfig.get_int(limit_knob)
    if limit <= 0:
        return False
    key = f"{kind}:{_today_key()}"
    coll = _db().guest_global_counters
    from pymongo import ReturnDocument

    doc = coll.find_one_and_update(
        {"_id": key},
        {"$inc": {"count": 1}, "$setOnInsert": {"created_at": _now()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    count = int((doc or {}).get("count") or 0)
    if count > limit:
        coll.update_one({"_id": key}, {"$inc": {"count": -1}})
        logger.warning(
            "guest %s circuit breaker OPEN (daily count=%d limit=%d)", kind, count, limit
        )
        return False
    return True


def _release_global(kind: str) -> None:
    """Roll back one global reservation (dispatch failed after reserve)."""
    try:
        _db().guest_global_counters.update_one(
            {"_id": f"{kind}:{_today_key()}"}, {"$inc": {"count": -1}}
        )
    except Exception:  # pragma: no cover - best-effort rollback
        pass


def try_reserve_global_tailor() -> bool:
    return _try_reserve_global("guest_tailors", "global_daily_guest_tailors")


def release_global_tailor() -> None:
    _release_global("guest_tailors")


def try_reserve_global_parse() -> bool:
    """Global daily cap on anonymous resume PARSES (FLASH spend)."""
    return _try_reserve_global("guest_parses", "global_daily_guest_parses")


def release_global_parse() -> None:
    _release_global("guest_parses")


def try_reserve_global_matches() -> bool:
    """Global daily cap on anonymous job-match scrapes (cache misses only —
    a same-day cache hit must never consume the budget)."""
    return _try_reserve_global("guest_matches", "global_daily_guest_matches")


def release_global_matches() -> None:
    _release_global("guest_matches")


# ---------------------------------------------------------------------------
# Matches run reservation — once per (subject, date) (spec 3.7)
# ---------------------------------------------------------------------------

def reserve_matches_run(guest_id: str) -> Dict[str, Any]:
    """Atomically reserve today's matches run for the guest's quota subject.

    The global daily counter must be spent ONCE per (subject, date) — a retry
    after a failed run re-dispatches WITHOUT consuming it twice. Returns
    {"first": True} when this call won today's reservation (caller must then
    spend the global counter), else {"first": False, "job_id": <last job>}.
    """
    sid = subject_for_guest(guest_id)
    today = _today_key()
    if not sid:
        return {"first": True}  # degenerate: no subject -> legacy behavior
    won = _subjects().find_one_and_update(
        {"subject_id": sid, "matches_reserved": {"$ne": today}},
        {"$set": {
            "matches_reserved": today,
            "matches_job_id": None,
            "last_seen_at": _now(),
        }},
    )
    if won is not None:
        return {"first": True}
    doc = _subjects().find_one({"subject_id": sid}, {"matches_job_id": 1}) or {}
    jid = doc.get("matches_job_id")
    return {"first": False, "job_id": str(jid) if jid else None}


def attach_matches_job(guest_id: str, job_id: str) -> None:
    """Stamp the dispatched matches job onto today's reservation (called
    AFTER a successful dispatch so a dispatch failure never pins a dead job)."""
    sid = subject_for_guest(guest_id)
    if not sid or not job_id:
        return
    _subjects().update_one(
        {"subject_id": sid}, {"$set": {"matches_job_id": str(job_id)}}
    )


def release_matches_run(guest_id: str) -> None:
    """Pre-dispatch rollback of today's matches reservation. Guarded on
    matches_job_id being unset so a release can never orphan a live run."""
    sid = subject_for_guest(guest_id)
    if not sid:
        return
    _subjects().update_one(
        {"subject_id": sid, "matches_job_id": None},
        {"$set": {"matches_reserved": None}},
    )


# ---------------------------------------------------------------------------
# Guest async jobs (ride the resume_jobs backbone; ownership = guest_id)
# ---------------------------------------------------------------------------

def create_guest_job(job_type: str, payload: Dict[str, Any], guest_id: str) -> str:
    """Create a resume_jobs row owned by a guest (user_email='' + guest_id
    stamp) and return its job_id. Dispatch stays with ResumeService.invoke_async."""
    from services.resume_service import get_resume_service

    gid = validate_guest_id(guest_id)
    if not gid:
        raise ValueError("invalid guest_id")
    svc = get_resume_service()
    job_id = svc.create_job(job_type, payload, user_email="")
    svc.resume_jobs.update_one({"job_id": job_id}, {"$set": {"guest_id": gid}})
    return job_id


def get_guest_job(job_id: str, guest_id: str) -> Optional[Dict[str, Any]]:
    """Guest-scoped job poll — mirrors ResumeService.get_job's response shape
    (payload stripped) but requires the guest_id capability to match."""
    gid = validate_guest_id(guest_id)
    jid = validate_guest_id(job_id)  # job ids are uuid4 too
    if not gid or not jid:
        return None
    from services.resume_service import get_resume_service

    job = get_resume_service().resume_jobs.find_one({"job_id": jid, "guest_id": gid})
    if not job:
        return None
    job.pop("_id", None)
    job.pop("payload", None)
    return job


# ---------------------------------------------------------------------------
# Matched jobs (free direct sources only) + subject-level same-day cache
# ---------------------------------------------------------------------------

def _cache_doc_for_guest(guest_id: str) -> Optional[Dict[str, Any]]:
    """Resolve the guest's snapshot doc (spec 1.4): subject-level first
    (today's (quota_subject_id, date) snapshot), falling back to the
    session's snapshot_id ref (carries guests through a missing HMAC key —
    each session is then its own standalone subject anyway).

    Every return path runs through _freshen_snapshot: this is THE single read
    seam for stored guest matches, so the freshness contract is re-applied on
    every cache/snapshot read without each caller having to remember."""
    sid = subject_for_guest(guest_id)
    if sid:
        snap = _snapshots().find_one({"quota_subject_id": sid, "date": _today_key()})
        if snap:
            return _freshen_snapshot(snap)
    doc = get_session(guest_id) or {}
    snap_id = doc.get("snapshot_id")
    if snap_id and isinstance(snap_id, str):
        return _freshen_snapshot(_snapshots().find_one({"snapshot_id": snap_id}))
    return None


def get_cached_matches(
    guest_id: str, seniority: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Same-day cached matches for the guest's quota subject (never re-spends
    the global matches budget). Visible rows are HARD-CAPPED at
    guest_jobs_max; the ranked pool behind them powers the INSTANT seniority
    re-rank (no re-scan, no quota) and the honest locked count. jd_sources
    are SERVER-ONLY and never leave this module."""
    cached = _cache_doc_for_guest(guest_id)
    if not cached or cached.get("date") != _today_key():
        return None
    max_jobs = max(1, pconfig.get_int("guest_jobs_max"))
    computed_at = cached.get("computed_at")
    out: Dict[str, Any] = {
        "date": cached.get("date"),
        "computed_at": computed_at.isoformat() if hasattr(computed_at, "isoformat") else computed_at,
        "min_score": cached.get("min_score"),
        "cached": True,
    }
    pool = list(cached.get("pool") or [])
    if pool:
        # Re-applied at READ time: a pool cached before the policy shipped
        # (or before its seed changed) must not serve what it now excludes.
        from services import employer_policy as _ep
        pool, out["policy"] = _ep.apply([dict(c) for c in pool], _ep.default_prefs())
        _ep.strip_internal(pool)
        view = normalize_seniority(seniority) if seniority else (
            cached.get("default_seniority") or "any"
        )
        jobs, pool_total, counts = _pool_view(pool, view, max_jobs)
        applied = view
        # The stored default can be empty for legacy pools — same honest
        # fallback the compute path uses, only when nothing was requested.
        if not jobs and pool and seniority is None:
            jobs, pool_total, counts = _pool_view(pool, "any", max_jobs)
            applied = "any"
        out["jobs"] = jobs
        out["seniority"] = applied
        out["seniority_counts"] = counts
        out["pool_total"] = pool_total
        out["locked_count"] = max(0, pool_total - len(jobs))
        if isinstance(cached.get("detected_experience"), dict) and cached["detected_experience"]:
            out["detected_experience"] = cached["detected_experience"]
    else:
        # Legacy snapshot (pre-pool): serve exactly what was stored.
        out["jobs"] = list(cached.get("jobs") or [])[:max_jobs]
    report = cached.get("sources_report")
    if isinstance(report, dict) and report:
        out["sources_report"] = report
        if isinstance(report.get("considered"), int):
            out["total_considered"] = report["considered"]
    if cached.get("empty_reason") and not out["jobs"] and not pool:
        out["empty_reason"] = cached["empty_reason"]
    return out


def find_cached_match(guest_id: str, match_id: str) -> Optional[Dict[str, Any]]:
    """Resolve a served match_id to its server-stored JD source
    ({title, company, jd_text, url}) — the ONLY tailorable reference. The JD
    never comes from the client (SSRF/injection posture). Resolves via the
    job_match_snapshots doc (spec 1.4)."""
    mid = validate_match_id(match_id)
    if not mid:
        return None
    cached = _cache_doc_for_guest(guest_id)
    if not cached:
        return None
    sources = cached.get("jd_sources") or {}
    src = sources.get(mid)
    if isinstance(src, dict):
        return src
    # Defensive fallback: derive from the public card (no stored description).
    # Pool rows beyond the jd_sources slice are still tailorable this way —
    # the tailor service resolves the JD from the card's URL server-side.
    for card in list(cached.get("jobs") or []) + list(cached.get("pool") or []):
        if card.get("match_id") == mid:
            return {
                "title": card.get("title") or "",
                "company": card.get("company") or "",
                "jd_text": "",
                "url": card.get("url") or card.get("apply_link") or "",
            }
    return None


def _store_matches(
    guest_id: str,
    jobs: List[Dict[str, Any]],
    min_score: int,
    jd_sources: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    sources_report: Optional[Dict[str, Any]] = None,
    timings: Optional[Dict[str, Any]] = None,
    empty_reason: Optional[str] = None,
    pool: Optional[List[Dict[str, Any]]] = None,
    detected_experience: Optional[Dict[str, Any]] = None,
    default_seniority: Optional[str] = None,
) -> Optional[str]:
    """Create/replace today's job_match_snapshots doc for the guest's quota
    subject (spec 1.4) and stamp the snapshot_id ref on the session.

    One snapshot per (quota_subject_id, date) — a same-day recompute
    REPLACES the doc (its snapshot_id is preserved by $setOnInsert).
    Unclaimed snapshots carry purge_after = now + 7d; a claimed session's
    snapshot is retained (purge_after null)."""
    gid = validate_guest_id(guest_id)
    if not gid:
        return None
    sid = subject_for_guest(gid)
    if not sid:
        return None
    session = _sessions().find_one(
        {"guest_id": gid}, {"resume_id": 1, "claimed_by": 1}
    ) or {}
    now = _now()
    max_jobs = max(1, pconfig.get_int("guest_jobs_max"))
    claimed = bool(session.get("claimed_by"))
    doc: Dict[str, Any] = {
        "owner_type": "guest",
        "guest_id": gid,
        "quota_subject_id": sid,   # server-only, never exposed to clients
        "resume_id": session.get("resume_id"),
        "date": _today_key(),
        "computed_at": now,
        "jobs": list(jobs or [])[:max_jobs],
        # v4: the full ranked pool behind the visible 5 — powers the instant
        # seniority re-rank + the honest locked count. Descriptions are NOT
        # stored on pool cards (jd_sources carries the tailorable top slice).
        "pool": list(pool or [])[:GUEST_POOL_MAX],
        "detected_experience": detected_experience or {},
        "default_seniority": normalize_seniority(default_seniority),
        "jd_sources": jd_sources or {},   # server-only
        "sources_report": sources_report or {},
        "timings": timings or {},
        "empty_reason": empty_reason,
        "min_score": min_score,
        "purge_after": None if claimed else now + timedelta(days=SNAPSHOT_RETENTION_DAYS),
    }
    from pymongo import ReturnDocument

    new_snapshot_id = uuid.uuid4().hex
    try:
        res = _snapshots().find_one_and_update(
            {"quota_subject_id": sid, "date": doc["date"]},
            {"$set": doc, "$setOnInsert": {"snapshot_id": new_snapshot_id}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        snapshot_id = str((res or {}).get("snapshot_id") or new_snapshot_id)
    except TypeError:  # pragma: no cover - mongomock signature fallback
        _snapshots().update_one(
            {"quota_subject_id": sid, "date": doc["date"]},
            {"$set": doc, "$setOnInsert": {"snapshot_id": new_snapshot_id}},
            upsert=True,
        )
        found = _snapshots().find_one({"quota_subject_id": sid, "date": doc["date"]})
        snapshot_id = str((found or {}).get("snapshot_id") or new_snapshot_id)
    _sessions().update_one(
        {"guest_id": gid},
        {"$set": {"snapshot_id": snapshot_id, "last_seen_at": now}},
    )
    return snapshot_id


def _guest_role_terms(structured: Dict[str, Any]) -> List[str]:
    """Derive up to 3 target-role search terms from the guest's own resume.

    Returns [] when the resume yields no usable role evidence. It deliberately
    does NOT fall back to "software engineer".

    That fallback used to live here, and it read only experience[].title. So a
    nurse, an accountant, a teacher, or any new grad whose roles did not parse
    into that one field triggered a full software-engineering scan, whose
    results were then scored against their (non-tech) skills. The guest saw
    near-zero matches and an empty_reason blaming the market. The cause was us.

    Evidence is widened instead: experience titles, then the resume's own
    headline/title field, then the field of study. If all are empty the caller
    must say so honestly rather than guess a career.
    """
    terms: List[str] = []
    seen = set()

    def _add(raw: Any) -> None:
        if len(terms) >= 3:
            return
        text = InputSanitizer.sanitize_string(str(raw or ""), max_length=120).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            terms.append(text)

    for exp in (structured.get("experience") or []):
        _add(exp.get("title"))
        if len(terms) >= 3:
            return terms

    # No parsed work titles: the resume may still declare a role directly
    # (headline / target title), which is common on student and career-change
    # resumes where experience[] is empty or non-professional.
    for key in ("title", "job_title", "headline", "headline_title", "target_title"):
        _add(structured.get(key))
    if terms:
        return terms

    # Last resort: field of study. A degree names a domain, not a job, so this
    # is weaker evidence and stops at one term.
    for edu in (structured.get("education") or []):
        _add(edu.get("degree") or edu.get("field") or edu.get("coursework"))
        if terms:
            break

    return terms


def _role_title_filter(structured: Dict[str, Any]):
    """Resume-derived title-relevance predicate for the ATS fetcher boundary
    (spec 3.2). Replaces the tech-only _ATS_TECH_TITLE drop so non-tech
    resumes are not zeroed out; a tech resume keeps its current supply (the
    predicate is the UNION of the resume's role tokens and the tech default).
    Returns None (pipeline default) when the resume yields no usable tokens."""
    from services.daily_pipeline_service import (
        _default_title_filter, _significant_term_tokens,
    )

    tokens: List[str] = []
    seen = set()
    for term in _guest_role_terms(structured):
        for tok in _significant_term_tokens(term):
            if tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    if not tokens:
        return None
    patterns = [
        re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", re.IGNORECASE)
        for t in tokens
    ]

    def _relevant(title: str) -> bool:
        t = str(title or "")
        if any(p.search(t) for p in patterns):
            return True
        return _default_title_filter(t)

    return _relevant


def _fetch_workday_details(
    apply_urls: List[str], overall_budget: float = _ENRICH_OVERALL_BUDGET
) -> Dict[str, str]:
    """CXS job DETAIL descriptions for description-less Workday candidates
    (spec 3.1). Best effort; module-level so tests can patch it."""
    from services.workday_cxs_fetcher import fetch_job_details

    return fetch_job_details(
        apply_urls,
        per_request_timeout=_ENRICH_PER_REQUEST_TIMEOUT,
        overall_budget=overall_budget,
    )


def _fetch_free_jobs(
    structured: Dict[str, Any],
    progress_cb=None,
    *,
    resume_skills: Optional[List[str]] = None,
    resume_years: Optional[int] = None,
    tz_offset_minutes: int = 0,
):
    """Run the SAME three sources as the authed Job Opportunities feed —
    the Workday tenant catalog (~1000 tenants), the Career Pages catalog,
    and the MNC catalog — in PARALLEL threads inside this one Lambda, then
    merge. This replaced the thin run_pipeline scan (v4): guests now get
    exactly the logged-in feed's coverage; only the display is capped.

    Each service scores against the guest's parsed skills (passed through
    the payload since guests have no profile doc) and benefits from the
    services' shared 3h Mongo caches, so common titles are warm and fast.

    Returns (rows, sources_report) in the spec-1.4 shape. Module-level so
    tests can patch it (legacy patches returning a bare list still work)."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutTimeout, as_completed
    from services.job_service import guest_cost_context

    terms = _guest_role_terms(structured)
    if not terms:
        # No role evidence anywhere in the resume. Scanning ~1000 tenants for a
        # guessed career burns the whole budget to produce matches the guest
        # will not recognise, and the old "software engineer" default made that
        # failure look like a market result. Say what actually happened.
        logger.info("guest matches: no role terms derivable from resume, skipping scan")
        return [], {
            "ok": [], "failed": [], "timed_out": [], "considered": 0,
            "timings_ms": {}, "empty_reason": "role_undetermined",
        }
    skills = resume_skills if resume_skills is not None else _resume_skills(structured)
    base_payload = {
        "titles": terms,
        "industries": [],
        "companies": [],
        "location": "",
        "remote_only": False,
        "us_only": True,
        "force_refresh": False,
        "user_email": "",            # anonymous — services use the overrides below
        "resume_skills": skills,
        "resume_years": resume_years,
        "tz_offset_minutes": tz_offset_minutes,
    }

    def _run_workday():
        from services.workday_jobs_service import run_workday_jobs_search
        return run_workday_jobs_search({
            **base_payload,
            "overall_timeout": GUEST_WORKDAY_TIMEOUT,
            "enrich_top": GUEST_ENRICH_TOP,
        })

    def _run_career_pages():
        from services.career_pages_service import run_career_pages_search
        return run_career_pages_search({
            **base_payload,
            "overall_timeout": GUEST_SIDE_SOURCE_TIMEOUT,
            "enrich_top": GUEST_ENRICH_TOP,
        })

    def _run_mnc():
        from services.mnc_jobs_service import run_mnc_search
        return run_mnc_search({
            **base_payload,
            "overall_timeout": GUEST_SIDE_SOURCE_TIMEOUT,
            "enrich_top": GUEST_ENRICH_TOP,
            # cost_class gate: an anonymous guest must NEVER trigger metered
            # aggregator (RapidAPI/JSearch) calls. The MNC catalog's
            # engine == "aggregator" rows are cost_class "paid" and are
            # dropped BEFORE any fetch. This is not conditional on the key
            # being absent — the key IS configured in this deployment.
            "exclude_paid": True,
        })

    sources = {
        "workday": _run_workday,
        "career_pages": _run_career_pages,
        "mnc": _run_mnc,
    }
    rows: List[Dict[str, Any]] = []
    report: Dict[str, Any] = {"ok": [], "failed": [], "timed_out": [], "considered": 0}
    timings_ms: Dict[str, int] = {}
    started = _clock()
    done = 0
    # NO `with` block: executor __exit__ blocks on running futures, which let
    # one straggling source eat the WHOLE match budget past the as_completed
    # timeout (live incident: fetch 230s, zero scoring time). shutdown with
    # wait=False abandons stragglers instead — partial results, on time.
    pool = ThreadPoolExecutor(max_workers=len(sources))
    # Paid-source chokepoint. mnc_jobs_service already drops aggregator rows
    # before any fetch (exclude_paid=True), so this is defense in depth: it
    # makes job_service._search_jsearch_raw refuse outright if any future
    # caller reaches JSearch by another path from inside this fan-out.
    # A module-global flag, NOT a ContextVar — ThreadPoolExecutor does not
    # propagate contextvars to pool threads.
    #
    # The context must span the WHOLE fan-out, not just submit(): the flag is
    # process-global, so releasing it at submit time would clear it while the
    # pool threads are still mid-fetch. Stragglers abandoned by the
    # shutdown(wait=False) below can still outlive it — exclude_paid remains
    # the load-bearing guarantee, and this is the second line.
    guest_cost = guest_cost_context()
    guest_cost.__enter__()
    futures = {pool.submit(fn): name for name, fn in sources.items()}
    try:
        for fut in as_completed(futures, timeout=GUEST_FETCH_BUDGET_SECONDS):
            name = futures[fut]
            done += 1
            try:
                result = fut.result() or {}
                jobs = list(result.get("jobs") or [])
                for j in jobs:
                    j["_guest_source"] = name
                rows.extend(jobs)
                report["ok"].append(name)
            except Exception as e:
                logger.warning("guest matches: %s source failed: %s", name, type(e).__name__)
                report["failed"].append(name)
            timings_ms[f"{name}_ms"] = int((_clock() - started) * 1000)
            if progress_cb:
                try:
                    progress_cb(done, len(sources))
                except Exception:
                    pass
    except _FutTimeout:
        for fut, name in futures.items():
            if not fut.done():
                fut.cancel()
                report["timed_out"].append(name)
    finally:
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # pragma: no cover - cancel_futures needs py3.9+
            pool.shutdown(wait=False)
        guest_cost.__exit__(None, None, None)

    # Merge: one posting can surface from more than one catalog — dedupe on
    # (company, title, location), keeping the copy with the richer signal.
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for j in rows:
        key = (
            str(j.get("company") or "").strip().lower(),
            str(j.get("title") or "").strip().lower(),
            str(j.get("location") or "").strip().lower(),
        )
        prev = merged.get(key)
        if prev is None:
            merged[key] = j
            continue
        prev_rank = (len(prev.get("description") or ""), prev.get("match_score") or 0)
        new_rank = (len(j.get("description") or ""), j.get("match_score") or 0)
        if new_rank > prev_rank:
            merged[key] = j
    out = list(merged.values())
    report["considered"] = len(out)
    return out, {**report, "timings_ms": timings_ms}


# --- inventory read path (ADR-003 cutover) ---------------------------------
# How many candidates the ranking stage gets. The scorer is pure regex over a
# precomputed requirement profile, so a few hundred rows cost single-digit
# milliseconds; the number is chosen for RECALL, not for speed. It is the pool
# the top 5 is chosen from, so too small a pool silently caps quality.
INVENTORY_CANDIDATE_LIMIT = _env_int("GUEST_INVENTORY_CANDIDATES", 400)
# Below this many scorable candidates the inventory is treated as not yet warm
# for this role family and the caller may fall back to the live scan.
INVENTORY_MIN_CANDIDATES = _env_int("GUEST_INVENTORY_MIN_CANDIDATES", 12)


def _resume_role_families(structured: Dict[str, Any]) -> List[str]:
    """Role families to retrieve for, widest sensible set.

    Retrieval and ranking have opposite failure modes. A retrieval filter that
    is too tight returns nothing and looks like an empty market; the scorer, by
    contrast, is good at pushing a bad fit down. So this errs wide: every family
    implied by the resume's titles AND by the same role terms the live scan
    would have searched for, deduped, order-preserved.
    """
    from services import fit_score

    families: List[str] = []

    def _add(value: Any) -> None:
        fam = fit_score.classify_role(value) if value else None
        if fam and fam not in families:
            families.append(fam)

    for exp in (structured.get("experience") or [])[:6]:
        if isinstance(exp, dict):
            _add(exp.get("title") or exp.get("position") or exp.get("role"))
    for term in _guest_role_terms(structured)[:6]:
        _add(term)
    _add(structured.get("target_role") or structured.get("headline"))
    return families


_US_COUNTRY_VALUES = frozenset({
    "us", "usa", "u.s.", "u.s.a.", "united states", "united states of america",
})

# FULL state names. `workday_jobs_service._US_STATE_RE` only carries two-letter
# codes, which is enough for a lenient filter where an unmatched row is kept
# anyway - but under strict gating a miss is a DROP, and boards very commonly
# write "Tempe, Arizona" or "Austin, Texas". Without this, real US roles would
# be silently discarded, which is the exact failure strict mode exists to avoid
# on the other side.
#
# Deliberately excluded: "Washington" (collides with Washington DC vs the state
# is fine, but also with countless non-US uses) is INCLUDED because the risk is
# low; "Georgia" is included despite the country of the same name, because
# `_FOREIGN_COUNTRY_RE` is consulted first and would have to name Georgia for a
# real collision - and a US job board writing "Georgia" means the state
# essentially always.
# Bare "US" / "U.S." as a standalone token. `career_pages_service._US_WORD_RE`
# matches "usa", "u.s." and "united states" but NOT a lone "US", and on live
# inventory "US" was the single most common location string of all (828 rows in
# a 9000-row audit) - every one of them a US role being discarded.
_US_TOKEN_RE = re.compile(r"\b(us|u\.s\.|u\.s|usa)\b", re.IGNORECASE)

# Major US cities that routinely appear WITHOUT a state qualifier ("San
# Francisco", "New York City", "Seattle"). Under lenient gating these fell
# through to the keep-by-default tier; under strict gating an unrecognised city
# is a drop, so they need explicit recognition or real jobs vanish.
#
# Deliberately EXCLUDED as genuinely ambiguous: Cambridge, Manchester,
# Birmingham, Richmond, Windsor, Hamilton - each names a well-known non-US city
# too, and a wrong keep is worse than a missed keep on this path.
_US_CITY_RE = re.compile(
    r"\b(san francisco|new york city|nyc|seattle|los angeles|san diego|"
    r"san jose|silicon valley|palo alto|mountain view|sunnyvale|santa clara|"
    r"menlo park|redwood city|cupertino|oakland|berkeley|sacramento|"
    r"san antonio|dallas|houston|fort worth|plano|irving|denver|boulder|"
    r"atlanta|miami|orlando|tampa|jacksonville|charlotte|raleigh|durham|"
    r"nashville|memphis|louisville|indianapolis|columbus|cleveland|"
    r"cincinnati|pittsburgh|philadelphia|baltimore|bethesda|reston|herndon|"
    r"mclean|arlington|alexandria|minneapolis|st\.? paul|milwaukee|"
    r"kansas city|st\.? louis|omaha|salt lake city|las vegas|phoenix|"
    r"scottsdale|tempe|tucson|albuquerque|portland|boise|anchorage|honolulu|"
    r"brooklyn|queens|bronx|manhattan|long island|newark|jersey city|"
    r"hoboken|stamford|hartford|providence|buffalo|rochester|albany|"
    r"bellevue|redmond|kirkland|tacoma|spokane|des moines|madison|"
    r"ann arbor|grand rapids|chattanooga|huntsville|birmingham al|"
    r"new orleans|baton rouge|oklahoma city|tulsa|wichita|little rock|"
    r"colorado springs|fort collins|fort lauderdale|west palm beach|"
    r"boca raton|sarasota|naples fl|greenville sc|columbia sc|charleston sc)\b",
    re.IGNORECASE)

_US_STATE_NAME_RE = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|"
    r"kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
    r"new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|"
    r"pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|"
    r"utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|"
    r"district of columbia|puerto rico)\b",
    re.IGNORECASE)


def guest_us_strict() -> bool:
    """Strict US gating for guest previews. `GUEST_US_STRICT=0` reverts."""
    return (os.getenv("GUEST_US_STRICT") or "1").strip().lower() not in ("0", "false", "no")


def is_us_confident(row: Dict[str, Any]) -> bool:
    """Does this posting carry POSITIVE evidence of being a US role?

    Deliberately stricter than the lenient `career_pages_service._keep_us_lenient`
    (the `GUEST_US_STRICT=0` rollback), which keeps ambiguous locations
    ("3 Locations", "Remote", "Getafe Area") on the reasoning that dropping them
    loses real US roles. That trade is right for a browse tab, where a user can
    see the location and judge, and wrong for a guest preview: five cards are the
    product's entire first impression, and one
    posting in Hyderabad reads as "this tool does not understand me".

    So the burden of proof inverts here. Ambiguous means dropped, and the honest
    consequence is fewer cards, which ADR-001 already established as preferable
    to padded ones.

    Order matters:
      1. an explicit foreign COUNTRY beats everything, including a colliding US
         state code ("Canada - Remote (ON, AB, or NS Only)" - "or" is Oregon);
      2. a parsed country field is authoritative when the source gave us one;
      3. an explicit US marker or a US state code proves the case;
      4. anything else is unproven, and unproven is dropped.
    """
    from services.career_pages_service import (
        _FOREIGN_CITY_RE, _FOREIGN_COUNTRY_RE, _US_WORD_RE,
    )
    from services.workday_jobs_service import _US_STATE_RE

    location = str(row.get("location") or "").strip()
    country = str(row.get("country_hint") or "").strip().lower()

    if country:
        return country in _US_COUNTRY_VALUES
    if not location:
        return False
    # An UNAMBIGUOUS US marker wins even alongside a foreign country, because
    # boards concatenate multi-location postings ("Remote, Canada; Remote,
    # United States") and a role open in the US is a US role wherever else it
    # is also open. Only spelled-out markers qualify here - a two-letter STATE
    # CODE stays below the foreign veto, because "Canada - Remote (ON, AB, or
    # NS Only)" would otherwise be rescued by "or" colliding with Oregon.
    if _US_WORD_RE.search(location) or _US_TOKEN_RE.search(location):
        return True
    # US state names are masked before the foreign-country veto is consulted:
    # `_FOREIGN_COUNTRY_RE` matches "mexico" inside "New Mexico", which would
    # veto an entire US state. (The same collision exists on the legacy path;
    # it is hidden there only because an unmatched row is kept anyway.)
    if _FOREIGN_COUNTRY_RE.search(_US_STATE_NAME_RE.sub(" ", location)):
        return False
    # Below the veto on purpose. A US state or city name alongside a foreign
    # COUNTRY is weak evidence, not multi-location evidence: "Dublin, Ireland
    # (Mountain View)" is an Irish posting that happens to name a US office.
    # A two-letter state code sits here for the additional reason that "or"
    # collides with Oregon in "Canada - Remote (ON, AB, or NS Only)".
    if (_US_STATE_RE.search(location) or _US_STATE_NAME_RE.search(location)
            or _US_CITY_RE.search(location)):
        return True
    if _FOREIGN_CITY_RE.search(location):
        return False
    # Bare "Remote", "3 Locations", a lone city, an unrecognised region: real
    # US roles hide in here, and so does everything else. Unproven.
    return False


def _inventory_row(doc: Dict[str, Any], tz_offset_minutes: int) -> Optional[Dict[str, Any]]:
    """One `job_postings` document -> one guest pipeline row.

    Emits the SAME shape the three live scanners emit, which is what lets the
    entire downstream path (freshness gate, scoring, ranking, card build,
    snapshot, seniority views) stay byte-for-byte unchanged. `description` is
    deliberately empty: the body lives in S3 and the scorer reads
    `requirements` instead, so no JD text crosses the wire on this path.
    """
    from services.workday_jobs_service import _days_ago

    posted_at = doc.get("posted_at")
    if not isinstance(posted_at, datetime):
        return None                      # ADR-001: no date, no card. Never guessed.
    iso = (posted_at if posted_at.tzinfo else posted_at.replace(tzinfo=timezone.utc)).isoformat()
    days = _days_ago(iso, tz_offset_minutes)
    if days is None:
        return None

    locations = doc.get("locations") or []
    location = ""
    country = ""
    if isinstance(locations, list) and locations:
        first = locations[0]
        if isinstance(first, dict):
            location = str(first.get("raw") or "")
            country = str(first.get("country") or "")
        else:
            location = str(first)

    url = str(doc.get("canonical_url") or "")
    return {
        # Geo inputs for `_keep_us`. The crawler stores the whole board, US or
        # not, so the US contract is enforced on READ.
        "country_hint": country.lower(),
        "is_remote": doc.get("work_mode") == "remote",
        "job_id": doc.get("job_id"),
        "title": str(doc.get("title_raw") or ""),
        "company": str(doc.get("employer_name") or ""),
        "location": location,
        "url": url,
        "apply_link": url,
        "source": "Workday" if doc.get("source") == "workday" else "Career Page",
        "description": "",
        "requirements": doc.get("requirements") or None,
        "posted": iso,
        "posted_text": iso[:10],
        "date_posted": iso,
        "days_ago": days,
        "work_mode": doc.get("work_mode"),
        "seniority_hint": doc.get("seniority"),
        "_guest_source": "inventory",
    }


def _fetch_from_inventory(
    structured: Dict[str, Any],
    progress_cb=None,
    *,
    resume_skills: Optional[List[str]] = None,
    resume_years: Optional[int] = None,
    tz_offset_minutes: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Candidate retrieval from the canonical inventory. Drop-in replacement
    for `_fetch_free_jobs`, same signature and same return contract.

    This is the whole latency story. `_fetch_free_jobs` issues roughly 7,400
    outbound requests to ~1,100 third-party hosts inside the user's request and
    still reaches only ~9% of the registry before its 165s budget expires. This
    issues ONE indexed Mongo query against postings a scheduled crawler already
    collected, covering 100% of the registry, and returns in milliseconds.

    ADR-004 is enforced structurally: `cost_classes=["free"]` sits in the query
    itself, so a metered aggregator row cannot reach an anonymous guest even by
    accident.
    """
    from services import fit_score, job_inventory_service as inv

    started = _clock()
    families = _resume_role_families(structured)
    if not families:
        # Same honest failure the live scan reports: no role evidence means we
        # cannot say what this person does, and guessing produces matches they
        # will not recognise.
        return [], {"ok": [], "failed": [], "timed_out": [], "considered": 0,
                    "timings_ms": {}, "empty_reason": "role_undetermined"}

    try:
        docs = inv.find_active_fresh(
            role_families=families,
            max_age_days=GUEST_MAX_AGE_DAYS,
            cost_classes=[inv.COST_CLASS_FREE],   # ADR-004, in the query
            limit=INVENTORY_CANDIDATE_LIMIT,
        )
    except Exception as e:
        logger.warning("guest matches: inventory read failed: %s", type(e).__name__)
        return [], {"ok": [], "failed": ["inventory"], "timed_out": [],
                    "considered": 0, "timings_ms": {}}

    # US-only contract. The crawler is deliberately geo-agnostic - it mirrors
    # each board in full - so this filter is what makes the guest feed US-only,
    # and the legacy scan enforced it by passing us_only=True down to the three
    # source services. Reusing their own `_keep_us` keeps one definition of
    # "is this a US role" across both retrieval paths.
    from services.career_pages_service import _keep_us_lenient

    strict = guest_us_strict()
    # Lenient mode (GUEST_US_STRICT=0) is the rollback: it keeps ambiguous
    # locations. `_keep_us` itself is now a HARD filter (drops ambiguous), so the
    # rollback must use the dedicated lenient contract, not `_keep_us`.
    keep_us = is_us_confident if strict else _keep_us_lenient

    rows: List[Dict[str, Any]] = []
    without_requirements = 0
    dropped_non_us = 0
    for doc in docs:
        # Job-side evidence gate. A posting whose body we never read produces no
        # requirements, and the scorer can then only judge it on its title. It
        # is capped at 45 by `insufficient_job_requirements`, but a guest's top
        # five is the product's first impression and does not get to contain
        # rows we have not actually read. It stays in the inventory and remains
        # browsable to signed-in users; it just cannot be a headline match.
        if not fit_score.has_job_requirements(doc.get("requirements")):
            without_requirements += 1
            continue
        row = _inventory_row(doc, tz_offset_minutes)
        if row is None:
            continue
        if not keep_us(row):
            dropped_non_us += 1
            continue
        rows.append(row)

    if progress_cb:
        try:
            progress_cb(1, 1)
        except Exception:
            pass

    report = {
        "ok": ["inventory"], "failed": [], "timed_out": [],
        "considered": len(rows),
        "role_families": families,
        "retrieved": len(docs),
        "dropped_no_requirements": without_requirements,
        "dropped_non_us": dropped_non_us,
        "us_gate": "strict" if strict else "lenient",
        "timings_ms": {"inventory_ms": int((_clock() - started) * 1000)},
    }
    return rows, report


_SAFE_URL_PREFIXES = ("http://", "https://")


def _safe_job_url(value: Any) -> Optional[str]:
    """http(s)-only guard for job-card link fields — the FE must never
    receive a javascript:/data:/vbscript: URL from us (defense at the source;
    the FE guards too)."""
    if not isinstance(value, str):
        return None
    candidate = InputSanitizer.sanitize_string(value, max_length=2048)
    if candidate.lower().startswith(_SAFE_URL_PREFIXES):
        return candidate
    return None


# The ONLY relative path a card may carry: the server-generated company-logo
# route (exact sha1 shape). Every other field keeps the strict http(s) guard —
# this allowance is per-field for logo_url, never a general weakening.
_LOGO_URL_RE = re.compile(r"^/api/jobs/logo/[0-9a-f]{40}$")


def _safe_logo_url(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = InputSanitizer.sanitize_string(value, max_length=2048)
    if _LOGO_URL_RE.match(candidate):
        return candidate
    return _safe_job_url(candidate)


def _resume_skills(structured: Dict[str, Any]) -> List[str]:
    from schemas.resume_schemas import flatten_skills

    seen, out = set(), []
    for s in flatten_skills(structured) or []:
        n = str(s or "").strip().lower()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def normalize_seniority(value: Any) -> str:
    """Sanitize a client-requested seniority view to the known set."""
    v = str(value or "").strip().lower()
    return v if v in GUEST_SENIORITY_LEVELS else "any"


#: `fit_score.LEVELS` folded onto the three views this toggle offers.
#: Internships land in `entry` because the guest toggle has no fourth view;
#: the authenticated feed keeps them apart.
_GUEST_VIEW_OF_LEVEL: Dict[str, str] = {
    "intern": "entry", "entry": "entry", "mid": "mid", "senior": "senior",
    "staff": "senior", "director": "senior", "executive": "senior",
}


def _seniority_of(title: Any) -> str:
    """'senior' | 'mid' | 'entry' | 'neutral' (no marker — shows everywhere).

    Delegates to `fit_score.title_level`, the one classifier the crawler, the
    inventory and the scorer all use. The three regexes this used to own were
    a fourth, weaker copy of that vocabulary, and because `_SENIORITY_ALLOWED`
    turns the result into a HARD exclusion, everything the copy missed was
    invisible in the view it belonged to. It had no "campus", "rotational",
    "trainee", "fresher", "university graduate", "associate", "level 1" or
    "Engineer I/II" — i.e. most of how large employers actually title their
    graduate pipeline — so an `entry` guest was shown almost nothing, which is
    exactly the complaint that prompted this. It also read "Lead" and
    "Architect" as senior with no regard for "Lead Cashier".

    Falls back to the local regexes only if the scorer cannot be imported: this
    is a display filter and it must degrade, not raise.
    """
    t = str(title or "")
    try:
        from services import fit_score
        level = fit_score.title_level(t)
        return _GUEST_VIEW_OF_LEVEL.get(level or "", "neutral")
    except Exception:  # pragma: no cover - taxonomy unavailable
        if _SENIOR_TITLE_RE.search(t):
            return "senior"
        if _ENTRY_TITLE_RE.search(t):
            return "entry"
        if _MID_TITLE_RE.search(t):
            return "mid"
        return "neutral"


# A marker EXCLUDES mismatched views; neutral titles show everywhere.
_SENIORITY_ALLOWED: Dict[str, Optional[frozenset]] = {
    "any": None,
    "entry": frozenset({"entry", "neutral"}),
    "mid": frozenset({"mid", "neutral"}),
    "senior": frozenset({"senior", "neutral"}),
}


def experience_level_for_years(years: Any) -> str:
    """Deterministic level from exact date-range math (resume_parser):
    <=2y entry, <=5y mid, else senior — the same mapping the guest profile
    reveal uses, so the toggle's 'Detected' chip always agrees with it."""
    try:
        y = float(years or 0)
    except (TypeError, ValueError):
        y = 0.0
    return "entry" if y <= 2 else "mid" if y <= 5 else "senior"


_VIEW_PER_COMPANY_CAP = 2


def _pool_view(
    pool: List[Dict[str, Any]], seniority: str, max_jobs: int,
) -> Tuple[List[Dict[str, Any]], int, Dict[str, int]]:
    """Slice the ranked pool into one seniority view: (visible top rows,
    qualifying total for that view, per-view counts for the toggle).

    Diversity: the visible slice caps at 2 rows per company, backfilling
    from the skipped rows only if the cap leaves empty slots — a single
    high-volume employer (live run: 4 of 5 were one company's near-identical
    postings) must not monopolize a guest's whole first impression."""
    lvl = normalize_seniority(seniority)
    allowed = _SENIORITY_ALLOWED[lvl]
    rows = [c for c in pool if allowed is None or c.get("seniority") in allowed]
    picked: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    per_company: Dict[str, int] = {}
    for c in rows:
        if len(picked) >= max_jobs:
            break
        key = str(c.get("company") or "").strip().lower()
        if per_company.get(key, 0) >= _VIEW_PER_COMPANY_CAP:
            skipped.append(c)
            continue
        per_company[key] = per_company.get(key, 0) + 1
        picked.append(c)
    for c in skipped:  # backfill in rank order if diversity left gaps
        if len(picked) >= max_jobs:
            break
        picked.append(c)
    counts = {"any": len(pool)}
    for k in GUEST_SENIORITY_LEVELS:
        a = _SENIORITY_ALLOWED[k]
        counts[k] = sum(1 for c in pool if c.get("seniority") in a)
    return picked, len(rows), counts


def _match_why(matched_skills: List[str]) -> str:
    """Short deterministic explanation from the scorer's matched skills.
    Scores are estimates, never hiring probabilities. No LLM, sentence case,
    no dashes."""
    skills = [str(s) for s in (matched_skills or [])[:3] if s]
    if not skills:
        return "Estimated match based on overlap between this role and your resume."
    if len(skills) == 1:
        listed = skills[0]
    elif len(skills) == 2:
        listed = f"{skills[0]} and {skills[1]}"
    else:
        listed = f"{skills[0]}, {skills[1]} and {skills[2]}"
    return f"Estimated match because this posting mentions {listed} from your resume."


def _sponsorship_for_card(company: Any, signal_cache: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """{signal, note} from public-filing data (sponsorship_signals — DOL LCA +
    USCIS Data Hub, ingested by scripts/ingest_sponsorship.py), else None.
    Honest language only: "signals from public filings", never "verified
    sponsor"; absent data → null, never a guess."""
    if not company:
        return None
    sig = signal_cache.get(str(company))
    if not sig:
        return None
    band = sig.get("band")
    if band not in ("low", "moderate", "strong"):
        return None
    headline = str(sig.get("headline") or "").strip().rstrip(".")
    if not headline:
        return None
    return {
        "signal": band,
        "note": f"Signals from public filings: {headline}. Not a guarantee of sponsorship.",
    }


def _build_card(row: Dict[str, Any], verdict: Dict[str, Any]) -> Dict[str, Any]:
    """Public job card from a scored pipeline row — whitelist + URL-scheme
    guards + deterministic score/gaps/why fields."""
    card = {k: row.get(k) for k in _JOB_CARD_FIELDS if row.get(k) is not None}
    # Link fields must be http(s) — drop anything else (javascript:, data:).
    for url_field in ("apply_link", "url"):
        if url_field in card:
            safe = _safe_job_url(card[url_field])
            if safe is None:
                card.pop(url_field)
            else:
                card[url_field] = safe
    # logo_url additionally allows the server-generated relative
    # /api/jobs/logo/<sha1> path (exact shape only).
    if "logo_url" in card:
        safe_logo = _safe_logo_url(card["logo_url"])
        if safe_logo is None:
            card.pop("logo_url")
        else:
            card["logo_url"] = safe_logo
    card["match_score"] = verdict["match_score"]
    card["matched_skills"] = verdict.get("matched_skills") or []
    # Honest gap signal: the scorer's top unmatched resume-relative skills
    # (deterministic, additive v2.1 field — absent on older cached cards).
    card["gaps"] = (verdict.get("missing_top") or [])[:5]
    card["why"] = _match_why(card["matched_skills"])
    # ADR-002: the versioned, explainable fit block. Deliberately TRIMMED to
    # roughly 500 bytes. This rides into every job_match_snapshots doc, up to
    # GUEST_POOL_MAX rows, so the per-component prose and the strengths list
    # are dropped (both are recomputable from the engine and the explanation
    # already carries the headline). Remembering the Atlas 512MB incident.
    if verdict.get("score_version"):
        card["fit"] = {
            "score_version": verdict["score_version"],
            "band": verdict.get("band"),
            "confidence": verdict.get("confidence"),
            "components": verdict.get("component_scores") or {},
            "explanation": str(verdict.get("explanation") or "")[:400],
            "missing_requirements": (verdict.get("missing_requirements") or [])[:4],
            "hard_constraints": [
                {"type": c.get("type"), "status": c.get("status")}
                for c in (verdict.get("hard_constraints") or [])
                if c.get("status") in ("missing", "unknown")
            ][:4],
        }
    return card


def _classify_empty_reason(
    rows_count: int, skills_count: int, sources_report: Dict[str, Any], fetch_failed: bool
) -> str:
    """spec 3.5: distinguish completed-empty from provider failure so the FE
    can render honest states."""
    if fetch_failed or (rows_count == 0 and not (sources_report or {}).get("ok")):
        return "sources_unavailable"
    if skills_count == 0:
        return "low_signal_resume"
    return "no_openings_matched"


def inventory_mode_enabled() -> bool:
    """Is the canonical inventory the primary retrieval path?

    `GUEST_MATCH_MODE=scan` is the single-env-var rollback to the legacy live
    fan-out, and is the only value that turns this off.
    """
    return (os.getenv("GUEST_MATCH_MODE") or "inventory").strip().lower() != "scan"


def compute_guest_matches(
    guest_id: str,
    job_id: Optional[str] = None,
    seniority: Optional[str] = None,
    tz_offset_minutes: int = 0,
    *,
    inventory_only: bool = False,
) -> Optional[Dict[str, Any]]:
    """Async-job body for `guest_matches` (spec 3.1-3.8, v4): run the SAME
    three sources as the authed Job Opportunities feed in parallel, score
    every row with jd_match_scorer against the guest's parsed resume, enrich
    the top description-less Workday candidates with real CXS detail
    descriptions and re-score, keep only score >= guest_jobs_min_score, and
    persist a ranked POOL (up to GUEST_POOL_MAX) in today's
    job_match_snapshots doc. Guests SEE the top guest_jobs_max (5) for their
    detected (or requested) seniority; the rest surface as an honest locked
    count and power the instant seniority re-rank.

    Bounded by a wall-clock budget: on expiry, whatever genuine partial
    results exist are persisted. When job_id is given, honest progress phases
    are emitted via update_job_progress."""
    gid = validate_guest_id(guest_id)
    if not gid:
        raise ValueError("invalid guest_id")
    # get_session hydrates the resume view from the resume_assets ref (v3).
    doc = get_session(gid)
    if not doc:
        raise ValueError("guest session not found")
    structured = ((doc.get("resume") or {}).get("structured")) or {}
    if not structured:
        raise ValueError("No parsed resume on this guest session")

    cfg = pconfig.get_guest_config()
    min_score = int(cfg.get("guest_jobs_min_score", 40))
    max_jobs = max(1, int(cfg.get("guest_jobs_max", 5)))

    # Deterministic experience detection: exact date-range math (merged
    # intervals — parallel jobs never double-count), mapped to the same
    # entry/mid/senior bands the profile reveal shows.
    from services.resume_parser import ResumeParser
    detected_years = ResumeParser._estimate_experience_years(structured)
    detected_level = experience_level_for_years(detected_years)
    requested = normalize_seniority(seniority) if seniority else None
    default_view = requested or detected_level

    started = _clock()
    deadline = started + GUEST_MATCH_BUDGET_SECONDS

    def _remaining() -> float:
        return deadline - _clock()

    def _progress(phase: str, pct: int) -> None:
        if not job_id:
            return
        try:
            from services.resume_service import get_resume_service

            get_resume_service().update_job_progress(job_id, phase, pct)
        except Exception:  # pragma: no cover - progress must never break
            pass

    # ADR-002: one shared, versioned engine. The old jd_match_scorer built its
    # denominator from the RESUME's own skills, so it measured "how much of
    # this resume appears in this posting" and had ~15 reachable values. The
    # fit engine derives required skills from the JOB DESCRIPTION and is the
    # SAME engine the feed pipelines use to select candidates, so selection
    # and display can no longer disagree.
    from services import fit_score

    _progress("reading_resume", 10)
    skills = _resume_skills(structured)
    # Built once; every posting reuses it (requirement extraction is memoised
    # per JD inside the engine).
    fit_profile = fit_score.build_resume_profile(structured)

    # --- searching_sources (30..70 as providers complete) ------------------
    _progress("searching_sources", 30)

    def _source_progress(done: int, total: int) -> None:
        _progress("searching_sources", 30 + int(40 * done / max(1, total)))

    timings: Dict[str, int] = {}
    fetch_failed = False
    src_report: Optional[Dict[str, Any]] = None
    t_fetch = _clock()

    def _run_fetcher(fn) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Invoke a retrieval function, tolerating legacy one-argument doubles.

        The signature sniffing predates the inventory path: tests patch
        `_fetch_free_jobs` with a bare `fn(structured)` returning a list, and
        that contract is still honoured.
        """
        try:
            params = inspect.signature(fn).parameters
            accepts_cb = len(params) >= 2
            accepts_profile = "resume_skills" in params
        except (TypeError, ValueError):  # pragma: no cover - exotic patch
            accepts_cb, accepts_profile = False, False
        kwargs = (
            {
                "resume_skills": skills,
                "resume_years": detected_years,
                "tz_offset_minutes": tz_offset_minutes,
            }
            if accepts_profile else {}
        )
        out = fn(structured, _source_progress, **kwargs) if accepts_cb else fn(structured)
        if isinstance(out, tuple) and len(out) == 2:
            return list(out[0] or []), dict(out[1] or {})
        return list(out or []), None

    # --- retrieval mode ----------------------------------------------------
    # `inventory` reads the canonical `job_postings` collection (ADR-003) and
    # returns in milliseconds; `scan` is the legacy live fan-out that costs up
    # to 210s. Default is inventory WITH an automatic fallback: while the
    # crawler is still warming a role family, a thin result would be a worse
    # answer than a slow one, so a cold read scans instead of reporting an empty
    # market. `GUEST_MATCH_MODE=scan` forces the legacy path outright, which is
    # the one-env-var rollback.
    rows: List[Dict[str, Any]] = []
    used_mode = "scan"

    if inventory_mode_enabled():
        used_mode = "inventory"
        try:
            rows, src_report = _run_fetcher(_fetch_from_inventory)
        except Exception as e:
            logger.warning("guest matches: inventory read failed: %s", type(e).__name__)
            rows, src_report = [], None
        # "No role terms" is a genuine answer, not a cold cache - scanning would
        # burn 210s to produce the same empty result.
        undetermined = (src_report or {}).get("empty_reason") == "role_undetermined"
        # Warmth is measured BEFORE the quality gates, not after. `rows` has
        # already had non-US, undated and evidence-less postings removed, and
        # those removals are real answers about the market, not symptoms of a
        # cold cache. Judging warmth on the filtered count would send a niche
        # US-thin role down the 210s live scan to rediscover the same thinness.
        retrieved = int((src_report or {}).get("retrieved") or len(rows))
        if retrieved < INVENTORY_MIN_CANDIDATES and not undetermined:
            logger.warning(
                "guest matches: inventory cold for families=%s (%d candidates < %d) "
                "- falling back to live scan",
                (src_report or {}).get("role_families"), retrieved,
                INVENTORY_MIN_CANDIDATES,
            )
            used_mode, rows, src_report = "scan_fallback", [], None

    if used_mode != "inventory":
        # `inventory_only` is the SYNCHRONOUS caller (the HTTP route). It runs
        # inside a 60s Lambda and must never start a 210s fan-out, so a cold
        # inventory returns None and the route falls back to the async job.
        if inventory_only:
            return None
        try:
            rows, src_report = _run_fetcher(_fetch_free_jobs)
        except Exception as e:
            logger.warning("guest matches: free-source fetch failed: %s", type(e).__name__)
            rows, src_report, fetch_failed = [], None, True

    if src_report is not None:
        src_report["mode"] = used_mode

    sources_report: Dict[str, Any] = src_report or {
        "ok": [] if fetch_failed else ["workday", "career_pages", "mnc"],
        "failed": ["workday", "career_pages", "mnc"] if fetch_failed else [],
        "timed_out": [],
        "considered": len(rows),
    }
    src_timings = sources_report.pop("timings_ms", {}) or {}
    timings["fetch_ms"] = int((_clock() - t_fetch) * 1000)
    for k, v in src_timings.items():
        try:
            timings[k] = int(v)
        except (TypeError, ValueError):
            pass

    # --- guest freshness contract: BEFORE any scoring work ------------------
    # The feeds hand back their own 30-day window; guests get GUEST_MAX_AGE_DAYS.
    # Dropping here (not at render time) means stale rows never consume scoring
    # budget, never reach enrichment, and can never be persisted into a
    # snapshot. Fewer survivors is a success — nothing is padded back in.
    fetched_count = len(rows)
    rows = filter_fresh_for_guest(rows)
    dropped_stale = fetched_count - len(rows)
    if dropped_stale:
        sources_report["dropped_stale"] = dropped_stale
        sources_report["max_age_days"] = GUEST_MAX_AGE_DAYS

    # Feed rows carry posted_text/date_posted; the legacy card field is
    # `posted` — map it so cached cards keep one canonical field.
    for row in rows:
        if not row.get("posted"):
            row["posted"] = row.get("posted_text") or row.get("date_posted") or None

    # --- initial deterministic scoring (budget-checked per row) ------------
    # Post-fetch grace: whatever the fetch consumed, scoring/ranking (pure
    # CPU regex, no network) always gets a guaranteed window — a slow scan
    # must degrade to fewer sources, never to unscored rows.
    deadline = max(deadline, _clock() + GUEST_SCORE_GRACE_SECONDS)
    t_score = _clock()
    budget_expired = False
    scored: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []  # (row, verdict)
    for row in rows:
        if _remaining() <= 0:
            budget_expired = True
            break
        # `requirements` short-circuits JD extraction. Inventory rows carry the
        # profile the crawler already extracted (identical output, asserted by
        # test_inventory_scoring_parity), so the JD body never has to be loaded
        # to produce a score. Live-scan rows carry None and extract inline.
        verdict = fit_score.score_one(
            jd_text=str(row.get("description") or ""),
            title=str(row.get("title") or ""),
            resume_skills=skills,
            profile=fit_profile,
            location=str(row.get("location") or ""),
            job={"requirements": row.get("requirements")} if row.get("requirements") else None,
        )
        scored.append((row, verdict))
    timings["score_ms"] = int((_clock() - t_score) * 1000)

    # --- 3.1: CXS detail enrichment for description-less Workday rows ------
    t_enrich = _clock()
    enriched = 0
    # Inventory rows are already requirement-parsed at crawl time, so there is
    # nothing to enrich and no reason to open a socket on the request path.
    if used_mode == "inventory":
        pass
    elif not budget_expired and _remaining() > 0:
        candidates = [
            (row, verdict) for row, verdict in scored
            if str(row.get("source") or "") in ("Workday Direct", "Workday")
            and not str(row.get("description") or "").strip()
        ]
        candidates.sort(key=lambda rv: rv[1].get("match_score", 0), reverse=True)
        candidates = candidates[:_ENRICH_MAX_CANDIDATES]
        urls = []
        by_url: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
        for row, verdict in candidates:
            u = str(row.get("url") or row.get("apply_link") or "").strip()
            if u and u not in by_url:
                urls.append(u)
                by_url[u] = (row, verdict)
        if urls:
            try:
                details = _fetch_workday_details(
                    urls,
                    overall_budget=min(_ENRICH_OVERALL_BUDGET, max(1, _remaining())),
                )
            except Exception as e:  # pragma: no cover - enrichment best effort
                logger.warning("guest matches: detail enrichment skipped: %s", type(e).__name__)
                details = {}
            for u, text in (details or {}).items():
                pair = by_url.get(u)
                if not pair or not text:
                    continue
                row, verdict = pair
                row["description"] = str(text)[:15000]
                fresh = fit_score.score_one(
                    jd_text=row["description"],
                    title=str(row.get("title") or ""),
                    resume_skills=skills,
                    profile=fit_profile,
                    location=str(row.get("location") or ""),
                )
                verdict.clear()
                verdict.update(fresh)
                enriched += 1
    timings["enrich_ms"] = int((_clock() - t_enrich) * 1000)
    if budget_expired and "budget" not in (sources_report.get("timed_out") or []):
        sources_report.setdefault("timed_out", []).append("budget")

    # --- ranking + honest floor (no padding — spec 3.6) --------------------
    _progress("ranking", 80)
    qualifying = [
        (_build_card(row, verdict), row)
        for row, verdict in scored
        if verdict.get("match_score", 0) >= min_score
    ]
    # Fresh-first tiebreak: equal scores rank by recency (viewer-local days).
    qualifying.sort(key=lambda pair: (
        -(pair[0].get("match_score") or 0),
        pair[0].get("days_ago") if pair[0].get("days_ago") is not None else 99,
    ))
    pool_pairs = qualifying[:GUEST_POOL_MAX]

    # Public-filings sponsorship signal (one bulk Mongo read). Any failure or
    # absent ingest data → null on every card (honest "no data", never wrong).
    signal_cache: Dict[str, Any] = {}
    try:
        from services.sponsorship_service import bulk_signals
        companies = [str(c.get("company") or "") for c, _ in pool_pairs if c.get("company")]
        if companies:
            signal_cache = bulk_signals(companies)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("guest matches: sponsorship lookup unavailable: %s", e)
        signal_cache = {}

    # employer-policy-1.0.0: a guest has no stored choice, so the DOCUMENTED
    # DEFAULT (strict_direct, unknown shown as unverified) applies - decided
    # on the scored row (it still has the JD body for client-placement
    # wording) and annotated onto the public card.
    from services import employer_policy as _ep
    _probes = [dict(row, company=card.get("company"), title=card.get("title"),
                    apply_link=card.get("apply_link") or card.get("url"))
               for card, row in pool_pairs]
    _allowed, guest_policy = _ep.apply(_probes, _ep.default_prefs())
    _allowed_ids = {id(p) for p in _allowed}
    _pairs = []
    for (card, row), probe in zip(pool_pairs, _probes):
        if id(probe) in _allowed_ids:
            card.update(_ep.public_blocks(probe))
            _pairs.append((card, row))
    pool_pairs = _pairs

    pool: List[Dict[str, Any]] = []
    jd_sources: Dict[str, Dict[str, Any]] = {}
    for card, row in pool_pairs:
        match_id = uuid.uuid4().hex  # opaque server-minted id — the only tailorable reference
        card["match_id"] = match_id
        card["seniority"] = _seniority_of(card.get("title"))
        card["sponsorship"] = _sponsorship_for_card(card.get("company"), signal_cache)
        # JD sources for the top slice only — any of these rows can become
        # visible via the seniority toggle and must stay tailorable, but the
        # snapshot must not balloon (Atlas 512MB incident).
        if len(jd_sources) < GUEST_JD_SOURCES_MAX:
            jd_sources[match_id] = {
                "title": str(card.get("title") or ""),
                "company": str(card.get("company") or ""),
                "jd_text": str(row.get("description") or "")[:_JD_TEXT_MAX],
                "url": card.get("url") or card.get("apply_link") or "",
            }
        pool.append(card)

    # Company logos — (re)stamped at cache-build time so every cached guest
    # card carries the server-derived logo_url AND the company_logos docs the
    # /api/jobs/logo route needs exist (the route never fetches for keys
    # without a doc). Overwrites any row-provided value: deterministic from
    # the company name, never client-influenced.
    try:
        from services.company_logo_service import stamp_logo_urls
        stamp_logo_urls(pool)
    except Exception as e:  # pragma: no cover - logos must never break matches
        logger.warning("guest matches: logo stamping skipped: %s", type(e).__name__)

    _progress("finalizing", 95)
    # Default view: the DETECTED level (or the caller's explicit choice).
    # A detected view that filters everything away falls back to "any" for
    # the default only — never a fake empty day when matches exist.
    top, pool_total, seniority_counts = _pool_view(pool, default_view, max_jobs)
    applied_view = default_view
    if not top and pool and requested is None:
        top, pool_total, seniority_counts = _pool_view(pool, "any", max_jobs)
        applied_view = "any"
    detected_experience = {"level": detected_level, "years": detected_years}

    sources_report["considered"] = len(rows)
    empty_reason = None
    if not pool:
        empty_reason = _classify_empty_reason(
            len(rows), len(skills), sources_report, fetch_failed
        )
    timings["total_ms"] = int((_clock() - started) * 1000)
    _store_matches(
        gid, top, min_score,
        jd_sources=jd_sources,
        sources_report=sources_report,
        timings=timings,
        empty_reason=empty_reason,
        pool=pool,
        detected_experience=detected_experience,
        default_seniority=applied_view,
    )
    # One structured no-PII WARNING summary per run (spec 3.8): counts and
    # durations only — guest_id is an opaque capability uuid, never identity.
    logger.warning(
        "guest matches summary: guest=%s rows=%d scored=%d enriched=%d pool=%d served=%d "
        "min_score=%d level=%s empty_reason=%s budget_expired=%s total_ms=%d",
        gid, len(rows), len(scored), enriched, len(pool), len(top),
        min_score, applied_view, empty_reason, budget_expired, timings["total_ms"],
    )
    result: Dict[str, Any] = {
        "jobs": top,
        "date": _today_key(),
        "min_score": min_score,
        "total_considered": len(rows),
        "sources_report": sources_report,
        "cached": False,
        "seniority": applied_view,
        "detected_experience": detected_experience,
        "seniority_counts": seniority_counts,
        "pool_total": pool_total,
        "locked_count": max(0, pool_total - len(top)),
        "policy": guest_policy,
    }
    if empty_reason:
        result["empty_reason"] = empty_reason
    return result


# ---------------------------------------------------------------------------
# Download capability tokens (versioned HMAC; dedicated secret; fail closed)
# ---------------------------------------------------------------------------

class DownloadTokensUnavailable(Exception):
    """GUEST_DOWNLOAD_TOKEN_SECRET unset outside development — fail closed."""


class DownloadTokenInvalid(Exception):
    """Malformed/forged/unknown token → generic 404 (no oracle)."""


class DownloadTokenExpired(Exception):
    """Well-formed, signature-valid, but past exp → 410."""


_dev_download_secret: Optional[str] = None
_download_secret_warned = False


def _download_secret() -> Optional[bytes]:
    """The dedicated download-token secret (`GUEST_DOWNLOAD_TOKEN_SECRET`,
    SSM `SSM_GUEST_DOWNLOAD_TOKEN_SECRET`). NEVER JWT_SECRET_KEY. Missing in
    production → None (mint/verify fail closed, 503-style message, never 401).
    In development a per-process random secret keeps local flows working
    (tokens simply die on restart) — same pattern as approval_token_service."""
    global _dev_download_secret, _download_secret_warned
    value = (_get_config_value("GUEST_DOWNLOAD_TOKEN_SECRET", "") or "").strip()
    if value:
        return value.encode("utf-8")
    env = os.getenv("ENVIRONMENT", "development").strip().lower()
    if env != "development":
        if not _download_secret_warned:
            logger.warning(
                "GUEST_DOWNLOAD_TOKEN_SECRET not configured — guest downloads "
                "disabled (fail closed)"
            )
            _download_secret_warned = True
        return None
    if _dev_download_secret is None:
        _dev_download_secret = _pysecrets.token_hex(32)
        logger.warning(
            "guest_service: GUEST_DOWNLOAD_TOKEN_SECRET unset — using a "
            "per-process dev secret (download links break on restart)"
        )
    return _dev_download_secret.encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(seg: str) -> bytes:
    seg = str(seg or "")
    pad = (-len(seg)) % 4
    return base64.urlsafe_b64decode(seg + "=" * pad)


def _sign_download(payload_b64: str, key: bytes) -> bytes:
    # The version prefix is inside the signed bytes — a v2 token can never be
    # replayed as v1.
    data = f"{_DOWNLOAD_TOKEN_VERSION}.{payload_b64}".encode("ascii")
    return _hmac.new(key, data, hashlib.sha256).digest()


def mint_download_token(
    record_id: str, guest_id: str, fmt: str, ttl_seconds: int = DOWNLOAD_TOKEN_TTL_SECONDS
) -> Tuple[str, str]:
    """Mint a short-lived download capability token.

    Versioned HMAC over ``record_id|guest_id|format|exp`` (TTL 15 min).
    Returns (token, expires_at_iso). Raises DownloadTokensUnavailable when
    the secret is not configured (fail closed — the route answers with a
    503-style capacity message, never 401). Unlimited re-issue; downloads
    never consume quota.
    """
    key = _download_secret()
    if key is None:
        raise DownloadTokensUnavailable()
    rid = validate_guest_id(record_id)
    gid = validate_guest_id(guest_id)
    if not rid or not gid or fmt not in DOWNLOAD_FORMATS:
        raise DownloadTokenInvalid()
    exp_dt = _now() + timedelta(seconds=int(ttl_seconds))
    exp = int(exp_dt.timestamp())
    payload_b64 = _b64e(f"{rid}|{gid}|{fmt}|{exp}".encode("utf-8"))
    sig_b64 = _b64e(_sign_download(payload_b64, key))
    token = f"{_DOWNLOAD_TOKEN_VERSION}.{payload_b64}.{sig_b64}"
    return token, exp_dt.isoformat()


def verify_download_token(token: str) -> Dict[str, str]:
    """Verify a download token: constant-time HMAC first, shape second,
    expiry last (so 410 only ever fires for a well-formed VALID token).
    Returns {record_id, guest_id, format}."""
    key = _download_secret()
    if key is None:
        raise DownloadTokensUnavailable()
    if not token or not isinstance(token, str) or len(token) > 1024:
        raise DownloadTokenInvalid()
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _DOWNLOAD_TOKEN_VERSION or not parts[1] or not parts[2]:
        raise DownloadTokenInvalid()
    _, payload_b64, sig_b64 = parts
    expected = _sign_download(payload_b64, key)
    try:
        provided = _b64d(sig_b64)
    except (ValueError, TypeError):
        raise DownloadTokenInvalid()
    if not _hmac.compare_digest(expected, provided):
        raise DownloadTokenInvalid()
    try:
        payload = _b64d(payload_b64).decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        raise DownloadTokenInvalid()
    fields = payload.split("|")
    if len(fields) != 4:
        raise DownloadTokenInvalid()
    rid = validate_guest_id(fields[0])
    gid = validate_guest_id(fields[1])
    fmt = fields[2]
    if not rid or not gid or fmt not in DOWNLOAD_FORMATS:
        raise DownloadTokenInvalid()
    try:
        exp = int(fields[3])
    except (TypeError, ValueError):
        raise DownloadTokenInvalid()
    if _now().timestamp() >= exp:
        raise DownloadTokenExpired()
    return {"record_id": rid, "guest_id": gid, "format": fmt}


def render_record_file(record: Dict[str, Any], fmt: str) -> Tuple[bytes, str]:
    """Render (or serve the version-cached file for) a tailoring record via
    the EXISTING shared resume_renderer — the same polished output as the
    authed /api/resume/download path, including its render_version dedup
    guard. Returns (file_bytes, filename)."""
    from services.resume_service import get_resume_service
    from services.resume_versioning import ensure_versions, find_version, latest_version

    svc = get_resume_service()
    versions, _migrated = ensure_versions(record)
    target = None
    cur_id = record.get("current_version_id")
    if cur_id:
        target = find_version(versions, cur_id)
    if target is None:
        target = latest_version(versions)
    tailored = (target or {}).get("tailored_resume") or record.get("tailored_resume") or {}
    jd_analysis = record.get("jd_analysis") or {}
    filename = svc.renderer.build_filename(tailored, jd_analysis, fmt)

    # Versioned dedup: an already-rendered current-format file is served as-is.
    cached = ((target or {}).get("files") or {}).get(fmt) or {}
    if cached.get("s3_key") and cached.get("render_version") == svc.renderer.RENDER_VERSION:
        try:
            from services.s3_service import get_storage_service
            data = get_storage_service().get_resume(cached["s3_key"])
            if data:
                return data, cached.get("filename") or filename
        except Exception as cache_err:  # pragma: no cover - cache miss path
            logger.warning("guest download: cached fetch failed, re-rendering: %s", cache_err)

    resolution = None
    if fmt == "pdf":
        data, resolution = svc.renderer.generate_pdf(tailored)
    else:
        data = svc.renderer.generate_docx(tailored)
    if not data or len(data) < 100:
        raise ValueError("render produced no output")
    return data, filename


# ---------------------------------------------------------------------------
# Retention cleanup (claim-aware; never a blind TTL)
# ---------------------------------------------------------------------------

def record_resume_file_key(guest_id: str, s3_key: str) -> None:
    """Track a guest resume file's S3 key on the session so retention
    cleanup can delete the object together with the expiring session.
    Best-effort — losing the pointer only means a stray S3 object."""
    gid = validate_guest_id(guest_id)
    if not gid or not s3_key:
        return
    try:
        _sessions().update_one(
            {"guest_id": gid},
            {"$addToSet": {"resume_file_s3_keys": str(s3_key)}},
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("guest resume file key not recorded for %s: %s", gid, e)


def _delete_s3_keys(keys: List[str]) -> int:
    """Best-effort S3 deletes — an S3 hiccup must never block a Mongo purge."""
    deleted = 0
    if not keys:
        return 0
    try:
        from services.s3_service import get_storage_service

        storage = get_storage_service()
        for key in keys:
            try:
                storage.delete_resume(key)
                deleted += 1
            except Exception as del_err:
                logger.warning("guest retention: S3 delete failed for %s: %s", key, del_err)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("guest retention: S3 cleanup unavailable: %s", e)
    return deleted


def run_retention_cleanup() -> Dict[str, Any]:
    """Delete ONLY expired unclaimed guest data: sessions with
    ``claimed_by: null`` whose ``last_seen_at`` is older than
    ``retention_days`` (0 = keep forever → skip), plus their unclaimed
    tailoring_records, guest resume_assets docs (and their S3 objects),
    job_match_snapshots, stale guest resume_jobs, and the sessions' legacy
    resume_file_s3_keys. Claimed data is NEVER touched by retention —
    account erasure handles it.

    Also enforces the spec-2 orphan rule: assets stuck parse_status=pending
    with no object_key for >24h are swept (doc + any stray object)."""
    days = pconfig.get_int("retention_days")
    if days <= 0:
        return {
            "skipped": True,
            "retention_days": days,
            "sessions_deleted": 0,
            "records_deleted": 0,
            "jobs_deleted": 0,
        }
    cutoff = _now() - timedelta(days=days)
    expiring = list(
        _sessions().find(
            {"claimed_by": None, "last_seen_at": {"$lt": cutoff}},
            {"guest_id": 1, "resume_file_s3_keys": 1},
        )
    )
    ids = [str(d["guest_id"]) for d in expiring]

    files_deleted = 0
    assets_deleted = 0
    snapshots_deleted = 0
    records_deleted = 0
    jobs_deleted = 0
    sessions_deleted = 0
    if ids:
        # Guest-owned resume assets (claimed ones are excluded — a claimed
        # session never appears in `expiring`, and the claim_status guard
        # protects against any drift).
        assets = list(
            _db().resume_assets.find(
                {"guest_id": {"$in": ids}, "claim_status": {"$ne": "claimed"}},
                {"resume_id": 1, "object_key": 1},
            )
        )
        s3_keys = [k for d in expiring for k in (d.get("resume_file_s3_keys") or []) if k]
        s3_keys += [str(a["object_key"]) for a in assets if a.get("object_key")]
        files_deleted = _delete_s3_keys(sorted(set(s3_keys)))
        if assets:
            res = _db().resume_assets.delete_many(
                {"resume_id": {"$in": [a["resume_id"] for a in assets]}}
            )
            assets_deleted = int(res.deleted_count)
        snaps = _snapshots().delete_many({"guest_id": {"$in": ids}})
        snapshots_deleted = int(snaps.deleted_count)
        recs = _db().tailoring_records.delete_many(
            {"guest_id": {"$in": ids}, "user_email": None}
        )
        records_deleted = int(recs.deleted_count)
        jobs = _db().resume_jobs.delete_many({"guest_id": {"$in": ids}})
        jobs_deleted = int(jobs.deleted_count)
        sess = _sessions().delete_many({"guest_id": {"$in": ids}, "claimed_by": None})
        sessions_deleted = int(sess.deleted_count)

    # Spec-2 orphan rule: pending assets with no object_key >24h — Mongo doc
    # plus any stray object under the derived key (an S3 write may have
    # landed without the $set). Never touches claimed assets.
    orphan_assets_deleted = 0
    try:
        from services import resume_access

        orphan_cutoff = _now() - timedelta(hours=24)
        orphans = list(
            _db().resume_assets.find(
                {
                    "parse_status": "pending",
                    "object_key": None,
                    "created_at": {"$lt": orphan_cutoff},
                    "claim_status": {"$ne": "claimed"},
                },
                {"resume_id": 1},
            )
        )
        if orphans:
            _delete_s3_keys(
                [resume_access.asset_object_key(str(o["resume_id"])) for o in orphans]
            )
            res = _db().resume_assets.delete_many(
                {"resume_id": {"$in": [o["resume_id"] for o in orphans]},
                 "parse_status": "pending", "object_key": None}
            )
            orphan_assets_deleted = int(res.deleted_count)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("guest retention: orphan sweep failed: %s", type(e).__name__)

    logger.info(
        "guest retention cleanup: sessions=%d records=%d jobs=%d files=%d "
        "assets=%d snapshots=%d orphans=%d (retention_days=%d)",
        sessions_deleted, records_deleted, jobs_deleted, files_deleted,
        assets_deleted, snapshots_deleted, orphan_assets_deleted, days,
    )
    return {
        "skipped": False,
        "retention_days": days,
        "sessions_deleted": sessions_deleted,
        "records_deleted": records_deleted,
        "jobs_deleted": jobs_deleted,
        "files_deleted": files_deleted,
        "assets_deleted": assets_deleted,
        "snapshots_deleted": snapshots_deleted,
        "orphan_assets_deleted": orphan_assets_deleted,
    }


# ---------------------------------------------------------------------------
# Claim v2 — exact-capability, atomic, idempotent; free day-1 carry-in once
# ---------------------------------------------------------------------------

def _daily_usage_snapshot(email: str) -> Tuple[int, int]:
    try:
        from services import usage_service
        usage = usage_service.get_usage(email)
        return int(usage.get("used") or 0), int(usage.get("limit") or 0)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("guest claim: usage snapshot failed: %s", e)
        return 0, 0


def _already_claimed_summary(email: str) -> Dict[str, Any]:
    used, limit = _daily_usage_snapshot(email)
    return {
        "claimed": True,
        "already_claimed": True,
        "resume_imported": False,
        "records_transferred": 0,
        "tailors_carried": 0,
        "daily_tailors_used": used,
        "daily_tailors_limit": limit,
    }


def _tick_claim_step(gid: str, email: str, step: str) -> None:
    _sessions().update_one(
        {"guest_id": gid, "claim.email": email},
        {"$set": {f"claim.steps.{step}": True}},
    )


def claim_session(guest_id: str, user_email: str) -> Dict[str, Any]:
    """Flip guest artifacts to the authenticated user — claim STATE MACHINE
    (spec 1.7): unclaimed -> claim_pending -> transferring -> completed.

      1. CAS unclaimed→claim_pending (find_one_and_update guarded on state
         unclaimed OR (claim_pending/transferring AND claim.email == caller))
         — a second account gets the already_claimed error (403 at the
         route); the SAME account resumes an interrupted claim.
      2. transferring: idempotent steps, each ticking claim.steps flags:
         records (tailoring_records guest→email), resume (resume_assets
         claim_status→claimed + owner attach user_id/email + purge_after→
         null; snapshots retained), subject link, day-1 carry-in (existing
         (subject, UTC day) unique double guard; premium untouched).
      3. completed: legacy claimed_by/claimed_at set + claim.state=completed.

    Exact-capability-only: the claim covers THIS session and the records its
    guest_id proves — sibling sessions in the strong-signal cluster keep
    their own capabilities. On completion, guest download minting for the
    claimed records STOPS (mint requires user_email null) and outstanding
    pre-claim tokens die at redemption (same guest-owned re-check).
    """
    gid = validate_guest_id(guest_id)
    if not gid:
        return {"error": "invalid_guest_id"}
    email = InputSanitizer.sanitize_email(user_email or "")
    if not email:
        return {"error": "invalid_user"}

    # get_session hydrates the resume view from the resume_assets ref (v3).
    session = get_session(gid)
    if not session:
        return {"error": "not_found"}

    claim = session.get("claim") or {}
    already_claimed_by = session.get("claimed_by")
    if already_claimed_by and already_claimed_by != email:
        # Capability token was already consumed by another account.
        return {"error": "already_claimed"}
    if already_claimed_by == email or (
        claim.get("state") == "completed" and claim.get("email") == email
    ):
        # Repeat claim by the same account: idempotent, zero re-transfer.
        return _already_claimed_summary(email)

    subject_id = subject_for_guest(gid)
    now = _now()

    # --- 1. CAS unclaimed -> claim_pending (same-account resume allowed) --
    flipped = _sessions().find_one_and_update(
        {
            "guest_id": gid,
            "$or": [
                # Fresh claim: never claimed, state unclaimed/absent.
                {"claimed_by": None, "claim.state": {"$in": [None, "unclaimed"]}},
                {"claimed_by": None, "claim": {"$exists": False}},
                # Same-account resume of an interrupted claim.
                {"claim.state": {"$in": ["claim_pending", "transferring"]},
                 "claim.email": email},
            ],
        },
        {"$set": {
            "claim.state": "claim_pending",
            "claim.email": email,
            "last_seen_at": now,
        }},
    )
    if flipped is None:
        # Lost the CAS: either another account owns the claim, or ours
        # completed concurrently. EVERY transfer step is reserved for the
        # CAS winner — a losing call must transfer NOTHING.
        current = _sessions().find_one(
            {"guest_id": gid}, {"claimed_by": 1, "claim": 1}
        ) or {}
        cur_claim = current.get("claim") or {}
        if current.get("claimed_by") == email or cur_claim.get("email") == email:
            return _already_claimed_summary(email)
        return {"error": "already_claimed"}

    # Stamp started_at once (survives interrupted-claim resumes).
    _sessions().update_one(
        {"guest_id": gid, "claim.email": email, "claim.started_at": None},
        {"$set": {"claim.started_at": now}},
    )

    # --- 2. transferring: idempotent steps, flags tick as each lands ------
    _sessions().update_one(
        {"guest_id": gid, "claim.email": email},
        {"$set": {"claim.state": "transferring"}},
    )

    # 2a. records: this session's unclaimed tailoring_records -> the account.
    rec_result = _db().tailoring_records.update_many(
        {"guest_id": gid, "user_email": None},
        {"$set": {"user_email": email, "updated_at": now}},
    )
    _tick_claim_step(gid, email, "records")

    # 2b. resume: attach ownership on the guest-owned resume_assets docs
    # (claim_status -> claimed, user_id/email attached, retention TTL
    # lifted). The claimed session's snapshots are retained too.
    resume_imported = False
    try:
        user_doc = _db().users.find_one({"email": email}, {"_id": 1})
        user_id = str(user_doc["_id"]) if user_doc else None
        had_resume = _db().resume_assets.find_one(
            {"user_email": email, "structured": {"$ne": None}}, {"_id": 1}
        ) is not None
        _db().resume_assets.update_many(
            {"guest_id": gid, "claim_status": {"$ne": "claimed"}},
            {"$set": {
                "claim_status": "claimed",
                "user_email": email,
                "user_id": user_id,
                "purge_after": None,
            }},
        )
        _snapshots().update_many(
            {"guest_id": gid}, {"$set": {"purge_after": None}}
        )
        claimed_rid = session.get("resume_id")
        if claimed_rid and not had_resume:
            from services import resume_access

            asset = resume_access.get_asset(claimed_rid)
            if asset and asset.get("structured"):
                resume_imported = True
                # Adopt as the account's active resume only when it has none.
                resume_access.adopt_active_if_unset(email, claimed_rid)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("guest claim: resume attach failed for %s: %s", gid, e)
    else:
        _tick_claim_step(gid, email, "resume")

    # 2c. link the account on the quota subject.
    if subject_id:
        _subjects().update_one(
            {"subject_id": subject_id},
            {"$addToSet": {"linked_accounts": email}, "$set": {"last_seen_at": now}},
        )
        _tick_claim_step(gid, email, "subject_link")

    # 2d. seed day-1 FREE usage once per (subject, day) — the existing
    # guest_claim_carryins unique (subject_id, date) marker is the double
    # guard, so an interrupted-claim resume can never double-seed.
    tailors_carried = 0
    carried = 0
    if subject_id:
        subj = _subjects().find_one({"subject_id": subject_id}, {"tailors_used": 1}) or {}
        try:
            carried = max(0, int(subj.get("tailors_used") or 0))
        except (TypeError, ValueError):
            carried = 0
    if carried > 0 and subject_id:
        try:
            from services import entitlements_service

            if entitlements_service.get_plan(email) == entitlements_service.FREE:
                marker = _db().guest_claim_carryins.update_one(
                    {"subject_id": subject_id, "date": _today_key()},
                    {
                        "$setOnInsert": {
                            "subject_id": subject_id,
                            "date": _today_key(),
                            "email": email,
                            "created_at": now,
                        }
                    },
                    upsert=True,
                )
                if marker.upserted_id is not None:  # we won the once-per-day marker
                    from services import usage_service

                    usage_service.increment_usage(email, n=carried)
                    tailors_carried = carried
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("guest claim: usage seed failed for %s: %s", gid, e)
    _tick_claim_step(gid, email, "carryin")

    # --- 3. completed: legacy claimed_by/claimed_at + claim.state ---------
    _sessions().update_one(
        {"guest_id": gid, "claim.email": email},
        {"$set": {
            "claimed_by": email,
            "claimed_at": now,
            "claim.state": "completed",
            "claim.completed_at": now,
            "last_seen_at": now,
        }},
    )

    used, limit = _daily_usage_snapshot(email)
    logger.info(
        "guest claim: %s claimed (records=%d resume_imported=%s carried=%d)",
        gid, rec_result.modified_count, resume_imported, tailors_carried,
    )
    return {
        "claimed": True,
        "already_claimed": False,
        "resume_imported": resume_imported,
        "records_transferred": int(rec_result.modified_count),
        "tailors_carried": tailors_carried,
        "daily_tailors_used": used,
        "daily_tailors_limit": limit,
    }
