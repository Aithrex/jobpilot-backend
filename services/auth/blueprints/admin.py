"""Product admin blueprint (/api/admin).

The in-house replacement for the portfolio auth service's admin panel, scoped to
PRODUCT operations only (users, usage/quota, feedback, resumes, tailoring,
applications, interview-prep, activity). Portfolio-only admin (ephemeral
environments / infra) is intentionally dropped.

Every route is gated by @admin_required (users.is_admin, plus a seed allowlist
for fresh databases). Reads existing collections — no schema changes.
"""
import logging
import functools
from datetime import datetime, timezone, timedelta

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.db_connect import DBConnect
from services import usage_service
from services import admin_metrics_service as ops
from services import stripe_service as billing

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger(__name__)

ADMIN_EMAILS = {"harshith@aithrex.tech", "mannesiddardha@gmail.com"}
FEEDBACK_STATUSES = {"open", "responded", "resolved"}


def _db():
    return DBConnect().get_db()


def _now():
    return datetime.now(timezone.utc)


def _iso(v):
    if not hasattr(v, "isoformat"):
        return None
    s = v.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


def _is_admin(email: str) -> bool:
    if email in ADMIN_EMAILS:
        return True
    doc = _db().users.find_one({"email": email}, {"is_admin": 1})
    return bool(doc and doc.get("is_admin"))


def admin_required(fn):
    @functools.wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        email = get_jwt_identity()
        if not _is_admin(email):
            return jsonify({"error": "forbidden", "message": "Admin access required."}), 403
        return fn(*args, **kwargs)
    return wrapper


# ------------------------------------------------------------------
# Ops cockpit (§6.9) — health, redacted-by-default users, content ratings,
# audited PII reveal, per-user entitlement overrides.
# ------------------------------------------------------------------
@admin_bp.route("/ops/health", methods=["GET"])
@admin_required
def ops_health():
    try:
        hours = int(request.args.get("window_hours", 24))
    except (TypeError, ValueError):
        hours = 24
    return jsonify(ops.ops_health(hours)), 200


@admin_bp.route("/ops/user-search", methods=["GET"])
@admin_required
def ops_user_search():
    return jsonify(ops.user_search(request.args.get("q", ""))), 200


@admin_bp.route("/ops/user/<path:email>", methods=["GET"])
@admin_required
def ops_user_detail(email):
    unredact = request.args.get("unredact") in ("1", "true", "yes")
    return jsonify(ops.user_detail(email, redact=not unredact, actor=get_jwt_identity())), 200


@admin_bp.route("/ops/content-samples", methods=["GET"])
@admin_required
def ops_content_samples():
    return jsonify(ops.content_samples(request.args.get("kind", ""))), 200


@admin_bp.route("/ops/rate-sample", methods=["POST"])
@admin_required
def ops_rate_sample():
    d = request.get_json(force=True, silent=True) or {}
    res = ops.rate_sample(d.get("kind"), d.get("sample_id"), d.get("rating"), get_jwt_identity(), d.get("notes"))
    return jsonify(res), (200 if res.get("ok") else 400)


@admin_bp.route("/ops/plan-distribution", methods=["GET"])
@admin_required
def ops_plan_distribution():
    return jsonify(ops.plan_distribution()), 200


@admin_bp.route("/ops/quota-breaches", methods=["GET"])
@admin_required
def ops_quota_breaches():
    return jsonify(ops.quota_breaches()), 200


@admin_bp.route("/ops/audit", methods=["GET"])
@admin_required
def ops_audit():
    return jsonify({"audit": ops.recent_audit()}), 200


@admin_bp.route("/ops/override/<path:email>", methods=["GET"])
@admin_required
def ops_get_override(email):
    return jsonify(billing.get_override(email)), 200


@admin_bp.route("/ops/override/<path:email>", methods=["PUT"])
@admin_required
def ops_set_override(email):
    d = request.get_json(force=True, silent=True) or {}
    res = billing.set_override(email, d.get("overrides"), actor=get_jwt_identity())
    return jsonify(res), (400 if res.get("error") else 200)


# ---------------------------------------------------------------------------
# Guest Onboarding runtime config (platform_config._id="guest_onboarding")
# ---------------------------------------------------------------------------
# The admin tailoring-control surface for the guest funnel: quotas, score
# threshold, circuit breaker, retention — all editable at runtime, no deploy.
# Validation (types + sane bounds) lives in platform_config_service so the
# route stays thin. PUT writes audit fields (updated_at/updated_by).
@admin_bp.route("/guest-config", methods=["GET"])
@admin_required
def guest_config_get():
    from services import platform_config_service as pconfig
    from services.guest_service import guest_onboarding_env_enabled

    cfg = pconfig.get_guest_config(force_refresh=True)
    cfg["updated_at"] = _iso(cfg.get("updated_at"))
    # Surface the env kill-switch state so the admin UI can explain why the
    # feature is off even when the runtime `enabled` knob is on.
    cfg["env_flag_enabled"] = guest_onboarding_env_enabled()
    return jsonify(cfg), 200


