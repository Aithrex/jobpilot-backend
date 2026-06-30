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
from services.usage_service import check_and_increment as _quota_check_and_increment

apply_bp = Blueprint("apply", __name__)
logger = logging.getLogger(__name__)

# Cap the stored vault so a malformed/oversized client payload can't bloat Mongo.
_MAX_VAULT_BYTES = 256 * 1024


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
        allowed, usage = _quota_check_and_increment(user_email, 1)
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
