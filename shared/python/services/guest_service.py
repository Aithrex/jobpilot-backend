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
import logging
import os
import re
import secrets as _pysecrets
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
_JOB_CARD_FIELDS = (
    "title", "company", "location", "apply_link", "url", "source",
    "posted", "tier", "logo_url",
)


def _db():
    return DBConnect().get_db()


def _sessions():
    return _db().guest_sessions


def _subjects():
    return _db().guest_quota_subjects


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
    locally; see MANUAL_STEPS.md). With no salt configured we fail CLOSED for
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
    gid = validate_guest_id(guest_id)
    if not gid:
        return None
    return _sessions().find_one({"guest_id": gid})


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
        "matched_jobs": None,
        "tailor_record_ids": [],
        "merged_into": None,
        "claimed_by": None,
        "claimed_at": None,
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
                "matched_jobs": None,
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
) -> Dict[str, Any]:
    """Store the parsed resume on the session, extract v2 HMAC identity
    signals and run strong-signal resolution/merge. Returns the recognition
    summary (this session's OWN parsed name only — never another session's)."""
    gid = validate_guest_id(guest_id)
    if not gid:
        raise ValueError("invalid guest_id")

    session = _sessions().find_one({"guest_id": gid})
    if not session:
        raise ValueError("guest session not found")

    signals = identity.extract_signals(structured, fingerprint_hash, raw_text)
    set_fields: Dict[str, Any] = {
        "last_seen_at": _now(),
        "resume": {
            "filename": InputSanitizer.sanitize_string(filename or "", max_length=255),
            "structured": structured,
            "raw_text": (raw_text or "")[:60000],
            "uploaded_at": _now(),
        },
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
    """The subject-level cache doc, falling back to the session-level cache
    (the fallback carries the guests through a missing HMAC key — each session
    is then its own standalone subject anyway)."""
    sid = subject_for_guest(guest_id)
    if sid:
        subj = _subjects().find_one({"subject_id": sid}, {"matched_jobs": 1}) or {}
        cached = subj.get("matched_jobs")
        if cached:
            return cached
    doc = get_session(guest_id) or {}
    return doc.get("matched_jobs")


def get_cached_matches(guest_id: str) -> Optional[Dict[str, Any]]:
    """Same-day cached matches for the guest's quota subject (never re-spends
    the global matches budget). Rows are HARD-CAPPED at guest_jobs_max — the
    server never returns more (no hidden locked rows for CSS to hide)."""
    cached = _cache_doc_for_guest(guest_id)
    if not cached or cached.get("date") != _today_key():
        return None
    max_jobs = max(1, pconfig.get_int("guest_jobs_max"))
    computed_at = cached.get("computed_at")
    return {
        "jobs": list(cached.get("jobs") or [])[:max_jobs],
        "date": cached.get("date"),
        "computed_at": computed_at.isoformat() if hasattr(computed_at, "isoformat") else computed_at,
        "min_score": cached.get("min_score"),
        "cached": True,
    }


def find_cached_match(guest_id: str, match_id: str) -> Optional[Dict[str, Any]]:
    """Resolve a served match_id to its server-stored JD source
    ({title, company, jd_text, url}) — the ONLY tailorable reference. The JD
    never comes from the client (SSRF/injection posture)."""
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
    for card in cached.get("jobs") or []:
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
) -> None:
    cache = {
        "date": _today_key(),
        "jobs": jobs,
        "jd_sources": jd_sources or {},
        "computed_at": _now(),
        "min_score": min_score,
    }
    gid = validate_guest_id(guest_id)
    if not gid:
        return
    _sessions().update_one(
        {"guest_id": gid},
        {"$set": {"matched_jobs": cache, "last_seen_at": _now()}},
    )
    sid = subject_for_guest(gid)
    if sid:
        _subjects().update_one(
            {"subject_id": sid},
            {"$set": {"matched_jobs": cache, "last_seen_at": _now()}},
        )


def _guest_role_terms(structured: Dict[str, Any]) -> List[str]:
    """Derive up to 3 target-role search terms from the guest's own resume."""
    terms: List[str] = []
    seen = set()
    for exp in (structured.get("experience") or []):
        title = InputSanitizer.sanitize_string(str(exp.get("title") or ""), max_length=120).strip()
        if title and title.lower() not in seen:
            seen.add(title.lower())
            terms.append(title)
        if len(terms) >= 3:
            break
    return terms or ["software engineer"]


