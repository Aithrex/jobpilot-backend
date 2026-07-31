"""Sprint-based Learning Hub blueprint.

Mounted UNDER /api/resume/hub so it rides the existing "ANY /api/resume/{proxy+}"
edge route (no new Terraform route). Thin: delegates to the shared sprint_*
services, sanitizes all client input, and gates LLM spend before calling.

The Learning Hub itself is free (in _CORE); only the LLM-spending actions —
path generation (`sprint_generate`) and the AI mentor (`hub_mentor`) — are
metered. Auth failures never return 401 (401 is reserved for missing/bad JWT).
"""
import logging

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.db_connect import DBConnect
from utils.security import InputSanitizer

from services import entitlements_service
from services import career_paths
from services import sprint_curriculum as curriculum
from services import sprint_challenge_service as challenge_service
from services import sprint_progress_service as progress_service
from services import sprint_mentor_service
from services import sprint_submission_service
from services.sprint_submission_service import SubmissionError
from services import sprint_github_validator
from services import sprint_journey

sprint_hub_bp = Blueprint("sprint_hub", __name__)
logger = logging.getLogger(__name__)

# Real proof-of-work verification: days are graded against a public GitHub repo
# instead of the auto-passing NoOp default. Installed once at import.
sprint_github_validator.install()


def _is_admin(email: str) -> bool:
    try:
        u = DBConnect().get_db().users.find_one({"email": email}, {"is_admin": 1})
        return bool(u and u.get("is_admin"))
    except Exception:
        return False


def _overrides(source: dict, is_admin: bool):
    """Extract admin-only role/difficulty overrides (sanitized, whitelisted)."""
    if not is_admin:
        return None, None
    role = InputSanitizer.sanitize_string(str(source.get("role") or ""), 120) or None
    diff = InputSanitizer.sanitize_string(str(source.get("difficulty") or ""), 40) or None
    return role, diff


def _req_season(source: dict):
    """A requested, KNOWN season_id from the request (else None → use active)."""
    s = InputSanitizer.sanitize_string(str(source.get("season_id") or source.get("season") or ""), 40)
    return s if s in curriculum.SEASONS else None


def _req_tz(source: dict) -> int:
    """Client's UTC offset in minutes (frontend sends -getTimezoneOffset())."""
    try:
        return int(source.get("tz_offset_minutes") or 0)
    except (TypeError, ValueError):
        return 0


def _resolve_ctx(email: str, source: dict, is_admin: bool):
    """Resolve context for the requested chapter (or the user's active chapter),
    LINEARLY GATED: a locked chapter is refused unless the caller is an admin.

    Returns (ctx, tz_offset_minutes, error_tuple_or_None).
    """
    role, diff = _overrides(source, is_admin)
    base = challenge_service.resolve_context(email, role, diff, is_admin)
    role_key, difficulty = base["role_key"], base["difficulty"]
    tz = _req_tz(source)

    requested = _req_season(source)
    if requested:
        if not is_admin and not sprint_journey.is_unlocked(email, role_key, difficulty, requested):
            return None, tz, (jsonify({
                "error": "This chapter is locked — finish the previous one first.",
                "locked": True,
            }), 409)
        season_id = requested
    else:
        season_id = sprint_journey.active_season(email, role_key, difficulty)

    ctx = challenge_service.resolve_context(email, role, diff, is_admin, season_id=season_id)
    return ctx, tz, None


def _dispatch_generation(email: str, ctx: dict):
    """Gate + kick off the async generation job. Returns (job_id, usage_or_none)."""
    allowed, usage = entitlements_service.check_and_increment(email, "sprint_generate")
    if not allowed:
        return None, usage

    db = DBConnect().get_db()
    # Mark generating (atomic-ish guard so refetches don't stack docs).
    db.sprint_paths.update_one(
        {"path_key": ctx["path_key"]},
        {"$set": {"path_key": ctx["path_key"], "status": "generating"},
         "$setOnInsert": {"created_at": progress_service._now()}},
        upsert=True,
    )
    payload = {
        "user_email": email,
        "path_key": ctx["path_key"],
        "canonical_role": ctx["role_key"],
        "difficulty": ctx["difficulty"],
        "season_id": ctx["season_id"],
        "curriculum_version": ctx["curriculum_version"],
        "is_preview": ctx["is_preview"],
    }
    from services.resume_service import get_resume_service, ResumeService
    job_id = get_resume_service().create_job("sprint_generate", payload, user_email=email)
    ResumeService.invoke_async(job_id, "sprint_generate", payload)
    return job_id, None


