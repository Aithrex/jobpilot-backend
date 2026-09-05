"""
Resume blueprint — Resume tailoring, ATS scoring, and document generation endpoints.

Reuses the same JWT from /api/jobs/auth (job_search_token).

Async pattern: LLM-backed endpoints submit a job, Lambda processes it
asynchronously, and the frontend polls GET /job/<id> for the result.
  1. POST /extract-jd   → returns { job_id }   (instant)
  2. POST /tailor       → returns { job_id }   (instant)
  3. POST /ats-scores   → returns { job_id }   (instant)
  4. GET  /job/<id>     → returns { status, result | error }

User-specific endpoints with S3 storage:
  5. POST /upload       → parse PDF + store in S3
  6. POST /download     → generate PDF/DOCX + store in S3
  7. GET  /status       → check active base resume for user
  8. GET  /versions     → list base resume versions
  9. GET  /generated    → list generated/tailored resumes
 10. PUT  /active       → set active resume ({resume_id}, Resume Platform v3)
 11. GET  /download-file/<s3_key> → download resume from S3 (legacy keys)
 12. DELETE /file/<resume_id>     → delete a resume asset (S3 + doc + re-point)
"""

import io
import json
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import InputSanitizer, get_rate_limiter, get_client_ip
from utils.db_connect import DBConnect
from utils.datadog_metrics import dd_metric
from services.s3_service import get_storage_service
from services.usage_service import get_usage as _get_usage
# Atomic tailor quota (Resume Platform v3, spec 1.6): routes RESERVE before
# dispatch; the worker settles completed, terminal failures refund.
from services import quota_ledger_service as _quota_ledger


def _quota_exceeded_response(usage: dict):
    """Uniform 429 response with friendly copy for daily quota hits."""
    limit = usage.get("limit", 5)
    return (
        jsonify({
            "error": "daily_quota_exceeded",
            "message": (
                f"You've used all {limit} of your daily tailored resumes. "
                "We cap this to keep the AI tailoring free for everyone — "
                "your counter resets at midnight UTC. "
                "Need more for a specific job hunt push? Send us a note "
                "via the Feedback button and we'll bump your limit."
            ),
            "usage": usage,
        }),
        429,
    )
from services.resume_versioning import (
    LIST_VIEW_PROJECTION,
    MAX_VERSIONS_PER_RECORD,
    VersionLimitError,
    canonical_content_hash,
    ensure_versions,
    find_version,
    latest_version,
    append_version,
    serialize_list_row,
    serialize_record,
)


def _version_limit_response():
    """Uniform 409 for the bounded-refinement cap (spec 1.5)."""
    return (
        jsonify({"error": "version_limit_reached", "limit": MAX_VERSIONS_PER_RECORD}),
        409,
    )

resume_bp = Blueprint("resume", __name__)
logger = logging.getLogger(__name__)


def _parse_tailor_base_args(data: dict, user_email: str):
    """Validate optional resume_id / selection_mode BEFORE quota reserve.

    A bad pin must never consume a tailor slot. Returns
    ``(resume_id_or_None, selection_mode, error_response_or_None)``.
    """
    from services import resume_access

    raw_id = InputSanitizer.sanitize_string(
        str(data.get("resume_id") or ""), max_length=64
    )
    mode = InputSanitizer.sanitize_string(
        str(data.get("selection_mode") or "auto"), max_length=16
    ).lower()
    if mode not in ("auto", "active"):
        mode = "auto"
    if not raw_id:
        return None, mode, None
    asset = resume_access.get_owned_asset(user_email, raw_id)
    if not asset:
        return None, mode, (jsonify({"error": "resume_not_found"}), 404)
    if asset.get("parse_status") != "parsed" or not asset.get("structured"):
        return None, mode, (jsonify({"error": "resume_not_parsed"}), 409)
    return raw_id, mode, None


# ------------------------------------------------------------------
# POST /api/resume/extract-jd — Submit JD extraction job
# ------------------------------------------------------------------


@resume_bp.route("/extract-jd", methods=["POST"])
@jwt_required()
def extract_jd():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"resume_tailor:{client_ip}", max_requests=10, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Try again in a few minutes."}), 429

    data = request.get_json(force=True) or {}
    jd_text = data.get("job_description", "")
    if not jd_text or not isinstance(jd_text, str):
        return jsonify({"error": "job_description is required"}), 400

    from services.tailoring_policy import enabled
    if enabled() and len(jd_text) > 56000:
        return jsonify({"error": "Job description is too long (max 56000 characters)."}), 400
    jd_text = InputSanitizer.sanitize_string(jd_text, max_length=56000 if enabled() else 10000)
    if not jd_text:
        return jsonify({"error": "Invalid job description"}), 400

    try:
        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()
        user_email = get_jwt_identity()
        job_id = svc.create_job("extract_jd", {"job_description": jd_text}, user_email=user_email)
        ResumeService.invoke_async(job_id, "extract_jd", {"job_description": jd_text})
        return jsonify({"job_id": job_id}), 202

    except Exception as e:
        logger.error(f"JD extraction error: {e}")
        return jsonify({"error": "Failed to start JD analysis. Please try again."}), 500


# ------------------------------------------------------------------
# GET /api/resume/usage/today — Current user's daily tailor quota
# ------------------------------------------------------------------
@resume_bp.route("/usage/today", methods=["GET"])
@jwt_required()
def usage_today():
    user_email = get_jwt_identity()
    return jsonify(_get_usage(user_email)), 200


# ------------------------------------------------------------------
# POST /api/resume/tailor — Submit resume tailoring job
# ------------------------------------------------------------------


@resume_bp.route("/tailor", methods=["POST"])
@jwt_required()
def tailor():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"resume_tailor:{client_ip}", max_requests=10, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Try again in a few minutes."}), 429

    data = request.get_json(force=True) or {}
    jd_analysis = data.get("jd_analysis")
    if not jd_analysis or not isinstance(jd_analysis, dict):
        return jsonify({"error": "jd_analysis is required"}), 400

    try:
        user_email = get_jwt_identity()
        resume_id, selection_mode, base_err = _parse_tailor_base_args(data, user_email)
        if base_err:
            return base_err
        # Atomic RESERVE before dispatch (spec 1.6) — no check-then-increment.
        allowed, entry_ids, usage = _quota_ledger.reserve_tailor(
            user_email,
            idempotency_key=(data.get("idempotency_key") or None),
        )
        if not allowed:
            return _quota_exceeded_response(usage)

        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()
        payload = {
            "jd_analysis": jd_analysis,
            "user_email": user_email,
            "quota_entry_id": entry_ids[0],
            "selection_mode": selection_mode,
        }
        if resume_id:
            payload["resume_id"] = resume_id
        try:
            job_id = svc.create_job("tailor", payload, user_email=user_email)
            _quota_ledger.attach_job(entry_ids[0], job_id)
            ResumeService.invoke_async(job_id, "tailor", payload)
        except Exception:
            # Dispatch never happened — give the slot back before the 500.
            _quota_ledger.refund_entry(entry_ids[0])
            raise
        return jsonify({"job_id": job_id, "usage": usage}), 202

    except Exception as e:
        logger.error(f"Resume tailor error: {e}")
        return jsonify({"error": "Failed to start resume tailoring. Please try again."}), 500


# ------------------------------------------------------------------
# POST /api/resume/tailor-with-jd — Combined: extract JD + tailor in ONE job
# ------------------------------------------------------------------
# This endpoint replaces the two-step /extract-jd then /tailor sequence on
# the frontend, eliminating one HTTP round-trip + ~22s of poll lag. Inside
# the job, project generation is also run in parallel with tailor.tailor()
# for another ~10s saving. Same LLM calls, just a tighter orchestration.
#
# Returns BOTH jd_analysis and tailored_resume on completion.
# Old /extract-jd and /tailor endpoints remain operational for fallback.


@resume_bp.route("/tailor-with-jd", methods=["POST"])
@jwt_required()
def tailor_with_jd():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"resume_tailor:{client_ip}", max_requests=10, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Try again in a few minutes."}), 429

    data = request.get_json(force=True) or {}
    jd_text = data.get("job_description", "")
    if not jd_text or not isinstance(jd_text, str):
        return jsonify({"error": "job_description is required"}), 400

    from services.tailoring_policy import enabled
    if enabled() and len(jd_text) > 56000:
        return jsonify({"error": "Job description is too long (max 56000 characters)."}), 400
    jd_text = InputSanitizer.sanitize_string(jd_text, max_length=56000 if enabled() else 10000)
    if not jd_text:
        return jsonify({"error": "Invalid job description"}), 400

    try:
        user_email = get_jwt_identity()
        resume_id, selection_mode, base_err = _parse_tailor_base_args(data, user_email)
        if base_err:
            return base_err
        # Atomic RESERVE before dispatch (spec 1.6) — no check-then-increment.
        allowed, entry_ids, usage = _quota_ledger.reserve_tailor(
            user_email,
            idempotency_key=(data.get("idempotency_key") or None),
        )
        if not allowed:
            return _quota_exceeded_response(usage)

        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()
        payload = {
            "job_description": jd_text,
            "user_email": user_email,
            "quota_entry_id": entry_ids[0],
            "selection_mode": selection_mode,
        }
        if resume_id:
            payload["resume_id"] = resume_id
        source_job_id = InputSanitizer.sanitize_string(
            str(data.get("source_job_id") or ""), max_length=256
        )
        if source_job_id:
            payload["source_job_id"] = source_job_id
        try:
            job_id = svc.create_job("extract_and_tailor", payload, user_email=user_email)
            _quota_ledger.attach_job(entry_ids[0], job_id)
            ResumeService.invoke_async(job_id, "extract_and_tailor", payload)
        except Exception:
            _quota_ledger.refund_entry(entry_ids[0])
            raise
        return jsonify({"job_id": job_id, "usage": usage}), 202

    except Exception as e:
        logger.error(f"Resume tailor-with-jd error: {e}")
        return (
            jsonify({"error": "Failed to start resume tailoring. Please try again."}),
            500,
        )


# ------------------------------------------------------------------
# POST /api/resume/regenerate — Regenerate tailored resume with user feedback
# ------------------------------------------------------------------
@resume_bp.route("/regenerate", methods=["POST"])
@jwt_required()
def regenerate():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"resume_regen:{client_ip}", max_requests=10, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Try again in a few minutes."}), 429

    data = request.get_json(force=True) or {}
    jd_analysis = data.get("jd_analysis")
    tailored_resume = data.get("tailored_resume")
    user_feedback = data.get("user_feedback", "").strip()

    if not jd_analysis or not isinstance(jd_analysis, dict):
        return jsonify({"error": "jd_analysis is required"}), 400
    if not tailored_resume or not isinstance(tailored_resume, dict):
        return jsonify({"error": "tailored_resume is required"}), 400
    if not user_feedback:
        return jsonify({"error": "user_feedback is required"}), 400
    if len(user_feedback) > 2000:
        return jsonify({"error": "Feedback too long (max 2000 characters)"}), 400

    try:
        user_email = get_jwt_identity()

        # Refinement policy (spec 1.5/1.6): regenerating WITHIN a record does
        # NOT consume tailor quota — only NEW sessions reserve. The bounded
        # version cap is enforced up front when the client names its record,
        # so we never spend an LLM call whose result can't be saved.
        record_id = InputSanitizer.sanitize_string(
            str(data.get("record_id") or ""), max_length=64
        )
        rec = None
        if record_id:
            rec = DBConnect().get_db().tailoring_records.find_one(
                {"record_id": record_id, "user_email": user_email},
                {"versions": 1, "base_resume_id": 1},
            )
            if rec and len(rec.get("versions") or []) >= MAX_VERSIONS_PER_RECORD:
                return _version_limit_response()
            if not rec:
                return jsonify({"error": "Record not found"}), 404

        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()
        payload = {
            "jd_analysis": jd_analysis,
            "tailored_resume": tailored_resume,
            "user_feedback": user_feedback,
            "user_email": user_email,
            "record_id": record_id,
        }
        # Ground regenerate in the SAME base that was tailored, not the
        # global active pointer (which may be a different uploaded file).
        base_rid = InputSanitizer.sanitize_string(
            str(data.get("resume_id") or (rec or {}).get("base_resume_id") or ""),
            max_length=64,
        )
        if base_rid:
            payload["resume_id"] = base_rid
        job_id = svc.create_job("regenerate", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "regenerate", payload)
        return jsonify({"job_id": job_id}), 202

    except Exception as e:
        logger.error(f"Resume regenerate error: {e}")
        return jsonify({"error": "Failed to start resume regeneration. Please try again."}), 500


