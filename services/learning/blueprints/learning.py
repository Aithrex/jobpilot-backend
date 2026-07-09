"""Learning / Growth blueprint (/api/learning) — ADR-003 extractable mirror.

Re-exposes the résumé→hired→noticed loop under `/api/learning/*`. Today the same
handlers are served by the resume Lambda under `/api/resume/learning/*`; this
blueprint lets the domain be lifted into its own Lambda behind one Terraform route
without touching call sites (the frontend just swaps the prefix).

Everything routes through the shared layer (`services.learning`, `entitlements_service`,
`resume_service` async substrate). All service imports are LAZY inside handlers so
`create_app()` stays import-safe with no AWS creds (the deploy smoke test does
`import app; app.create_app()`).

Integrity invariants preserved verbatim from the resume routes:
  * gate spend via entitlements BEFORE any LLM job;
  * long LLM work is async (`{job_id}` → poll the resume job substrate), never inline;
  * a completion post is refused unless the task is VERIFIED;
  * auto-publish never surprises — it holds 30 min with a cancel path.
"""
import logging

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import InputSanitizer

learning_bp = Blueprint("learning", __name__)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Suggest + plan (async LLM — ride the shared resume_service job substrate)
# ---------------------------------------------------------------------------
@learning_bp.route("/differentiators", methods=["POST"])
@jwt_required()
def differentiators():
    user_email = get_jwt_identity()
    from services import entitlements_service, differentiation_service
    from services.resume_service import get_resume_service, ResumeService
    if not getattr(differentiation_service, "LEARNING_DIFF_ENABLED", True):
        return jsonify({"error": "not_available"}), 404
    allowed, usage = entitlements_service.check_and_increment(user_email, "differentiation_analysis")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "differentiation_analysis", usage)
    payload = {"user_email": user_email}
    job_id = get_resume_service().create_job("learning_differentiators", payload, user_email=user_email)
    ResumeService.invoke_async(job_id, "learning_differentiators", payload)
    return jsonify({"job_id": job_id}), 202


@learning_bp.route("/plan", methods=["POST"])
@jwt_required()
def plan():
    user_email = get_jwt_identity()
    from services import entitlements_service, learning_service
    from services.resume_service import get_resume_service, ResumeService
    body = request.get_json(silent=True) or {}
    diff = body.get("differentiator")
    if not (isinstance(diff, dict) and diff.get("gap")):
        return jsonify({"error": "A differentiator with a 'gap' is required"}), 400

    plan_tier = entitlements_service.get_plan(user_email)
    cap = entitlements_service.quota(plan_tier, "active_learning_paths")
    if cap is not None and learning_service.count_active_paths(user_email) >= cap:
        return entitlements_service.upgrade_required_response(user_email, "learning")
    allowed, usage = entitlements_service.check_and_increment(user_email, "learning_plan_generate")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "learning", usage)

    try:
        days = int(body.get("days") or 14)
    except (TypeError, ValueError):
        days = 14
    payload = {
        "user_email": user_email,
        "differentiator": {
            "gap": InputSanitizer.sanitize_string(str(diff.get("gap")), 120),
            "why_recruiters_care": InputSanitizer.sanitize_string(str(diff.get("why_recruiters_care") or ""), 500),
            "market_frequency": diff.get("market_frequency"),
        },
        "days": 7 if days <= 7 else 14,
        "level": InputSanitizer.sanitize_string(str(body.get("level") or "auto"), 16),
    }
    job_id = get_resume_service().create_job("learning_plan", payload, user_email=user_email)
    ResumeService.invoke_async(job_id, "learning_plan", payload)
    return jsonify({"job_id": job_id}), 202


@learning_bp.route("/paths", methods=["GET"])
@jwt_required()
def paths_list():
    from services import learning_service
    return jsonify({"paths": learning_service.list_paths(get_jwt_identity())}), 200


@learning_bp.route("/paths/<path_id>", methods=["GET"])
@jwt_required()
def path_get(path_id):
    from services import learning_service
    data = learning_service.get_path(get_jwt_identity(), path_id)
    if not data:
        return jsonify({"error": "Path not found"}), 404
    return jsonify(data), 200


