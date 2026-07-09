"""
Apply blueprint — backend for the Job Application Autofill Chrome extension.

Mounted at /api/apply/* on the jobs-resume Lambda (which already has the
MongoDB connection + the Claude LLM client). It rides its OWN API Gateway
route ("ANY /api/apply/{proxy+}" in infrastructure/terraform/lambda.tf) — kept
separate from /api/resume so the extension's traffic shows up as its own route
group in CloudWatch and is easy to monitor independently.

Endpoints (both @jwt_required — the extension stores the same JWT the web app
issues from /api/auth and sends it as a Bearer token):

  GET  /api/apply/profile  → ApplicationProfile JSON the extension seeds into
                             local storage (resume-derived base + any cross-device
                             saved vault under "saved").
  PUT  /api/apply/profile  → persist the user's edited vault so other devices can
                             restore it (collection: application_profiles).
  POST /api/apply/answer   → distinctive, resume-grounded answers to an
                             open-ended application question (2-3 variants); each
                             generation is logged to per-company answer history.
  GET  /api/apply/answers  → past generated answers (optionally by company) for
                             reuse (collection: application_answers).
"""
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import InputSanitizer, get_rate_limiter, get_client_ip
from utils.db_connect import DBConnect
from services import entitlements_service
from services import auto_submit_service

apply_bp = Blueprint("apply", __name__)
logger = logging.getLogger(__name__)

# Cap the stored vault so a malformed/oversized client payload can't bloat Mongo.
_MAX_VAULT_BYTES = 256 * 1024

# Server-side backstop for the extension's client-side fill cap
# (jobpilot-frontend/extension/lib/ratelimit.js — 40/hr, 150/day, self-labeled
# "not a security boundary"). Enforced per AUTHENTICATED USER here so a tampered
# or bypassed client can't spray-and-pray. Uses the same in-process rate limiter
# as the per-IP answer budget below — best-effort per Lambda container (API
# Gateway throttling remains the primary global limit), matching the documented
# behavior of RateLimiter.
_MAX_FILLS_PER_HOUR = 40
_MAX_FILLS_PER_DAY = 150


def _fill_cap_response(scope: str, limit: int):
    """429 in the standard quota shape when a per-user fill cap is hit."""
    return (
        jsonify({
            "error": "fill_limit_exceeded",
            "message": (
                f"You've reached your {scope} autofill limit ({limit}). "
                "Slow, tailored applications convert — take a short break."
            ),
            "usage": {"scope": scope, "limit": limit},
        }),
        429,
    )


def _fill_cap_exceeded(user_email: str):
    """Per-user rolling fill cap (hour + day). Returns a 429 response tuple if
    exceeded, else None. Backs the extension's client-only courtesy limit with a
    real server boundary. Hour is checked first so an hour-blocked request does
    not also consume a day slot."""
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(
        f"apply_fill_hour:{user_email}", max_requests=_MAX_FILLS_PER_HOUR, window_seconds=3600
    ):
        return _fill_cap_response("hourly", _MAX_FILLS_PER_HOUR)
    if limiter.is_rate_limited(
        f"apply_fill_day:{user_email}", max_requests=_MAX_FILLS_PER_DAY, window_seconds=86400
    ):
        return _fill_cap_response("daily", _MAX_FILLS_PER_DAY)
    return None


def _quota_exceeded_response(usage: dict):
    """429 with friendly copy — mirrors blueprints/resume.py:_quota_exceeded_response."""
    limit = usage.get("limit", 5)
    return (
        jsonify({
            "error": "daily_quota_exceeded",
            "message": (
                f"You've used all {limit} of your daily AI generations. "
                "We cap this to keep it free for everyone — your counter "
                "resets at midnight UTC."
            ),
            "usage": usage,
        }),
        429,
    )


# ------------------------------------------------------------------
# GET /api/apply/profile — seed data for the extension's local vault
# ------------------------------------------------------------------
@apply_bp.route("/profile", methods=["GET"])
@jwt_required()
def get_profile():
    user_email = get_jwt_identity()
    try:
        from services.resume_service import get_resume_service
        from services.answer_generator import build_application_profile

        svc = get_resume_service()
        base_resume = svc.get_base_resume(user_email=user_email)

        # Work-authorization seed comes from the visa profile if the user has
        # filled one in via the Visa Timeline Copilot — optional, soft-fail.
        visa_profile = None
        try:
            from services.visa_timeline_service import normalize_profile
            db = DBConnect().get_db()
            doc = db.visa_profiles.find_one({"user_email": user_email})
            if doc:
                visa_profile = normalize_profile(doc)
        except Exception as e:
            logger.info("apply/profile: visa profile load soft-fail: %s", e)

        profile = build_application_profile(base_resume, visa_profile)

        # Cross-device restore: the user's last-saved vault (their manual edits —
        # address, custom answers, work-auth wording). The extension prefers this
        # as the merge base, then overlays fresh resume-derived fields on top.
        saved = None
        try:
            db = DBConnect().get_db()
            doc = db.application_profiles.find_one(
                {"user_email": user_email}, {"_id": 0, "vault": 1}
            )
            if doc:
                saved = doc.get("vault")
        except Exception as e:
            logger.info("apply/profile: saved vault load soft-fail: %s", e)

        return jsonify({"profile": profile, "saved": saved}), 200
    except Exception as e:
        logger.exception("apply/profile error: %s", e)
        return jsonify({"error": "Failed to load application profile"}), 500


