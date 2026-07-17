"""Guest Onboarding service — sessions, quotas, matching, claim.

Backs the guest blueprints (resume Lambda `/api/resume/guest/*`, jobs Lambda
`/api/jobs/guest/matches`) and the guest async-job branches in
services.resume_service._process_job. Lives in the shared layer because the
guest domain spans BOTH the resume and jobs Lambdas (same rationale as
job_service/resume_service — see the coupling note in CLAUDE.md).

Key properties:
  * guest_id is a uuid4 capability token — validated as UUID before ANY Mongo
    filter; 404/generic responses on mismatch (no existence oracle).
  * Quota = LIFETIME `guest_tailor_limit` per identity CLUSTER (sum across
    merged sessions), plus a platform-wide per-day circuit breaker on
    anonymous LLM spend (`global_daily_guest_tailors`, atomic $inc).
  * Matched jobs are cached per guest per day in the session doc.
  * Claim is an atomic ownership flip guarded by the claimed_by field, so a
    double claim can never double-seed the user's daily tailor usage.
  * No resume PII in logs — guest_ids/counters only (spec §Security 4).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
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

_FLAG_TRUE = {"1", "true", "yes", "on"}
_FLAG_FALSE = {"0", "false", "no", "off"}

# Whitelisted job-card fields returned to guests (no internal scoring debris).
_JOB_CARD_FIELDS = (
    "title", "company", "location", "apply_link", "url", "source",
    "posted", "tier",
)


def _db():
    return DBConnect().get_db()


def _sessions():
    return _db().guest_sessions


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
            _sessions().update_one({"guest_id": gid}, update)
            existing.update(update["$set"])
            return existing

    new_id = str(uuid.uuid4())
    doc = {
        "guest_id": new_id,
        "created_at": _now(),
        "last_seen_at": _now(),
        "fingerprint_hash": fp or None,
        "ip_hash": ip_hash or None,  # null when GUEST_IP_HASH_SALT is unset
        "identity": {
            "name_normalized": "",
            "emails": [],
            "phones": [],
            "resume_hashes": [],
        },
        "resume": None,
        "matched_jobs": None,
        "tailors_used": 0,
        "tailor_record_ids": [],
        "merged_into": None,
        "claimed_by": None,
        "claimed_at": None,
    }
    _sessions().insert_one(doc)
    doc.pop("_id", None)
    logger.info("guest session created: %s", new_id)
    return doc


def attach_parsed_resume(
    guest_id: str,
    structured: Dict[str, Any],
    raw_text: str,
    filename: str,
    fingerprint_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """Store the parsed resume on the session, extract identity signals and
    run soft 2-of-N resolution/merge. Returns the recognition summary."""
    gid = validate_guest_id(guest_id)
    if not gid:
        raise ValueError("invalid guest_id")

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
    }
    if signals["fingerprint"]:
        set_fields["fingerprint_hash"] = signals["fingerprint"]
    add_to_set: Dict[str, Any] = {}
    if signals["emails"]:
        add_to_set["identity.emails"] = {"$each": signals["emails"]}
    if signals["phones"]:
        add_to_set["identity.phones"] = {"$each": signals["phones"]}
    if signals["resume_hash"]:
        add_to_set["identity.resume_hashes"] = {"$each": [signals["resume_hash"]]}
    update: Dict[str, Any] = {"$set": set_fields}
    if add_to_set:
        update["$addToSet"] = add_to_set
    result = _sessions().update_one({"guest_id": gid}, update)
    if result.matched_count == 0:
        raise ValueError("guest session not found")

    recognition = identity.resolve_and_merge(gid, signals)
    limit = pconfig.get_int("guest_tailor_limit")
    used = identity.cluster_tailors_used(gid)
    return {
        "recognized": bool(recognition.get("recognized")),
        "recognized_name": recognition.get("recognized_name") or "",
        "tailors_used": used,
        "tailors_limit": limit,
    }


def session_summary(guest_id: Optional[str], fingerprint_hash: Optional[str]) -> Dict[str, Any]:
    """GET /guest/session payload. If guest_id is unknown but the fingerprint
    matches an identity cluster, return the designed recognition response."""
    doc = get_session(guest_id) if guest_id else None
    if doc:
        limit = pconfig.get_int("guest_tailor_limit")
        used = identity.cluster_tailors_used(doc["guest_id"])
        cached = doc.get("matched_jobs") or {}
        matched_today = (
            len(cached.get("jobs") or []) if cached.get("date") == _today_key() else 0
        )
        return {
            "exists": True,
            "recognized": False,
            "recognized_name": (doc.get("identity") or {}).get("name_normalized") or "",
            "tailors_used": used,
            "tailors_limit": limit,
            "has_resume": bool((doc.get("resume") or {}).get("structured")),
            "matched_jobs_today": matched_today,
            "claimed": bool(doc.get("claimed_by")),
        }

    # Unknown guest_id — fingerprint-only recognition (single signal: it may
    # RECOGNIZE for the "welcome back" banner but never links quota by itself).
    # A fingerprint is low-entropy/guessable, so a single-signal hit NEVER
    # reveals the stored name (disclosure oracle); recognized_name is only
    # returned to the guest_id capability holder above — i.e. 2+ signals,
    # matching the spec's own identity rule.
    fp = identity.sanitize_fingerprint(fingerprint_hash)
    if fp:
        match = _sessions().find_one({"fingerprint_hash": fp}, {"claimed_by": 1})
        if match:
            return {
                "exists": False,
                "recognized": True,
                "recognized_name": "",
                "claimed": bool(match.get("claimed_by")),
            }
    return {"exists": False, "recognized": False}


# ---------------------------------------------------------------------------
# Quotas — cluster lifetime limit + global daily circuit breaker
# ---------------------------------------------------------------------------

def tailor_quota_state(guest_id: str) -> Dict[str, Any]:
    limit = pconfig.get_int("guest_tailor_limit")
    used = identity.cluster_tailors_used(guest_id)
    return {
        "tailors_used": used,
        "tailors_limit": limit,
        "remaining": max(0, limit - used),
    }


def check_guest_tailor_quota(guest_id: str) -> Tuple[bool, Dict[str, Any]]:
    state = tailor_quota_state(guest_id)
    return state["remaining"] > 0, state


def try_charge_guest_tailor(guest_id: str) -> Tuple[bool, Dict[str, Any]]:
    """Atomically charge ONE tailor against the cluster lifetime limit.

    Increment-then-verify-and-rollback (same pattern as the global circuit
    breaker): $inc this session's tailors_used first (atomic per doc), then
    verify the cluster SUM; over the limit → roll our $inc back and report
    exhausted. Concurrent charges each see the other's increment in the sum,
    so the cluster can never exceed the cap (a check-then-inc TOCTOU could).
    Returns (charged, quota_state); on failure the state carries the same
    fields _quota_exhausted_response renders (429 {quota_exhausted:true}).
    """
    gid = validate_guest_id(guest_id)
    limit = pconfig.get_int("guest_tailor_limit")
    if not gid:
        return False, {"tailors_used": 0, "tailors_limit": limit, "remaining": 0}
    if limit <= 0:
        return False, {
            "tailors_used": identity.cluster_tailors_used(gid),
            "tailors_limit": limit,
            "remaining": 0,
        }
    charged = _sessions().find_one_and_update(
        {"guest_id": gid},
        {"$inc": {"tailors_used": 1}, "$set": {"last_seen_at": _now()}},
    )
    if charged is None:  # unknown session — nothing was incremented
        return False, {"tailors_used": 0, "tailors_limit": limit, "remaining": 0}
    used = identity.cluster_tailors_used(gid)  # includes our +1
    if used > limit:
        _sessions().update_one({"guest_id": gid}, {"$inc": {"tailors_used": -1}})
        return False, {
            "tailors_used": used - 1,
            "tailors_limit": limit,
            "remaining": max(0, limit - (used - 1)),
        }
    return True, {
        "tailors_used": used,
        "tailors_limit": limit,
        "remaining": max(0, limit - used),
    }


def increment_guest_tailors(guest_id: str, n: int = 1) -> None:
    gid = validate_guest_id(guest_id)
    if not gid:
        return
    _sessions().update_one(
        {"guest_id": gid},
        {"$inc": {"tailors_used": int(n)}, "$set": {"last_seen_at": _now()}},
    )


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
# Matched jobs (free direct sources only) + per-day cache
# ---------------------------------------------------------------------------

def get_cached_matches(guest_id: str) -> Optional[Dict[str, Any]]:
    doc = get_session(guest_id)
    if not doc:
        return None
    cached = doc.get("matched_jobs") or {}
    if cached.get("date") != _today_key():
        return None
    computed_at = cached.get("computed_at")
    return {
        "jobs": cached.get("jobs") or [],
        "date": cached.get("date"),
        "computed_at": computed_at.isoformat() if hasattr(computed_at, "isoformat") else computed_at,
        "min_score": cached.get("min_score"),
        "cached": True,
    }


def _store_matches(guest_id: str, jobs: List[Dict[str, Any]], min_score: int) -> None:
    _sessions().update_one(
        {"guest_id": guest_id},
        {
            "$set": {
                "matched_jobs": {
                    "date": _today_key(),
                    "jobs": jobs,
                    "computed_at": _now(),
                    "min_score": min_score,
                },
                "last_seen_at": _now(),
            }
        },
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


def _resume_skills(structured: Dict[str, Any]) -> List[str]:
    from schemas.resume_schemas import flatten_skills

    seen, out = set(), []
    for s in flatten_skills(structured) or []:
        n = str(s or "").strip().lower()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def compute_guest_matches(guest_id: str) -> Dict[str, Any]:
    """Async-job body for `guest_matches`: fetch free sources, score each row
    with jd_match_scorer against the guest's parsed resume, keep only
    score >= guest_jobs_min_score, cap at guest_jobs_max, cache per day."""
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
    max_jobs = int(cfg.get("guest_jobs_max", 8))

    from services.jd_match_scorer import score_one

    skills = _resume_skills(structured)
    rows = _fetch_free_jobs(structured)

    scored: List[Dict[str, Any]] = []
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
        card["match_score"] = verdict["match_score"]
        card["matched_skills"] = verdict.get("matched_skills") or []
        scored.append(card)

    scored.sort(key=lambda r: r.get("match_score", 0), reverse=True)
    top = scored[:max_jobs]
    _store_matches(gid, top, min_score)
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
# Claim — atomic ownership flip after signup/login
# ---------------------------------------------------------------------------

def claim_session(guest_id: str, user_email: str) -> Dict[str, Any]:
    """Flip guest artifacts to the authenticated user. Idempotent:

      1. Atomic claimed_by guard (find_one_and_update unclaimed→claimed) —
         ALL transfer steps below run only for the winner of this flip, so a
         concurrent claim by another account transfers nothing and gets
         already_claimed, and a repeat claim by the same account is a no-op.
      2. Guest tailoring_records (user_email=None + guest_id in cluster) get
         user_email set (winner only).
      3. The parsed resume is copied into user_resumes IF the user has none
         (winner only).
      4. Today's tailor usage is seeded ONCE (never double-counts with
         usage_service).
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

    ids = identity.cluster_ids(gid)
    carried = identity.cluster_tailors_used(gid)

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
        return {
            "claimed": True,
            "first_claim": False,
            "records_transferred": 0,
            "resume_copied": False,
            "carried_tailors": 0,
        }

    # Winner only from here on.
    # Mark the rest of the cluster too (recognition/no-reuse), guarded the
    # same way so a concurrent claim of a sibling doesn't overwrite.
    _sessions().update_many(
        {"guest_id": {"$in": ids}, "claimed_by": None},
        {"$set": {"claimed_by": email, "claimed_at": _now()}},
    )

    # --- 2. ownership flip on guest tailor records (winner only) ----------
    rec_result = _db().tailoring_records.update_many(
        {"guest_id": {"$in": ids}, "user_email": None},
        {"$set": {"user_email": email, "updated_at": _now()}},
    )

    # --- 3. copy resume into user_resumes if the user has none ------------
    resume_copied = False
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
                resume_copied = True
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("guest claim: resume copy failed for %s: %s", gid, e)

    # --- 4. seed today's usage ONCE (guarded by the claimed flip) ---------
    usage_seeded = 0
    if first_claim and carried > 0:
        try:
            from services import usage_service

            usage_service.increment_usage(email, n=carried)
            usage_seeded = carried
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("guest claim: usage seed failed for %s: %s", gid, e)

    logger.info(
        "guest claim: %s claimed (first=%s records=%d resume_copied=%s carried=%d)",
        gid, first_claim, rec_result.modified_count, resume_copied, usage_seeded,
    )
    return {
        "claimed": True,
        "first_claim": first_claim,
        "records_transferred": int(rec_result.modified_count),
        "resume_copied": resume_copied,
        "carried_tailors": usage_seeded,
    }