# ---------------------------------------------------------------------------
# GitHub push-sync (sync — a single consented GitHub action, no LLM)
# ---------------------------------------------------------------------------
@learning_bp.route("/paths/<path_id>/task/<task_ref>/github", methods=["POST"])
@jwt_required()
def task_github(path_id, task_ref):
    user_email = get_jwt_identity()
    from services.learning import github_sync
    task_ref = InputSanitizer.sanitize_string(task_ref, max_length=64)
    try:
        return jsonify(github_sync.ensure_task_repo(user_email, path_id, task_ref)), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 404
    except Exception as e:
        logger.exception("github sync error: %s", e)
        return jsonify({"error": "Could not set up the repo"}), 500


# ---------------------------------------------------------------------------
# Verify (async LLM — proof-of-work judge)
# ---------------------------------------------------------------------------
@learning_bp.route("/paths/<path_id>/task/<task_ref>/verify", methods=["POST"])
@jwt_required()
def task_verify(path_id, task_ref):
    user_email = get_jwt_identity()
    from services import entitlements_service
    from services.resume_service import get_resume_service, ResumeService
    task_ref = InputSanitizer.sanitize_string(task_ref, max_length=64)
    body = request.get_json(silent=True) or {}
    proof = {
        "url": InputSanitizer.sanitize_string(str(body.get("url") or ""), 500),
        "text": InputSanitizer.sanitize_string(str(body.get("text") or ""), 20000),
        "type": InputSanitizer.sanitize_string(str(body.get("type") or ""), 32),
    }
    allowed, usage = entitlements_service.check_and_increment(user_email, "task_verify")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "task_verify", usage)
    payload = {"user_email": user_email, "path_id": path_id, "task_ref": task_ref, "proof": proof}
    job_id = get_resume_service().create_job("learning_verify", payload, user_email=user_email)
    ResumeService.invoke_async(job_id, "learning_verify", payload)
    return jsonify({"job_id": job_id}), 202


# ---------------------------------------------------------------------------
# Completion post (verified-only) — draft is async LLM; publish is sync
# ---------------------------------------------------------------------------
@learning_bp.route("/paths/<path_id>/task/<task_ref>/post", methods=["POST"])
@jwt_required()
def task_post(path_id, task_ref):
    user_email = get_jwt_identity()
    from services import entitlements_service
    from services.learning import publish
    from services.resume_service import get_resume_service, ResumeService
    task_ref = InputSanitizer.sanitize_string(task_ref, max_length=64)

    # Refuse (and DON'T spend) unless the task is verified — fast DB-only precheck.
    if not publish.is_task_verified(user_email, path_id, task_ref):
        return jsonify({"error": "not_verified",
                        "message": "Only verified, proven work can be posted."}), 400

    allowed, usage = entitlements_service.check_and_increment(user_email, "completion_post")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "completion_post", usage)

    payload = {"user_email": user_email, "path_id": path_id, "task_ref": task_ref}
    job_id = get_resume_service().create_job("learning_completion_post", payload, user_email=user_email)
    ResumeService.invoke_async(job_id, "learning_completion_post", payload)
    return jsonify({"job_id": job_id}), 202


@learning_bp.route("/posts/<draft_id>/publish", methods=["POST"])
@jwt_required()
def post_publish(draft_id):
    user_email = get_jwt_identity()
    from services import entitlements_service
    from services.learning import publish
    draft_id = InputSanitizer.sanitize_string(draft_id, max_length=64)
    body = request.get_json(silent=True) or {}
    try:
        variant_index = int(body.get("variant_index") or 0)
    except (TypeError, ValueError):
        variant_index = 0
    mode = InputSanitizer.sanitize_string(str(body.get("mode") or "manual"), 16).lower()
    if mode not in ("manual", "auto"):
        mode = "manual"
    # Auto-publish (posting on the user's behalf) is a Premium+ capability.
    if mode == "auto" and not entitlements_service.feature_allowed(user_email, "auto_post"):
        return entitlements_service.upgrade_required_response(user_email, "auto_post")
    try:
        return jsonify(publish.publish_completion_post(user_email, draft_id, variant_index, mode)), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        logger.exception("post publish error: %s", e)
        return jsonify({"error": "Could not publish the post"}), 500


@learning_bp.route("/posts/<schedule_id>/cancel", methods=["POST"])
@jwt_required()
def post_cancel(schedule_id):
    user_email = get_jwt_identity()
    from services.learning import publish
    schedule_id = InputSanitizer.sanitize_string(schedule_id, max_length=64)
    try:
        return jsonify(publish.cancel_scheduled_post(user_email, schedule_id)), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 404
    except Exception as e:
        logger.exception("post cancel error: %s", e)
        return jsonify({"error": "Could not cancel the scheduled post"}), 500