# ------------------------------------------------------------------
# PUT /api/apply/profile — persist the user's edited vault (cross-device)
# ------------------------------------------------------------------
@apply_bp.route("/profile", methods=["PUT"])
@jwt_required()
def save_profile():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    vault = data.get("profile")
    if not isinstance(vault, dict):
        return jsonify({"error": "profile (object) is required"}), 400

    # Size guard — reject obviously oversized payloads before touching Mongo.
    try:
        import json as _json
        if len(_json.dumps(vault)) > _MAX_VAULT_BYTES:
            return jsonify({"error": "profile too large"}), 413
    except (TypeError, ValueError):
        return jsonify({"error": "profile is not serializable"}), 400

    try:
        db = DBConnect().get_db()
        db.application_profiles.update_one(
            {"user_email": user_email},
            {"$set": {"user_email": user_email, "vault": vault, "updated_at": datetime.utcnow()}},
            upsert=True,
        )
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception("apply/profile save error: %s", e)
        return jsonify({"error": "Failed to save profile"}), 500


# ------------------------------------------------------------------
# POST /api/apply/answer — distinctive answer to an open-ended question
# ------------------------------------------------------------------
@apply_bp.route("/answer", methods=["POST"])
@jwt_required()
def generate_answer():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    # Reuse the resume_tailor IP bucket budget shape — 10 calls / 5 min.
    if limiter.is_rate_limited(f"apply_answer:{client_ip}", max_requests=10, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Try again in a few minutes."}), 429

    data = request.get_json(force=True) or {}
    question = data.get("question", "")
    if not question or not isinstance(question, str):
        return jsonify({"error": "question is required"}), 400

    question = InputSanitizer.sanitize_string(question, max_length=2000)
    if not question:
        return jsonify({"error": "Invalid question"}), 400

    job_description = InputSanitizer.sanitize_string(
        data.get("job_description", "") or "", max_length=10000
    )
    company = InputSanitizer.sanitize_string(data.get("company", "") or "", max_length=200)
    role = InputSanitizer.sanitize_string(data.get("role", "") or "", max_length=200)
    tone = InputSanitizer.sanitize_string(data.get("tone", "professional") or "professional", max_length=40)

    try:
        user_email = get_jwt_identity()

        # Server-side fill cap (per authenticated user) — gate before any spend.
        fill_capped = _fill_cap_exceeded(user_email)
        if fill_capped is not None:
            return fill_capped

        # Meter the DEDICATED autoapply_answer bucket, not the tailor bucket —
        # so the extension has its own quota and we don't double-count tailor.
        allowed, usage = entitlements_service.check_and_increment(user_email, "autoapply_answer")
        if not allowed:
            return _quota_exceeded_response(usage)

        from services.resume_service import get_resume_service
        from services.answer_generator import AnswerGenerator

        svc = get_resume_service()
        base_resume = svc.get_base_resume(user_email=user_email)
        if not base_resume or not base_resume.get("structured"):
            return jsonify({
                "error": "no_resume",
                "message": "Upload and parse a base resume on the portfolio first so "
                           "answers can be grounded in your real experience.",
            }), 400

        variants = AnswerGenerator().generate(
            question=question,
            base_resume_doc=base_resume,
            job_description=job_description,
            company=company,
            role=role,
            tone=tone,
        )
        if not variants:
            return jsonify({"error": "Failed to generate an answer. Please try again."}), 502

        # Log to per-company answer history for later reuse — soft-fail so a
        # history write never breaks the user-facing generation.
        try:
            db = DBConnect().get_db()
            db.application_answers.insert_one({
                "user_email": user_email,
                "company": company,
                "role": role,
                "question": question,
                "variants": variants,
                "created_at": datetime.utcnow(),
            })
        except Exception as e:
            logger.info("apply/answer: history write soft-fail: %s", e)

        return jsonify({"variants": variants, "usage": usage}), 200
    except Exception as e:
        logger.exception("apply/answer error: %s", e)
        return jsonify({"error": "Failed to generate answer. Please try again."}), 500


# ------------------------------------------------------------------
# GET /api/apply/answers — past generated answers for reuse
# ------------------------------------------------------------------
@apply_bp.route("/answers", methods=["GET"])
@jwt_required()
def list_answers():
    user_email = get_jwt_identity()
    company = InputSanitizer.sanitize_string(request.args.get("company", "") or "", max_length=200)
    try:
        limit = int(request.args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))

    query = {"user_email": user_email}
    if company:
        # Case-insensitive exact match so "Acme" and "acme" collapse. re.escape
        # neutralizes any regex metacharacters in the user-supplied company.
        import re
        query["company"] = {"$regex": f"^{re.escape(company)}$", "$options": "i"}

    try:
        db = DBConnect().get_db()
        cursor = (
            db.application_answers.find(query, {"_id": 0})
            .sort("created_at", -1)
            .limit(limit)
        )
        items = []
        for doc in cursor:
            created = doc.get("created_at")
            items.append({
                "company": doc.get("company", ""),
                "role": doc.get("role", ""),
                "question": doc.get("question", ""),
                "variants": doc.get("variants", []),
                "created_at": created.isoformat() if hasattr(created, "isoformat") else None,
            })
        return jsonify({"answers": items}), 200
    except Exception as e:
        logger.exception("apply/answers error: %s", e)
        return jsonify({"error": "Failed to load answer history"}), 500