# ------------------------------------------------------------------
# POST /api/resume/rewrite-bullet — Rewrite a single bullet with JD context
# ------------------------------------------------------------------
@resume_bp.route("/records/<record_id>/experience", methods=["POST"])
@jwt_required()
def confirm_tailoring_experience(record_id):
    from services.tailoring_discovery import confirm
    user_email = get_jwt_identity()
    if get_rate_limiter().is_rate_limited(f"tailoring_evidence:{user_email}", max_requests=20, window_seconds=300):
        return jsonify({"error": "Please wait before adding more experience."}), 429
    data = request.get_json(force=True) or {}
    if data.get("confirmed") is not True:
        return jsonify({"error": "Confirm that this describes work you actually did."}), 400
    try:
        key = confirm(DBConnect().get_db(), user_email, record_id, data.get("entity"), data.get("text"))
        return jsonify({"fact_id": key, "message": "Saved. Available on your next refinement."}), 200
    except LookupError as e:
        return jsonify({"error": str(e)}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@resume_bp.route("/rewrite-bullet", methods=["POST"])
@jwt_required()
def rewrite_bullet():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"rewrite_bullet:{client_ip}", max_requests=30, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Try again in a few minutes."}), 429

    data = request.get_json(force=True) or {}
    bullet = data.get("bullet", "").strip()
    job_title = data.get("job_title", "")
    company = data.get("company", "")
    jd_keywords = data.get("jd_keywords", [])

    if not bullet:
        return jsonify({"error": "bullet is required"}), 400
    if len(bullet) > 1000:
        return jsonify({"error": "Bullet too long (max 1000 chars)"}), 400

    from services.tailoring_policy import enabled
    if enabled():
        from services.resume_service import get_resume_service, ResumeService
        svc = get_resume_service()
        payload = {"bullet": bullet, "job_title": job_title, "jd_keywords": jd_keywords,
                   "user_email": get_jwt_identity()}
        job_id = svc.create_job("rewrite_bullet_evidence", payload, user_email=get_jwt_identity())
        ResumeService.invoke_async(job_id, "rewrite_bullet_evidence", payload)
        return jsonify({"job_id": job_id}), 202

    try:
        from services.claude_client import claude_json, CLAUDE_FLASH

        keyword_ctx = f"Target keywords: {', '.join(jd_keywords[:20])}\n" if jd_keywords else ""
        role_ctx = (
            f"Target role: {job_title}" + (f" at {company}" if company else "") + "\n"
            if job_title
            else ""
        )

        prompt = (
            "Rewrite this resume bullet to be more impactful and ATS-optimized.\n\n"
            f"{role_ctx}{keyword_ctx}"
            "RULES:\n"
            "- Start with a strong action verb.\n"
            "- Follow: Action Verb + Technology + Impact.\n"
            "- MUST be ONE single sentence, max 200 characters.\n"
            "- Do NOT invent metrics or technologies.\n"
            "- No buzzwords.\n\n"
            f"ORIGINAL: {bullet}\n\n"
            'Respond with exactly: {"rewritten": "your single sentence here"}'
        )

        schema = {"rewritten": str}
        result = claude_json(
            prompt, max_tokens=6000, temperature=0.4, model=CLAUDE_FLASH, schema=schema
        )
        rewritten = result.get("rewritten", "").strip()
        # Strip any leading/trailing quotes the LLM may have added
        if rewritten.startswith('"'):
            rewritten = rewritten.lstrip('"')
        if rewritten.endswith('"'):
            rewritten = rewritten.rstrip('"')

        if not rewritten:
            return jsonify({"rewritten": bullet}), 200
        return jsonify({"rewritten": rewritten}), 200

    except Exception as e:
        logger.error(f"Bullet rewrite error: {e}", exc_info=True)
        return jsonify({"error": f"Rewrite failed: {str(e)[:200]}"}), 500


# ------------------------------------------------------------------
# POST /api/resume/ats-scores — Submit ATS scoring job
# ------------------------------------------------------------------


@resume_bp.route("/ats-scores", methods=["POST"])
@jwt_required()
def ats_scores():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"resume_ats:{client_ip}", max_requests=10, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Try again in a few minutes."}), 429

    data = request.get_json(force=True) or {}
    tailored = data.get("tailored_resume")
    jd_analysis = data.get("jd_analysis")

    if not tailored or not isinstance(tailored, dict):
        return jsonify({"error": "tailored_resume is required"}), 400
    if not jd_analysis or not isinstance(jd_analysis, dict):
        return jsonify({"error": "jd_analysis is required"}), 400

    try:
        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()
        user_email = get_jwt_identity()
        payload = {"tailored_resume": tailored, "jd_analysis": jd_analysis}
        job_id = svc.create_job("ats_scores", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "ats_scores", payload)
        return jsonify({"job_id": job_id}), 202

    except Exception as e:
        logger.error(f"ATS scoring error: {e}")
        return jsonify({"error": "Failed to start ATS scoring. Please try again."}), 500


# ------------------------------------------------------------------
# POST /api/resume/verify — Re-run claim verification on an edited resume
# ------------------------------------------------------------------
@resume_bp.route("/verify", methods=["POST"])
@jwt_required()
def verify_resume():
    """Re-check a (possibly user-edited) tailored resume against the
    candidate's fact store — claim-guard audit + entailment pass. One FLASH
    call; returns 202 + job_id like every other long-running resume job."""
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"resume_verify:{client_ip}", max_requests=10, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Try again in a few minutes."}), 429

    data = request.get_json(force=True) or {}
    tailored = data.get("tailored_resume")
    if not tailored or not isinstance(tailored, dict):
        return jsonify({"error": "tailored_resume is required"}), 400

    try:
        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()
        user_email = get_jwt_identity()
        payload = {
            "tailored_resume": tailored,
            "jd_analysis": data.get("jd_analysis") or {},
            "user_email": user_email,
        }
        rec_id = InputSanitizer.sanitize_string(
            str(data.get("record_id") or ""), max_length=64
        )
        rec = None
        if rec_id:
            rec = DBConnect().get_db().tailoring_records.find_one(
                {"record_id": rec_id, "user_email": user_email},
                {"base_resume_id": 1},
            )
        base_rid = InputSanitizer.sanitize_string(
            str(data.get("resume_id") or (rec or {}).get("base_resume_id") or ""),
            max_length=64,
        )
        if base_rid:
            payload["resume_id"] = base_rid
        job_id = svc.create_job("verify_resume", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "verify_resume", payload)
        return jsonify({"job_id": job_id}), 202

    except Exception as e:
        logger.error(f"Resume verification error: {e}")
        return jsonify({"error": "Failed to start verification. Please try again."}), 500


@resume_bp.route("/craft-event", methods=["POST"])
@jwt_required()
def craft_event():
    """Fire-and-forget product event (review Accept/Remove/Edit/dismiss)."""
    data = request.get_json(force=True) or {}
    action = InputSanitizer.sanitize_string(str(data.get("action") or ""), max_length=64)
    if not action:
        return jsonify({"error": "action is required"}), 400
    logger.info(
        "craft_event user=%s action=%s target=%s",
        get_jwt_identity(),
        action,
        InputSanitizer.sanitize_string(str(data.get("target") or ""), max_length=80),
    )
    return jsonify({"ok": True}), 200


# ------------------------------------------------------------------
# Profile facts — the enrichment confirmation surface.
# LinkedIn-archive / GitHub facts become tailoring-eligible ONLY after the
# user explicitly confirms them here. Confirmed facts enrich skills/summary/
# bullets of EXISTING roles; they never create new experience entries (the
# uploaded resume stays the structural source of truth).
# ------------------------------------------------------------------
@resume_bp.route("/profile-facts", methods=["GET"])
@jwt_required()
def profile_facts_list():
    try:
        from services.profile_facts import list_candidate_facts
        return jsonify(list_candidate_facts(get_jwt_identity())), 200
    except Exception as e:
        logger.error(f"profile-facts list error: {e}")
        return jsonify({"error": "Failed to load profile facts"}), 500


@resume_bp.route("/profile-facts/confirm", methods=["POST"])
@jwt_required()
def profile_facts_confirm():
    data = request.get_json(force=True) or {}
    decisions = data.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        return jsonify({"error": "decisions list is required"}), 400
    try:
        from services.profile_facts import record_decisions
        updated = record_decisions(get_jwt_identity(), decisions)
        return jsonify({
            "updated": updated,
            "note": (
                "Confirmed facts enrich tailoring (skills, summary, bullets of "
                "existing roles). To add a new role, update your base resume."
            ),
        }), 200
    except Exception as e:
        logger.error(f"profile-facts confirm error: {e}")
        return jsonify({"error": "Failed to save decisions"}), 500


# ------------------------------------------------------------------
# POST /api/resume/cover-letter — Generate a cover letter
# ------------------------------------------------------------------
@resume_bp.route("/cover-letter", methods=["POST"])
@jwt_required()
def cover_letter():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"cover_letter:{client_ip}", max_requests=10, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Try again in a few minutes."}), 429

    data = request.get_json(force=True) or {}
    tailored_resume = data.get("tailored_resume")
    jd_analysis = data.get("jd_analysis")

    if not tailored_resume or not isinstance(tailored_resume, dict):
        return jsonify({"error": "tailored_resume is required"}), 400
    if not jd_analysis or not isinstance(jd_analysis, dict):
        return jsonify({"error": "jd_analysis is required"}), 400

    try:
        user_email = get_jwt_identity()
        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()
        payload = {
            "tailored_resume": tailored_resume,
            "jd_analysis": jd_analysis,
            "user_email": user_email,
        }
        job_id = svc.create_job("cover_letter", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "cover_letter", payload)
        return jsonify({"job_id": job_id}), 202

    except Exception as e:
        logger.error(f"Cover letter error: {e}")
        return jsonify({"error": "Failed to start cover letter generation"}), 500


# ------------------------------------------------------------------
# POST /api/resume/cover-letter/download — Render cover letter as PDF
# ------------------------------------------------------------------
@resume_bp.route("/cover-letter/download", methods=["POST"])
@jwt_required()
def cover_letter_download():
    """Render a cover letter text block into a single-page PDF using fpdf2.

    Synchronous — rendering is fast (<100ms) and doesn't need an async job.
    """
    data = request.get_json(force=True) or {}
    text = data.get("cover_letter", "")
    candidate_name = (data.get("candidate_name") or "Applicant").strip()
    job_title = (data.get("job_title") or "").strip()
    company = (data.get("company") or "").strip()

    if not text or not isinstance(text, str):
        return jsonify({"error": "cover_letter text is required"}), 400

    text = InputSanitizer.sanitize_string(text, max_length=6000)

    # fpdf2 with built-in Times font only supports Latin-1. Smart quotes, em/en
    # dashes, ellipses, and other Unicode punctuation crash rendering. Map them
    # to ASCII-safe equivalents before we draw. (Full Unicode would require
    # embedding a TTF font, which bloats the Lambda deployment.)
    _PUNCT_MAP = str.maketrans(
        {
            "\u2014": "-",  # em dash
            "\u2013": "-",  # en dash
            "\u2212": "-",  # minus
            "\u2018": "'",  # left single quote
            "\u2019": "'",  # right single quote
            "\u201c": '"',  # left double quote
            "\u201d": '"',  # right double quote
            "\u2026": "...",  # ellipsis
            "\u00a0": " ",  # non-breaking space
            "\u2022": "-",  # bullet
            "\u2009": " ",  # thin space
            "\u200b": "",  # zero-width space
            "\u00b7": "-",  # middle dot
        }
    )
    text = text.translate(_PUNCT_MAP)
    candidate_name = candidate_name.translate(_PUNCT_MAP)
    job_title = job_title.translate(_PUNCT_MAP)
    company = company.translate(_PUNCT_MAP)
    # Final safety net: encode to latin-1 and drop anything unsupported
    text = text.encode("latin-1", errors="replace").decode("latin-1")
    candidate_name = candidate_name.encode("latin-1", errors="replace").decode("latin-1")
    job_title = job_title.encode("latin-1", errors="replace").decode("latin-1")
    company = company.encode("latin-1", errors="replace").decode("latin-1")

    try:
        from fpdf import FPDF

        pdf = FPDF(format="A4", unit="mm")
        pdf.set_auto_page_break(auto=True, margin=20)
        pdf.add_page()
        pdf.set_margins(left=20, top=20, right=20)

        # Header — candidate name
        pdf.set_font("Times", "B", 16)
        pdf.cell(0, 9, candidate_name, new_x="LMARGIN", new_y="NEXT")

        # Subtitle — target role
        if job_title or company:
            subtitle_parts = [p for p in [job_title, company] if p]
            pdf.set_font("Times", "I", 11)
            pdf.set_text_color(90, 90, 90)
            pdf.cell(0, 6, " - ".join(subtitle_parts), new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)

        # Divider line
        pdf.ln(3)
        y = pdf.get_y()
        pdf.set_draw_color(200, 200, 200)
        pdf.line(20, y, 190, y)
        pdf.ln(5)

        # Date
        pdf.set_font("Times", "", 10)
        pdf.set_text_color(110, 110, 110)
        pdf.cell(0, 5, datetime.now().strftime("%B %d, %Y"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(5)

        # Body — paragraphs separated by blank lines
        pdf.set_font("Times", "", 11)
        for paragraph in text.split("\n\n"):
            para = paragraph.strip()
            if not para:
                continue
            # fpdf2 multi_cell auto-wraps; replace internal single newlines with spaces
            para = " ".join(line.strip() for line in para.split("\n") if line.strip())
            pdf.multi_cell(0, 5.8, para)
            pdf.ln(2.5)

        # Output (fpdf2 returns bytearray — normalize to bytes)
        raw = pdf.output()
        pdf_bytes = bytes(raw) if isinstance(raw, (bytearray, memoryview)) else raw

        from services.resume_renderer import ResumeRenderer
        filename = ResumeRenderer.build_cover_letter_filename(
            candidate_name=candidate_name,
            job_title=job_title,
            company=company,
            ext="pdf",
        )

        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=filename,
        )

    except Exception as e:
        logger.error(f"Cover letter PDF render error: {e}")
        return jsonify({"error": "Failed to render cover letter PDF"}), 500


# ------------------------------------------------------------------
# POST /api/resume/batch-tailor — Submit multiple JDs for parallel tailoring
# ------------------------------------------------------------------
@resume_bp.route("/batch-tailor", methods=["POST"])
@jwt_required()
def batch_tailor():
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"batch_tailor:{client_ip}", max_requests=3, window_seconds=600):
        return (
            jsonify({"error": "Rate limit exceeded. Max 3 batch submissions per 10 minutes."}),
            429,
        )

    data = request.get_json(force=True) or {}
    jd_list = data.get("jd_list", [])

    if not isinstance(jd_list, list) or len(jd_list) == 0:
        return jsonify({"error": "jd_list is required (array of JD objects)"}), 400
    # Cap lifted from 5 → 25 to support the Daily Pipeline → Batch Tailor
    # handoff flow (a student often wants to tailor 10-20 roles per morning).
    # The 3-per-10-min rate limiter above still protects against abuse.
    if len(jd_list) > 25:
        return jsonify({"error": "Maximum 25 job descriptions per batch"}), 400

    for i, jd in enumerate(jd_list):
        if not isinstance(jd, dict) or not jd.get("text", "").strip():
            return jsonify({"error": f"JD #{i+1} is missing text"}), 400

    try:
        user_email = get_jwt_identity()

        # Plan gate FIRST (spec 1.6): batch tailoring is a paid capability —
        # free plans get the standard 403 upgrade_required shape.
        from services import entitlements_service
        if not entitlements_service.feature_allowed(user_email, "batch_tailor"):
            return jsonify({
                "error": "upgrade_required",
                "feature": "batch_tailor",
                "plan": entitlements_service.get_plan(user_email),
                "required_plans": entitlements_service.required_plans_for("batch_tailor"),
                "message": "This feature requires an upgraded plan.",
            }), 403

        # Atomic all-or-nothing RESERVE of n slots (spec 1.6).
        allowed, entry_ids, usage = _quota_ledger.reserve_tailor(
            user_email, n=len(jd_list)
        )
        if not allowed:
            # Tell the user exactly how many would fit if they trimmed the batch.
            usage_msg = usage.copy()
            usage_msg["requested"] = len(jd_list)
            return _quota_exceeded_response(usage_msg)

        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()

        jobs = []
        dispatched = 0
        try:
            for jd, entry_id in zip(jd_list, entry_ids):
                # Each JD goes through: extract_jd → tailor (handled by batch_tailor_item job type)
                payload = {
                    "jd_text": jd["text"].strip()[:15000],
                    "jd_title": jd.get("title", "").strip(),
                    "user_email": user_email,
                    "quota_entry_id": entry_id,
                    # source_job_id ties the batch result back to a saved_jobs row
                    # when the batch was launched from the Daily Pipeline handoff.
                    # Without this the tailored result never updates the kanban
                    # nor increments the streak — it just lives in the jobs table.
                    "source_job_id": (jd.get("source_job_id") or "").strip() or None,
                    "selection_mode": "auto",
                }
                job_id = svc.create_job("batch_tailor_item", payload, user_email=user_email)
                _quota_ledger.attach_job(entry_id, job_id)
                ResumeService.invoke_async(job_id, "batch_tailor_item", payload)
                dispatched += 1
                jobs.append({"job_id": job_id, "title": jd.get("title", "")})
        except Exception:
            # Refund every slot whose item never dispatched (dispatched items
            # settle their own entries via complete_job/fail_job).
            _quota_ledger.refund_entries(entry_ids[dispatched:])
            raise

        return jsonify({"jobs": jobs, "usage": usage}), 202

    except Exception as e:
        logger.error(f"Batch tailor error: {e}")
        return jsonify({"error": "Failed to start batch tailoring"}), 500


# ------------------------------------------------------------------
# GET /api/resume/streak — Daily application streak summary
# ------------------------------------------------------------------
@resume_bp.route("/streak", methods=["GET"])
@jwt_required()
def get_streak_route():
    user_email = get_jwt_identity()
    tz = request.args.get("tz") or None
    force_rebuild = request.args.get("rebuild") == "1"
    debug = request.args.get("debug") == "1"
    try:
        from services.streak_service import get_streak
        result = get_streak(user_email, tz=tz, force_rebuild=force_rebuild)

        if debug:
            from datetime import timezone as _tz
            try:
                from zoneinfo import ZoneInfo
                local = ZoneInfo(tz) if tz else _tz.utc
            except Exception:
                local = _tz.utc

            db = DBConnect().get_db()
            doc = db.parse_streaks.find_one({"user_email": user_email}, {"_id": 0}) or {}
            records = list(
                db.tailoring_records.find(
                    {"user_email": user_email},
                    {"_id": 0, "record_id": 1, "created_at": 1},
                ).sort("created_at", -1).limit(50)
            )
            mapped = []
            for r in records:
                ts = r.get("created_at")
                utc_iso = local_iso = local_date = None
                if hasattr(ts, "isoformat"):
                    ts_aware = ts if ts.tzinfo else ts.replace(tzinfo=_tz.utc)
                    utc_iso = ts_aware.astimezone(_tz.utc).isoformat()
                    local_dt = ts_aware.astimezone(local)
                    local_iso = local_dt.isoformat()
                    local_date = local_dt.strftime("%Y-%m-%d")
                mapped.append({
                    "record_id": r.get("record_id"),
                    "created_at_utc": utc_iso,
                    "created_at_local": local_iso,
                    "local_date": local_date,
                })
            result["_debug"] = {
                "tz_received": tz,
                "stored_doc": doc,
                "records": mapped,
            }

        return jsonify(result), 200
    except Exception as e:
        logger.exception("Get streak error: %s", e)
        return jsonify({"error": "Failed to load streak"}), 500


# ------------------------------------------------------------------
# GET /api/resume/tailoring-records — List user's tailoring history
# ------------------------------------------------------------------
@resume_bp.route("/tailoring-records", methods=["GET"])
@jwt_required()
def list_tailoring_records():
    user_email = get_jwt_identity()
    include_full = request.args.get("include") == "full"
    # `view=list` is the application tracker's slim path: the projection happens
    # in Mongo, so the heavy fields are never read rather than read-and-
    # discarded. Opt-in, because interview prep and the tailored-resumes page
    # still read the full shape off this same endpoint.
    list_view = request.args.get("view") == "list"
    # `limit` query param tunable; bumped default from 100 → 1000 so the
    # tracker can show ALL of a user's records. The 100 cap was causing the
    # funnel summary (server aggregation) and the tracker's own counts (local
    # records) to disagree once a user exceeded 100 total tailoring records.
    try:
        limit = int(request.args.get("limit") or 1000)
    except (TypeError, ValueError):
        limit = 1000
    limit = max(50, min(limit, 5000))
    try:
        db = DBConnect().get_db()

        if list_view:
            rows = db.tailoring_records.aggregate([
                {"$match": {"user_email": user_email}},
                {"$sort": {"created_at": -1}},
                {"$limit": limit},
                {"$project": LIST_VIEW_PROJECTION},
            ])
            return jsonify({"records": [serialize_list_row(r) for r in rows]}), 200

        projection = {"_id": 0}
        records = list(
            db.tailoring_records.find({"user_email": user_email}, projection)
            .sort("created_at", -1)
            .limit(limit)
        )
        out = []
        for r in records:
            versions, migrated = ensure_versions(r)
            if migrated:
                # Persist the lazy migration so next reads are cheap.
                db.tailoring_records.update_one(
                    {"record_id": r.get("record_id"), "user_email": user_email},
                    {
                        "$set": {
                            "versions": versions,
                            "current_version_id": versions[-1]["version_id"],
                        }
                    },
                )
                r["versions"] = versions
                r.setdefault("current_version_id", versions[-1]["version_id"])
            if not r.get("current_version_id") and versions:
                r["current_version_id"] = versions[-1]["version_id"]
            out.append(serialize_record(r, include_full=include_full))
        return jsonify({"records": out}), 200
    except Exception as e:
        logger.exception("List tailoring records error: %s", e)
        return jsonify({"error": "Failed to load tailoring history"}), 500


# ------------------------------------------------------------------
# GET /api/resume/tailoring-records/<record_id> — Fetch one record (full)
# ------------------------------------------------------------------
@resume_bp.route("/tailoring-records/<record_id>", methods=["GET"])
@jwt_required()
def get_tailoring_record(record_id):
    user_email = get_jwt_identity()
    try:
        db = DBConnect().get_db()
        record = db.tailoring_records.find_one(
            {"record_id": record_id, "user_email": user_email}
        )
        if not record:
            return jsonify({"error": "Record not found"}), 404
        versions, migrated = ensure_versions(record)
        if migrated:
            db.tailoring_records.update_one(
                {"record_id": record_id, "user_email": user_email},
                {
                    "$set": {
                        "versions": versions,
                        "current_version_id": versions[-1]["version_id"],
                    }
                },
            )
            record["versions"] = versions
            record["current_version_id"] = versions[-1]["version_id"]
        return jsonify({"record": serialize_record(record, include_full=True)}), 200
    except Exception as e:
        logger.exception("Get tailoring record error: %s", e)
        return jsonify({"error": "Failed to load record"}), 500


# ------------------------------------------------------------------
# POST /api/resume/tailoring-records/<record_id>/versions — Save a new version
# ------------------------------------------------------------------
@resume_bp.route("/tailoring-records/<record_id>/versions", methods=["POST"])
@jwt_required()
def save_version(record_id):
    """Append a new version to a tailoring record.

    Idempotent: if the submitted `tailored_resume` has the same content hash
    as the latest version, no new version is created and the existing one is
    returned. This lets the frontend safely call this on every edit/regen
    without bloating the version chain.
    """
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    tailored_resume = data.get("tailored_resume")
    source = data.get("source", "edited")
    user_feedback = data.get("user_feedback")
    ats_scores = data.get("ats_scores")
    parent_version_id = data.get("parent_version_id")
    set_current = bool(data.get("set_current", True))

    if not tailored_resume or not isinstance(tailored_resume, dict):
        return jsonify({"error": "tailored_resume is required"}), 400
    if source not in ("initial", "regenerated", "edited"):
        return jsonify({"error": "invalid source"}), 400

    try:
        db = DBConnect().get_db()
        record = db.tailoring_records.find_one(
            {"record_id": record_id, "user_email": user_email}
        )
        if not record:
            return jsonify({"error": "Record not found"}), 404

        versions, _ = ensure_versions(record)
        from services.resume_versioning import StaleVersionError
        try:
            version, created = append_version(
                versions,
                tailored_resume,
                source=source,
                parent_version_id=parent_version_id,
                ats_scores=ats_scores,
                user_feedback=user_feedback,
            )
        except StaleVersionError as e:
            return jsonify({"error": str(e), "code": "stale_version"}), 409
        except VersionLimitError:
            # Bounded refinement (spec 1.5): the 6th distinct version is refused.
            return _version_limit_response()
        update = {
            "$set": {
                "versions": versions,
                "updated_at": datetime.utcnow(),
            }
        }
        if set_current:
            update["$set"]["current_version_id"] = version["version_id"]
        saved = db.tailoring_records.update_one(
            {"record_id": record_id, "user_email": user_email,
             "updated_at": record.get("updated_at")},
            update,
        )
        if not saved.matched_count:
            return jsonify({"error": "The resume changed. Reload before saving.",
                            "code": "stale_version"}), 409
        return jsonify({
            "version_id": version["version_id"],
            "version_number": version["version_number"],
            "content_hash": version["content_hash"],
            "created": created,
        }), 201 if created else 200
    except Exception as e:
        logger.exception("Save version error: %s", e)
        return jsonify({"error": "Failed to save version"}), 500


# ------------------------------------------------------------------
# PUT /api/resume/tailoring-records/<record_id>/current-version
# ------------------------------------------------------------------
@resume_bp.route("/tailoring-records/<record_id>/current-version", methods=["PUT"])
@jwt_required()
def set_current_version(record_id):
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    version_id = data.get("version_id")
    if not version_id:
        return jsonify({"error": "version_id is required"}), 400

    try:
        db = DBConnect().get_db()
        record = db.tailoring_records.find_one(
            {"record_id": record_id, "user_email": user_email}
        )
        if not record:
            return jsonify({"error": "Record not found"}), 404
        versions, _ = ensure_versions(record)
        if not find_version(versions, version_id):
            return jsonify({"error": "Version not found"}), 404
        db.tailoring_records.update_one(
            {"record_id": record_id, "user_email": user_email},
            {"$set": {"current_version_id": version_id, "updated_at": datetime.utcnow()}},
        )
        return jsonify({"current_version_id": version_id}), 200
    except Exception as e:
        logger.exception("Set current version error: %s", e)
        return jsonify({"error": "Failed to set current version"}), 500


# ------------------------------------------------------------------
# DELETE /api/resume/tailoring-records/<record_id> — Delete a record
# ------------------------------------------------------------------
@resume_bp.route("/tailoring-records/<record_id>", methods=["DELETE"])
@jwt_required()
def delete_tailoring_record(record_id):
    """Delete a tailoring record AND its rendered files (spec section 2).

    Every versions[].files[fmt].s3_key is deleted from S3 first, per-object
    results inspected. Any S3 failure marks the record `deletion_pending`
    (for the retention sweep) and returns 502 WITHOUT deleting the Mongo doc
    — the stored refs are the only path back to the orphaned objects. Only a
    fully clean S3 pass deletes the doc.
    """
    user_email = get_jwt_identity()
    try:
        db = DBConnect().get_db()
        record = db.tailoring_records.find_one(
            {"record_id": record_id, "user_email": user_email},
            {"versions": 1},
        )
        if not record:
            return jsonify({"error": "Record not found"}), 404

        s3_keys = []
        for version in record.get("versions") or []:
            for f in (version.get("files") or {}).values():
                if isinstance(f, dict) and f.get("s3_key"):
                    s3_keys.append(f["s3_key"])

        failed = 0
        if s3_keys:
            # Late import so the storage seam resolves at call time
            # (test/monkeypatch friendly — mirrors DELETE /file/<resume_id>).
            from services.s3_service import get_storage_service as _get_storage

            storage = _get_storage()
            for key in s3_keys:
                try:
                    storage.delete_resume(key)
                except Exception as e:
                    failed += 1
                    logger.warning(
                        "Record delete: S3 delete failed for record %s: %s",
                        record_id, type(e).__name__,
                    )

        if failed:
            db.tailoring_records.update_one(
                {"record_id": record_id, "user_email": user_email},
                {"$set": {"deletion_pending": True, "updated_at": datetime.utcnow()}},
            )
            return jsonify({
                "error": "deletion_pending",
                "message": (
                    "Some generated files could not be removed yet. The record "
                    "is queued for cleanup and will be deleted automatically."
                ),
                "files_deleted": len(s3_keys) - failed,
                "files_pending": failed,
            }), 502

        db.tailoring_records.delete_one(
            {"record_id": record_id, "user_email": user_email}
        )
        return jsonify({"message": "Record deleted", "files_deleted": len(s3_keys)}), 200
    except Exception as e:
        logger.exception("Delete tailoring record error: %s", e)
        return jsonify({"error": "Failed to delete record"}), 500


# ------------------------------------------------------------------
# PATCH /api/resume/tailoring-records/<record_id>/application
# Update application tracker fields for a record.
# ------------------------------------------------------------------
_APPLICATION_STATUSES = {
    "saved", "draft", "applied", "interviewing", "offer", "rejected", "withdrawn", "ghosted",
}

# E1 (defect E) — this route's status vocabulary mapped onto the canonical
# OUTCOME axis (application_ledger_service.OUTCOME_STATUSES). The
# tailoring_records.application mirror above stays authoritative until E3; this
# map only feeds the additive write-through that ALSO lands the outcome on the
# canonical `applications` row. "draft" is the pre-application state -> "none";
# every other status maps to the same-named outcome.
_STATUS_TO_OUTCOME = {
    "saved": "none",
    "draft": "none",
    "applied": "applied",
    "interviewing": "interviewing",
    "offer": "offer",
    "rejected": "rejected",
    "withdrawn": "withdrawn",
    "ghosted": "ghosted",
}


@resume_bp.route("/tailoring-records/<record_id>/application", methods=["PATCH"])
@jwt_required()
def update_application(record_id):
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}

    updates = {}
    status = data.get("status")
    # Read existing record once — we need to know if an apply confirmation was
    # pending (so we can fire the streak event when the user confirms).
    db_pre = DBConnect().get_db()
    existing_app = (db_pre.tailoring_records.find_one(
        {"record_id": record_id, "user_email": user_email},
        {"application": 1, "_id": 0},
    ) or {}).get("application") or {}
    # `confirmation_pending` is the flow-agnostic flag (single tailor, batch,
    # anywhere else we ask "did you apply?"). `batch_confirmation_pending` is
    # its original batch-only name, still written by older clients.
    was_confirmation_pending = bool(
        existing_app.get("confirmation_pending")
        or existing_app.get("batch_confirmation_pending")
    )
    was_status = existing_app.get("status")

    if status is not None:
        if status not in _APPLICATION_STATUSES:
            return jsonify({"error": f"Invalid status. Must be one of {sorted(_APPLICATION_STATUSES)}"}), 400
        updates["application.status"] = status
        # Stamp applied_at automatically when transitioning from draft → applied
        if status == "applied":
            updates["application.applied_at"] = updates.get("application.applied_at") or datetime.utcnow()

    # Apply-confirmation flag — accept a boolean under either key and write
    # both, so the canonical name and the legacy batch-only name never drift
    # apart for a record. Typically false, set by the "I applied" / "Not yet" /
    # "Dismiss" buttons on the board and the post-download tailor prompt.
    if "confirmation_pending" in data or "batch_confirmation_pending" in data:
        pending = bool(data.get(
            "confirmation_pending", data.get("batch_confirmation_pending")))
        updates["application.confirmation_pending"] = pending
        updates["application.batch_confirmation_pending"] = pending

    for field in ("recruiter_name", "recruiter_email", "recruiter_company",
                  "next_action_note", "notes", "job_url", "resume_used"):
        if field in data:
            val = data.get(field)
            if isinstance(val, str):
                val = InputSanitizer.sanitize_string(val, max_length=2000)
            updates[f"application.{field}"] = val

    # Date fields (ISO-8601 strings from frontend) — store as datetime for easier queries
    for field in ("applied_at", "next_action_date"):
        if field in data:
            raw = data.get(field)
            if raw:
                try:
                    updates[f"application.{field}"] = datetime.fromisoformat(
                        raw.replace("Z", "+00:00")
                    )
                except Exception:
                    return jsonify({"error": f"Invalid {field}"}), 400
            else:
                updates[f"application.{field}"] = None

    # Day label for "which day did I apply?" — a civil date, not an instant.
    # `applied_at` stays UTC and answers "when"; `applied_date` is frozen in the
    # user's zone at confirmation time so it never shifts when they travel.
    # MUST run after the parse loop above so an explicitly back-dated
    # applied_at produces its own day rather than today's.
    if "application.applied_at" in updates:
        applied_dt = updates["application.applied_at"]
        if applied_dt is None:
            updates["application.applied_date"] = None
            updates["application.applied_tz"] = None
        else:
            from services.user_time import applied_day_for_user
            day, tz_used = applied_day_for_user(
                applied_dt, user_email, data.get("tz"))
            updates["application.applied_date"] = day
            updates["application.applied_tz"] = tz_used

    # Interview dates — array of ISO strings
    if "interview_dates" in data:
        raw_list = data.get("interview_dates") or []
        if not isinstance(raw_list, list):
            return jsonify({"error": "interview_dates must be a list"}), 400
        parsed = []
        for d in raw_list[:20]:
            try:
                parsed.append(datetime.fromisoformat(str(d).replace("Z", "+00:00")))
            except Exception:
                pass
        updates["application.interview_dates"] = parsed

    if not updates:
        return jsonify({"error": "No fields to update"}), 400

    updates["application.updated_at"] = datetime.utcnow()
    updates["updated_at"] = datetime.utcnow()

    try:
        db = DBConnect().get_db()
        result = db.tailoring_records.update_one(
            {"record_id": record_id, "user_email": user_email},
            {"$set": updates},
        )
        if not result.matched_count:
            return jsonify({"error": "Record not found"}), 404

        # Streak: increment the daily counter ONLY when a batch-confirmation
        # transitioned to "applied" (user clicked "I applied" on the prompt).
        # Otherwise normal status changes shouldn't double-fire the streak.
        if (
            was_confirmation_pending
            and was_status != "applied"
            and status == "applied"
        ):
            try:
                from services.streak_service import record_application
                record_application(user_email)
                logger.info(
                    "batch confirmation: streak incremented for %s record %s",
                    user_email, record_id,
                )
            except Exception as e:
                logger.warning("batch confirmation: streak update failed: %s", e)

        # Bidirectional sync — when the tailoring_record's application status
        # changes AND the record is linked to a saved_job from the Daily
        # Pipeline (source_job_id present), propagate the status to that
        # saved_job so the Daily Pipeline UI greys-out the row, hides "Open
        # posting", and tags it as applied. Without this, batch-tailor
        # confirmations updated only the tailoring_record and the
        # Daily Pipeline kept showing the job as fresh / clickable.
        if status:
            try:
                rec_meta = db.tailoring_records.find_one(
                    {"record_id": record_id, "user_email": user_email},
                    {"source_job_id": 1, "_id": 0},
                ) or {}
                src_job_id = (rec_meta.get("source_job_id") or "").strip()
                if src_job_id:
                    from services.job_service import get_job_service
                    get_job_service().update_saved_job(
                        src_job_id, status=status, user_email=user_email,
                    )
                    logger.info(
                        "tailoring_record→saved_job sync: %s → status=%s",
                        src_job_id, status,
                    )
            except Exception as e:
                logger.warning("tailoring→saved_job sync failed: %s", e)

        # E1 (defect E) — write-through to the canonical OUTCOME axis. When the
        # user moves this record's application status, ALSO stamp the
        # employer-response outcome on the canonical `applications` row
        # (find-or-created, linked by tailoring_record_id). The
        # tailoring_records mirror above stays authoritative until E3, so this is
        # additive and best-effort: a ledger hiccup must never fail the user's
        # tracker update — swallow to a WARN. Lazy import keeps the ledger off the
        # cold-start path.
        if status:
            outcome = _STATUS_TO_OUTCOME.get(status)
            if outcome:
                try:
                    from services import application_ledger_service as als
                    als.set_outcome(
                        user_email, record_id=record_id, outcome=outcome,
                        source="user",
                    )
                except Exception as e:  # canonical write is non-authoritative in E1
                    logger.warning(
                        "PATCH /application: canonical set_outcome failed "
                        "(record=%s): %s", str(record_id)[:24], e)

        record = db.tailoring_records.find_one(
            {"record_id": record_id, "user_email": user_email},
            {"application": 1, "_id": 0},
        )
        app = record.get("application") or {}
        for k in ("applied_at", "next_action_date", "updated_at"):
            v = app.get(k)
            if hasattr(v, "isoformat"):
                app[k] = v.isoformat()
        if "interview_dates" in app:
            app["interview_dates"] = [
                d.isoformat() if hasattr(d, "isoformat") else d
                for d in (app["interview_dates"] or [])
            ]
        return jsonify({"application": app}), 200
    except Exception as e:
        logger.exception("Update application error: %s", e)
        return jsonify({"error": "Failed to update application"}), 500