@admin_bp.route("/guest-config", methods=["PUT"])
@admin_required
def guest_config_put():
    from services import platform_config_service as pconfig
    from services.guest_service import guest_onboarding_env_enabled

    body = request.get_json(force=True, silent=True) or {}
    clean, errors = pconfig.validate_guest_config_patch(body)
    if errors:
        return jsonify({"error": "invalid_config", "fields": errors}), 400
    if not clean:
        return jsonify({"error": "invalid_config", "fields": {"_body": "No valid keys provided"}}), 400
    cfg = pconfig.update_guest_config(clean, updated_by=get_jwt_identity())
    cfg["updated_at"] = _iso(cfg.get("updated_at"))
    cfg["env_flag_enabled"] = guest_onboarding_env_enabled()
    logger.info("guest-config updated by %s: keys=%s", get_jwt_identity(), sorted(clean.keys()))
    return jsonify(cfg), 200


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
@admin_bp.route("/stats", methods=["GET"])
@admin_required
def stats():
    db = _db()
    now = _now()
    d7, d30 = now - timedelta(days=7), now - timedelta(days=30)

    def _safe(fn, default=0):
        try:
            return fn()
        except Exception as e:
            logger.warning("admin stat failed: %s", e)
            return default

    total_users = _safe(lambda: db.users.count_documents({}))
    users_7d = _safe(lambda: db.users.count_documents({"created_at": {"$gte": d7}}))
    users_30d = _safe(lambda: db.users.count_documents({"created_at": {"$gte": d30}}))

    total_base = _safe(lambda: db.user_resumes.count_documents({"type": "base"}))
    legacy_gen = _safe(lambda: db.user_resumes.count_documents({"type": "generated"}))
    total_parsed = _safe(lambda: db.user_resumes.count_documents({"parsed": {"$exists": True}})) or total_base

    total_tailoring = _safe(lambda: db.tailoring_records.count_documents({"deleted": {"$ne": True}}))
    tailoring_7d = _safe(lambda: db.tailoring_records.count_documents({"created_at": {"$gte": d7}}))
    tailoring_30d = _safe(lambda: db.tailoring_records.count_documents({"created_at": {"$gte": d30}}))

    def _versions():
        cur = db.tailoring_records.aggregate([
            {"$project": {"n": {"$size": {"$ifNull": ["$versions", []]}}}},
            {"$group": {"_id": None, "total": {"$sum": "$n"}}},
        ])
        for row in cur:
            return int(row.get("total", 0))
        return 0

    def _apps_by_status():
        cur = db.tailoring_records.aggregate([
            {"$match": {"application.status": {"$exists": True, "$ne": None}}},
            {"$group": {"_id": "$application.status", "n": {"$sum": 1}}},
        ])
        return {row["_id"]: row["n"] for row in cur if row.get("_id")}

    apps_by_status = _safe(_apps_by_status, {})
    total_apps = sum(apps_by_status.values()) if apps_by_status else 0
    total_prep = _safe(lambda: db.tailoring_records.count_documents({"interview_prep": {"$exists": True, "$ne": None}}))

    return jsonify({
        "total_users": total_users,
        "users_7d": users_7d,
        "users_30d": users_30d,
        "total_parsed_resumes": total_parsed,
        "total_base_resumes": total_base,
        "legacy_generated_resumes": legacy_gen,
        "total_tailoring_sessions": total_tailoring,
        "tailoring_7d": tailoring_7d,
        "tailoring_30d": tailoring_30d,
        "total_versions": _safe(_versions),
        "cached_pdf_files": 0,
        "cached_docx_files": 0,
        "total_applications": total_apps,
        "applications_by_status": apps_by_status,
        "total_interview_prep_packs": total_prep,
    }), 200


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@admin_bp.route("/users", methods=["GET"])
@admin_required
def users():
    db = _db()
    docs = list(db.users.find({}).sort("created_at", -1).limit(1000))
    emails = [d["email"] for d in docs if d.get("email")]
    today = usage_service.get_today_usage_for_users(emails) if emails else {}

    out = []
    for d in docs:
        email = d.get("email")
        custom = d.get("daily_tailor_limit")
        out.append({
            "id": str(d.get("_id")),
            "email": email,
            "name": d.get("name"),
            "role": d.get("role"),
            "sector": d.get("sector"),
            "created_at": _iso(d.get("created_at")),
            "last_login": _iso(d.get("last_login")),
            "last_login_ip": d.get("last_login_ip"),
            "login_attempts": int(d.get("login_attempts", 0)),
            "base_resumes": db.user_resumes.count_documents({"user_email": email, "type": "base"}),
            "generated_resumes": db.user_resumes.count_documents({"user_email": email, "type": "generated"}),
            "tailoring_sessions": db.tailoring_records.count_documents({"user_email": email, "deleted": {"$ne": True}}),
            "has_parsed_resume": db.user_resumes.count_documents({"user_email": email, "parsed": {"$exists": True}}) > 0,
            "daily_tailor_limit_custom": custom if isinstance(custom, int) else None,
            "daily_tailor_limit_effective": usage_service.get_user_limit(email),
            "tailored_today": today.get(email, 0),
            "plan": d.get("plan") or "free",
        })
    return jsonify({"users": out, "default_daily_limit": usage_service.DEFAULT_DAILY_LIMIT}), 200