# ==================================================================
# OPT-IN AUTO-SUBMIT (§ guardrailed) — all gated by AUTO_SUBMIT_ENABLED.
#
# The shipped default is the never-submit Review Card. Auto-submit only ever
# happens when the master flag is ON *and* the user opted in *and* an eligible,
# single-use, past-undo, unexpired approval exists for that exact application.
# The extension NEVER submits without seeing such a record via .../pending.
# All business/consent failures are 400/403/404/409/429 — never 401.
# ==================================================================
_MAX_AUTO_SUBMIT_ANSWERS_BYTES = 128 * 1024


def _auto_submit_disabled():
    """404 when the master kill-switch is off — the feature is invisible in prod
    until an operator flips AUTO_SUBMIT_ENABLED, mirroring the other flagged
    features (voice_graph / mail_ingress / gmail_oauth)."""
    if not auto_submit_service.auto_submit_enabled():
        return jsonify({"error": "not_found"}), 404
    return None


def _auto_submit_error(e: "auto_submit_service.AutoSubmitError"):
    """Map a typed service error to its HTTP status + JSON body (never 401)."""
    return jsonify({"error": e.error, "message": e.message}), e.status


# ------------------------------------------------------------------
# GET/PUT /api/apply/auto-submit/settings — the per-user opt-in
# ------------------------------------------------------------------
@apply_bp.route("/auto-submit/settings", methods=["GET"])
@jwt_required()
def auto_submit_get_settings():
    disabled = _auto_submit_disabled()
    if disabled is not None:
        return disabled
    user_email = get_jwt_identity()
    try:
        return jsonify(auto_submit_service.get_settings(user_email)), 200
    except Exception as e:
        logger.exception("apply/auto-submit settings GET error: %s", e)
        return jsonify({"error": "Failed to load auto-submit settings"}), 500