# ------------------------------------------------------------------
# POST /api/resume/tailoring-records/<record_id>/interview-prep
# Generate (or return cached) interview prep pack for a tailoring record.
# ------------------------------------------------------------------
@resume_bp.route("/tailoring-records/<record_id>/interview-prep", methods=["POST"])
@jwt_required()
def generate_interview_prep(record_id):
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"interview_prep:{client_ip}", max_requests=10, window_seconds=600):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(silent=True) or {}
    force = bool(data.get("force", False))

    try:
        db = DBConnect().get_db()
        record = db.tailoring_records.find_one(
            {"record_id": record_id, "user_email": user_email}
        )
        if not record:
            return jsonify({"error": "Record not found"}), 404

        # Return cached pack unless caller forces a regenerate.
        cached = record.get("interview_prep")
        if cached and not force:
            gen = cached.get("generated_at")
            if hasattr(gen, "isoformat"):
                cached = {**cached, "generated_at": gen.isoformat()}
            return jsonify({"interview_prep": cached, "cached": True}), 200

        # Pick the current/latest version for grounding
        versions, _ = ensure_versions(record)
        current_id = record.get("current_version_id")
        current = next((v for v in versions if v.get("version_id") == current_id), None)
        if current is None:
            current = versions[-1] if versions else None
        tailored = (current or {}).get("tailored_resume") or record.get("tailored_resume") or {}

        jd_analysis = record.get("jd_analysis") or {}
        jd_text = (record.get("jd_text") or "")[:6000]

        contact_name = (tailored.get("contact") or {}).get("name") or "The candidate"
        job_title = jd_analysis.get("job_title") or "the role"
        company = jd_analysis.get("company") or ""
        company_ctx = f" at {company}" if company and company != "Not specified" else ""

        # Compact resume summary for prompt grounding
        def _compact_resume(r):
            out = []
            summ = r.get("summary", "")
            if summ:
                out.append(f"Summary: {summ}")
            for e in (r.get("experience") or [])[:5]:
                bullets = "; ".join((e.get("bullets") or [])[:4])
                out.append(
                    f"- {e.get('title','')} at {e.get('company','')} "
                    f"({e.get('dates','')}): {bullets}"
                )
            skills = r.get("skills") or {}
            flat = []
            for k, v in skills.items():
                if isinstance(v, list) and v:
                    flat.append(f"{k}: {', '.join(v[:8])}")
            if flat:
                out.append("Skills: " + " | ".join(flat[:6]))
            return "\n".join(out)[:3500]

        resume_ctx = _compact_resume(tailored)

        from services.claude_client import claude_json, CLAUDE_PRO

        prompt = (
            f"You are a world-class interview coach preparing {contact_name} for "
            f"an interview for {job_title}{company_ctx}. Generate a rich, practical "
            "prep pack grounded ONLY in the provided resume and job description. "
            "Do not invent experience or metrics that aren't there.\n\n"
            f"=== JOB DESCRIPTION ===\n{jd_text}\n\n"
            f"=== JD ANALYSIS ===\n"
            f"Required skills: {', '.join((jd_analysis.get('required_skills') or [])[:25])}\n"
            f"Keywords: {', '.join((jd_analysis.get('keywords') or [])[:25])}\n"
            f"Seniority: {jd_analysis.get('seniority', '')}\n\n"
            f"=== CANDIDATE RESUME ===\n{resume_ctx}\n\n"
            "=== INSTRUCTIONS ===\n"
            "First classify the role in `role_type` as ONE of: \n"
            "  coding | data | devops | design | pm | business | research | generic\n"
            "Then generate interview prep with difficulty distribution across every "
            "question. Each question has a `difficulty` of `easy`, `medium`, or "
            "`hard`. Include a balanced mix in every list.\n\n"
            "For the role-specific section, populate ONE of (or more if relevant):\n"
            "  - coding_problems: if role_type is coding/devops/data (with algorithmic leanings).\n"
            "  - system_design_prompts: always when seniority is senior/staff/principal.\n"
            "  - case_studies: if role_type is business/pm/design/research.\n"
            "  - data_challenges: if role_type is data.\n\n"
            "Respond as strict JSON with these top-level keys:\n"
            "{\n"
            '  "role_type": "coding|data|devops|design|pm|business|research|generic",\n'
            '  "elevator_pitch": "2-3 sentence self intro the candidate can open with",\n'
            '  "talking_points": ["5-7 strongest selling points tied to the JD"],\n'
            '  "behavioral_questions": [\n'
            '     {"question": "...", "why_asked": "...", "answer_outline": "S: ... T: ... A: ... R: ...", "difficulty": "easy|medium|hard"}\n'
            "  ],\n"
            '  "technical_questions": [\n'
            '     {"question": "...", "why_asked": "...", "answer_outline": "...", "difficulty": "easy|medium|hard"}\n'
            "  ],\n"
            '  "company_specific": [\n'
            '     {"question": "...", "why_asked": "...", "answer_outline": "...", "difficulty": "easy|medium|hard"}\n'
            "  ],\n"
            '  "coding_problems": [\n'
            '     {"title": "...", "difficulty": "easy|medium|hard",\n'
            '      "problem_statement": "Clear prompt, like a LeetCode description.",\n'
            '      "constraints": ["..."],\n'
            '      "examples": [{"input": "...", "output": "...", "explanation": "optional"}],\n'
            '      "hints": ["progressive hints — give structure, not solution"],\n'
            '      "approach": "2-4 sentence outline of the intended approach",\n'
            '      "complexity": "Time: O(...), Space: O(...)",\n'
            '      "skill_tags": ["array", "hash-map"]}\n'
            "  ],\n"
            '  "case_studies": [\n'
            '     {"title": "...", "difficulty": "easy|medium|hard",\n'
            '      "scenario": "2-4 sentence business situation",\n'
            '      "subtasks": ["bullets the candidate must solve"],\n'
            '      "evaluation_criteria": ["what the interviewer looks for"],\n'
            '      "hints": ["nudges, not answers"]}\n'
            "  ],\n"
            '  "system_design_prompts": [\n'
            '     {"title": "...", "difficulty": "easy|medium|hard",\n'
            '      "scope": "1 sentence — what is being designed",\n'
            '      "requirements": ["functional + non-functional"],\n'
            '      "suggested_components": ["API gateway", "DB", ...],\n'
            '      "discussion_points": ["tradeoffs to surface"]}\n'
            "  ],\n"
            '  "data_challenges": [\n'
            '     {"title": "...", "difficulty": "easy|medium|hard",\n'
            '      "scenario": "realistic dataset / problem",\n'
            '      "deliverable": "what answer or artefact to produce",\n'
            '      "hints": ["statistical or SQL nudges"]}\n'
            "  ],\n"
            '  "gaps_to_address": ["JD requirements not obvious in the resume — with a 1-line mitigation"],\n'
            '  "questions_to_ask_them": ["5-7 thoughtful questions for the interviewer"],\n'
            '  "red_flags": [\n'
            '     {"question": "possible tough probe", "answer_outline": "1-2 sentence defence", "difficulty": "medium|hard"}\n'
            "  ]\n"
            "}\n"
            "Counts: behavioral 6-8 (mix of easy/medium/hard), technical 6-8, "
            "company_specific 3-5, red_flags 3-5. For coding/system_design/case_studies "
            "return 3-5 items each if relevant to the role. For data_challenges return "
            "3-5 if role is data-focused. Leave arrays empty (never null) if not relevant. "
            "Keep answer_outline under 60 words. No markdown headers, no preamble."
        )

        schema = {
            "role_type": str,
            "elevator_pitch": str,
            "talking_points": list,
            "behavioral_questions": list,
            "technical_questions": list,
            "company_specific": list,
            "coding_problems": list,
            "case_studies": list,
            "system_design_prompts": list,
            "data_challenges": list,
            "gaps_to_address": list,
            "questions_to_ask_them": list,
            "red_flags": list,
        }

        result = claude_json(
            prompt, max_tokens=24000, temperature=0.55, model=CLAUDE_PRO, schema=schema
        )

        pack = {
            "content": result,
            "generated_at": datetime.utcnow(),
            "grounded_version_id": current.get("version_id") if current else None,
        }
        db.tailoring_records.update_one(
            {"record_id": record_id, "user_email": user_email},
            {"$set": {"interview_prep": pack, "updated_at": datetime.utcnow()}},
        )
        return jsonify({
            "interview_prep": {**pack, "generated_at": pack["generated_at"].isoformat()},
            "cached": False,
        }), 200
    except Exception as e:
        logger.exception("Interview prep error: %s", e)
        return jsonify({"error": f"Failed to generate prep: {str(e)[:200]}"}), 500


# ------------------------------------------------------------------
# POST /api/resume/tailoring-records/<record_id>/practice-question
# Generate a single fresh question in a category + difficulty, avoiding
# the set of hashes the client says it has already seen. Used by the
# "Give me another" / flashcard / mock-interview flows.
# ------------------------------------------------------------------
_PRACTICE_CATEGORIES = {
    "behavioral", "technical", "company", "coding",
    "system_design", "case_study", "data_challenge",
}
_DIFFICULTIES = {"easy", "medium", "hard"}


@resume_bp.route("/tailoring-records/<record_id>/practice-question", methods=["POST"])
@jwt_required()
def practice_question(record_id):
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"practice_q:{client_ip}", max_requests=60, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    category = (data.get("category") or "behavioral").lower()
    difficulty = (data.get("difficulty") or "medium").lower()
    seen_titles = data.get("seen_titles") or []  # list of strings/titles to avoid repeating
    seen_titles = [str(t).strip()[:200] for t in seen_titles if t][:30]
    focus = InputSanitizer.sanitize_string(data.get("focus") or "", max_length=200)

    if category not in _PRACTICE_CATEGORIES:
        return jsonify({"error": "Invalid category"}), 400
    if difficulty not in _DIFFICULTIES:
        return jsonify({"error": "Invalid difficulty"}), 400

    try:
        db = DBConnect().get_db()
        record = db.tailoring_records.find_one(
            {"record_id": record_id, "user_email": user_email}
        )
        if not record:
            return jsonify({"error": "Record not found"}), 404

        versions, _ = ensure_versions(record)
        current_id = record.get("current_version_id")
        current = next((v for v in versions if v.get("version_id") == current_id), None) \
            or (versions[-1] if versions else None)
        tailored = (current or {}).get("tailored_resume") or {}
        jd_analysis = record.get("jd_analysis") or {}
        jd_text = (record.get("jd_text") or "")[:4000]

        job_title = jd_analysis.get("job_title") or "the role"

        # Condensed resume signal
        summary = tailored.get("summary", "")
        skills_flat = []
        for k, v in (tailored.get("skills") or {}).items():
            if isinstance(v, list) and v:
                skills_flat.append(f"{k}: {', '.join(v[:6])}")
        exp_flat = [
            f"{e.get('title','')} @ {e.get('company','')}"
            for e in (tailored.get("experience") or [])[:4]
        ]

        avoid_block = ""
        if seen_titles:
            avoid_block = (
                "\nThe candidate has ALREADY seen these items, so return a DIFFERENT "
                "question (different angle, different topic):\n- "
                + "\n- ".join(seen_titles[:20])
            )

        focus_block = f"\nFocus specifically on: {focus}" if focus else ""

        category_instructions = {
            "behavioral": (
                "Generate ONE behavioral interview question with a STAR-format answer outline."
            ),
            "technical": (
                "Generate ONE technical question the interviewer could ask — specific to the "
                "technologies in the JD. Provide a concise answer outline."
            ),
            "company": (
                "Generate ONE question that probes cultural fit or motivation for this company."
            ),
            "coding": (
                "Generate ONE algorithmic / coding problem the interviewer could give on a "
                "whiteboard. Include problem_statement, constraints, examples, hints (nudges "
                "not answers), approach, complexity, and skill_tags."
            ),
            "system_design": (
                "Generate ONE system design prompt. Include scope, requirements (functional + "
                "non-functional), suggested_components, and discussion_points for tradeoffs."
            ),
            "case_study": (
                "Generate ONE business / product case study. Include scenario, subtasks, "
                "evaluation_criteria, and hints."
            ),
            "data_challenge": (
                "Generate ONE data-analysis or SQL challenge. Include scenario, deliverable, "
                "and hints."
            ),
        }[category]

        prompt = (
            f"You are an elite interview coach. Role: {job_title}. "
            f"Target difficulty: {difficulty}. "
            f"{category_instructions}\n\n"
            f"=== JOB DESCRIPTION (excerpt) ===\n{jd_text}\n\n"
            f"=== CANDIDATE CONTEXT ===\nSummary: {summary}\n"
            f"Roles: {' | '.join(exp_flat)}\n"
            f"Skills: {' | '.join(skills_flat[:5])}\n"
            f"{focus_block}{avoid_block}\n\n"
            "Return strict JSON with exactly one top-level key `question` holding the item object.\n"
            "The inner shape depends on category:\n"
            "  behavioral/technical/company: {question, why_asked, answer_outline, difficulty}\n"
            "  coding: {title, problem_statement, constraints[], examples[{input,output,explanation?}], hints[], approach, complexity, skill_tags[], difficulty}\n"
            "  system_design: {title, scope, requirements[], suggested_components[], discussion_points[], difficulty}\n"
            "  case_study: {title, scenario, subtasks[], evaluation_criteria[], hints[], difficulty}\n"
            "  data_challenge: {title, scenario, deliverable, hints[], difficulty}\n"
            "Always wrap: {\"question\": { ... }}. No markdown, no preamble, no extra keys."
        )

        from services.claude_client import claude_json, CLAUDE_PRO
        result = claude_json(
            prompt, max_tokens=8000, temperature=0.75, model=CLAUDE_PRO
        ) or {}
        # Unwrap if the LLM still returned fields at the top level instead of under `question`.
        payload = result.get("question")
        if not isinstance(payload, dict):
            payload = {k: v for k, v in result.items() if k not in ("category", "difficulty")}
        return jsonify({"category": category, "difficulty": difficulty, "question": payload}), 200
    except Exception as e:
        logger.exception("Practice question error: %s", e)
        return jsonify({"error": f"Failed to generate question: {str(e)[:200]}"}), 500


# ------------------------------------------------------------------
# POST /api/resume/tailoring-records/<record_id>/chat
# Context-aware AI interview coach. The LLM receives the resume + JD as
# system context and the most recent messages, then replies. History is
# persisted on the record (capped at last 40 messages).
# ------------------------------------------------------------------
_CHAT_HISTORY_CAP = 40


@resume_bp.route("/tailoring-records/<record_id>/chat", methods=["POST"])
@jwt_required()
def interview_chat(record_id):
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"interview_chat:{client_ip}", max_requests=60, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    message = InputSanitizer.sanitize_string(data.get("message") or "", max_length=4000)
    reset = bool(data.get("reset", False))

    if reset:
        try:
            db = DBConnect().get_db()
            db.tailoring_records.update_one(
                {"record_id": record_id, "user_email": user_email},
                {"$set": {"interview_chat": [], "updated_at": datetime.utcnow()}},
            )
            return jsonify({"messages": []}), 200
        except Exception as e:
            logger.exception("Chat reset error: %s", e)
            return jsonify({"error": "Failed to reset chat"}), 500

    if not message:
        return jsonify({"error": "message is required"}), 400

    try:
        db = DBConnect().get_db()
        record = db.tailoring_records.find_one(
            {"record_id": record_id, "user_email": user_email}
        )
        if not record:
            return jsonify({"error": "Record not found"}), 404

        history = list(record.get("interview_chat") or [])[-(_CHAT_HISTORY_CAP - 1):]

        # Build context
        versions, _ = ensure_versions(record)
        current_id = record.get("current_version_id")
        current = next((v for v in versions if v.get("version_id") == current_id), None) \
            or (versions[-1] if versions else None)
        tailored = (current or {}).get("tailored_resume") or {}
        jd_analysis = record.get("jd_analysis") or {}
        jd_text = (record.get("jd_text") or "")[:3500]

        contact_name = (tailored.get("contact") or {}).get("name") or "The candidate"
        job_title = jd_analysis.get("job_title") or "the role"
        company = jd_analysis.get("company") or ""

        summary = tailored.get("summary", "")
        exp_compact = []
        for e in (tailored.get("experience") or [])[:4]:
            bullets = "; ".join((e.get("bullets") or [])[:3])
            exp_compact.append(f"- {e.get('title','')} @ {e.get('company','')}: {bullets}")
        skills_flat = []
        for k, v in (tailored.get("skills") or {}).items():
            if isinstance(v, list) and v:
                skills_flat.append(f"{k}: {', '.join(v[:6])}")

        system_preamble = (
            f"You are a personal interview coach for {contact_name}, preparing for "
            f"{job_title}{' at ' + company if company and company != 'Not specified' else ''}. "
            "You have full knowledge of their resume and the job description below. "
            "Ground every answer in this context. Use a collaborative, practical tone. "
            "Offer concrete examples, mini-drills, or STAR-structured answers when relevant. "
            "Never pretend to be the interviewer unless explicitly asked to role-play. "
            "Keep replies under 250 words unless the user asks for more.\n\n"
            f"=== JD ({job_title}{' / ' + company if company else ''}) ===\n{jd_text}\n"
            f"Required skills: {', '.join((jd_analysis.get('required_skills') or [])[:20])}\n"
            f"Keywords: {', '.join((jd_analysis.get('keywords') or [])[:20])}\n\n"
            f"=== RESUME ===\nSummary: {summary}\n"
            + "\n".join(exp_compact) + "\n"
            + ("Skills: " + " | ".join(skills_flat[:6]) if skills_flat else "")
        )

        # Convert stored messages to LLM-friendly text
        convo = []
        for m in history:
            role = "User" if m.get("role") == "user" else "Coach"
            convo.append(f"{role}: {m.get('content','')}")
        convo.append(f"User: {message}")
        convo_text = "\n\n".join(convo[-20:])  # keep recent turns within prompt budget

        prompt = (
            f"{system_preamble}\n\n"
            "=== CONVERSATION SO FAR ===\n"
            f"{convo_text}\n\n"
            "Respond ONLY as strict JSON: {\"reply\": \"your coach response\"}. "
            "Keep the reply warm, practical, and under 250 words unless the user "
            "asked for more depth. Offer drills, example answers, or STAR templates "
            "when relevant. No markdown headers; light markdown bullets are fine."
        )

        from services.claude_client import claude_json, CLAUDE_FLASH
        result = claude_json(
            prompt=prompt,
            max_tokens=5000,
            temperature=0.55,
            model=CLAUDE_FLASH,
            schema={"reply": str},
        )
        reply = ((result or {}).get("reply") or "").strip()
        if not reply:
            reply = "I'm not sure I caught that — could you rephrase?"

        now = datetime.utcnow()
        new_history = history + [
            {"role": "user", "content": message, "at": now},
            {"role": "assistant", "content": reply, "at": now},
        ]
        new_history = new_history[-_CHAT_HISTORY_CAP:]
        db.tailoring_records.update_one(
            {"record_id": record_id, "user_email": user_email},
            {"$set": {"interview_chat": new_history, "updated_at": now}},
        )
        # Serialize for response
        out_msgs = [
            {**m, "at": m["at"].isoformat() if hasattr(m.get("at"), "isoformat") else m.get("at")}
            for m in new_history
        ]
        return jsonify({"reply": reply, "messages": out_msgs}), 200
    except Exception as e:
        logger.exception("Interview chat error: %s", e)
        return jsonify({"error": f"Chat failed: {str(e)[:200]}"}), 500