@admin_bp.route("/user/<path:email>/quota", methods=["PATCH"])
@admin_required
def update_quota(email):
    data = request.get_json(force=True, silent=True) or {}
    limit = data.get("daily_tailor_limit")
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        return jsonify({"error": "daily_tailor_limit must be a non-negative integer or null"}), 400
    usage_service.set_user_limit(email, limit if limit is not None else -1)
    today = usage_service.get_today_usage_for_users([email]).get(email, 0)
    user = _db().users.find_one({"email": email}, {"daily_tailor_limit": 1})
    custom = (user or {}).get("daily_tailor_limit")
    return jsonify({
        "email": email,
        "daily_tailor_limit_custom": custom if isinstance(custom, int) else None,
        "daily_tailor_limit_effective": usage_service.get_user_limit(email),
        "tailored_today": today,
    }), 200


@admin_bp.route("/user/<path:email>", methods=["GET"])
@admin_required
def user_detail(email):
    # This legacy route returns full PII; audit every reveal so it can't bypass the
    # §6.9 audited-access control (admins are trusted, but access is attributable).
    try:
        ops.write_audit(get_jwt_identity(), "view_user_pii", email)
    except Exception:
        pass
    db = _db()
    u = db.users.find_one({"email": email})
    if not u:
        return jsonify({"error": "user not found"}), 404
    user = {
        "id": str(u.get("_id")),
        "email": u.get("email"),
        "name": u.get("name"),
        "role": u.get("role"),
        "sector": u.get("sector"),
        "created_at": _iso(u.get("created_at")),
        "last_login": _iso(u.get("last_login")),
        "last_login_ip": u.get("last_login_ip"),
        "login_attempts": int(u.get("login_attempts", 0)),
        "fingerprint_hash": (u.get("fingerprints") or [None])[0],
        "session_id": u.get("session_id"),
        "plan": u.get("plan") or "free",
        "auth_providers": u.get("auth_providers", []),
    }
    base = list(db.user_resumes.find({"user_email": email, "type": "base"}).limit(50))
    gen = list(db.user_resumes.find({"user_email": email, "type": "generated"}).limit(50))
    parsed = next((r.get("parsed") for r in base if r.get("parsed")), None)
    records = list(db.tailoring_records.find({"user_email": email, "deleted": {"$ne": True}}).sort("created_at", -1).limit(100))
    return jsonify({
        "user": user,
        "parsed_resume": parsed,
        "base_resumes": [_clean(r) for r in base],
        "generated_resumes": [_clean(r) for r in gen],
        "tailoring_records": [_clean(r) for r in records],
    }), 200


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------
@admin_bp.route("/feedback", methods=["GET"])
@admin_required
def feedback_list():
    db = _db()
    status = (request.args.get("status") or "").strip().lower()
    query = {"status": status} if status in FEEDBACK_STATUSES else {}
    docs = list(db.feedback.find(query).sort("created_at", -1).limit(500))
    return jsonify({"feedback": [_feedback_view(d) for d in docs]}), 200


@admin_bp.route("/feedback/<fid>", methods=["PATCH"])
@admin_required
def feedback_update(fid):
    from bson import ObjectId
    db = _db()
    data = request.get_json(force=True, silent=True) or {}
    updates = {}
    if "admin_response" in data:
        updates["admin_response"] = (data.get("admin_response") or "")[:4000] or None
        updates["responded_at"] = _now()
    if "status" in data:
        st = (data.get("status") or "").lower()
        if st not in FEEDBACK_STATUSES:
            return jsonify({"error": "invalid status"}), 400
        updates["status"] = st
    if not updates:
        return jsonify({"error": "nothing to update"}), 400
    try:
        oid = ObjectId(fid)
    except Exception:
        return jsonify({"error": "invalid id"}), 400
    db.feedback.update_one({"_id": oid}, {"$set": updates})
    doc = db.feedback.find_one({"_id": oid})
    if not doc:
        return jsonify({"error": "not found"}), 404
    return jsonify(_feedback_view(doc)), 200