@apply_bp.route("/auto-submit/settings", methods=["PUT"])
@jwt_required()
def auto_submit_put_settings():
    disabled = _auto_submit_disabled()
    if disabled is not None:
        return disabled
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    enabled = data.get("auto_submit_enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "auto_submit_enabled (boolean) is required"}), 400
    try:
        return jsonify(auto_submit_service.set_settings(user_email, enabled)), 200
    except Exception as e:
        logger.exception("apply/auto-submit settings PUT error: %s", e)
        return jsonify({"error": "Failed to save auto-submit settings"}), 500


# ------------------------------------------------------------------
# POST /api/apply/auto-submit/approve — create/approve an approval record
# ------------------------------------------------------------------
@apply_bp.route("/auto-submit/approve", methods=["POST"])
@jwt_required()
def auto_submit_approve():
    disabled = _auto_submit_disabled()
    if disabled is not None:
        return disabled
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    # Sanitize the application identifier BEFORE it ever reaches a Mongo filter.
    application_id = InputSanitizer.sanitize_string(
        data.get("application_id") or data.get("job_id") or "", max_length=256
    )
    if not application_id:
        return jsonify({"error": "application_id is required"}), 400

    # The application PAGE url — the extension matches an eligible approval to the
    # current tab by normalized host+path, so store it on the record. Optional
    # (in-app approvals may pass only an id); sanitized, capped.
    application_url = InputSanitizer.sanitize_string(
        data.get("url") or data.get("application_url") or "", max_length=2000
    ) or None

    answers = data.get("answers")
    if not isinstance(answers, (list, dict)):
        return jsonify({"error": "answers (object or array of reviewed values) is required"}), 400
    try:
        import json as _json
        if len(_json.dumps(answers, default=str)) > _MAX_AUTO_SUBMIT_ANSWERS_BYTES:
            return jsonify({"error": "answers payload too large"}), 413
    except (TypeError, ValueError):
        return jsonify({"error": "answers is not serializable"}), 400

    try:
        # Opt-in gate first (403) so a non-opted-in user never consumes quota.
        if not auto_submit_service.user_opted_in(user_email):
            return jsonify({
                "error": "auto_submit_opt_in_required",
                "message": "Turn on auto-submit in settings before approving an application.",
            }), 403

        # Idempotent no-op: a live approval already covers this application — do
        # NOT re-charge quota or the fill cap.
        existing = auto_submit_service.get_active_approval(user_email, application_id)
        if existing is not None:
            return jsonify({"approval": existing, "created": False}), 200

        # Respect the SAME server-side fill cap the autofill path uses — an
        # approved auto-submit is a real application action.
        fill_capped = _fill_cap_exceeded(user_email)
        if fill_capped is not None:
            return fill_capped

        # Per-plan daily auto-submit cap (abuse/safety) — meter BEFORE creating.
        allowed, usage = entitlements_service.check_and_increment(user_email, "auto_submit")
        if not allowed:
            return _quota_exceeded_response(usage)

        record, created = auto_submit_service.create_approval(
            user_email, application_id, answers, source="in_app", url=application_url
        )
        return jsonify({"approval": record, "created": created, "usage": usage}), 201
    except auto_submit_service.AutoSubmitError as e:
        return _auto_submit_error(e)
    except Exception as e:
        logger.exception("apply/auto-submit approve error: %s", e)
        return jsonify({"error": "Failed to create approval"}), 500


# ------------------------------------------------------------------
# POST /api/apply/auto-submit/cancel — undo within the window
# ------------------------------------------------------------------
@apply_bp.route("/auto-submit/cancel", methods=["POST"])
@jwt_required()
def auto_submit_cancel():
    disabled = _auto_submit_disabled()
    if disabled is not None:
        return disabled
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    application_id = InputSanitizer.sanitize_string(
        data.get("application_id") or data.get("job_id") or "", max_length=256
    )
    if not application_id:
        return jsonify({"error": "application_id is required"}), 400
    try:
        record = auto_submit_service.cancel_approval(user_email, application_id)
        return jsonify({"approval": record}), 200
    except auto_submit_service.AutoSubmitError as e:
        return _auto_submit_error(e)
    except Exception as e:
        logger.exception("apply/auto-submit cancel error: %s", e)
        return jsonify({"error": "Failed to cancel approval"}), 500


# ------------------------------------------------------------------
# GET /api/apply/auto-submit/pending — the eligible-to-submit queue
# ------------------------------------------------------------------
@apply_bp.route("/auto-submit/pending", methods=["GET"])
@jwt_required()
def auto_submit_pending():
    disabled = _auto_submit_disabled()
    if disabled is not None:
        return disabled
    user_email = get_jwt_identity()
    try:
        return jsonify(auto_submit_service.list_pending_eligible(user_email)), 200
    except Exception as e:
        logger.exception("apply/auto-submit pending error: %s", e)
        return jsonify({"error": "Failed to load pending approvals"}), 500


# ------------------------------------------------------------------
# POST /api/apply/auto-submit/mark-submitted — extension reports the click
# ------------------------------------------------------------------
@apply_bp.route("/auto-submit/mark-submitted", methods=["POST"])
@jwt_required()
def auto_submit_mark_submitted():
    disabled = _auto_submit_disabled()
    if disabled is not None:
        return disabled
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    application_id = InputSanitizer.sanitize_string(
        data.get("application_id") or data.get("job_id") or "", max_length=256
    )
    if not application_id:
        return jsonify({"error": "application_id is required"}), 400
    try:
        record = auto_submit_service.mark_submitted(user_email, application_id)
        return jsonify({"approval": record}), 200
    except auto_submit_service.AutoSubmitError as e:
        return _auto_submit_error(e)
    except Exception as e:
        logger.exception("apply/auto-submit mark-submitted error: %s", e)
        return jsonify({"error": "Failed to mark submitted"}), 500