def _fetch_free_jobs(structured: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run the FREE direct sources (ATS boards + Workday CXS — the daily
    pipeline's skip_apify=True path; NO Apify, no paid actors) and return the
    flattened candidate rows. Module-level so tests can patch it."""
    from services.daily_pipeline_service import run_pipeline

    terms = _guest_role_terms(structured)
    result = run_pipeline(
        workday_titles=terms,
        custom_role_terms=terms,
        past_days=3,
        user_email="",           # anonymous — no profile/applied-index reads
        location="United States",
        experience_level="any",  # wide fetch; resume-match scoring filters below
        employment_type="ANY",
        work_arrangement="any",
        skip_apify=True,
    )
    return list(result.get("apply_now") or []) + list(result.get("verify_dates") or [])


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


def compute_guest_matches(guest_id: str) -> Dict[str, Any]:
    """Async-job body for `guest_matches`: fetch free sources, score each row
    with jd_match_scorer against the guest's parsed resume, keep only
    score >= guest_jobs_min_score, cap at guest_jobs_max (5), mint a match_id
    per card, add the deterministic `why` + public-filings `sponsorship`, and
    cache on the quota subject for the day."""
    gid = validate_guest_id(guest_id)
    if not gid:
        raise ValueError("invalid guest_id")
    doc = _sessions().find_one({"guest_id": gid})
    if not doc:
        raise ValueError("guest session not found")
    structured = ((doc.get("resume") or {}).get("structured")) or {}
    if not structured:
        raise ValueError("No parsed resume on this guest session")

    cfg = pconfig.get_guest_config()
    min_score = int(cfg.get("guest_jobs_min_score", 40))
    max_jobs = max(1, int(cfg.get("guest_jobs_max", 5)))

    from services.jd_match_scorer import score_one

    skills = _resume_skills(structured)
    rows = _fetch_free_jobs(structured)

    scored: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for row in rows:
        verdict = score_one(
            jd_text=str(row.get("description") or ""),
            title=str(row.get("title") or ""),
            resume_skills=skills,
        )
        if verdict["match_score"] < min_score:
            continue
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
        scored.append((card, row))

    scored.sort(key=lambda pair: pair[0].get("match_score", 0), reverse=True)
    top_pairs = scored[:max_jobs]

    # Public-filings sponsorship signal (one bulk Mongo read). Any failure or
    # absent ingest data → null on every card (honest "no data", never wrong).
    signal_cache: Dict[str, Any] = {}
    try:
        from services.sponsorship_service import bulk_signals
        companies = [str(c.get("company") or "") for c, _ in top_pairs if c.get("company")]
        if companies:
            signal_cache = bulk_signals(companies)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("guest matches: sponsorship lookup unavailable: %s", e)
        signal_cache = {}

    top: List[Dict[str, Any]] = []
    jd_sources: Dict[str, Dict[str, Any]] = {}
    for card, row in top_pairs:
        match_id = uuid.uuid4().hex  # opaque server-minted id — the only tailorable reference
        card["match_id"] = match_id
        card["sponsorship"] = _sponsorship_for_card(card.get("company"), signal_cache)
        jd_sources[match_id] = {
            "title": str(card.get("title") or ""),
            "company": str(card.get("company") or ""),
            "jd_text": str(row.get("description") or "")[:15000],
            "url": card.get("url") or card.get("apply_link") or "",
        }
        top.append(card)

    # Company logos — (re)stamped at cache-build time so every cached guest
    # card carries the server-derived logo_url AND the company_logos docs the
    # /api/jobs/logo route needs exist (the route never fetches for keys
    # without a doc). Overwrites any row-provided value: deterministic from
    # the company name, never client-influenced.
    try:
        from services.company_logo_service import stamp_logo_urls
        stamp_logo_urls(top)
    except Exception as e:  # pragma: no cover - logos must never break matches
        logger.warning("guest matches: logo stamping skipped: %s", type(e).__name__)

    _store_matches(gid, top, min_score, jd_sources=jd_sources)
    logger.info(
        "guest matches computed for %s: %d/%d rows cleared score>=%d",
        gid, len(top), len(rows), min_score,
    )
    return {
        "jobs": top,
        "date": _today_key(),
        "min_score": min_score,
        "total_considered": len(rows),
        "cached": False,
    }


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

    if fmt == "pdf":
        data = svc.renderer.generate_pdf(tailored)
    else:
        data = svc.renderer.generate_docx(tailored)
    if not data or len(data) < 100:
        raise ValueError("render produced no output")
    return data, filename


# ---------------------------------------------------------------------------
# Retention cleanup (claim-aware; never a blind TTL)
# ---------------------------------------------------------------------------

def run_retention_cleanup() -> Dict[str, Any]:
    """Delete ONLY expired unclaimed guest data: sessions with
    ``claimed_by: null`` whose ``last_seen_at`` is older than
    ``retention_days`` (0 = keep forever → skip), plus their unclaimed
    tailoring_records and stale guest resume_jobs. Claimed data is never
    touched. Subjects keep their quota counters (abuse memory)."""
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
    ids = [
        str(d["guest_id"])
        for d in _sessions().find(
            {"claimed_by": None, "last_seen_at": {"$lt": cutoff}}, {"guest_id": 1}
        )
    ]
    if not ids:
        return {
            "skipped": False,
            "retention_days": days,
            "sessions_deleted": 0,
            "records_deleted": 0,
            "jobs_deleted": 0,
        }
    recs = _db().tailoring_records.delete_many({"guest_id": {"$in": ids}, "user_email": None})
    jobs = _db().resume_jobs.delete_many({"guest_id": {"$in": ids}})
    sess = _sessions().delete_many({"guest_id": {"$in": ids}, "claimed_by": None})
    logger.info(
        "guest retention cleanup: sessions=%d records=%d jobs=%d (retention_days=%d)",
        sess.deleted_count, recs.deleted_count, jobs.deleted_count, days,
    )
    return {
        "skipped": False,
        "retention_days": days,
        "sessions_deleted": int(sess.deleted_count),
        "records_deleted": int(recs.deleted_count),
        "jobs_deleted": int(jobs.deleted_count),
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


def claim_session(guest_id: str, user_email: str) -> Dict[str, Any]:
    """Flip guest artifacts to the authenticated user (v2). Atomic +
    idempotent + exact-capability-only:

      1. Atomic claimed_by guard (find_one_and_update unclaimed→claimed) —
         all transfer steps run only for the winner of this flip; a repeat
         claim by the same account returns already_claimed:true with zero
         re-transfer; another account gets the already_claimed error (403).
      2. Transfers ONLY the presented capability's session chain (sessions
         already merged server-side by strong-signal resolution — never a
         wider soft cluster) + its unclaimed tailoring_records.
      3. Copies the parsed resume into user_resumes IF the user has none.
      4. Links the account email on the quota subject.
      5. Seeds day-1 FREE-plan usage exactly once per (subject, UTC day),
         double-guarded by the claim flip AND the guest_claim_carryins
         unique (subject_id, date) marker. Premium plans untouched.
    """
    gid = validate_guest_id(guest_id)
    if not gid:
        return {"error": "invalid_guest_id"}
    email = InputSanitizer.sanitize_email(user_email or "")
    if not email:
        return {"error": "invalid_user"}

    session = _sessions().find_one({"guest_id": gid})
    if not session:
        return {"error": "not_found"}

    already_claimed_by = session.get("claimed_by")
    if already_claimed_by and already_claimed_by != email:
        # Capability token was already consumed by another account.
        return {"error": "already_claimed"}

    subject_id = subject_for_guest(gid)

    # --- 1. atomic unclaimed→claimed flip (the idempotency guard) ---------
    flipped = _sessions().find_one_and_update(
        {"guest_id": gid, "claimed_by": None},
        {"$set": {"claimed_by": email, "claimed_at": _now(), "last_seen_at": _now()}},
    )
    first_claim = flipped is not None
    if not first_claim:
        # We lost the atomic flip (or this is a repeat claim). EVERY transfer
        # step below is reserved for the winner: a concurrent claim by another
        # account must do nothing (no PII transfer race), and a repeat claim
        # by the same account is a no-op (idempotent — no re-copy).
        current = _sessions().find_one({"guest_id": gid}, {"claimed_by": 1}) or {}
        if current.get("claimed_by") != email:
            return {"error": "already_claimed"}
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

    # Winner only from here on. EXACT-capability-only (v2): the claim covers
    # THIS session and the records its guest_id proves — sibling sessions in
    # the strong-signal cluster keep their own capabilities (another device's
    # session can be claimed later, by the same or a linked account; artifact
    # access never crosses guest_ids).

    # --- 2. ownership flip on THIS session's guest records (winner only) --
    rec_result = _db().tailoring_records.update_many(
        {"guest_id": gid, "user_email": None},
        {"$set": {"user_email": email, "updated_at": _now()}},
    )

    # --- 3. copy resume into user_resumes if the user has none ------------
    resume_imported = False
    resume = session.get("resume") or {}
    structured = resume.get("structured")
    if structured:
        existing = _db().user_resumes.find_one(
            {"user_email": email, "structured": {"$exists": True}}, {"_id": 1}
        )
        if not existing:
            try:
                from services.resume_parser import ResumeParser

                ResumeParser(_db()).save_parsed_resume(
                    structured,
                    resume.get("raw_text") or "",
                    user_email=email,
                )
                resume_imported = True
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("guest claim: resume copy failed for %s: %s", gid, e)

    # --- 4. link the account on the quota subject -------------------------
    if subject_id:
        _subjects().update_one(
            {"subject_id": subject_id},
            {"$addToSet": {"linked_accounts": email}, "$set": {"last_seen_at": _now()}},
        )

    # --- 5. seed day-1 FREE usage once per (subject, day) -----------------
    tailors_carried = 0
    carried = 0
    if subject_id:
        subj = _subjects().find_one({"subject_id": subject_id}, {"tailors_used": 1}) or {}
        try:
            carried = max(0, int(subj.get("tailors_used") or 0))
        except (TypeError, ValueError):
            carried = 0
    if first_claim and carried > 0 and subject_id:
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
                            "created_at": _now(),
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
