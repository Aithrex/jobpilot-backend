"""Admin ops-cockpit read models (Compass §6.9).

Read-only aggregations over EXISTING collections for the admin cockpit, plus two
write paths that exist to improve the product and keep PII reveals attributable:

  * `audit_log`   — one row per PII un-redaction (actor / action / target / at).
  * `eval_ratings`— human quality ratings of generated content (essays, posts,
                    learning plans, authored resumes) for the rating widget.

Every function here assumes the caller is ALREADY `@admin_required`-gated at the
route — there is NO auth in this module. PII is redacted by default; the only
un-redacted read path writes an `audit_log` row so every reveal is traceable.

No LLM calls. No schema changes. Queries are bounded (tight limits, short
windows) because this repo defines no indexes — most reads are COLLSCAN.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

# resume_jobs.job_type values that spend Bedrock tokens. Non-LLM jobs
# (gmail_sync, event_scrape, job_search) are intentionally excluded from the
# LLM spend proxy. Keep in sync with resume_service._process_job branches.
LLM_JOB_TYPES = {
    "extract_jd", "learning_plan", "post_draft", "vg_extract",
    "vg_authored_resume", "six_seconds", "referral_draft", "upload_parse",
    "tailor", "regenerate", "cover_letter", "extract_and_tailor",
    "batch_tailor_item", "ats_scores", "daily_pipeline",
}

# Stale "processing" jobs older than this are treated as stuck (queue-depth
# proxy — the repo has no real queue; async work is Lambda self-invoke).
STALE_PROCESSING_MINUTES = 15

# Human-rating sample sources. kind -> how to read + preview a recent doc.
# (collection, id_field, preview_fn) — preview_fn returns a short text blurb.
_SAMPLE_KINDS = ("essay", "post", "learning_plan", "authored_resume")

# Ratings are 1..5 stars.
RATING_MIN, RATING_MAX = 1, 5


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _db():
    return DBConnect().get_db()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v: Any) -> Optional[str]:
    if not hasattr(v, "isoformat"):
        return None
    s = v.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


def _safe_count(coll, filt: Dict[str, Any]) -> int:
    try:
        return int(coll.count_documents(filt))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("count failed on %s: %s", getattr(coll, "name", "?"), e)
        return 0


def _mask_email(email: Optional[str]) -> Optional[str]:
    """h****@d****.com — enough to disambiguate without exposing the address."""
    if not email or "@" not in str(email):
        return None
    local, _, domain = str(email).partition("@")
    dom_name, _, tld = domain.partition(".")

    def _m(s: str) -> str:
        s = s or ""
        if len(s) <= 1:
            return (s or "*") + "***"
        return s[0] + "****"

    masked = f"{_m(local)}@{_m(dom_name)}"
    return f"{masked}.{tld}" if tld else masked


def _mask_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    name = str(name).strip()
    return (name[0] + "***") if name else None


def _redact_user(u: Dict[str, Any]) -> Dict[str, Any]:
    """Redacted user view: mask identifiers, drop deep-PII / security fields."""
    return {
        "id": str(u.get("_id")),
        "email": _mask_email(u.get("email")),
        "name": _mask_name(u.get("name")),
        "role": u.get("role"),
        "sector": u.get("sector"),
        "plan": u.get("plan") or "free",
        "created_at": _iso(u.get("created_at")),
        "last_login": _iso(u.get("last_login")),
        "redacted": True,
    }


def _full_user(u: Dict[str, Any]) -> Dict[str, Any]:
    """Un-redacted user view (audited caller only)."""
    return {
        "id": str(u.get("_id")),
        "email": u.get("email"),
        "name": u.get("name"),
        "role": u.get("role"),
        "sector": u.get("sector"),
        "plan": u.get("plan") or "free",
        "created_at": _iso(u.get("created_at")),
        "last_login": _iso(u.get("last_login")),
        "last_login_ip": u.get("last_login_ip"),
        "login_attempts": int(u.get("login_attempts", 0)),
        "fingerprint_hash": (u.get("fingerprints") or [None])[0],
        "session_id": u.get("session_id"),
        "auth_providers": u.get("auth_providers", []),
        "stripe_customer_id": u.get("stripe_customer_id"),
        "redacted": False,
    }


# ---------------------------------------------------------------------------
# Audit log (write) — every PII reveal is attributable
# ---------------------------------------------------------------------------
def write_audit(actor: Optional[str], action: str, target: Optional[str],
                meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Append an audit row. Soft-fails so an audit write never breaks a read.

    Callers should always pass the authenticated admin email as `actor`.
    """
    doc: Dict[str, Any] = {
        "actor": actor or "unknown",
        "action": InputSanitizer.sanitize_string(action or "", 120),
        "target": target,
        "at": _now(),
    }
    if meta:
        doc["meta"] = meta
    try:
        _db().audit_log.insert_one(dict(doc))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("audit_log write failed (%s/%s): %s", actor, action, e)
    doc["at"] = _iso(doc["at"])
    return doc