@resume_bp.route("/tailoring-records/<record_id>/chat", methods=["GET"])
@jwt_required()
def get_interview_chat(record_id):
    user_email = get_jwt_identity()
    try:
        db = DBConnect().get_db()
        record = db.tailoring_records.find_one(
            {"record_id": record_id, "user_email": user_email},
            {"interview_chat": 1, "_id": 0},
        )
        if not record:
            return jsonify({"error": "Record not found"}), 404
        msgs = record.get("interview_chat") or []
        out = [
            {**m, "at": m["at"].isoformat() if hasattr(m.get("at"), "isoformat") else m.get("at")}
            for m in msgs
        ]
        return jsonify({"messages": out}), 200
    except Exception as e:
        logger.exception("Get chat error: %s", e)
        return jsonify({"error": "Failed to load chat"}), 500


# ------------------------------------------------------------------
# POST /api/resume/tailoring-records/<record_id>/mock-evaluate
# Scores a user's answer to a question (0-100), lists strengths,
# improvements, and a model "ideal answer" grounded in the JD + resume.
# ------------------------------------------------------------------
@resume_bp.route("/tailoring-records/<record_id>/mock-evaluate", methods=["POST"])
@jwt_required()
def mock_evaluate(record_id):
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"mock_eval:{client_ip}", max_requests=60, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    question = InputSanitizer.sanitize_string(data.get("question") or "", max_length=1500)
    user_answer = InputSanitizer.sanitize_string(data.get("user_answer") or "", max_length=5000)
    category = (data.get("category") or "behavioral").lower()
    if not question or not user_answer:
        return jsonify({"error": "question and user_answer are required"}), 400

    try:
        db = DBConnect().get_db()
        record = db.tailoring_records.find_one(
            {"record_id": record_id, "user_email": user_email}
        )
        if not record:
            return jsonify({"error": "Record not found"}), 404

        versions, _ = ensure_versions(record)
        current_id = record.get("current_version_id")
        current = next((v for v in versions if v.get("version_id") == current_id), None) \
            or (versions[-1] if versions else None)
        tailored = (current or {}).get("tailored_resume") or {}
        jd_analysis = record.get("jd_analysis") or {}
        job_title = jd_analysis.get("job_title") or "the role"

        summary = tailored.get("summary", "")
        skills_flat = []
        for k, v in (tailored.get("skills") or {}).items():
            if isinstance(v, list) and v:
                skills_flat.append(f"{k}: {', '.join(v[:6])}")

        prompt = (
            f"You are a strict but fair interview evaluator for {job_title}. "
            "Score the candidate's answer on a 0-100 scale across: relevance, "
            "clarity, specificity/metrics, structure (STAR when behavioral), and "
            "technical accuracy. Ground feedback in the resume context below and "
            "the job description. Do NOT invent resume facts.\n\n"
            f"=== JOB ===\n{job_title}\n"
            f"Required skills: {', '.join((jd_analysis.get('required_skills') or [])[:20])}\n\n"
            f"=== RESUME CONTEXT ===\nSummary: {summary}\n"
            f"Skills: {' | '.join(skills_flat[:4])}\n\n"
            f"=== QUESTION ({category}) ===\n{question}\n\n"
            f"=== CANDIDATE'S ANSWER ===\n{user_answer}\n\n"
            "Return strict JSON:\n"
            "{\n"
            '  "score": 0-100 integer,\n'
            '  "verdict": "one short sentence summary",\n'
            '  "strengths": ["2-4 concrete things the answer did well"],\n'
            '  "improvements": ["2-4 concrete improvements with examples"],\n'
            '  "missing_points": ["bullets the interviewer expected but did not hear"],\n'
            '  "model_answer": "A concise, strong ideal answer tailored to this candidate (<=150 words)",\n'
            '  "dimensions": {"relevance": 0-100, "clarity": 0-100, "specificity": 0-100, "structure": 0-100, "technical_accuracy": 0-100},\n'
            '  "weak_spot_tags": ["2-4 short tags for what was weak, e.g. \\"no metrics\\", \\"no STAR structure\\", \\"too vague\\", \\"rambling\\""]\n'
            "}"
        )

        schema = {
            "score": int,
            "verdict": str,
            "strengths": list,
            "improvements": list,
            "missing_points": list,
            "model_answer": str,
            "dimensions": dict,
            "weak_spot_tags": list,
        }

        from services.claude_client import claude_json, CLAUDE_PRO
        result = claude_json(
            prompt, max_tokens=6000, temperature=0.3, model=CLAUDE_PRO, schema=schema
        )
        # Pillar D — persist every evaluation so the Interview Pattern Coach can
        # aggregate weak spots across roles (previously scored-and-discarded).
        try:
            from services import interview_coach_service
            interview_coach_service.record_attempt(
                user_email, record_id, job_title, category, question, user_answer, result or {}
            )
        except Exception as _cap_err:
            logger.warning("interview attempt capture failed: %s", _cap_err)
        return jsonify({"evaluation": result}), 200
    except Exception as e:
        logger.exception("Mock evaluate error: %s", e)
        return jsonify({"error": f"Evaluation failed: {str(e)[:200]}"}), 500


# ------------------------------------------------------------------
# POST /api/resume/save-record — Store complete tailoring session
# ------------------------------------------------------------------


def _record_streak_once(col, record, user_email, tz=None):
    """Fire the daily streak exactly once per record (guarded by the
    `streak_recorded` flag). Worker-persisted records (spec 1.5) get their
    streak when the client first checks in via /save-record."""
    if record.get("streak_recorded"):
        return
    claimed = col.update_one(
        {
            "record_id": record.get("record_id"),
            "user_email": user_email,
            "streak_recorded": {"$ne": True},
        },
        {"$set": {"streak_recorded": True}},
    )
    if not claimed.modified_count:
        return
    try:
        from services.streak_service import record_application
        record_application(user_email, tz=tz)
    except Exception as streak_err:
        logger.warning(f"Streak update failed for {user_email}: {streak_err}")


@resume_bp.route("/save-record", methods=["POST"])
@jwt_required()
def save_record():
    """Store / settle a tailoring session record.

    Resume Platform v3 (spec 1.5): the tailor workers persist the canonical
    record themselves and return `record_id` in the job result — this route
    is now idempotent-by-record_id (upsert): with a record_id it attaches
    ats_scores + fires the once-per-record streak, NEVER creating a
    duplicate; without one it dedups by initial content hash before falling
    back to the legacy create path."""
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}

    record_id = InputSanitizer.sanitize_string(
        str(data.get("record_id") or ""), max_length=64
    ) or None
    jd_text = data.get("jd_text", "")
    jd_analysis = data.get("jd_analysis")
    tailored_resume = data.get("tailored_resume")
    ats_scores = data.get("ats_scores")
    base_resume_filename = data.get("base_resume_filename", "")
    base_resume_s3_key = data.get("base_resume_s3_key", "")
    user_tz = data.get("tz") or request.args.get("tz") or None

    if not record_id and (not jd_analysis or not tailored_resume):
        return jsonify({"error": "jd_analysis and tailored_resume are required"}), 400

    try:
        db = DBConnect().get_db()
        col = db.tailoring_records

        existing = None
        if record_id:
            existing = col.find_one({"record_id": record_id, "user_email": user_email})
            if not existing and (not jd_analysis or not tailored_resume):
                return jsonify({"error": "Record not found"}), 404
        elif tailored_resume:
            # Worker-saved dedup: an identical initial version already exists
            # (the FE didn't echo the record_id back) — settle onto it instead
            # of creating a duplicate record.
            content_hash = canonical_content_hash(tailored_resume)
            existing = col.find_one(
                {"user_email": user_email, "versions.content_hash": content_hash}
            )

        if existing:
            # Idempotent settle — attach ATS scores to the current version
            # and mirror them at the top level for legacy list queries.
            rid = existing["record_id"]
            versions, _ = ensure_versions(existing)
            current_id = existing.get("current_version_id") or (
                latest_version(versions) or {}
            ).get("version_id")
            for v in versions:
                if v.get("version_id") == current_id:
                    if ats_scores:
                        v["ats_scores"] = ats_scores
                    break
            update_set = {
                "versions": versions,
                "current_version_id": current_id,
                "updated_at": datetime.utcnow(),
            }
            if ats_scores:
                update_set["ats_scores"] = ats_scores
                update_set["ats_scored_at"] = datetime.utcnow()
            col.update_one(
                {"record_id": rid, "user_email": user_email},
                {"$set": update_set},
            )
            _record_streak_once(col, existing, user_email, tz=user_tz)
            return jsonify({"record_id": rid, "updated": True}), 200

        # Create (legacy clients / upsert of a missing record_id) with an
        # initial version.
        import uuid as _uuid
        from services.resume_versioning import build_initial_version

        rid = record_id or str(_uuid.uuid4())
        initial = build_initial_version(
            tailored_resume, source="initial", ats_scores=ats_scores
        )
        record = {
            "record_id": rid,
            "user_email": user_email,
            "base_resume_filename": base_resume_filename,
            "base_resume_s3_key": base_resume_s3_key,
            "jd_text": jd_text[:15000] if jd_text else "",
            "jd_analysis": jd_analysis,
            "tailored_resume": tailored_resume,  # legacy mirror; versions[0] is canonical
            "ats_scores": ats_scores,
            "versions": [initial],
            "current_version_id": initial["version_id"],
            "streak_recorded": True,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "ats_scored_at": datetime.utcnow() if ats_scores else None,
        }
        col.insert_one(record)
        logger.info(f"Tailoring record saved: {rid} for {user_email}")

        try:
            from services.streak_service import record_application
            record_application(user_email, tz=user_tz)
        except Exception as streak_err:
            logger.warning(f"Streak update failed for {user_email}: {streak_err}")

        return jsonify({"record_id": rid, "version_id": initial["version_id"]}), 201

    except Exception as e:
        logger.error(f"Save record error: {e}")
        return jsonify({"error": "Failed to save record"}), 500


# ------------------------------------------------------------------
# GET /api/resume/job/<job_id> — Poll for job result
# ------------------------------------------------------------------


@resume_bp.route("/job/<job_id>", methods=["GET"])
@jwt_required()
def get_job(job_id):
    try:
        from services.resume_service import get_resume_service

        user_email = get_jwt_identity()
        job = get_resume_service().get_job(job_id, user_email=user_email)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        for key in ("created_at", "completed_at"):
            if hasattr(job.get(key), "isoformat"):
                job[key] = job[key].isoformat()
        return jsonify(job), 200
    except Exception as e:
        logger.error(f"Job status error: {e}")
        return jsonify({"error": "Failed to check job status"}), 500


# ------------------------------------------------------------------
# POST /api/resume/six-seconds — Recruiter's Six Seconds (§6.11.2)
# ------------------------------------------------------------------
@resume_bp.route("/six-seconds", methods=["POST"])
@jwt_required()
def six_seconds():
    user_email = get_jwt_identity()
    from services import entitlements_service, recruiter_gaze_service
    from services.resume_service import get_resume_service, ResumeService
    from services.resume_parser import ResumeParser

    db = DBConnect().get_db()

    # 1. A read needs a parsed resume. Check FIRST so a no-resume open never
    #    spends quota — the tab auto-runs on mount, and charging before this
    #    check silently pushed users into the upgrade wall before they had even
    #    uploaded a resume. The FE handles this synchronous {error:no_resume}.
    resume = ResumeParser(db).get_structured_resume(user_email=user_email)
    if not resume or not resume.get("structured"):
        return jsonify({"error": "no_resume"}), 200

    fingerprint = recruiter_gaze_service.six_seconds_fingerprint(resume)
    force = bool((request.get_json(silent=True) or {}).get("force"))

    # 2. The read only changes when the resume changes. Serve a cached read for
    #    the current resume version WITHOUT charging, so passively re-opening the
    #    tab is free; only an explicit re-run (force) or a new resume version
    #    spends a quota unit. The FE handles this synchronous result body.
    if not force:
        cached = recruiter_gaze_service.get_cached_six_seconds(user_email, fingerprint)
        if cached:
            return jsonify(cached), 200

    # 3. Fresh compute — NOW gate + charge, before we spend on the LLM.
    allowed, usage = entitlements_service.check_and_increment(user_email, "six_seconds")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "six_seconds", usage)
    payload = {"user_email": user_email, "fingerprint": fingerprint}
    job_id = get_resume_service().create_job("six_seconds", payload, user_email=user_email)
    ResumeService.invoke_async(job_id, "six_seconds", payload)
    return jsonify({"job_id": job_id}), 202


# ------------------------------------------------------------------
# LinkedIn archive importer + Referral Finder (§2.1 / §6.11.3)
# ------------------------------------------------------------------
@resume_bp.route("/linkedin-archive", methods=["POST"])
@jwt_required()
def linkedin_archive_upload():
    user_email = get_jwt_identity()
    if get_rate_limiter().is_rate_limited(f"li_archive:{get_client_ip(request)}", max_requests=5, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded. Max 5 imports per hour."}), 429
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".zip"):
        return jsonify({"error": "Upload your LinkedIn data export (.zip)"}), 400
    zip_bytes = f.read()
    if len(zip_bytes) > 30 * 1024 * 1024:
        return jsonify({"error": "File too large (max 30 MB)"}), 400
    from services import linkedin_archive_service as li
    try:
        parsed = li.parse_archive(zip_bytes)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    summary = li.save_archive(user_email, parsed)
    diff = li.diff_against_resume(user_email, parsed)
    return jsonify({"summary": summary, "diff": diff}), 200


@resume_bp.route("/linkedin-archive/diff", methods=["GET"])
@jwt_required()
def linkedin_archive_diff():
    user_email = get_jwt_identity()
    from services import linkedin_archive_service as li
    arc = li.get_archive(user_email)
    if not arc:
        return jsonify({"error": "no_archive", "message": "No LinkedIn archive imported yet."}), 404
    return jsonify(li.diff_against_resume(user_email, arc)), 200


@resume_bp.route("/referrals", methods=["GET"])
@jwt_required()
def referrals():
    user_email = get_jwt_identity()
    from services import entitlements_service as ent
    if not ent.feature_allowed(user_email, "referral_finder"):
        return ent.upgrade_required_response(user_email, "referral_finder")
    company = InputSanitizer.sanitize_string(request.args.get("company", ""), max_length=200)
    if not company.strip():
        return jsonify({"error": "company query param required"}), 400
    from services import referral_finder_service as rf
    return jsonify({"company": company, "referrals": rf.find_referrals(user_email, company)}), 200