# ---------------------------------------------------------------------------
# Path — start (generate if needed) + read
# ---------------------------------------------------------------------------
@sprint_hub_bp.route("/journey", methods=["GET"])
@jwt_required()
def hub_journey():
    """The campaign map: ordered chapters with per-user locked/active/completed
    state. Drives the chapter-select UI."""
    email = get_jwt_identity()
    is_admin = _is_admin(email)
    role, diff = _overrides(request.args, is_admin)
    base = challenge_service.resolve_context(email, role, diff, is_admin)
    return jsonify(sprint_journey.journey(email, base["role_key"], base["difficulty"])), 200


@sprint_hub_bp.route("/start", methods=["POST"])
@jwt_required()
def hub_start():
    email = get_jwt_identity()
    body = request.get_json(silent=True) or {}
    ctx, tz, err = _resolve_ctx(email, body, _is_admin(email))
    if err:
        return err

    view = challenge_service.build_path_view(email, ctx, tz_offset_minutes=tz)
    if view:
        return jsonify(view), 200

    job_id, usage = _dispatch_generation(email, ctx)
    if job_id is None:
        return entitlements_service.upgrade_required_response(email, "sprint_generate", usage)
    return jsonify({"job_id": job_id, "status": "generating", "path_key": ctx["path_key"]}), 202


@sprint_hub_bp.route("/path", methods=["GET"])
@jwt_required()
def hub_path():
    email = get_jwt_identity()
    ctx, tz, err = _resolve_ctx(email, request.args, _is_admin(email))
    if err:
        return err

    view = challenge_service.build_path_view(email, ctx, tz_offset_minutes=tz)
    if view:
        return jsonify(view), 200
    doc = challenge_service.get_path_doc(ctx["path_key"])
    return jsonify({"status": (doc or {}).get("status", "absent"), "path_key": ctx["path_key"]}), 200


@sprint_hub_bp.route("/day/<day_key>", methods=["GET"])
@jwt_required()
def hub_day(day_key):
    email = get_jwt_identity()
    day_key = InputSanitizer.sanitize_string(day_key, 60)
    ctx, tz, err = _resolve_ctx(email, request.args, _is_admin(email))
    if err:
        return err
    day = challenge_service.get_day(email, ctx, day_key, tz_offset_minutes=tz)
    if day is None:
        return jsonify({"error": "Challenge not available"}), 404
    return jsonify(day), 200


# ---------------------------------------------------------------------------
# Submit a day
# ---------------------------------------------------------------------------
@sprint_hub_bp.route("/day/<day_key>/submit", methods=["POST"])
@jwt_required()
def hub_submit(day_key):
    email = get_jwt_identity()
    day_key = InputSanitizer.sanitize_string(day_key, 60)
    body = request.get_json(silent=True) or {}
    note = InputSanitizer.sanitize_string(str(body.get("note") or ""), 2000)
    # Proof of work — a public GitHub repo URL the validator grades the day against.
    evidence = {
        "repo_url": InputSanitizer.sanitize_string(str(body.get("repo_url") or ""), 500),
    }
    ctx, tz, err = _resolve_ctx(email, body, _is_admin(email))
    if err:
        return err
    try:
        # State errors surface now; the slow part (repo download + LLM grading)
        # runs as an async job — it far exceeds the API Gateway timeout inline.
        # tz threads through so the async recompute agrees with the user's view.
        sprint_submission_service.precheck(email, ctx, day_key, tz_offset_minutes=tz)
        from services.resume_service import get_resume_service, ResumeService

        payload = {"user_email": email, "ctx": ctx, "day_key": day_key,
                   "note": note, "evidence": evidence, "tz_offset_minutes": tz}
        job_id = get_resume_service().create_job("sprint_verify", payload, user_email=email)
        ResumeService.invoke_async(job_id, "sprint_verify", payload)
        return jsonify({"job_id": job_id, "status": "verifying"}), 202
    except SubmissionError as e:
        return jsonify({"error": e.message}), e.status_code
    except Exception as e:
        logger.error("hub submit error: %s", e)
        return jsonify({"error": "Could not submit challenge"}), 500