def recent_audit(limit: int = 100) -> List[Dict[str, Any]]:
    """Recent audit rows for the cockpit's audit panel."""
    limit = max(1, min(int(limit or 100), 500))
    out: List[Dict[str, Any]] = []
    try:
        for d in _db().audit_log.find({}).sort("at", -1).limit(limit):
            out.append({
                "actor": d.get("actor"),
                "action": d.get("action"),
                "target": d.get("target"),
                "at": _iso(d.get("at")),
                "meta": d.get("meta"),
            })
    except Exception as e:
        logger.warning("recent_audit failed: %s", e)
    return out


# ---------------------------------------------------------------------------
# Ops health — ingestion / job health, queue depth, LLM spend proxy, error rate
# ---------------------------------------------------------------------------
def ops_health(window_hours: int = 24) -> Dict[str, Any]:
    """Job/ingestion health over the last `window_hours` + today's LLM volume.

    LLM "spend" is a CALL-COUNT proxy only — real Bedrock dollar cost needs
    CloudWatch / Cost Explorer and is out of scope for a Mongo read model.
    """
    window_hours = max(1, min(int(window_hours or 24), 24 * 14))
    db = _db()
    now = _now()
    since = now - timedelta(hours=window_hours)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Job health: counts by (type, status) within the window.
    by_type: Dict[str, Dict[str, int]] = {}
    try:
        cur = db.resume_jobs.aggregate([
            {"$match": {"created_at": {"$gte": since}}},
            {"$group": {"_id": {"t": "$job_type", "s": "$status"}, "n": {"$sum": 1}}},
        ])
        for row in cur:
            key = row.get("_id") or {}
            t = key.get("t") or "unknown"
            s = key.get("s") or "unknown"
            by_type.setdefault(t, {})
            by_type[t][s] = by_type[t].get(s, 0) + int(row.get("n", 0))
    except Exception as e:
        logger.warning("ops_health job aggregation failed: %s", e)

    completed = sum(c.get("completed", 0) for c in by_type.values())
    failed = sum(c.get("failed", 0) for c in by_type.values())
    processing = sum(c.get("processing", 0) for c in by_type.values())
    total = completed + failed + processing
    error_rate = round(failed / total, 4) if total else 0.0

    # Queue-depth proxy: currently-processing + stale (stuck) processing jobs.
    stale_processing = _safe_count(db.resume_jobs, {
        "status": "processing",
        "created_at": {"$lt": now - timedelta(minutes=STALE_PROCESSING_MINUTES)},
    })

    # LLM spend proxy: LLM job calls created today, by type.
    llm_by_type: Dict[str, int] = {}
    try:
        cur = db.resume_jobs.aggregate([
            {"$match": {
                "created_at": {"$gte": start_of_day},
                "job_type": {"$in": sorted(LLM_JOB_TYPES)},
            }},
            {"$group": {"_id": "$job_type", "n": {"$sum": 1}}},
        ])
        llm_by_type = {row["_id"]: int(row.get("n", 0)) for row in cur if row.get("_id")}
    except Exception as e:
        logger.warning("ops_health llm aggregation failed: %s", e)

    return {
        "generated_at": _iso(now),
        "window_hours": window_hours,
        "jobs": {
            "total": total,
            "completed": completed,
            "failed": failed,
            "processing": processing,
            "error_rate": error_rate,
            "by_type": by_type,
        },
        "queue": {
            "processing": processing,
            "stale_processing": stale_processing,
            "stale_threshold_minutes": STALE_PROCESSING_MINUTES,
        },
        "llm_spend_proxy": {
            "date": now.strftime("%Y-%m-%d"),
            "calls_by_type": llm_by_type,
            "calls_total": sum(llm_by_type.values()),
            "note": (
                "Call-counts only. Real Bedrock cost requires CloudWatch / Cost "
                "Explorer per model — treat these as a volume proxy, not dollars."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Users — search + detail (PII redacted by default; un-redact is audited)
# ---------------------------------------------------------------------------
def user_search(query: str, limit: int = 25) -> Dict[str, Any]:
    """Find users by email/name fragment. Results are PII-redacted for display
    but carry the real `email` as the actionable key (the admin already typed a
    fragment of it) so the UI can open a (still-redacted) detail view.
    """
    q = InputSanitizer.sanitize_string(query or "", 200).strip()
    if not q:
        return {"query": "", "results": []}

    # NEVER let user input reach an operator position: escape it and build the
    # regex server-side. The operator ($regex) is ours; only the literal is theirs.
    safe = re.escape(q)
    limit = max(1, min(int(limit or 25), 100))
    filt = {"$or": [
        {"email": {"$regex": safe, "$options": "i"}},
        {"name": {"$regex": safe, "$options": "i"}},
    ]}

    results: List[Dict[str, Any]] = []
    try:
        for u in _db().users.find(filt).sort("created_at", -1).limit(limit):
            view = _redact_user(u)
            view["email_key"] = u.get("email")  # actionable lookup key for detail
            results.append(view)
    except Exception as e:
        logger.warning("user_search failed: %s", e)
    return {"query": q, "results": results, "count": len(results)}


def user_detail(user_email: str, redact: bool = True,
                actor: Optional[str] = None) -> Dict[str, Any]:
    """User detail for the cockpit.

    PII is redacted by default. Pass `redact=False` to reveal full PII — that
    path writes an `audit_log` row (actor / action / target / at) and REQUIRES
    an `actor` (the authenticated admin email).
    """
    email = InputSanitizer.sanitize_email(user_email) or InputSanitizer.sanitize_string(user_email or "", 320)
    if not email:
        return {"error": "invalid_email"}

    u = _db().users.find_one({"email": email})
    if not u:
        return {"error": "not_found", "email": _mask_email(email)}

    if redact:
        user_view = _redact_user(u)
    else:
        # Audited reveal — attribute every un-redaction.
        write_audit(actor, "unredact_user_pii", email)
        user_view = _full_user(u)

    db = _db()
    activity = {
        "base_resumes": _safe_count(db.user_resumes, {"user_email": email, "type": "base"}),
        "generated_resumes": _safe_count(db.user_resumes, {"user_email": email, "type": "generated"}),
        "authored_resumes": _safe_count(db.authored_resumes, {"user_email": email}),
        "tailoring_sessions": _safe_count(db.tailoring_records, {"user_email": email, "deleted": {"$ne": True}}),
        "post_drafts": _safe_count(db.post_drafts, {"user_email": email}),
        "learning_paths": _safe_count(db.learning_paths, {"user_email": email}),
    }
    return {"user": user_view, "activity": activity, "redacted": bool(redact)}


# ---------------------------------------------------------------------------
# Content samples + human ratings (this is how the product improves)
# ---------------------------------------------------------------------------
def _sample_essay(d: Dict[str, Any]) -> Dict[str, Any]:
    variants = d.get("variants") or []
    first = variants[0] if variants else ""
    if isinstance(first, dict):
        first = first.get("text") or first.get("answer") or ""
    return {
        "sample_id": str(d.get("_id")),
        "context": {"company": d.get("company"), "role": d.get("role"), "question": d.get("question")},
        "preview": (str(first) or "")[:600],
    }


def _sample_post(d: Dict[str, Any]) -> Dict[str, Any]:
    variants = d.get("variants") or []
    first = variants[0] if variants else ""
    if isinstance(first, dict):
        first = first.get("text") or first.get("body") or first.get("content") or ""
    return {
        "sample_id": d.get("draft_id") or str(d.get("_id")),
        "context": {"audience": d.get("audience"), "relation": d.get("relation")},
        "preview": (str(first) or "")[:600],
    }


def _sample_learning_plan(d: Dict[str, Any]) -> Dict[str, Any]:
    modules = d.get("modules") or []
    return {
        "sample_id": d.get("path_id") or str(d.get("_id")),
        "context": {"topic": d.get("topic"), "target_role": d.get("target_role"), "modules": len(modules)},
        "preview": (str(d.get("title") or "") + " — " + str(d.get("summary") or ""))[:600],
    }


def _sample_authored_resume(d: Dict[str, Any]) -> Dict[str, Any]:
    scaffold = d.get("structured") or {}
    summary = ""
    if isinstance(scaffold, dict):
        summary = scaffold.get("summary") or scaffold.get("headline") or ""
    return {
        "sample_id": d.get("resume_id") or str(d.get("_id")),
        "context": {"style_dial": d.get("style_dial"), "voice_distance": d.get("voice_distance")},
        "preview": (str(summary) or "")[:600],
    }


_SAMPLE_SPEC = {
    "essay": ("application_answers", _sample_essay),
    "post": ("post_drafts", _sample_post),
    "learning_plan": ("learning_paths", _sample_learning_plan),
    "authored_resume": ("authored_resumes", _sample_authored_resume),
}


def content_samples(kind: str, limit: int = 20) -> Dict[str, Any]:
    """Sample recent generated content of `kind` for the human-rating widget.

    kind ∈ {essay, post, learning_plan, authored_resume}. Author email is
    masked (this is a quality-rating surface, not a user-inspection one).
    """
    kind = InputSanitizer.sanitize_string(kind or "", 40).strip().lower()
    spec = _SAMPLE_SPEC.get(kind)
    if not spec:
        return {"error": "invalid_kind", "valid_kinds": list(_SAMPLE_SPEC.keys())}
    coll_name, builder = spec
    limit = max(1, min(int(limit or 20), 100))

    out: List[Dict[str, Any]] = []
    try:
        coll = _db()[coll_name]
        for d in coll.find({}).sort("created_at", -1).limit(limit):
            row = builder(d)
            row["kind"] = kind
            row["user_email_masked"] = _mask_email(d.get("user_email"))
            row["created_at"] = _iso(d.get("created_at"))
            out.append(row)
    except Exception as e:
        logger.warning("content_samples failed for %s: %s", kind, e)
    return {"kind": kind, "samples": out, "count": len(out)}


def rate_sample(kind: str, sample_id: str, rating: int, rater: Optional[str],
                notes: Optional[str] = None) -> Dict[str, Any]:
    """Write a human quality rating into `eval_ratings`. 1..5 stars.

    This is the product-improvement signal — keep it cheap and append-only.
    """
    kind = InputSanitizer.sanitize_string(kind or "", 40).strip().lower()
    if kind not in _SAMPLE_SPEC:
        return {"error": "invalid_kind", "valid_kinds": list(_SAMPLE_SPEC.keys())}
    sid = InputSanitizer.sanitize_string(sample_id or "", 200).strip()
    if not sid:
        return {"error": "missing_sample_id"}
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return {"error": "invalid_rating"}
    if not (RATING_MIN <= rating <= RATING_MAX):
        return {"error": "invalid_rating", "range": [RATING_MIN, RATING_MAX]}

    doc = {
        "kind": kind,
        "sample_id": sid,
        "rating": rating,
        "rater": rater or "unknown",
        "notes": InputSanitizer.sanitize_string(notes or "", 2000) or None,
        "at": _now(),
    }
    try:
        _db().eval_ratings.insert_one(dict(doc))
    except Exception as e:
        logger.warning("rate_sample write failed: %s", e)
        return {"error": "write_failed"}
    doc["at"] = _iso(doc["at"])
    return {"ok": True, "rating": doc}


# ---------------------------------------------------------------------------
# Plan distribution + quota breaches
# ---------------------------------------------------------------------------
def plan_distribution() -> Dict[str, Any]:
    """User counts by plan (missing/invalid plan folds into 'free')."""
    from services import entitlements_service as ent
    counts = {p: 0 for p in ent.VALID_PLANS}
    total = 0
    try:
        cur = _db().users.aggregate([
            {"$group": {"_id": "$plan", "n": {"$sum": 1}}},
        ])
        for row in cur:
            plan = row.get("_id")
            plan = plan if plan in ent.VALID_PLANS else ent.DEFAULT_PLAN
            counts[plan] = counts.get(plan, 0) + int(row.get("n", 0))
            total += int(row.get("n", 0))
    except Exception as e:
        logger.warning("plan_distribution failed: %s", e)
    return {"total": total, "by_plan": counts}


def quota_breaches(limit: int = 200) -> Dict[str, Any]:
    """Best-effort recent quota breaches (users at/over a metered limit).

    Scans today's `tailor_usage` and the current period's `feature_usage`,
    resolves each user's per-plan limit via entitlements, and flags rows where
    used >= limit. Bounded scan (no indexes); the plan lookup is cached per user.
    """
    from services import entitlements_service as ent

    limit = max(1, min(int(limit or 200), 1000))
    db = _db()
    now = _now()
    today = now.strftime("%Y-%m-%d")

    plan_cache: Dict[str, str] = {}

    def _plan_for(email: str) -> str:
        if email not in plan_cache:
            try:
                plan_cache[email] = ent.get_plan(email)
            except Exception:
                plan_cache[email] = ent.DEFAULT_PLAN
        return plan_cache[email]

    breaches: List[Dict[str, Any]] = []

    # Tailoring quota (metered in tailor_usage, per-user override wins).
    try:
        from services import usage_service
        for rec in db.tailor_usage.find({"date": today}).limit(limit):
            email = rec.get("email")
            if not email:
                continue
            used = int(rec.get("count", 0))
            try:
                lim = usage_service.get_user_limit(email)
            except Exception:
                lim = None
            if isinstance(lim, int) and lim >= 0 and used >= lim:
                breaches.append({
                    "email": _mask_email(email),
                    "feature": "tailor",
                    "used": used,
                    "limit": lim,
                    "period": "day",
                })
    except Exception as e:
        logger.warning("quota_breaches tailor scan failed: %s", e)

    # Other metered features (feature_usage). date key is day OR month.
    month = now.strftime("%Y-%m")
    try:
        for rec in db.feature_usage.find({"date": {"$in": [today, month]}}).limit(limit):
            email = rec.get("email") or rec.get("user_email")
            feature = rec.get("feature")
            if not email or not feature:
                continue
            used = int(rec.get("count", 0))
            try:
                lim = ent.quota(_plan_for(email), feature)
            except Exception:
                lim = None
            if isinstance(lim, int) and used >= lim:
                breaches.append({
                    "email": _mask_email(email),
                    "feature": feature,
                    "used": used,
                    "limit": lim,
                    "period": ent.FEATURE_PERIODS.get(feature, "day"),
                })
    except Exception as e:
        logger.warning("quota_breaches feature scan failed: %s", e)

    breaches = breaches[:limit]
    return {"generated_at": _iso(now), "count": len(breaches), "breaches": breaches}