@resume_bp.route("/referrals/draft", methods=["POST"])
@jwt_required()
def referrals_draft():
    user_email = get_jwt_identity()
    if get_rate_limiter().is_rate_limited(f"referral_draft:{user_email}", max_requests=30, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded. Try again later."}), 429
    from services import entitlements_service as ent
    if not ent.feature_allowed(user_email, "referral_finder"):
        return ent.upgrade_required_response(user_email, "referral_finder")
    body = request.get_json(silent=True) or {}
    referral, job = body.get("referral") or {}, body.get("job") or {}
    if not referral.get("position"):
        return jsonify({"error": "referral.position required"}), 400
    from services.resume_service import get_resume_service, ResumeService
    payload = {"user_email": user_email, "referral": referral, "job": job}
    job_id = get_resume_service().create_job("referral_draft", payload, user_email=user_email)
    ResumeService.invoke_async(job_id, "referral_draft", payload)
    return jsonify({"job_id": job_id}), 202


# ------------------------------------------------------------------
# Pipeline Intelligence — funnel, ghost detection, cohort benchmark (§6.8)
# ------------------------------------------------------------------
@resume_bp.route("/pipeline/funnel", methods=["GET"])
@jwt_required()
def pipeline_funnel():
    user_email = get_jwt_identity()
    try:
        from services.pipeline_intel_service import funnel
        return jsonify(funnel(user_email)), 200
    except Exception as e:
        logger.error("pipeline/funnel error: %s", e)
        return jsonify({"error": "Failed to load pipeline funnel"}), 500


@resume_bp.route("/pipeline/ghosts", methods=["GET"])
@jwt_required()
def pipeline_ghosts():
    user_email = get_jwt_identity()
    try:
        from services.pipeline_intel_service import ghosts
        return jsonify({"ghosts": ghosts(user_email)}), 200
    except Exception as e:
        logger.error("pipeline/ghosts error: %s", e)
        return jsonify({"error": "Failed to load ghosted applications"}), 500


@resume_bp.route("/pipeline/ghosts/mark", methods=["POST"])
@jwt_required()
def pipeline_mark_ghosts():
    user_email = get_jwt_identity()
    try:
        from services.pipeline_intel_service import mark_ghosted
        return jsonify(mark_ghosted(user_email)), 200
    except Exception as e:
        logger.error("pipeline/ghosts/mark error: %s", e)
        return jsonify({"error": "Failed to mark ghosted applications"}), 500


@resume_bp.route("/pipeline/cohort", methods=["GET"])
@jwt_required()
def pipeline_cohort():
    user_email = get_jwt_identity()
    try:
        from services.pipeline_intel_service import cohort_benchmark
        return jsonify(cohort_benchmark(user_email)), 200
    except Exception as e:
        logger.error("pipeline/cohort error: %s", e)
        return jsonify({"error": "Failed to compute cohort benchmark"}), 500


# ------------------------------------------------------------------
# Signal loop — post performance metrics feed the bandit (§6.7 / §2.2)
# ------------------------------------------------------------------
@resume_bp.route("/posts/<post_id>/metrics", methods=["POST"])
@jwt_required()
def record_post_metrics(post_id):
    user_email = get_jwt_identity()
    if get_rate_limiter().is_rate_limited(f"post_metrics:{user_email}", max_requests=60, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded. Try again later."}), 429
    post_id = InputSanitizer.sanitize_string(post_id, max_length=120)
    body = request.get_json(silent=True) or {}
    from services import post_metrics_service
    # `spec` is not accepted from the client — the bandit spec is read server-side.
    result = post_metrics_service.record_metric(
        user_email, post_id, body.get("source", "user_reported"),
        body.get("metrics") or {},
    )
    return jsonify(result), 200


@resume_bp.route("/posts/<post_id>/metrics", methods=["GET"])
@jwt_required()
def get_post_metrics(post_id):
    user_email = get_jwt_identity()
    post_id = InputSanitizer.sanitize_string(post_id, max_length=120)
    from services import post_metrics_service
    return jsonify({"post_id": post_id, "reward": post_metrics_service.reward_for(post_id, user_email)}), 200


# ------------------------------------------------------------------
# Notification preferences (§2.5) — per-channel + per-event toggles
# ------------------------------------------------------------------
@resume_bp.route("/notifications/prefs", methods=["GET"])
@jwt_required()
def get_notification_prefs():
    from services.notifications import service as notify
    return jsonify(notify.get_prefs(get_jwt_identity())), 200


@resume_bp.route("/notifications/prefs", methods=["PUT"])
@jwt_required()
def update_notification_prefs():
    from services.notifications import service as notify
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(notify.set_prefs(get_jwt_identity(), body)), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@resume_bp.route("/notifications/unsubscribe", methods=["POST"])
@jwt_required()
def unsubscribe_notifications():
    from services.notifications import service as notify
    return jsonify(notify.set_unsubscribed(get_jwt_identity(), True)), 200


# ------------------------------------------------------------------
# Mail-ingress Track A (§2.4) — forward recruiting mail to a unique address.
# The inbound SES webhook is a separate, Terraform-gated top-level prefix.
# ------------------------------------------------------------------
@resume_bp.route("/mail-ingress/provision", methods=["POST"])
@jwt_required()
def mail_ingress_provision():
    from services import mail_ingress_service as mi
    if not mi.mail_ingress_enabled():
        return jsonify({"error": "Not found"}), 404
    try:
        return jsonify(mi.provision_address(get_jwt_identity())), 200
    except Exception as e:
        logger.error("mail-ingress provision error: %s", e)
        return jsonify({"error": "Failed to provision ingress address"}), 500


@resume_bp.route("/mail-ingress/suggestions", methods=["GET"])
@jwt_required()
def mail_ingress_suggestions():
    from services import mail_ingress_service as mi
    if not mi.mail_ingress_enabled():
        return jsonify({"error": "Not found"}), 404
    include_resolved = request.args.get("include_resolved", "false").lower() == "true"
    try:
        return jsonify({"suggestions": mi.list_suggestions(get_jwt_identity(), include_resolved)}), 200
    except Exception as e:
        logger.error("mail-ingress suggestions error: %s", e)
        return jsonify({"error": "Failed to load suggestions"}), 500


@resume_bp.route("/mail-ingress/suggestions/<suggestion_id>/resolve", methods=["POST"])
@jwt_required()
def mail_ingress_resolve(suggestion_id):
    from services import mail_ingress_service as mi
    if not mi.mail_ingress_enabled():
        return jsonify({"error": "Not found"}), 404
    action = ((request.get_json(silent=True) or {}).get("action") or "").strip().lower()
    if action not in ("confirm", "reject"):
        return jsonify({"error": "action must be 'confirm' or 'reject'"}), 400
    try:
        return jsonify({"suggestion": mi.resolve_suggestion(get_jwt_identity(), suggestion_id, action)}), 200
    except LookupError:
        return jsonify({"error": "Suggestion not found"}), 404
    except Exception as e:
        logger.error("mail-ingress resolve error: %s", e)
        return jsonify({"error": "Failed to resolve suggestion"}), 500


# ------------------------------------------------------------------
# POST /api/resume/download — Generate PDF or DOCX
# ------------------------------------------------------------------

_LAYOUT_HEADERS = (
    "X-Resume-Pages", "X-Resume-Outcome",
    "X-Resume-Fill-Ratio", "X-Resume-Dropped",
)


def _layout_payload(resolution) -> dict:
    return resolution.to_dict() if resolution is not None else {}


def _attach_layout_headers(resp, layout: dict):
    if not layout:
        return resp
    resp.headers["X-Resume-Pages"] = str(layout.get("pages", ""))
    resp.headers["X-Resume-Outcome"] = str(layout.get("outcome", ""))
    resp.headers["X-Resume-Fill-Ratio"] = str(layout.get("fill_ratio", ""))
    resp.headers["X-Resume-Dropped"] = json.dumps(layout.get("dropped") or [])
    if layout.get("export_check"):
        resp.headers["X-Resume-Export-Check"] = layout["export_check"]["status"]
    return resp


def _render_document(svc, tailored, fmt):
    """Return (bytes, layout_dict). Both formats share the same solver."""
    if fmt == "pdf":
        file_bytes, resolution = svc.renderer.generate_pdf(tailored)
    else:
        file_bytes = svc.renderer.generate_docx(tailored)
        _working, resolution = svc.renderer._compute_pdf_layout(tailored)
    layout = resolution.to_dict()
    from services.tailoring_pipeline import is_result
    if is_result(tailored):
        from services.tailoring_assessment import check_export
        layout["export_check"], _ = check_export(file_bytes, tailored, format=fmt)
    return file_bytes, layout


@resume_bp.route("/layout-spec", methods=["GET"])
def layout_spec():
    """Public LayoutSpec JSON — the frontend preview's source of truth."""
    from services.resume_renderer import DEFAULT_SPEC
    return jsonify(DEFAULT_SPEC.to_public_dict())


@resume_bp.route("/download", methods=["POST"])
@jwt_required()
def download():
    """Render (or serve cached) PDF/DOCX for a tailored resume.

    Dedup rules:
      - If `record_id` + `version_id` are provided and `versions.files[format]`
        exists with a matching content_hash, stream the cached S3 file — no
        re-render, no new DB row.
      - If the version's `files[format]` is missing, render, upload to S3,
        and $set the sub-doc atomically.
      - If the payload's content_hash differs from the stored version's, a new
        version is appended first (source='edited'), then cached.
      - If no `record_id` is provided, fall back to legacy behavior (render +
        optional S3 upload to user_resumes without dedup linkage).
    """
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"resume_download:{client_ip}", max_requests=30, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    tailored = data.get("tailored_resume")
    jd_analysis = data.get("jd_analysis", {})
    fmt = data.get("format", "pdf")
    record_id = data.get("record_id")
    version_id = data.get("version_id")
    source = data.get("source", "edited")
    auto_save_on_edit = bool(data.get("auto_save_on_edit", True))

    if not tailored or not isinstance(tailored, dict):
        return jsonify({"error": "tailored_resume is required"}), 400
    if fmt not in ("pdf", "docx"):
        return jsonify({"error": "format must be 'pdf' or 'docx'"}), 400

    mimetype = (
        "application/pdf" if fmt == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    try:
        from services.resume_service import get_resume_service

        svc = get_resume_service()
        storage = get_storage_service()
        db = DBConnect().get_db()

        content_hash = canonical_content_hash(tailored)
        filename = svc.renderer.build_filename(tailored, jd_analysis, fmt)

        # ── Versioned path (preferred) ──
        if record_id:
            record = db.tailoring_records.find_one(
                {"record_id": record_id, "user_email": user_email}
            )
            if not record:
                return jsonify({"error": "Record not found"}), 404

            versions, migrated = ensure_versions(record)
            target = find_version(versions, version_id) if version_id else None
            if target is None:
                # Fall back to the record's current or latest version
                cur_id = record.get("current_version_id")
                target = find_version(versions, cur_id) if cur_id else latest_version(versions)

            # If the submitted content differs from the target version, append
            # a new version (source='edited') so nothing is lost.
            if target is None or target.get("content_hash") != content_hash:
                if auto_save_on_edit:
                    try:
                        target, _ = append_version(
                            versions,
                            tailored,
                            source=source if source in ("regenerated", "edited") else "edited",
                            parent_version_id=target.get("version_id") if target else None,
                        )
                    except VersionLimitError:
                        # Bounded refinement (spec 1.5): edited content past
                        # the cap can't be saved — refuse rather than render
                        # an untracked variant.
                        return _version_limit_response()
                else:
                    # An unsaved draft must never replace an immutable saved
                    # version's object key or cached checks.
                    file_bytes, layout = _render_document(svc, tailored, fmt)
                    resp = send_file(io.BytesIO(file_bytes), mimetype=mimetype,
                                     as_attachment=True, download_name=filename)
                    return _attach_layout_headers(resp, layout)

            # Cache hit? Serve from S3, no new row, no re-render.
            # The render_version guard ensures a format change in the renderer
            # (margins, name casing, layout) invalidates files rendered by older
            # code even when the resume content — and thus content_hash — is
            # unchanged. Without it, downloads keep serving the stale old format.
            files = target.get("files") or {}
            cached = files.get(fmt)
            if (
                cached
                and cached.get("s3_key")
                and cached.get("content_hash") == content_hash
                and cached.get("render_version") == svc.renderer.RENDER_VERSION
            ):
                try:
                    file_bytes = storage.get_resume(cached["s3_key"])
                    resp = send_file(
                        io.BytesIO(file_bytes),
                        mimetype=mimetype,
                        as_attachment=True,
                        download_name=cached.get("filename") or filename,
                    )
                    return _attach_layout_headers(resp, cached.get("layout") or {})
                except Exception as cache_err:
                    logger.warning("Cached S3 fetch failed, re-rendering: %s", cache_err)

            # Cache miss — render and upload.
            file_bytes, layout = _render_document(svc, tailored, fmt)
            if not file_bytes or len(file_bytes) < 100:
                return jsonify({"error": "Failed to generate document"}), 500

            # NEW renders land under the v3 key scheme (spec section 2):
            # tailoring/{record_id}/{version_id}/{render_version}.{fmt} —
            # no user id, job title, or timestamp in the key. Old keys stay
            # resolvable through the stored file refs.
            s3_key = None
            try:
                s3_key = storage.upload_tailoring_file(
                    record_id,
                    target.get("version_id") or "v0",
                    svc.renderer.RENDER_VERSION,
                    file_bytes,
                    fmt,
                )
            except Exception as s3_err:
                logger.warning(
                    "Failed to upload tailoring file to S3: %s", type(s3_err).__name__
                )

            # Attach file cache to the target version atomically.
            target.setdefault("files", {})
            target["files"][fmt] = {
                "s3_key": s3_key,
                "size_bytes": len(file_bytes),
                "filename": filename,
                "content_hash": content_hash,
                "render_version": svc.renderer.RENDER_VERSION,
                "rendered_at": datetime.utcnow(),
                "layout": layout,
            }
            update_set = {
                "versions": versions,
                "updated_at": datetime.utcnow(),
            }
            if not record.get("current_version_id"):
                update_set["current_version_id"] = target.get("version_id")
            saved = db.tailoring_records.update_one(
                {"record_id": record_id, "user_email": user_email,
                 "updated_at": record.get("updated_at")},
                {"$set": update_set},
            )
            if not saved.matched_count:
                return jsonify({"error": "The resume changed. Download the current version again.", "code": "stale_version"}), 409

            resp = send_file(
                io.BytesIO(file_bytes),
                mimetype=mimetype,
                as_attachment=True,
                download_name=filename,
            )
            return _attach_layout_headers(resp, layout)

        # ── Legacy path (no record_id) — generate once, no DB dedup ──
        file_bytes, layout = _render_document(svc, tailored, fmt)
        if not file_bytes or len(file_bytes) < 100:
            return jsonify({"error": "Failed to generate document"}), 500

        resp = send_file(
            io.BytesIO(file_bytes),
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename,
        )
        return _attach_layout_headers(resp, layout)
    except Exception as e:
        from services.resume_renderer import UnrenderableTextError
        from services.resume_versioning import StaleVersionError
        if isinstance(e, StaleVersionError):
            return jsonify({"error": str(e), "code": "stale_version"}), 409
        if isinstance(e, UnrenderableTextError):
            return jsonify({"error": str(e)}), 422
        logger.exception(f"Resume download error: {e}")
        return jsonify({"error": "Failed to generate document"}), 500


# ------------------------------------------------------------------
# POST /api/resume/upload — Upload and parse a new resume PDF
# ------------------------------------------------------------------


_MAX_UPLOAD_BYTES = 5 * 1024 * 1024


@resume_bp.route("/upload", methods=["POST"])
@jwt_required()
def upload():
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"resume_upload:{client_ip}", max_requests=5, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded. Max 5 uploads per hour."}), 429

    # Size gate BEFORE reading the body into memory, then a bounded read —
    # mirrors the guest onboarding pattern (spec section 4).
    if request.content_length and request.content_length > _MAX_UPLOAD_BYTES + 16 * 1024:
        return jsonify({"error": "File too large (max 5 MB)"}), 400

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    allowed_extensions = (".pdf", ".docx")
    if not file.filename or not any(
        file.filename.lower().endswith(ext) for ext in allowed_extensions
    ):
        return jsonify({"error": "Only PDF and DOCX files are accepted"}), 400

    file_bytes = file.read(_MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        return jsonify({"error": "File too large (max 5 MB)"}), 400

    is_pdf = file.filename.lower().endswith(".pdf")
    if is_pdf and not file_bytes[:5].startswith(b"%PDF"):
        return jsonify({"error": "File is not a valid PDF"}), 400

    original_filename = file.filename

    try:
        import base64
        import hashlib
        import os

        from services import resume_access
        from services.resume_service import get_resume_service, ResumeService
        from services.resume_parser import ResumeParser

        # Validate file structure (lightweight magic-byte checks).
        ResumeParser.validate_file(file_bytes, original_filename)
        # DOCX zip-bomb guard BEFORE anything opens the archive (spec §4).
        if original_filename.lower().endswith(".docx"):
            from utils.zip_guard import guard_zip_archive

            guard_zip_archive(file_bytes)

        # Content hash — used to skip LLM re-parse when the same file is
        # uploaded twice. Per-user (a user re-uploading the exact same bytes
        # gets back their existing parsed structured data instantly).
        content_hash = hashlib.sha256(file_bytes).hexdigest()

        db = DBConnect().get_db()
        svc = get_resume_service()

        # Dedup cache hit: this exact file was already uploaded AND parsed by
        # this user — re-point the active pointer at the existing asset and
        # synthesize an already-completed job (no LLM spend).
        existing = db.resume_assets.find_one(
            {
                "user_email": user_email,
                "content_hash": content_hash,
                "parse_status": "parsed",
            },
            sort=[("created_at", -1)],
        )
        if existing and existing.get("structured"):
            existing_id = existing["resume_id"]
            db.users.update_one(
                {"email": user_email},
                {
                    "$set": {"active_resume_id": existing_id},
                    "$unset": {"pending_active_resume_id": ""},
                },
            )
            job_id = svc.create_job(
                "upload_parse",
                {
                    "cached": True,
                    "content_hash": content_hash,
                    "user_email": user_email,
                    "resume_id": existing_id,
                },
                user_email=user_email,
            )
            svc.complete_job(
                job_id,
                {
                    "parsed_resume": resume_access.shape_asset(existing),
                    "resume_id": existing_id,
                    "cached": True,
                },
            )
            logger.info(
                "Resume upload cache HIT (asset %s) — skipping LLM parse", existing_id
            )
            return jsonify({"job_id": job_id, "resume_id": existing_id, "cached": True}), 202

        user = db.users.find_one({"email": user_email}, {"_id": 1})
        user_id = str(user["_id"]) if user else None
        mime_type = ResumeParser.get_mime_type(original_filename)

        # Resume Platform v3 write order (spec 1.1/2): Mongo asset doc FIRST
        # (parse_status='pending'), S3 second under
        # resume-assets/{resume_id}/original, then $set object_key.
        resume_id = resume_access.create_pending_asset(
            owner_type="user",
            user_email=user_email,
            user_id=user_id,
            filename=original_filename,
            mime_type=mime_type,
            size_bytes=len(file_bytes),
            content_hash=content_hash,
        )

        payload = {
            "resume_id": resume_id,
            "mime_type": mime_type,
            "content_hash": content_hash,
            "user_email": user_email,
        }
        try:
            # Late import so the storage seam resolves at call time
            # (test/monkeypatch friendly — mirrors the guest blueprint).
            from services.s3_service import get_storage_service as _get_storage

            object_key = _get_storage().upload_resume_asset(
                resume_id, file_bytes, mime_type
            )
            try:
                resume_access.attach_object_key(resume_id, object_key)
            except Exception:
                # Orphan rule: S3 write landed but the Mongo ref didn't —
                # compensating delete in the same request path (best effort).
                try:
                    _get_storage().delete_resume(object_key)
                except Exception:
                    pass
                raise
            payload["object_key"] = object_key
        except Exception as s3_err:
            if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
                # On Lambda there is no inline fallback (spec 1.3): the async
                # payload must never carry file bytes.
                resume_access.mark_asset_failed(resume_id, "storage_unavailable")
                logger.error(
                    "Resume upload: S3 unavailable for asset %s: %s",
                    resume_id, type(s3_err).__name__,
                )
                return jsonify({"error": "storage_unavailable"}), 503
            logger.warning(
                "Resume upload: S3 unavailable, dev inline fallback for asset %s: %s",
                resume_id, type(s3_err).__name__,
            )
            payload["file_bytes_b64_dev"] = base64.b64encode(file_bytes).decode("utf-8")

        # Stamp the active-candidate — the worker promotes it on successful
        # parse; a stale/failed parse can never overwrite a newer choice.
        resume_access.stamp_pending_active(user_email, resume_id)

        job_id = svc.create_job("upload_parse", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "upload_parse", payload)

        return jsonify({"job_id": job_id, "resume_id": resume_id}), 202

    except ValueError as e:
        logger.error(f"File validation error: {e}")
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        logger.error(f"Configuration error: {e}")
        return jsonify({"error": f"AI service is not configured: {str(e)}"}), 503
    except Exception as e:
        logger.error(f"Resume upload error: {e}")
        return jsonify({"error": "Failed to process resume upload"}), 500


# ------------------------------------------------------------------
# GET /api/resume/status — Check if a resume is uploaded (user-specific)
# ------------------------------------------------------------------


@resume_bp.route("/base-structured", methods=["GET"])
@jwt_required()
def base_structured():
    """Return the user's parsed base resume as structured JSON.

    Powers diff-vs-base on surfaces that didn't receive `base_resume` inside
    a tailor job result (history browser, batch handoff, page reloads).
    Shape-compatible with TailoredFullResume on the client.
    """
    user_email = get_jwt_identity()
    try:
        from services.resume_service import get_resume_service

        resume = get_resume_service().parser.ensure_structured_resume(
            user_email=user_email
        )
        if not resume or not resume.get("structured"):
            return jsonify({"error": "No parsed resume found"}), 404
        parsed_at = resume.get("parsed_at", "")
        if hasattr(parsed_at, "isoformat"):
            parsed_at = parsed_at.isoformat()
        return jsonify({
            "structured": resume["structured"],
            "filename": resume.get("filename", ""),
            "s3_key": resume.get("s3_key", ""),
            "parsed_at": parsed_at,
        }), 200
    except Exception as e:
        logger.error(f"base-structured fetch error: {e}")
        return jsonify({"error": "Failed to load base resume"}), 500


@resume_bp.route("/status", methods=["GET"])
@jwt_required()
def status():
    """Resume Platform v3: status is derived from `resume_assets` +
    `users.active_resume_id` (the legacy user_resumes reads are gone)."""
    user_email = get_jwt_identity()
    try:
        from services import resume_access

        db = DBConnect().get_db()
        active_asset = resume_access.get_active_asset(user_email)
        has_base_file = bool(
            (active_asset or {}).get("object_key")
            or db.resume_assets.find_one(
                {"user_email": user_email, "object_key": {"$ne": None}},
                {"_id": 1},
            )
        )

        from services.resume_service import get_resume_service

        svc = get_resume_service()
        resume = svc.get_base_resume(user_email=user_email)

        if not resume:
            # No structured/parsed resume found — but a base file might exist.
            # If so, attempt a re-parse from S3 (handles users whose parse
            # job failed or never completed after upload).
            if has_base_file:
                logger.info(
                    "Status: no parsed resume for %s but base file exists, attempting re-parse",
                    user_email,
                )
                resume = svc.parser.ensure_structured_resume(user_email=user_email)

            if not resume:
                return (
                    jsonify(
                        {
                            "has_resume": False,
                            "has_base_file": has_base_file,
                        }
                    ),
                    200,
                )

        parsed_at = resume.get("parsed_at", "")
        if hasattr(parsed_at, "isoformat"):
            parsed_at = parsed_at.isoformat()
        response = {
            "has_resume": True,
            "has_base_file": has_base_file,
            "skills": resume.get("skills", []),
            "experience_years": resume.get("experience_years"),
            "job_titles": resume.get("job_titles", []),
            "summary": resume.get("summary", ""),
            "parsed_at": parsed_at,
        }
        if active_asset:
            response["resume_id"] = active_asset.get("resume_id", "")
            response["s3_key"] = active_asset.get("object_key") or ""
            response["filename"] = active_asset.get("filename", "")
        return jsonify(response), 200
    except Exception as e:
        logger.error(f"Resume status error: {e}")
        return jsonify({"error": "Failed to check resume status"}), 500


# ------------------------------------------------------------------
# GET /api/resume/versions — List base resume versions
# ------------------------------------------------------------------


@resume_bp.route("/versions", methods=["GET"])
@jwt_required()
def list_versions():
    """List the user's uploaded resume assets (Resume Platform v3, spec 1.2).

    Each row: {resume_id, filename, uploaded_at, size_bytes, parse_status,
    is_active}.
    """
    user_email = get_jwt_identity()
    db = DBConnect().get_db()
    user = db.users.find_one({"email": user_email}, {"active_resume_id": 1}) or {}
    active_id = user.get("active_resume_id")
    versions = []
    # Ownership here is `user_email`, NOT `owner_type == "user"`.
    #
    # The two used to disagree, and that disagreement was the bug: this route
    # excluded `owner_type: "guest"` while `resume_access.list_parsed_assets`
    # — which tailoring, scoring and `base_resume_selector` all resolve
    # through — filters only on `user_email`. A resume uploaded during a guest
    # session and then CLAIMED at signup therefore became invisible in the
    # library while staying fully eligible for tailoring, so the product
    # tailored from a file the user could neither see nor delete. Measured
    # 2026-09-02: 10 of 18 guest assets were claimed and in exactly this state.
    #
    # `user_email` is the correct test because an UNCLAIMED guest asset never
    # carries one (verified live: every `claim_status: "unclaimed"` row has
    # `user_email: None`), so this cannot surface another person's upload.
    # `owner_type` is kept as provenance and returned below rather than used as
    # a filter — the guest retention sweep keys on `claim_status`, not on it.
    cursor = db.resume_assets.find(
        {"user_email": user_email},
        {
            "_id": 0,
            "resume_id": 1,
            "filename": 1,
            "created_at": 1,
            "size_bytes": 1,
            "parse_status": 1,
            "owner_type": 1,
        },
    ).sort("created_at", -1)
    for asset in cursor:
        uploaded_at = asset.get("created_at")
        if hasattr(uploaded_at, "isoformat"):
            uploaded_at = uploaded_at.isoformat()
        versions.append({
            "resume_id": asset.get("resume_id", ""),
            "filename": asset.get("filename", ""),
            "uploaded_at": uploaded_at,
            "size_bytes": int(asset.get("size_bytes") or 0),
            "parse_status": asset.get("parse_status", ""),
            "is_active": asset.get("resume_id") == active_id,
            # Provenance, so the UI can label a resume the user uploaded before
            # they had an account instead of it appearing from nowhere.
            "from_guest_session": asset.get("owner_type") == "guest",
        })
    return jsonify({"versions": versions}), 200


# ------------------------------------------------------------------
# GET /api/resume/generated — List generated/tailored resumes
# ------------------------------------------------------------------


@resume_bp.route("/generated", methods=["GET"])
@jwt_required()
def list_generated():
    user_email = get_jwt_identity()
    db = DBConnect().get_db()
    resumes = list(
        db.user_resumes.find({"user_email": user_email, "type": "generated"}, {"_id": 0}).sort(
            "generated_at", -1
        )
    )
    for r in resumes:
        if "generated_at" in r and hasattr(r["generated_at"], "isoformat"):
            r["generated_at"] = r["generated_at"].isoformat()
    return jsonify({"generated": resumes}), 200


# ------------------------------------------------------------------
# PUT /api/resume/active — Set active base resume
# ------------------------------------------------------------------


@resume_bp.route("/active", methods=["PUT"])
@jwt_required()
def set_active():
    """PUT {resume_id} — atomically set users.active_resume_id (spec 1.2).

    Validates ownership + parse_status='parsed', then $sets unconditionally
    (and clears any pending stamp so an in-flight parse can never overwrite
    this explicit choice).
    """
    user_email = get_jwt_identity()
    data = request.get_json(force=True, silent=True) or {}
    resume_id = InputSanitizer.sanitize_string(str(data.get("resume_id") or ""), max_length=64)
    if not resume_id:
        return jsonify({"error": "resume_id is required"}), 400
    db = DBConnect().get_db()
    asset = db.resume_assets.find_one(
        {"resume_id": resume_id, "user_email": user_email},
        {"parse_status": 1},
    )
    if not asset:
        return jsonify({"error": "Resume not found"}), 404
    if asset.get("parse_status") != "parsed":
        return jsonify({"error": "resume_not_parsed"}), 409
    db.users.update_one(
        {"email": user_email},
        {
            "$set": {"active_resume_id": resume_id},
            "$unset": {"pending_active_resume_id": ""},
        },
    )
    return jsonify({"message": "Active resume updated", "resume_id": resume_id}), 200


# ------------------------------------------------------------------
# GET /api/resume/download-file/<s3_key> — Download resume from S3
# ------------------------------------------------------------------


@resume_bp.route("/download-file/<path:s3_key>", methods=["GET"])
@jwt_required()
def download_file(s3_key):
    """Download an owned file from S3.

    Resume Platform v3: the path segment may be a `resume_id` (original-file
    download of a resume_assets doc) OR a legacy/generated S3 key. Ownership
    is always verified against a stored ref — the raw param never picks an
    arbitrary object:
      1. resume_id -> resume_assets (owner-scoped) -> object_key;
      2. generated key -> tailoring_records versions[].files refs;
      3. legacy user_resumes rows (old s3_key param form kept working).
    """
    user_email = get_jwt_identity()
    db = DBConnect().get_db()
    param = str(s3_key or "")

    # Late import so the storage seam resolves at call time (test-friendly).
    from services.s3_service import get_storage_service as _get_storage
    storage = _get_storage()

    object_key = None
    filename = "resume.pdf"
    variant = "base"
    mime_hint = None

    if "/" not in param:
        # Original-file download by resume_id (spec section 5/6 rewiring).
        rid = InputSanitizer.sanitize_string(param, max_length=64)
        asset = db.resume_assets.find_one(
            {"resume_id": rid, "user_email": user_email}
        )
        if not asset or not asset.get("object_key"):
            return jsonify({"error": "Resume not found"}), 404
        object_key = asset["object_key"]
        filename = asset.get("filename") or "resume.pdf"
        mime_hint = asset.get("mime_type") or None
    else:
        # Generated/tailored file — ownership via the version file refs.
        record = db.tailoring_records.find_one(
            {
                "user_email": user_email,
                "$or": [
                    {"versions.files.pdf.s3_key": param},
                    {"versions.files.docx.s3_key": param},
                ],
            },
            {"versions": 1},
        )
        if record:
            variant = "generated"
            object_key = param
            for version in record.get("versions") or []:
                for f in (version.get("files") or {}).values():
                    if isinstance(f, dict) and f.get("s3_key") == param:
                        filename = f.get("filename") or filename
        else:
            # Legacy rows (old key scheme referenced from user_resumes).
            resume = db.user_resumes.find_one(
                {"user_email": user_email, "s3_key": param}
            )
            if not resume:
                return jsonify({"error": "Resume not found"}), 404
            object_key = param
            filename = resume.get("filename", "resume.pdf")
            variant = "generated" if "/generated/" in param else "base"

    file_bytes = storage.get_resume(object_key)
    if mime_hint:
        content_type = mime_hint
    elif filename.lower().endswith(".docx") or object_key.endswith(".docx"):
        content_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    elif filename.lower().endswith(".pdf") or object_key.endswith(".pdf"):
        content_type = "application/pdf"
    else:
        content_type = "application/octet-stream"
    dd_metric(
        "portfolio.resume.download",
        1,
        tags=[f"variant:{variant}", f"format:{'pdf' if content_type == 'application/pdf' else 'other'}"],
    )
    return send_file(
        io.BytesIO(file_bytes), mimetype=content_type, as_attachment=True, download_name=filename
    )


# ------------------------------------------------------------------
# DELETE /api/resume/file/<resume_id> — Delete a resume asset
# ------------------------------------------------------------------


@resume_bp.route("/file/<resume_id>", methods=["DELETE"])
@jwt_required()
def delete_file(resume_id):
    """Delete a resume asset: S3 object + Mongo doc; if it was the active
    resume, atomically re-point to the newest remaining parsed asset or
    unset the pointer (spec 1.2 / 2)."""
    user_email = get_jwt_identity()
    rid = InputSanitizer.sanitize_string(str(resume_id or ""), max_length=64)
    if not rid:
        return jsonify({"error": "Resume not found"}), 404
    db = DBConnect().get_db()
    asset = db.resume_assets.find_one({"resume_id": rid, "user_email": user_email})
    if not asset:
        return jsonify({"error": "Resume not found"}), 404
    if asset.get("object_key"):
        try:
            from services.s3_service import get_storage_service as _get_storage

            _get_storage().delete_resume(asset["object_key"])
        except Exception as e:
            # Best effort — a stray object is swept later; the doc delete
            # and pointer re-point must still land.
            logger.warning("Resume delete: S3 delete failed for asset %s: %s", rid, e)
    db.resume_assets.delete_one({"resume_id": rid, "user_email": user_email})
    from services import resume_access

    resume_access.repoint_active_after_delete(user_email, rid)
    return jsonify({"message": "Resume deleted"}), 200


# ------------------------------------------------------------------
# Career Copilot — multi-agent RAG + learning playground
# ------------------------------------------------------------------


@resume_bp.route("/career-copilot/chat", methods=["POST"])
@jwt_required()
def career_copilot_chat():
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"career_copilot:{client_ip}", max_requests=30, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded. Try again shortly."}), 429

    data = request.get_json(force=True) or {}
    if data.get("reset"):
        from services.career_copilot_service import run_career_copilot
        out = run_career_copilot(user_email, "", reset=True)
        return jsonify(out), 200

    message = InputSanitizer.sanitize_string(data.get("message") or "", max_length=8000)
    if not message:
        return jsonify({"error": "message is required"}), 400
    jd_paste = InputSanitizer.sanitize_string(data.get("jd_paste") or "", max_length=20000)

    try:
        from services.career_copilot_service import run_career_copilot
        out = run_career_copilot(user_email, message, jd_paste=jd_paste, reset=False)
        return jsonify(out), 200
    except Exception as e:
        logger.exception("career_copilot chat: %s", e)
        return jsonify({"error": f"Copilot failed: {str(e)[:200]}"}), 500


@resume_bp.route("/career-copilot/messages", methods=["GET"])
@jwt_required()
def career_copilot_messages_get():
    user_email = get_jwt_identity()
    try:
        from services.career_copilot_service import get_messages_for_api
        return jsonify({"messages": get_messages_for_api(user_email)}), 200
    except Exception as e:
        logger.exception("career_copilot messages: %s", e)
        return jsonify({"error": "Failed to load history"}), 500


@resume_bp.route("/career-copilot/state", methods=["GET"])
@jwt_required()
def career_copilot_state_get():
    user_email = get_jwt_identity()
    try:
        from services.career_copilot_service import get_dashboard_bundle, get_messages_for_api
        bundle = get_dashboard_bundle(user_email)
        bundle["messages"] = get_messages_for_api(user_email)
        return jsonify(bundle), 200
    except Exception as e:
        logger.exception("career_copilot state: %s", e)
        return jsonify({"error": "Failed to load state"}), 500


@resume_bp.route("/career-copilot/behavior", methods=["POST"])
@jwt_required()
def career_copilot_behavior():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    tab = InputSanitizer.sanitize_string(data.get("tab") or "", max_length=32)
    if not tab:
        return jsonify({"error": "tab is required"}), 400
    try:
        from services.career_copilot_service import record_tab_event
        record_tab_event(user_email, tab)
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception("career_copilot behavior: %s", e)
        return jsonify({"error": "Failed"}), 500


@resume_bp.route("/career-copilot/playground/start", methods=["POST"])
@jwt_required()
def career_copilot_playground_start():
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"cp_playground:{client_ip}", max_requests=20, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    track_id = (data.get("track_id") or "").strip() or None
    custom_topic = InputSanitizer.sanitize_string(data.get("custom_topic") or "", max_length=500)
    if not track_id and not (custom_topic or "").strip():
        return jsonify({"ok": False, "error": "track_id or custom_topic required"}), 400
    try:
        from services.career_copilot_service import start_playground_track
        out = start_playground_track(
            user_email,
            track_id=track_id,
            custom_topic=custom_topic or None,
        )
        if not out.get("ok"):
            return jsonify(out), 400
        return jsonify(out), 200
    except Exception as e:
        logger.exception("playground start: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


@resume_bp.route("/career-copilot/playground/advance", methods=["POST"])
@jwt_required()
def career_copilot_playground_advance():
    user_email = get_jwt_identity()
    try:
        from services.career_copilot_service import complete_playground_step
        out = complete_playground_step(user_email)
        if not out.get("ok"):
            return jsonify(out), 400
        return jsonify(out), 200
    except Exception as e:
        logger.exception("playground advance: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


@resume_bp.route("/career-copilot/playground/reset", methods=["POST"])
@jwt_required()
def career_copilot_playground_reset():
    user_email = get_jwt_identity()
    try:
        from services.career_copilot_service import reset_playground
        reset_playground(user_email)
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception("playground reset: %s", e)
        return jsonify({"error": "Failed"}), 500


# ------------------------------------------------------------------
# Career Copilot — Outreach Campaigns
# ------------------------------------------------------------------


@resume_bp.route("/career-copilot/outreach/create", methods=["POST"])
@jwt_required()
def career_copilot_outreach_create():
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"cp_outreach_create:{client_ip}", max_requests=10, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    target_company = InputSanitizer.sanitize_string(data.get("target_company") or "", max_length=200)
    target_role = InputSanitizer.sanitize_string(data.get("target_role") or "", max_length=200)
    channel = InputSanitizer.sanitize_string(data.get("channel") or "", max_length=20)
    contacts = data.get("contacts") or []

    if not target_company or not target_role:
        return jsonify({"error": "target_company and target_role are required"}), 400
    if channel not in ("email", "linkedin", "twitter"):
        channel = "email"

    sanitized_contacts = []
    if isinstance(contacts, list):
        for c in contacts[:20]:
            if not isinstance(c, dict):
                continue
            sanitized_contacts.append({
                "name": InputSanitizer.sanitize_string(c.get("name") or "", max_length=100),
                "title": InputSanitizer.sanitize_string(c.get("title") or "", max_length=100),
                "notes": InputSanitizer.sanitize_string(c.get("notes") or "", max_length=500),
            })

    try:
        from services.career_copilot_service import create_outreach_campaign
        result = create_outreach_campaign(user_email, target_company, target_role, sanitized_contacts, channel)
        return jsonify(result), 200
    except Exception as e:
        logger.exception("outreach create: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


@resume_bp.route("/career-copilot/outreach/campaigns", methods=["GET"])
@jwt_required()
def career_copilot_outreach_campaigns():
    user_email = get_jwt_identity()
    try:
        from services.career_copilot_service import get_outreach_campaigns
        campaigns = get_outreach_campaigns(user_email)
        return jsonify({"campaigns": campaigns}), 200
    except Exception as e:
        logger.exception("outreach campaigns: %s", e)
        return jsonify({"error": "Failed to load campaigns"}), 500


@resume_bp.route("/career-copilot/outreach/generate-sequence", methods=["POST"])
@jwt_required()
def career_copilot_outreach_generate_sequence():
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"cp_outreach_seq:{client_ip}", max_requests=20, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    company = InputSanitizer.sanitize_string(data.get("company") or "", max_length=200)
    role = InputSanitizer.sanitize_string(data.get("role") or "", max_length=200)
    channel = InputSanitizer.sanitize_string(data.get("channel") or "", max_length=20)

    if not company or not role:
        return jsonify({"error": "company and role are required"}), 400
    if channel not in ("email", "linkedin", "twitter"):
        return jsonify({"error": "channel must be email, linkedin, or twitter"}), 400

    try:
        from services.career_copilot_service import generate_outreach_sequence
        result = generate_outreach_sequence(company, role, channel, user_email)
        return jsonify(result), 200
    except Exception as e:
        logger.exception("outreach generate-sequence: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


@resume_bp.route("/career-copilot/outreach/<campaign_id>/step", methods=["PATCH"])
@jwt_required()
def career_copilot_outreach_update_step(campaign_id):
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    step_index = data.get("step_index")
    status = InputSanitizer.sanitize_string(data.get("status") or "", max_length=32)
    notes = InputSanitizer.sanitize_string(data.get("notes") or "", max_length=500)

    if step_index is None or not isinstance(step_index, int):
        return jsonify({"error": "step_index (int) is required"}), 400
    if not status:
        return jsonify({"error": "status is required"}), 400

    try:
        from services.career_copilot_service import update_campaign_step
        result = update_campaign_step(user_email, campaign_id, step_index, status, notes)
        if not result:
            return jsonify({"error": "Campaign not found"}), 404
        return jsonify(result), 200
    except Exception as e:
        logger.exception("outreach update step: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


# ------------------------------------------------------------------
# Career Copilot — Memory Notes
# ------------------------------------------------------------------


@resume_bp.route("/career-copilot/memory", methods=["POST"])
@jwt_required()
def career_copilot_memory_save():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    key = InputSanitizer.sanitize_string(data.get("key") or "", max_length=64)
    value = InputSanitizer.sanitize_string(data.get("value") or "", max_length=500)

    if not key or not value:
        return jsonify({"error": "key and value are required"}), 400

    try:
        from services.career_copilot_service import save_memory_note
        save_memory_note(user_email, key, value)
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception("memory save: %s", e)
        return jsonify({"error": "Failed to save note"}), 500


@resume_bp.route("/career-copilot/memory", methods=["GET"])
@jwt_required()
def career_copilot_memory_get():
    user_email = get_jwt_identity()
    try:
        from services.career_copilot_service import get_memory_notes
        notes = get_memory_notes(user_email)
        return jsonify({"notes": notes}), 200
    except Exception as e:
        logger.exception("memory get: %s", e)
        return jsonify({"error": "Failed to load notes"}), 500


@resume_bp.route("/career-copilot/memory/<key>", methods=["DELETE"])
@jwt_required()
def career_copilot_memory_delete(key):
    user_email = get_jwt_identity()
    key = InputSanitizer.sanitize_string(key or "", max_length=64)
    if not key:
        return jsonify({"error": "key is required"}), 400

    try:
        from services.career_copilot_service import delete_memory_note
        delete_memory_note(user_email, key)
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception("memory delete: %s", e)
        return jsonify({"error": "Failed to delete note"}), 500


# ------------------------------------------------------------------
# Career Copilot — Job Intelligence
# ------------------------------------------------------------------


@resume_bp.route("/career-copilot/intelligence/stale", methods=["GET"])
@jwt_required()
def career_copilot_intelligence_stale():
    user_email = get_jwt_identity()
    try:
        stale_days = min(max(int(request.args.get("days", 5)), 1), 30)
    except (TypeError, ValueError):
        stale_days = 5
    try:
        from services.job_intelligence_service import get_stale_applications
        return jsonify(get_stale_applications(user_email, stale_days=stale_days)), 200
    except Exception as e:
        logger.exception("intelligence stale: %s", e)
        return jsonify({"error": "Failed to load stale applications"}), 500


@resume_bp.route("/career-copilot/intelligence/followup", methods=["POST"])
@jwt_required()
def career_copilot_intelligence_followup():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    record_id = InputSanitizer.sanitize_string(data.get("record_id") or "", max_length=64)
    channel = InputSanitizer.sanitize_string(data.get("channel") or "email", max_length=16)
    if not record_id:
        return jsonify({"error": "record_id is required"}), 400
    try:
        from services.job_intelligence_service import generate_followup_message
        out = generate_followup_message(user_email, record_id, channel)
        if not out.get("ok"):
            return jsonify(out), 404
        return jsonify(out), 200
    except Exception as e:
        logger.exception("intelligence followup: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


@resume_bp.route("/career-copilot/intelligence/followup/<record_id>/dismiss", methods=["POST"])
@jwt_required()
def career_copilot_intelligence_followup_dismiss(record_id):
    user_email = get_jwt_identity()
    try:
        from services.job_intelligence_service import dismiss_followup
        out = dismiss_followup(user_email, record_id)
        if not out.get("ok"):
            return jsonify(out), 404
        return jsonify(out), 200
    except Exception as e:
        logger.exception("intelligence followup dismiss: %s", e)
        return jsonify({"error": "Failed to dismiss follow-up"}), 500


@resume_bp.route("/career-copilot/intelligence/funnel", methods=["GET"])
@jwt_required()
def career_copilot_intelligence_funnel():
    user_email = get_jwt_identity()
    try:
        from services.job_intelligence_service import get_funnel_analytics
        return jsonify(get_funnel_analytics(user_email)), 200
    except Exception as e:
        logger.exception("intelligence funnel: %s", e)
        return jsonify({"error": "Failed to load funnel analytics"}), 500


@resume_bp.route("/career-copilot/intelligence/rejection-insights", methods=["GET"])
@jwt_required()
def career_copilot_intelligence_rejection_insights():
    user_email = get_jwt_identity()
    try:
        from services.job_intelligence_service import analyze_rejection_patterns
        return jsonify(analyze_rejection_patterns(user_email)), 200
    except Exception as e:
        logger.exception("intelligence rejection insights: %s", e)
        return jsonify({"error": "Failed to analyze rejection patterns"}), 500


@resume_bp.route("/career-copilot/intelligence/reframe", methods=["POST"])
@jwt_required()
def career_copilot_intelligence_reframe():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    record_id = InputSanitizer.sanitize_string(data.get("record_id") or "", max_length=64)
    if not record_id:
        return jsonify({"error": "record_id is required"}), 400
    try:
        from services.job_intelligence_service import reframe_rejection
        out = reframe_rejection(user_email, record_id)
        if not out.get("ok"):
            return jsonify(out), 404
        return jsonify(out), 200
    except Exception as e:
        logger.exception("intelligence reframe: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


@resume_bp.route("/career-copilot/intelligence/weekly-digest", methods=["GET"])
@jwt_required()
def career_copilot_intelligence_weekly_digest():
    user_email = get_jwt_identity()
    try:
        from services.job_intelligence_service import get_weekly_digest
        return jsonify(get_weekly_digest(user_email)), 200
    except Exception as e:
        logger.exception("intelligence weekly digest: %s", e)
        return jsonify({"error": "Failed to load weekly digest"}), 500


# ------------------------------------------------------------------
# Career Copilot — Momentum
# ------------------------------------------------------------------


@resume_bp.route("/career-copilot/momentum", methods=["GET"])
@jwt_required()
def career_copilot_momentum_get():
    user_email = get_jwt_identity()
    try:
        from services.job_intelligence_service import get_momentum_data
        return jsonify(get_momentum_data(user_email)), 200
    except Exception as e:
        logger.exception("momentum get: %s", e)
        return jsonify({"error": "Failed to load momentum data"}), 500


@resume_bp.route("/career-copilot/momentum/activity", methods=["POST"])
@jwt_required()
def career_copilot_momentum_activity():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    activity_type = InputSanitizer.sanitize_string(data.get("activity_type") or "", max_length=64)
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    if not activity_type:
        return jsonify({"error": "activity_type is required"}), 400
    try:
        from services.job_intelligence_service import record_activity
        return jsonify(record_activity(user_email, activity_type, metadata)), 200
    except Exception as e:
        logger.exception("momentum activity: %s", e)
        return jsonify({"error": "Failed to record activity"}), 500


# ------------------------------------------------------------------
# Career Copilot — Networking CRM
# ------------------------------------------------------------------


@resume_bp.route("/career-copilot/network/contacts", methods=["GET"])
@jwt_required()
def career_copilot_network_contacts_get():
    user_email = get_jwt_identity()
    try:
        from services.job_intelligence_service import list_contacts
        return jsonify(list_contacts(user_email)), 200
    except Exception as e:
        logger.exception("network contacts get: %s", e)
        return jsonify({"error": "Failed to load contacts"}), 500


@resume_bp.route("/career-copilot/network/contacts", methods=["POST"])
@jwt_required()
def career_copilot_network_contacts_add():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    payload = {
        "name": InputSanitizer.sanitize_string(data.get("name") or "", max_length=120),
        "company": InputSanitizer.sanitize_string(data.get("company") or "", max_length=120),
        "role": InputSanitizer.sanitize_string(data.get("role") or "", max_length=120),
        "platform": InputSanitizer.sanitize_string(data.get("platform") or "linkedin", max_length=32),
        "relationship_strength": InputSanitizer.sanitize_string(data.get("relationship_strength") or "cold", max_length=16),
        "notes": InputSanitizer.sanitize_string(data.get("notes") or "", max_length=1000),
        "last_contacted": InputSanitizer.sanitize_string(data.get("last_contacted") or "", max_length=40),
        "referral_status": InputSanitizer.sanitize_string(data.get("referral_status") or "none", max_length=32),
    }
    if not payload["name"]:
        return jsonify({"error": "name is required"}), 400
    try:
        from services.job_intelligence_service import add_contact
        return jsonify(add_contact(user_email, payload)), 200
    except Exception as e:
        logger.exception("network contacts add: %s", e)
        return jsonify({"error": "Failed to add contact"}), 500


@resume_bp.route("/career-copilot/network/contacts/<contact_id>", methods=["PUT"])
@jwt_required()
def career_copilot_network_contacts_update(contact_id):
    user_email = get_jwt_identity()
    contact_id = InputSanitizer.sanitize_string(contact_id or "", max_length=64)
    if not contact_id:
        return jsonify({"error": "contact_id is required"}), 400
    data = request.get_json(force=True) or {}
    patch = {
        "name": InputSanitizer.sanitize_string(data.get("name") or "", max_length=120),
        "company": InputSanitizer.sanitize_string(data.get("company") or "", max_length=120),
        "role": InputSanitizer.sanitize_string(data.get("role") or "", max_length=120),
        "platform": InputSanitizer.sanitize_string(data.get("platform") or "", max_length=32),
        "relationship_strength": InputSanitizer.sanitize_string(data.get("relationship_strength") or "", max_length=16),
        "notes": InputSanitizer.sanitize_string(data.get("notes") or "", max_length=1000),
        "last_contacted": InputSanitizer.sanitize_string(data.get("last_contacted") or "", max_length=40),
        "referral_status": InputSanitizer.sanitize_string(data.get("referral_status") or "", max_length=32),
    }
    patch = {k: v for k, v in patch.items() if v != ""}
    try:
        from services.job_intelligence_service import update_contact
        out = update_contact(user_email, contact_id, patch)
        if not out.get("ok"):
            return jsonify(out), 404
        return jsonify(out), 200
    except Exception as e:
        logger.exception("network contacts update: %s", e)
        return jsonify({"error": "Failed to update contact"}), 500


@resume_bp.route("/career-copilot/network/contacts/<contact_id>", methods=["DELETE"])
@jwt_required()
def career_copilot_network_contacts_delete(contact_id):
    user_email = get_jwt_identity()
    contact_id = InputSanitizer.sanitize_string(contact_id or "", max_length=64)
    if not contact_id:
        return jsonify({"error": "contact_id is required"}), 400
    try:
        from services.job_intelligence_service import delete_contact
        out = delete_contact(user_email, contact_id)
        return jsonify(out), 200 if out.get("ok") else 404
    except Exception as e:
        logger.exception("network contacts delete: %s", e)
        return jsonify({"error": "Failed to delete contact"}), 500


@resume_bp.route("/career-copilot/network/generate-intro", methods=["POST"])
@jwt_required()
def career_copilot_network_generate_intro():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    contact_id = InputSanitizer.sanitize_string(data.get("contact_id") or "", max_length=64)
    if not contact_id:
        return jsonify({"error": "contact_id is required"}), 400
    try:
        from services.job_intelligence_service import generate_intro_message
        out = generate_intro_message(user_email, contact_id)
        if not out.get("ok"):
            return jsonify(out), 404
        return jsonify(out), 200
    except Exception as e:
        logger.exception("network generate intro: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


@resume_bp.route("/career-copilot/network/insights", methods=["GET"])
@jwt_required()
def career_copilot_network_insights():
    user_email = get_jwt_identity()
    try:
        from services.job_intelligence_service import get_networking_insights
        return jsonify(get_networking_insights(user_email)), 200
    except Exception as e:
        logger.exception("network insights: %s", e)
        return jsonify({"error": "Failed to load networking insights"}), 500


# ------------------------------------------------------------------
# Career Copilot — Offer workspace
# ------------------------------------------------------------------


@resume_bp.route("/career-copilot/offers", methods=["GET"])
@jwt_required()
def career_copilot_offers_get():
    user_email = get_jwt_identity()
    try:
        from services.job_intelligence_service import list_offers
        return jsonify(list_offers(user_email)), 200
    except Exception as e:
        logger.exception("offers get: %s", e)
        return jsonify({"error": "Failed to load offers"}), 500


@resume_bp.route("/career-copilot/offers", methods=["POST"])
@jwt_required()
def career_copilot_offers_add():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    payload = {
        "company": InputSanitizer.sanitize_string(data.get("company") or "", max_length=120),
        "role": InputSanitizer.sanitize_string(data.get("role") or "", max_length=120),
        "base": data.get("base") or 0,
        "equity": data.get("equity") or 0,
        "bonus": data.get("bonus") or 0,
        "benefits": InputSanitizer.sanitize_string(data.get("benefits") or "", max_length=1500),
        "location": InputSanitizer.sanitize_string(data.get("location") or "", max_length=120),
        "remote": bool(data.get("remote")),
        "start_date": InputSanitizer.sanitize_string(data.get("start_date") or "", max_length=40),
        "deadline": InputSanitizer.sanitize_string(data.get("deadline") or "", max_length=40),
        "notes": InputSanitizer.sanitize_string(data.get("notes") or "", max_length=1500),
        "status": InputSanitizer.sanitize_string(data.get("status") or "active", max_length=24),
    }
    if not payload["company"] or not payload["role"]:
        return jsonify({"error": "company and role are required"}), 400
    try:
        from services.job_intelligence_service import add_offer
        return jsonify(add_offer(user_email, payload)), 200
    except Exception as e:
        logger.exception("offers add: %s", e)
        return jsonify({"error": "Failed to add offer"}), 500


@resume_bp.route("/career-copilot/offers/<offer_id>", methods=["PUT"])
@jwt_required()
def career_copilot_offers_update(offer_id):
    user_email = get_jwt_identity()
    offer_id = InputSanitizer.sanitize_string(offer_id or "", max_length=64)
    if not offer_id:
        return jsonify({"error": "offer_id is required"}), 400
    data = request.get_json(force=True) or {}
    patch = {
        "company": InputSanitizer.sanitize_string(data.get("company") or "", max_length=120),
        "role": InputSanitizer.sanitize_string(data.get("role") or "", max_length=120),
        "base": data.get("base"),
        "equity": data.get("equity"),
        "bonus": data.get("bonus"),
        "benefits": InputSanitizer.sanitize_string(data.get("benefits") or "", max_length=1500),
        "location": InputSanitizer.sanitize_string(data.get("location") or "", max_length=120),
        "remote": data.get("remote"),
        "start_date": InputSanitizer.sanitize_string(data.get("start_date") or "", max_length=40),
        "deadline": InputSanitizer.sanitize_string(data.get("deadline") or "", max_length=40),
        "notes": InputSanitizer.sanitize_string(data.get("notes") or "", max_length=1500),
        "status": InputSanitizer.sanitize_string(data.get("status") or "", max_length=24),
    }
    patch = {k: v for k, v in patch.items() if v is not None and v != ""}
    try:
        from services.job_intelligence_service import update_offer
        out = update_offer(user_email, offer_id, patch)
        if not out.get("ok"):
            return jsonify(out), 404
        return jsonify(out), 200
    except Exception as e:
        logger.exception("offers update: %s", e)
        return jsonify({"error": "Failed to update offer"}), 500


@resume_bp.route("/career-copilot/offers/<offer_id>", methods=["DELETE"])
@jwt_required()
def career_copilot_offers_delete(offer_id):
    user_email = get_jwt_identity()
    offer_id = InputSanitizer.sanitize_string(offer_id or "", max_length=64)
    if not offer_id:
        return jsonify({"error": "offer_id is required"}), 400
    try:
        from services.job_intelligence_service import delete_offer
        out = delete_offer(user_email, offer_id)
        return jsonify(out), 200 if out.get("ok") else 404
    except Exception as e:
        logger.exception("offers delete: %s", e)
        return jsonify({"error": "Failed to delete offer"}), 500


@resume_bp.route("/career-copilot/offers/compare", methods=["GET"])
@jwt_required()
def career_copilot_offers_compare():
    user_email = get_jwt_identity()
    try:
        from services.job_intelligence_service import compare_offers
        return jsonify(compare_offers(user_email)), 200
    except Exception as e:
        logger.exception("offers compare: %s", e)
        return jsonify({"error": "Failed to compare offers"}), 500


@resume_bp.route("/career-copilot/offers/negotiate", methods=["POST"])
@jwt_required()
def career_copilot_offers_negotiate():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    offer_id = InputSanitizer.sanitize_string(data.get("offer_id") or "", max_length=64)
    ask = InputSanitizer.sanitize_string(data.get("ask") or "", max_length=500)
    if not offer_id or not ask:
        return jsonify({"error": "offer_id and ask are required"}), 400
    try:
        from services.job_intelligence_service import generate_negotiation_script
        out = generate_negotiation_script(user_email, offer_id, ask)
        if not out.get("ok"):
            return jsonify(out), 404
        return jsonify(out), 200
    except Exception as e:
        logger.exception("offers negotiate: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


# ------------------------------------------------------------------
# Career Copilot — Session Timeline
# ------------------------------------------------------------------


@resume_bp.route("/career-copilot/timeline", methods=["GET"])
@jwt_required()
def career_copilot_timeline():
    user_email = get_jwt_identity()
    try:
        limit = min(int(request.args.get("limit", 20)), 100)
    except (ValueError, TypeError):
        limit = 20

    try:
        from services.career_copilot_service import get_session_timeline
        events = get_session_timeline(user_email, limit)
        return jsonify({"events": events}), 200
    except Exception as e:
        logger.exception("timeline: %s", e)
        return jsonify({"error": "Failed to load timeline"}), 500


# ------------------------------------------------------------------
# Career Copilot — Playground Quiz / Assessment
# ------------------------------------------------------------------


@resume_bp.route("/career-copilot/playground/quiz", methods=["POST"])
@jwt_required()
def career_copilot_playground_quiz():
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"cp_quiz:{client_ip}", max_requests=20, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    topic = InputSanitizer.sanitize_string(data.get("topic") or "", max_length=300)
    difficulty = InputSanitizer.sanitize_string(data.get("difficulty") or "", max_length=10)
    count = data.get("count", 3)

    if not topic:
        return jsonify({"error": "topic is required"}), 400
    if difficulty not in ("easy", "medium", "hard"):
        return jsonify({"error": "difficulty must be easy, medium, or hard"}), 400
    if not isinstance(count, int) or count < 3 or count > 5:
        return jsonify({"error": "count must be an integer between 3 and 5"}), 400

    try:
        from services.career_copilot_service import generate_quiz
        result = generate_quiz(user_email, topic, difficulty, count)
        return jsonify(result), 200
    except Exception as e:
        logger.exception("playground quiz: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


@resume_bp.route("/career-copilot/playground/evaluate", methods=["POST"])
@jwt_required()
def career_copilot_playground_evaluate():
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"cp_evaluate:{client_ip}", max_requests=30, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True) or {}
    question = InputSanitizer.sanitize_string(data.get("question") or "", max_length=2000)
    user_answer = InputSanitizer.sanitize_string(data.get("user_answer") or "", max_length=5000)

    if not question or not user_answer:
        return jsonify({"error": "question and user_answer are required"}), 400

    try:
        from services.career_copilot_service import submit_playground_answer
        result = submit_playground_answer(user_email, question, user_answer)
        return jsonify(result), 200
    except Exception as e:
        logger.exception("playground evaluate: %s", e)
        return jsonify({"error": str(e)[:200]}), 500


# ==================================================================
# Gmail integration — auto-update application status from inbox
# ==================================================================
# Flow:
#   1. GET  /gmail/status                  → is the user linked? gmail address, last sync.
#   2. GET  /gmail/auth-url                → returns a one-time Google consent URL.
#   3. POST /gmail/callback {code,state}   → exchanges code, stores encrypted refresh token.
#   4. POST /gmail/sync                    → pulls new mail, classifies, updates statuses.
#   5. GET  /gmail/suggestions             → list pending suggestions for review.
#   6. POST /gmail/suggestions/<id>/apply  → confirm and apply a suggested status change.
#   7. POST /gmail/suggestions/<id>/dismiss → discard a suggestion.
#   8. POST /gmail/disconnect              → revoke + remove the link.


@resume_bp.route("/gmail/status", methods=["GET"])
@jwt_required()
def gmail_status():
    user_email = get_jwt_identity()
    try:
        from services import gmail_service
    except Exception as e:
        logger.exception("gmail import failed: %s", e)
        return jsonify({"error": "Gmail integration unavailable"}), 500

    # `configured` reflects BOTH creds AND the GMAIL_OAUTH_ENABLED kill-switch,
    # so the FE hides the connect UI when the readonly scope is disabled.
    configured = gmail_service.gmail_configured() and gmail_service.gmail_oauth_enabled()
    conn = gmail_service.get_connection(user_email) if configured else None
    pending = 0
    if conn:
        pending = gmail_service._suggestions().count_documents({
            "user_email": user_email, "applied": False, "dismissed": False,
        })
    return jsonify({
        "configured": configured,
        "connected": bool(conn),
        "gmail_address": (conn or {}).get("gmail_address"),
        "last_synced_at": (conn or {}).get("last_synced_at").isoformat() if conn and conn.get("last_synced_at") else None,
        "pending_suggestions": pending,
    }), 200


@resume_bp.route("/gmail/auth-url", methods=["GET"])
@jwt_required()
def gmail_auth_url():
    user_email = get_jwt_identity()
    from services import gmail_service
    # Kill-switch: block the gmail.readonly consent flow independent of creds.
    if not gmail_service.gmail_oauth_enabled():
        return jsonify({"error": "Gmail integration is disabled"}), 503
    if not gmail_service.gmail_configured():
        return jsonify({"error": "Gmail OAuth is not configured on the server"}), 503
    try:
        url = gmail_service.build_auth_url(user_email)
        return jsonify({"auth_url": url}), 200
    except Exception as e:
        logger.exception("gmail auth-url error: %s", e)
        return jsonify({"error": "Failed to build Gmail auth URL"}), 500


@resume_bp.route("/gmail/callback", methods=["POST"])
@jwt_required()
def gmail_callback():
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}
    code = (data.get("code") or "").strip()
    state = (data.get("state") or "").strip()
    if not code or not state:
        return jsonify({"error": "code and state are required"}), 400

    from services import gmail_service
    # Kill-switch: refuse to complete a readonly-scope grant when disabled.
    if not gmail_service.gmail_oauth_enabled():
        return jsonify({"error": "Gmail integration is disabled"}), 503
    issued_to = gmail_service.consume_state(state)
    if not issued_to or issued_to != user_email:
        return jsonify({"error": "Invalid or expired OAuth state"}), 400
    try:
        tokens = gmail_service.exchange_code_for_tokens(code)
        result = gmail_service.store_connection(user_email, tokens)
        return jsonify({"connected": True, **result}), 200
    except Exception as e:
        logger.exception("gmail callback error: %s", e)
        return jsonify({"error": str(e)[:240] or "Failed to link Gmail"}), 500


@resume_bp.route("/gmail/sync", methods=["POST"])
@jwt_required()
def gmail_sync():
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"gmail_sync:{user_email}:{client_ip}", max_requests=6, window_seconds=300):
        return jsonify({"error": "Sync rate limit hit. Try again in a few minutes."}), 429

    # Kill-switch: no readonly-scope reads when the flag is off.
    from services import gmail_service
    if not gmail_service.gmail_oauth_enabled():
        return jsonify({"error": "Gmail integration is disabled"}), 503
    # Quick guard so we don't bother dispatching when the user isn't linked.
    if not gmail_service.get_connection(user_email):
        return jsonify({"error": "Gmail is not connected"}), 400

    # Run the sync as an async job — Gmail listing + per-match classifier
    # calls easily exceed API Gateway's 29s ceiling. The job_id flows back
    # to the UI which polls /resume/job/<id> until complete.
    try:
        from services.resume_service import get_resume_service, ResumeService
        svc = get_resume_service()
        payload = {"user_email": user_email}
        job_id = svc.create_job("gmail_sync", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "gmail_sync", payload)
        return jsonify({"ok": True, "job_id": job_id, "status": "started"}), 202
    except Exception as e:
        logger.exception("gmail sync dispatch error: %s", e)
        return jsonify({"error": "Failed to start Gmail sync"}), 500


@resume_bp.route("/gmail/suggestions", methods=["GET"])
@jwt_required()
def gmail_list_suggestions():
    user_email = get_jwt_identity()
    include_resolved = request.args.get("include_resolved", "false").lower() == "true"
    from services import gmail_service
    try:
        items = gmail_service.list_suggestions(user_email, include_resolved=include_resolved)
        return jsonify({"suggestions": items}), 200
    except Exception as e:
        logger.exception("gmail suggestions error: %s", e)
        return jsonify({"error": "Failed to load suggestions"}), 500


@resume_bp.route("/gmail/suggestions/<suggestion_id>/apply", methods=["POST"])
@jwt_required()
def gmail_apply_suggestion(suggestion_id):
    user_email = get_jwt_identity()
    from services import gmail_service
    try:
        s = gmail_service.apply_suggestion(user_email, suggestion_id)
        return jsonify({"suggestion": s}), 200
    except LookupError:
        return jsonify({"error": "Suggestion not found"}), 404
    except Exception as e:
        logger.exception("gmail apply error: %s", e)
        return jsonify({"error": "Failed to apply suggestion"}), 500


@resume_bp.route("/gmail/suggestions/<suggestion_id>/dismiss", methods=["POST"])
@jwt_required()
def gmail_dismiss_suggestion(suggestion_id):
    user_email = get_jwt_identity()
    from services import gmail_service
    try:
        ok = gmail_service.dismiss_suggestion(user_email, suggestion_id)
        if not ok:
            return jsonify({"error": "Suggestion not found"}), 404
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception("gmail dismiss error: %s", e)
        return jsonify({"error": "Failed to dismiss suggestion"}), 500


@resume_bp.route("/gmail/disconnect", methods=["POST"])
@jwt_required()
def gmail_disconnect():
    user_email = get_jwt_identity()
    from services import gmail_service
    try:
        gmail_service.disconnect(user_email)
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception("gmail disconnect error: %s", e)
        return jsonify({"error": "Failed to disconnect"}), 500


# ------------------------------------------------------------------
# Visa Timeline Copilot — F-1 / OPT / STEM / H-1B clock dashboard.
# Persists a small per-user profile and computes a milestone timeline.
# ------------------------------------------------------------------
@resume_bp.route("/visa/profile", methods=["GET"])
@jwt_required()
def get_visa_profile():
    user_email = get_jwt_identity()
    try:
        from services.visa_timeline_service import normalize_profile
        from utils.db_connect import DBConnect
        db = DBConnect().get_db()
        doc = db.visa_profiles.find_one({"user_email": user_email}) or {}
        profile = normalize_profile(doc)
        return jsonify({"profile": profile}), 200
    except Exception as e:
        logger.exception("visa profile GET error: %s", e)
        return jsonify({"error": "Failed to load profile"}), 500


@resume_bp.route("/visa/profile", methods=["PUT"])
@jwt_required()
def update_visa_profile():
    user_email = get_jwt_identity()
    try:
        from services.visa_timeline_service import normalize_profile
        from utils.db_connect import DBConnect
        body = request.get_json(silent=True) or {}
        profile = normalize_profile(body)
        db = DBConnect().get_db()
        db.visa_profiles.update_one(
            {"user_email": user_email},
            {"$set": {**profile, "user_email": user_email, "updated_at": datetime.utcnow()}},
            upsert=True,
        )
        return jsonify({"ok": True, "profile": profile}), 200
    except Exception as e:
        logger.exception("visa profile PUT error: %s", e)
        return jsonify({"error": "Failed to save profile"}), 500


@resume_bp.route("/visa/timeline", methods=["GET"])
@jwt_required()
def get_visa_timeline():
    user_email = get_jwt_identity()
    try:
        from services.visa_timeline_service import normalize_profile, compute_timeline, serialize_timeline
        from utils.db_connect import DBConnect
        db = DBConnect().get_db()
        doc = db.visa_profiles.find_one({"user_email": user_email}) or {}
        profile = normalize_profile(doc)
        milestones = compute_timeline(profile)
        serialized = serialize_timeline(milestones)

        # LLM-generated personalized recommendation (FLASH tier, soft-fail).
        # Deterministic milestones already cover "what" — this adds the
        # "what next" judgment that's hard to express in pure logic.
        recommendation = ""
        try:
            critical = [m for m in serialized if m.get("severity") == "critical"]
            warnings = [m for m in serialized if m.get("severity") == "warning"]
            if critical or warnings:
                from services.claude_client import claude_json, CLAUDE_FLASH
                prompt = (
                    "You are an experienced international-student advisor. In "
                    "2-3 short sentences, give this F-1 candidate a specific, "
                    "calm, actionable recommendation based on their visa state. "
                    "Reference dates and milestones by name when you mention "
                    "them. No fluff, no emojis.\n\n"
                    f"Visa status: {profile.get('visa_status')}\n"
                    f"STEM degree: {profile.get('stem_degree')}\n"
                    f"E-Verify employer: {profile.get('current_employer_e_verified')}\n"
                    f"Upcoming critical cliffs: {critical}\n"
                    f"Upcoming warnings: {warnings}\n\n"
                    'Return JSON: { "recommendation": "..." }.'
                )
                out = claude_json(
                    prompt=prompt, model=CLAUDE_FLASH, temperature=0.3,
                    schema={"recommendation": str}, max_tokens=200,
                )
                if isinstance(out, dict) and isinstance(out.get("recommendation"), str):
                    recommendation = out["recommendation"].strip()[:500]
        except Exception as e:
            logger.info("visa timeline recommendation soft-fail: %s", e)

        return jsonify({
            "profile": profile,
            "milestones": serialized,
            "recommendation": recommendation,
        }), 200
    except Exception as e:
        logger.exception("visa timeline error: %s", e)
        return jsonify({"error": "Failed to compute timeline"}), 500


@resume_bp.route("/opt-clock", methods=["GET"])
@jwt_required()
def get_opt_clock():
    """OPT Clock — a calm, precise projection of the F-1 student's unemployment
    runway on OPT (90 days pre-STEM / 150 on the STEM extension).

    Reuses the visa_timeline engine for all math; this route only loads the
    saved profile and serializes. `unemployment_days_used` (and therefore
    `days_remaining`, `status_band`) may be null when the student hasn't told
    us their count yet — the UI asks rather than guessing. We never invent
    numbers or dates.
    """
    user_email = get_jwt_identity()
    try:
        from services.visa_timeline_service import normalize_profile, compute_opt_clock
        db = DBConnect().get_db()
        doc = db.visa_profiles.find_one({"user_email": user_email}) or {}
        profile = normalize_profile(doc)
        return jsonify(compute_opt_clock(profile)), 200
    except Exception as e:
        logger.exception("opt-clock error: %s", e)
        return jsonify({"error": "Failed to compute OPT clock"}), 500



# ------------------------------------------------------------------
# Beta Lab features — Morning brief, JD reverse-search, A/B telemetry.
# Each endpoint is intentionally lightweight; surfaces in the Beta tab.
# ------------------------------------------------------------------
@resume_bp.route("/beta/morning-brief", methods=["GET"])
@jwt_required()
def beta_morning_brief():
    user_email = get_jwt_identity()
    try:
        from services.beta_features_service import build_morning_brief
        return jsonify(build_morning_brief(user_email)), 200
    except Exception as e:
        logger.exception("morning-brief error: %s", e)
        return jsonify({"ok": False, "error": "Failed to build brief"}), 500


@resume_bp.route("/beta/similar-roles", methods=["POST"])
@jwt_required()
def beta_similar_roles():
    user_email = get_jwt_identity()
    try:
        body = request.get_json(silent=True) or {}
        jd_text = InputSanitizer.sanitize_string(str(body.get("jd_text") or ""), max_length=15000)
        try:
            limit = int(body.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 25))
        from services.beta_features_service import find_similar_roles
        return jsonify(find_similar_roles(user_email, jd_text, limit=limit)), 200
    except Exception as e:
        logger.exception("similar-roles error: %s", e)
        return jsonify({"ok": False, "error": "Failed to find similar roles"}), 500


@resume_bp.route("/beta/ab-telemetry", methods=["GET"])
@jwt_required()
def beta_ab_telemetry():
    user_email = get_jwt_identity()
    try:
        from services.beta_features_service import ab_telemetry
        return jsonify(ab_telemetry(user_email)), 200
    except Exception as e:
        logger.exception("ab-telemetry error: %s", e)
        return jsonify({"ok": False, "error": "Failed to load telemetry"}), 500


# ==================================================================
# Pillar B — Learning Hub (/api/resume/learning/*)
#   AI study plans for any topic + the skill→job loop. Plan generation is a
#   PRO async job (poll GET /api/resume/job/<id>); the rest are sync.
# ==================================================================
def _user_skill_context(user_email):
    """Cheap skills + target-role context for plan generation (no resume parse):
    onboarding preferences + already-earned learning skills."""
    db = DBConnect().get_db()
    prefs = db.user_preferences.find_one({"user_email": user_email}) or {}
    user = db.users.find_one({"email": user_email}, {"earned_skills": 1}) or {}
    skills = list(prefs.get("current_skills") or [])
    skills += [e.get("skill") for e in (user.get("earned_skills") or []) if e.get("skill")]
    roles = prefs.get("target_roles") or []
    target_role = roles[0] if roles else None
    # de-dupe, keep order
    seen, uniq = set(), []
    for s in skills:
        if s and s.lower() not in seen:
            seen.add(s.lower()); uniq.append(s)
    return uniq, target_role


@resume_bp.route("/learning/plan", methods=["POST"])
@jwt_required()
def learning_plan_create():
    user_email = get_jwt_identity()
    from services import entitlements_service, learning_service
    from services.resume_service import get_resume_service, ResumeService

    body = request.get_json(silent=True) or {}

    # §6.6 differentiation-driven plan (practical, verified, adaptive depth). If a
    # chosen differentiator is supplied, build from it instead of a free-text topic.
    diff = body.get("differentiator")
    if diff and isinstance(diff, dict) and diff.get("gap"):
        from services import differentiation_service
        if getattr(differentiation_service, "LEARNING_DIFF_ENABLED", True):
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
            svc = get_resume_service()
            job_id = svc.create_job("learning_plan", payload, user_email=user_email)
            ResumeService.invoke_async(job_id, "learning_plan", payload)
            return jsonify({"job_id": job_id}), 202

    topic = InputSanitizer.sanitize_string(str(body.get("topic") or ""), max_length=200)
    if not topic:
        return jsonify({"error": "A topic is required"}), 400

    plan_tier = entitlements_service.get_plan(user_email)
    # Active-path cap (structural, not time-windowed).
    cap = entitlements_service.quota(plan_tier, "active_learning_paths")
    if cap is not None and learning_service.count_active_paths(user_email) >= cap:
        return entitlements_service.upgrade_required_response(user_email, "learning")
    # Metered generation count.
    allowed, usage = entitlements_service.check_and_increment(user_email, "learning_plan_generate")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "learning", usage)

    skills, pref_role = _user_skill_context(user_email)
    target_role = InputSanitizer.sanitize_string(str(body.get("target_role") or ""), 120) or pref_role
    try:
        weeks = max(3, min(int(body.get("weeks") or 6), 12))
    except (TypeError, ValueError):
        weeks = 6
    payload = {
        "user_email": user_email,
        "topic": topic,
        "target_role": target_role,
        "current_skills": skills,
        "weeks": weeks,
        "include_capstone": plan_tier == entitlements_service.PREMIUM_PLUS,
    }
    svc = get_resume_service()
    job_id = svc.create_job("learning_plan", payload, user_email=user_email)
    ResumeService.invoke_async(job_id, "learning_plan", payload)
    return jsonify({"job_id": job_id}), 202


# ------------------------------------------------------------------
# §6.6 — differentiation analysis + verified proof-of-work
# ------------------------------------------------------------------
@resume_bp.route("/learning/differentiators", methods=["POST"])
@jwt_required()
def learning_differentiators():
    user_email = get_jwt_identity()
    from services import entitlements_service, differentiation_service
    from services.resume_service import get_resume_service, ResumeService
    if not getattr(differentiation_service, "LEARNING_DIFF_ENABLED", True):
        return jsonify({"error": "not_available"}), 404
    allowed, usage = entitlements_service.check_and_increment(user_email, "differentiation_analysis")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "differentiation_analysis", usage)
    job_id = get_resume_service().create_job("learning_differentiators", {"user_email": user_email}, user_email=user_email)
    ResumeService.invoke_async(job_id, "learning_differentiators", {"user_email": user_email})
    return jsonify({"job_id": job_id}), 202


@resume_bp.route("/learning/paths/<path_id>/task/<task_ref>/verify", methods=["POST"])
@jwt_required()
def learning_task_verify(path_id, task_ref):
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


# --- completion loop: GitHub sync -> verified -> LinkedIn post (auto/manual) ---
@resume_bp.route("/learning/paths/<path_id>/task/<task_ref>/github", methods=["POST"])
@jwt_required()
def learning_task_github(path_id, task_ref):
    user_email = get_jwt_identity()
    task_ref = InputSanitizer.sanitize_string(task_ref, max_length=64)
    from services.learning import github_sync
    try:
        return jsonify(github_sync.ensure_task_repo(user_email, path_id, task_ref)), 200
    except Exception as e:
        logger.error("learning github sync error: %s", e)
        return jsonify({"error": "Failed to sync GitHub repo"}), 500


@resume_bp.route("/learning/paths/<path_id>/task/<task_ref>/post", methods=["POST"])
@jwt_required()
def learning_task_post(path_id, task_ref):
    user_email = get_jwt_identity()
    task_ref = InputSanitizer.sanitize_string(task_ref, max_length=64)
    from services import entitlements_service
    from services.learning import publish
    from services.resume_service import get_resume_service, ResumeService
    # Never draft a post for unverified work (checked before any LLM spend).
    if not publish.is_task_verified(user_email, path_id, task_ref):
        return jsonify({"error": "not_verified", "message": "Verify this task before sharing it."}), 400
    allowed, usage = entitlements_service.check_and_increment(user_email, "completion_post")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "completion_post", usage)
    payload = {"user_email": user_email, "path_id": path_id, "task_ref": task_ref}
    job_id = get_resume_service().create_job("learning_completion_post", payload, user_email=user_email)
    ResumeService.invoke_async(job_id, "learning_completion_post", payload)
    return jsonify({"job_id": job_id}), 202


@resume_bp.route("/learning/posts/<draft_id>/publish", methods=["POST"])
@jwt_required()
def learning_post_publish(draft_id):
    user_email = get_jwt_identity()
    draft_id = InputSanitizer.sanitize_string(draft_id, max_length=64)
    body = request.get_json(silent=True) or {}
    mode = (body.get("mode") or "manual").strip().lower()
    try:
        variant_index = int(body.get("variant_index") or 0)
    except (TypeError, ValueError):
        variant_index = 0
    from services import entitlements_service
    from services.learning import publish
    if mode == "auto" and not entitlements_service.feature_allowed(user_email, "auto_post"):
        return entitlements_service.upgrade_required_response(user_email, "auto_post")
    try:
        return jsonify(publish.publish_completion_post(user_email, draft_id, variant_index, mode)), 200
    except Exception as e:
        logger.error("learning post publish error: %s", e)
        return jsonify({"error": "Failed to publish post"}), 500


@resume_bp.route("/learning/posts/<schedule_id>/cancel", methods=["POST"])
@jwt_required()
def learning_post_cancel(schedule_id):
    user_email = get_jwt_identity()
    schedule_id = InputSanitizer.sanitize_string(schedule_id, max_length=64)
    from services.learning import publish
    return jsonify(publish.cancel_scheduled_post(user_email, schedule_id)), 200


@resume_bp.route("/learning/paths", methods=["GET"])
@jwt_required()
def learning_paths_list():
    from services import learning_service
    return jsonify({"paths": learning_service.list_paths(get_jwt_identity())}), 200


@resume_bp.route("/learning/trends", methods=["GET"])
@jwt_required()
def learning_trends():
    """Market-demand-ranked learning topics for this user (deterministic, no LLM
    spend). Rides the existing /api/resume/* edge — no Terraform change."""
    from services import trend_signal_service
    user_email = get_jwt_identity()
    try:
        window = int(request.args.get("window", 90))
    except (TypeError, ValueError):
        window = 90
    try:
        return jsonify(trend_signal_service.trending_for_user(user_email, window=window)), 200
    except Exception as e:
        logger.exception("learning trends error: %s", e)
        return jsonify({"error": "Could not load learning trends"}), 500


@resume_bp.route("/learning/paths/<path_id>", methods=["GET"])
@jwt_required()
def learning_path_get(path_id):
    from services import learning_service
    data = learning_service.get_path(get_jwt_identity(), path_id)
    if not data:
        return jsonify({"error": "Path not found"}), 404
    return jsonify(data), 200


@resume_bp.route("/learning/paths/<path_id>", methods=["DELETE"])
@jwt_required()
def learning_path_delete(path_id):
    from services import learning_service
    ok = learning_service.delete_path(get_jwt_identity(), path_id)
    return jsonify({"ok": ok}), (200 if ok else 404)


@resume_bp.route("/learning/paths/<path_id>/module/<int:week>/complete", methods=["POST"])
@jwt_required()
def learning_module_complete(path_id, week):
    from services import learning_service
    try:
        result = learning_service.complete_module(get_jwt_identity(), path_id, week)
        return jsonify(result), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 404
    except Exception as e:
        logger.exception("module complete error: %s", e)
        return jsonify({"error": "Could not record progress"}), 500


@resume_bp.route("/learning/paths/<path_id>/quiz", methods=["POST"])
@jwt_required()
def learning_quiz(path_id):
    user_email = get_jwt_identity()
    from services import entitlements_service, learning_service
    allowed, usage = entitlements_service.check_and_increment(user_email, "quiz")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "quiz", usage)
    body = request.get_json(silent=True) or {}
    try:
        week = int(body.get("week"))
    except (TypeError, ValueError):
        return jsonify({"error": "week is required"}), 400
    try:
        return jsonify(learning_service.generate_quiz(user_email, path_id, week)), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 404
    except Exception as e:
        logger.exception("quiz error: %s", e)
        return jsonify({"error": "Could not generate quiz"}), 500


# ==================================================================
# Pillar D — Interview Pattern Coach + ATS/salary trifecta
#   (/api/resume/interview/coach|attempts|fit, /api/resume/salary)
# ==================================================================
@resume_bp.route("/interview/coach", methods=["GET"])
@jwt_required()
def interview_coach():
    user_email = get_jwt_identity()
    from services import entitlements_service, interview_coach_service
    full = entitlements_service.feature_allowed(user_email, "interview_analytics_trends")
    predictive = entitlements_service.feature_allowed(user_email, "interview_analytics_predictive")
    try:
        return jsonify(interview_coach_service.coach_summary(user_email, full=full, predictive=predictive)), 200
    except Exception as e:
        logger.exception("coach error: %s", e)
        return jsonify({"error": "Could not build coaching summary"}), 500


@resume_bp.route("/interview/attempts", methods=["GET"])
@jwt_required()
def interview_attempts():
    user_email = get_jwt_identity()
    from services import interview_coach_service
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"attempts": interview_coach_service.get_attempts(user_email, limit=limit)}), 200


@resume_bp.route("/interview/fit", methods=["POST"])
@jwt_required()
def interview_fit():
    user_email = get_jwt_identity()
    from services import entitlements_service, interview_coach_service
    if not entitlements_service.feature_allowed(user_email, "interview_fit"):
        return entitlements_service.upgrade_required_response(user_email, "interview_fit")
    body = request.get_json(silent=True) or {}
    record_id = InputSanitizer.sanitize_string(str(body.get("record_id") or ""), 80)
    if not record_id:
        return jsonify({"error": "record_id is required"}), 400
    try:
        return jsonify(interview_coach_service.interview_fit(user_email, record_id)), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 404
    except Exception as e:
        logger.exception("fit error: %s", e)
        return jsonify({"error": "Could not assess interview fit"}), 500


@resume_bp.route("/salary", methods=["POST"])
@jwt_required()
def salary_estimate():
    user_email = get_jwt_identity()
    from services import entitlements_service, interview_coach_service
    allowed, usage = entitlements_service.check_and_increment(user_email, "salary")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "salary", usage)
    body = request.get_json(silent=True) or {}
    role = InputSanitizer.sanitize_string(str(body.get("role") or ""), 160)
    if not role:
        return jsonify({"error": "role is required"}), 400
    level = InputSanitizer.sanitize_string(str(body.get("level") or ""), 60)
    location = InputSanitizer.sanitize_string(str(body.get("location") or ""), 120)
    with_pct = entitlements_service.feature_allowed(user_email, "salary_percentiles")
    try:
        return jsonify(interview_coach_service.salary_benchmark(role, level, location, with_percentiles=with_pct)), 200
    except Exception as e:
        logger.exception("salary error: %s", e)
        return jsonify({"error": "Could not estimate salary"}), 500


# ==================================================================
# Pillar C — Hackathons/Webinars + AI post drafting
#   (/api/resume/events/*, /api/resume/posts)
# ==================================================================
@resume_bp.route("/events", methods=["GET"])
@jwt_required()
def events_list():
    user_email = get_jwt_identity()
    from services import event_scraper
    type_filter = (request.args.get("type") or "").strip().lower() or None
    if type_filter not in (None, "hackathon", "webinar"):
        type_filter = None
    try:
        events = event_scraper.list_events(user_email, type_filter=type_filter)
        return jsonify({"events": events}), 200
    except Exception as e:
        logger.exception("events list error: %s", e)
        return jsonify({"error": "Could not load events"}), 500


@resume_bp.route("/events/refresh", methods=["POST"])
@jwt_required()
def events_refresh():
    """Trigger a best-effort scrape of public event sources (idempotent upsert)."""
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"events_refresh:{client_ip}", max_requests=4, window_seconds=3600):
        return jsonify({"error": "Refreshed recently — try again later."}), 429
    import os
    from services.resume_service import get_resume_service, ResumeService
    # Only run the LLM 30% of legitimacy scoring when Bedrock is actually
    # reachable (Lambda via SSM, or explicit AWS creds) — otherwise score
    # heuristically so the scrape stays fast instead of retrying a dead model.
    use_llm = os.getenv("USE_SSM_SECRETS", "false").lower() == "true" or bool(os.getenv("AWS_ACCESS_KEY_ID"))
    payload = {"sources": None, "limit": 40, "use_llm": use_llm}
    svc = get_resume_service()
    job_id = svc.create_job("event_scrape", payload, user_email=user_email)
    ResumeService.invoke_async(job_id, "event_scrape", payload)
    return jsonify({"job_id": job_id}), 202


@resume_bp.route("/events/<event_id>", methods=["GET"])
@jwt_required()
def event_detail(event_id):
    from services import event_scraper
    ev = event_scraper.get_event(get_jwt_identity(), event_id)
    if not ev:
        return jsonify({"error": "Event not found"}), 404
    return jsonify(ev), 200


@resume_bp.route("/events/<event_id>/relation", methods=["POST"])
@jwt_required()
def event_relation(event_id):
    user_email = get_jwt_identity()
    from services import event_scraper
    body = request.get_json(silent=True) or {}
    relation = (body.get("relation") or "").strip().lower()
    outcome = InputSanitizer.sanitize_string(str(body.get("outcome") or ""), 500)
    notes = InputSanitizer.sanitize_string(str(body.get("notes") or ""), 1000)
    try:
        return jsonify(event_scraper.set_relation(user_email, event_id, relation, outcome, notes)), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400


@resume_bp.route("/events/draft-post", methods=["POST"])
@jwt_required()
def events_draft_post():
    user_email = get_jwt_identity()
    from services import entitlements_service
    from services.resume_service import get_resume_service, ResumeService
    allowed, usage = entitlements_service.check_and_increment(user_email, "post_draft")
    if not allowed:
        return entitlements_service.upgrade_required_response(user_email, "post_draft", usage)
    body = request.get_json(silent=True) or {}
    payload = {
        "user_email": user_email,
        "event_id": InputSanitizer.sanitize_string(str(body.get("event_id") or ""), 80) or None,
        "achievement": InputSanitizer.sanitize_string(str(body.get("achievement") or ""), 2000),
        "audience": (body.get("audience") or "general"),
        "relation": (body.get("relation") or "attended"),
    }
    if not payload["event_id"] and not payload["achievement"]:
        return jsonify({"error": "Provide an event_id or an achievement to write about"}), 400
    svc = get_resume_service()
    job_id = svc.create_job("post_draft", payload, user_email=user_email)
    ResumeService.invoke_async(job_id, "post_draft", payload)
    return jsonify({"job_id": job_id}), 202


@resume_bp.route("/posts", methods=["GET"])
@jwt_required()
def posts_list():
    from services import post_drafting_service
    return jsonify({"posts": post_drafting_service.list_drafts(get_jwt_identity())}), 200


# ==================================================================
# Pillar A — Personalized "Recommended for you" feed (/api/resume/recommendations)
# ==================================================================
@resume_bp.route("/recommendations", methods=["GET"])
@jwt_required()
def recommendations():
    user_email = get_jwt_identity()
    from services import recommendations_service
    try:
        return jsonify({"recommendations": recommendations_service.get_recommendations(user_email)}), 200
    except Exception as e:
        logger.exception("recommendations error: %s", e)
        return jsonify({"recommendations": []}), 200


@resume_bp.route("/overview", methods=["GET"])
@jwt_required()
def overview():
    """Cross-feature dashboard snapshot — the interconnection layer."""
    user_email = get_jwt_identity()
    from services import overview_service
    try:
        return jsonify(overview_service.get_overview(user_email)), 200
    except Exception as e:
        logger.exception("overview error: %s", e)
        return jsonify({}), 200


@resume_bp.route("/career-readiness", methods=["GET"])
@jwt_required()
def career_readiness():
    """Career Readiness Score (the Compass spine) — one honest 0-100 score
    synthesizing the cross-surface overview + the onboarding goal, with a single
    next best step. Deterministic, no LLM. Degrades to {} so the dashboard never
    breaks on it."""
    user_email = get_jwt_identity()
    from services import career_readiness_service
    try:
        return jsonify(career_readiness_service.compute_readiness(user_email)), 200
    except Exception as e:
        logger.exception("career-readiness error: %s", e)
        return jsonify({}), 200


@resume_bp.route("/github/link", methods=["POST"])
@jwt_required()
def github_link():
    """Link & analyze a PUBLIC GitHub profile (proof-of-skill). Username/URL only —
    no OAuth, no config. Free, rate-limited (each call hits GitHub). Stores a cached
    snapshot that feeds the Career Readiness 'Proof of skill' dimension."""
    from utils.security import get_rate_limiter, get_client_ip
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"github_link:{get_client_ip(request)}", max_requests=10, window_seconds=120):
        return jsonify({"error": "Rate limit exceeded — try again shortly"}), 429
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    raw = InputSanitizer.sanitize_string(str(data.get("username") or ""), max_length=120)
    if not raw:
        return jsonify({"error": "A GitHub username or profile URL is required"}), 400
    from services import github_profile_service
    try:
        snap = github_profile_service.link_and_store(user_email, raw)
        return jsonify({"linked": True, "profile": snap}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except Exception as e:
        logger.exception("github link error: %s", e)
        return jsonify({"error": "Failed to analyze GitHub profile"}), 500


@resume_bp.route("/github", methods=["GET"])
@jwt_required()
def github_get():
    """Return the cached GitHub proof snapshot for the user (or linked=False)."""
    user_email = get_jwt_identity()
    from services import github_profile_service
    try:
        snap = github_profile_service.get_stored(user_email)
        return jsonify({"linked": bool(snap), "profile": snap}), 200
    except Exception as e:
        logger.exception("github get error: %s", e)
        return jsonify({"linked": False, "profile": None}), 200


@resume_bp.route("/linkedin/audit", methods=["POST"])
@jwt_required()
def linkedin_audit():
    """AI LinkedIn self-audit. LinkedIn's API exposes no profile data, so we use
    the user's REAL résumé + target role to produce honest, specific optimization
    guidance (+ a ready-to-paste headline/About). Free, rate-limited (one FLASH call)."""
    from utils.security import get_rate_limiter, get_client_ip
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"linkedin_audit:{get_client_ip(request)}", max_requests=10, window_seconds=120):
        return jsonify({"error": "Rate limit exceeded — try again shortly"}), 429
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    url = InputSanitizer.sanitize_string(str(data.get("linkedin_url") or ""), max_length=300)
    from services import linkedin_audit_service
    try:
        return jsonify(linkedin_audit_service.audit(user_email, url)), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("linkedin audit error: %s", e)
        return jsonify({"error": "Audit failed"}), 500


# ==================================================================
# Connect & act — OAuth data-integrations (GitHub repos, LinkedIn posts)
# ------------------------------------------------------------------
# Lets a logged-in user connect their GitHub/LinkedIn accounts so JobPilot can
# act on their behalf. These mirror gmail_service's encrypted-token data-provider
# pattern and are kept fully separate from the sign-in OAuth in oauth_service.py.
# Endpoints live under the already-edge-routed /api/resume prefix.
#
# Auth nuance: /authorize is @jwt_required and stashes user_email in a CSRF
# state doc; the /callback is PUBLIC (browser redirect, no JWT) and resolves the
# user via that state doc.
# ==================================================================

_CONNECT_PROVIDERS = {"github", "linkedin"}


def _connect_service(provider: str):
    if provider == "github":
        from services import github_connect_service
        return github_connect_service
    if provider == "linkedin":
        from services import linkedin_connect_service
        return linkedin_connect_service
    return None


@resume_bp.route("/connect/<provider>/authorize", methods=["GET"])
@jwt_required()
def connect_authorize(provider):
    """Begin the connect-OAuth flow. Returns {authorize_url} or 503 if the
    provider's credentials are not configured for this deployment."""
    if provider not in _CONNECT_PROVIDERS:
        return jsonify({"error": "unknown_provider"}), 404
    svc = _connect_service(provider)
    if not svc.is_configured():
        return jsonify({"error": "provider_not_configured", "provider": provider}), 503
    user_email = get_jwt_identity()
    try:
        return jsonify({"authorize_url": svc.build_authorize_url(user_email)}), 200
    except Exception as e:
        logger.exception("connect authorize failed for %s: %s", provider, e)
        return jsonify({"error": "authorize_failed"}), 500


@resume_bp.route("/connect/<provider>/callback", methods=["GET"])
def connect_callback(provider):
    """PUBLIC OAuth callback (browser redirect, no JWT). Resolves the user via
    the CSRF state doc, exchanges the code, stores the connection, then redirects
    back to the frontend app."""
    from flask import redirect
    from utils.config import AppUrlsConfig

    def _back(fragment: str):
        return redirect(f"{AppUrlsConfig.FRONTEND_URL}/app?{fragment}")

    if provider not in _CONNECT_PROVIDERS:
        return _back("connect_error=unknown_provider")
    if request.args.get("error"):
        return _back(f"connect_error={InputSanitizer.sanitize_string(str(request.args.get('error')), max_length=80)}")
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        return _back("connect_error=invalid_request")
    svc = _connect_service(provider)
    try:
        svc.handle_callback(code, state)
        return _back(f"connected={provider}")
    except RuntimeError as e:
        if str(e) == "invalid_state":
            return _back("connect_error=invalid_state")
        logger.warning("connect callback failed for %s: %s", provider, e)
        return _back("connect_error=connect_failed")
    except Exception as e:
        logger.exception("connect callback error for %s: %s", provider, e)
        return _back("connect_error=connect_failed")


@resume_bp.route("/connect/status", methods=["GET"])
@jwt_required()
def connect_status():
    """Connection status for both providers."""
    user_email = get_jwt_identity()
    out = {"github": {"connected": False}, "linkedin": {"connected": False}}
    for provider in _CONNECT_PROVIDERS:
        try:
            out[provider] = _connect_service(provider).get_status(user_email)
        except Exception as e:
            logger.warning("connect status failed for %s: %s", provider, e)
    return jsonify(out), 200


@resume_bp.route("/connect/<provider>/disconnect", methods=["POST"])
@jwt_required()
def connect_disconnect(provider):
    if provider not in _CONNECT_PROVIDERS:
        return jsonify({"error": "unknown_provider"}), 404
    user_email = get_jwt_identity()
    try:
        _connect_service(provider).disconnect(user_email)
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception("connect disconnect error for %s: %s", provider, e)
        return jsonify({"error": "disconnect_failed"}), 500


@resume_bp.route("/connect/github/publish", methods=["POST"])
@jwt_required()
def connect_github_publish():
    """Human-approved action: scaffold a small real project grounded in the
    user's skills and publish it to a new public GitHub repo. Returns {repo_url}."""
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"connect_gh_publish:{get_client_ip(request)}", max_requests=5, window_seconds=600):
        return jsonify({"error": "Rate limit exceeded — try again shortly"}), 429
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    name = InputSanitizer.sanitize_string(str(data.get("name") or ""), max_length=80)
    description = InputSanitizer.sanitize_string(str(data.get("description") or ""), max_length=300)
    kind = InputSanitizer.sanitize_string(str(data.get("kind") or ""), max_length=60)
    if not name:
        return jsonify({"error": "A repository name is required"}), 400

    from services import github_connect_service
    if not github_connect_service.is_configured():
        return jsonify({"error": "provider_not_configured", "provider": "github"}), 503
    if not github_connect_service.get_status(user_email).get("connected"):
        return jsonify({"error": "Connect your GitHub account first"}), 400

    try:
        files = _scaffold_project_files(user_email, name, description, kind)
        result = github_connect_service.publish_project(user_email, name, description, files)
        return jsonify(result), 200
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("github publish error: %s", e)
        return jsonify({"error": "Failed to publish project"}), 500