# ---------------------------------------------------------------------------
# Analytics + achievements
# ---------------------------------------------------------------------------
@sprint_hub_bp.route("/analytics", methods=["GET"])
@jwt_required()
def hub_analytics():
    email = get_jwt_identity()
    ctx, _tz, err = _resolve_ctx(email, request.args, _is_admin(email))
    if err:
        return err
    path_doc = challenge_service.get_path_doc(ctx["path_key"])
    if not path_doc or path_doc.get("status") != "ready":
        return jsonify({"status": (path_doc or {}).get("status", "absent")}), 200
    progress = challenge_service.get_or_init_progress(email, ctx)
    data = progress_service.compute_analytics(email, ctx["path_key"], path_doc, progress)
    return jsonify(data), 200


@sprint_hub_bp.route("/achievements", methods=["GET"])
@jwt_required()
def hub_achievements():
    email = get_jwt_identity()
    return jsonify({"achievements": progress_service.list_achievements(email)}), 200


# ---------------------------------------------------------------------------
# AI mentor
# ---------------------------------------------------------------------------
@sprint_hub_bp.route("/mentor", methods=["POST"])
@jwt_required()
def hub_mentor():
    email = get_jwt_identity()
    body = request.get_json(silent=True) or {}
    question = InputSanitizer.sanitize_string(str(body.get("question") or ""), 2000)
    if not question:
        return jsonify({"error": "A question is required"}), 400

    allowed, usage = entitlements_service.check_and_increment(email, "hub_mentor")
    if not allowed:
        return entitlements_service.upgrade_required_response(email, "hub_mentor", usage)

    ctx, _tz, err = _resolve_ctx(email, body, _is_admin(email))
    if err:
        return err

    ticket = None
    day_key = InputSanitizer.sanitize_string(str(body.get("day_key") or ""), 60)
    if day_key:
        path_doc = challenge_service.get_path_doc(ctx["path_key"])
        day_doc = next((d for d in (path_doc or {}).get("days", []) if d["key"] == day_key), None)
        # Mentor only assists on RELEASED challenges. For unreleased days answer
        # without spending an LLM call. Gate on the season's publish state so a
        # stale cached full path can't leak future-day help.
        unreleased = day_doc is not None and (
            not curriculum.is_published(day_doc.get("day", 999), ctx["season_id"]) or not day_doc.get("ticket")
        )
        if unreleased:
            return jsonify({
                "answer": "This challenge isn't available yet — it's coming in a future update. "
                          "Focus on your current sprint and I'll be here when the next day unlocks!",
                "followups": [],
            }), 200
        ticket = (day_doc or {}).get("ticket")

    history = body.get("history") if isinstance(body.get("history"), list) else []
    answer = sprint_mentor_service.ask(
        question, ticket=ticket, role_label=ctx["role_label"],
        difficulty=ctx["difficulty"], history=history,
    )
    return jsonify(answer), 200


# ---------------------------------------------------------------------------
# Admin/dev — role catalog + force regenerate
# ---------------------------------------------------------------------------
@sprint_hub_bp.route("/roles", methods=["GET"])
@jwt_required()
def hub_roles():
    return jsonify({
        "roles": career_paths.all_paths(),
        "difficulties": [
            {"key": k, "label": career_paths.DIFFICULTY_LABELS[k]} for k in career_paths.DIFFICULTY_TIERS
        ],
        "is_admin": _is_admin(get_jwt_identity()),
    }), 200


@sprint_hub_bp.route("/regenerate", methods=["POST"])
@jwt_required()
def hub_regenerate():
    email = get_jwt_identity()
    if not _is_admin(email):
        return jsonify({"error": "admin only"}), 403
    body = request.get_json(silent=True) or {}
    role, diff = _overrides(body, True)
    season_id = _req_season(body) or curriculum.DEFAULT_SEASON
    ctx = challenge_service.resolve_context(email, role, diff, True, season_id=season_id)
    DBConnect().get_db().sprint_paths.delete_one({"path_key": ctx["path_key"]})
    job_id, usage = _dispatch_generation(email, ctx)
    if job_id is None:
        return entitlements_service.upgrade_required_response(email, "sprint_generate", usage)
    return jsonify({"job_id": job_id, "status": "generating", "path_key": ctx["path_key"]}), 202