# ---------------------------------------------------------------------------
# Resumes / tailoring / applications / interview-prep / activity
# ---------------------------------------------------------------------------
@admin_bp.route("/resumes", methods=["GET"])
@admin_required
def resumes():
    docs = list(_db().user_resumes.find({}).sort("parsed_at", -1).limit(500))
    return jsonify({"resumes": [_clean(d) for d in docs]}), 200


@admin_bp.route("/tailoring", methods=["GET"])
@admin_required
def tailoring():
    db = _db()
    status = (request.args.get("status") or "").strip()
    try:
        limit = max(1, min(int(request.args.get("limit", 200)), 2000))
    except (TypeError, ValueError):
        limit = 200
    query = {"deleted": {"$ne": True}}
    if status:
        query["application.status"] = status
    docs = list(db.tailoring_records.find(query, {"versions": 0}).sort("created_at", -1).limit(limit))
    return jsonify({"records": [_clean(d) for d in docs]}), 200


@admin_bp.route("/tailoring/<record_id>", methods=["GET"])
@admin_required
def tailoring_detail(record_id):
    db = _db()
    include_resume = request.args.get("include_current_resume") in ("1", "true", "yes")
    rec = db.tailoring_records.find_one({"record_id": record_id})
    if not rec:
        return jsonify({"error": "not found"}), 404
    if not include_resume:
        rec.pop("versions", None)
    return jsonify({"record": _clean(rec)}), 200


@admin_bp.route("/applications", methods=["GET"])
@admin_required
def applications():
    db = _db()
    status = (request.args.get("status") or "").strip()
    query = {"application.status": {"$exists": True, "$ne": None}, "deleted": {"$ne": True}}
    if status:
        query["application.status"] = status
    docs = list(db.tailoring_records.find(query, {"versions": 0, "interview_prep": 0}).sort("updated_at", -1).limit(1000))
    return jsonify({"applications": [_clean(d) for d in docs]}), 200


@admin_bp.route("/interview-prep", methods=["GET"])
@admin_required
def interview_prep():
    db = _db()
    docs = list(
        db.tailoring_records.find(
            {"interview_prep": {"$exists": True, "$ne": None}},
            {"versions": 0},
        ).sort("updated_at", -1).limit(500)
    )
    return jsonify({"prep_packs": [_clean(d) for d in docs]}), 200


@admin_bp.route("/resume-url", methods=["GET"])
@admin_required
def resume_url():
    s3_key = request.args.get("s3_key")
    if not s3_key:
        return jsonify({"error": "s3_key required"}), 400
    try:
        from services.s3_service import get_storage_service
        url = get_storage_service().get_presigned_url(s3_key)
        content_type = "application/pdf" if s3_key.lower().endswith(".pdf") else "application/octet-stream"
        return jsonify({"url": url, "content_type": content_type}), 200
    except Exception as e:
        logger.warning("resume-url presign failed: %s", e)
        return jsonify({"error": "could not sign url"}), 500


@admin_bp.route("/activity", methods=["GET"])
@admin_required
def activity():
    """Recent product activity feed: latest tailoring sessions + applications."""
    db = _db()
    out = []
    for r in db.tailoring_records.find({"deleted": {"$ne": True}}).sort("updated_at", -1).limit(50):
        app = r.get("application") or {}
        jd = r.get("jd_analysis") or {}
        out.append({
            "type": "application" if app.get("status") else "tailoring",
            "email": r.get("user_email", ""),
            "detail": app.get("status") or "tailored",
            "company": jd.get("company"),
            "ats_score": (r.get("ats_scores") or {}).get("overall"),
            "timestamp": _iso(r.get("updated_at") or r.get("created_at")),
        })
    return jsonify({"activities": out}), 200


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------
def _clean(doc: dict) -> dict:
    """Make a Mongo doc JSON-safe: stringify _id, ISO-ify datetimes (shallow)."""
    if not doc:
        return {}
    doc = dict(doc)
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    for k, v in list(doc.items()):
        if isinstance(v, datetime):
            doc[k] = _iso(v)
    return doc


def _feedback_view(d: dict) -> dict:
    return {
        "id": str(d.get("_id")),
        "email": d.get("email", ""),
        "message": d.get("message", ""),
        "type": d.get("type", "general"),
        "status": d.get("status", "open"),
        "admin_response": d.get("admin_response"),
        "created_at": _iso(d.get("created_at")),
        "responded_at": _iso(d.get("responded_at")),
    }