@resume_bp.route("/connect/linkedin/post", methods=["POST"])
@jwt_required()
def connect_linkedin_post():
    """Human-approved action: publish a post to the connected LinkedIn account."""
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"connect_li_post:{get_client_ip(request)}", max_requests=5, window_seconds=600):
        return jsonify({"error": "Rate limit exceeded — try again shortly"}), 429
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    text = InputSanitizer.sanitize_string(str(data.get("text") or ""), max_length=3000)
    if not text:
        return jsonify({"error": "Post text is required"}), 400

    from services import linkedin_connect_service
    if not linkedin_connect_service.is_configured():
        return jsonify({"error": "provider_not_configured", "provider": "linkedin"}), 503
    if not linkedin_connect_service.get_status(user_email).get("connected"):
        return jsonify({"error": "Connect your LinkedIn account first"}), 400

    try:
        result = linkedin_connect_service.create_post(user_email, text)
        return jsonify(result), 200
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("linkedin post error: %s", e)
        return jsonify({"error": "Failed to publish post"}), 500


def _scaffold_project_files(user_email: str, name: str, description: str, kind: str) -> dict:
    """Build a small, real, grounded starter project (README + a starter file)
    from the user's actual skills. No fabrication — README states it's a starter
    scaffold the user will build on."""
    skills = []
    try:
        from services.resume_parser import ResumeParser
        from schemas.resume_schemas import flatten_skills
        resume = ResumeParser(DBConnect().get_db()).ensure_structured_resume(user_email=user_email)
        if resume and resume.get("structured"):
            skills = [str(s) for s in (flatten_skills(resume["structured"]) or [])][:12]
    except Exception as e:
        logger.warning("scaffold: could not load skills for %s: %s", user_email, e)

    skills_line = ", ".join(skills) if skills else "the candidate's core skills"
    desc = description or f"A starter project scaffolded by JobPilot for {name}."
    is_python = any(s.lower() in ("python", "django", "flask", "fastapi", "pandas") for s in skills) or "python" in kind.lower()

    readme = (
        f"# {name}\n\n"
        f"{desc}\n\n"
        "## About\n\n"
        "This repository was scaffolded via JobPilot's GitHub integration as a "
        "starting point. It is intentionally minimal — extend it with your own work.\n\n"
        f"## Relevant skills\n\n{skills_line}\n\n"
        "## Getting started\n\n"
        + ("```bash\npython main.py\n```\n" if is_python else "```bash\nnode index.js\n```\n")
    )
    files = {"README.md": readme}
    if is_python:
        files["main.py"] = (
            '"""Starter entry point — replace with your project code."""\n\n\n'
            "def main() -> None:\n"
            f'    print("Welcome to {name}")\n\n\n'
            'if __name__ == "__main__":\n'
            "    main()\n"
        )
    else:
        files["index.js"] = (
            "// Starter entry point — replace with your project code.\n"
            f'console.log("Welcome to {name}");\n'
        )
    return files
