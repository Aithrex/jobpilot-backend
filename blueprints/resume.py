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
 10. PUT  /active       → set active base resume
 11. GET  /download-file/<s3_key> → download resume from S3
 12. DELETE /file/<s3_key>        → delete resume from S3
"""

import io
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import InputSanitizer, get_rate_limiter, get_client_ip
from utils.db_connect import DBConnect
from utils.datadog_metrics import dd_metric
from services.s3_service import get_storage_service
from services.usage_service import (
    check_and_increment as _quota_check_and_increment,
    check_quota as _quota_check,
    get_usage as _get_usage,
)


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
    canonical_content_hash,
    ensure_versions,
    find_version,
    latest_version,
    append_version,
    serialize_record,
)

resume_bp = Blueprint("resume", __name__)
logger = logging.getLogger(__name__)


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

    jd_text = InputSanitizer.sanitize_string(jd_text, max_length=10000)
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
        allowed, usage = _quota_check_and_increment(user_email, 1)
        if not allowed:
            return _quota_exceeded_response(usage)

        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()
        payload = {"jd_analysis": jd_analysis, "user_email": user_email}
        job_id = svc.create_job("tailor", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "tailor", payload)
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

    jd_text = InputSanitizer.sanitize_string(jd_text, max_length=10000)
    if not jd_text:
        return jsonify({"error": "Invalid job description"}), 400

    try:
        user_email = get_jwt_identity()
        allowed, usage = _quota_check_and_increment(user_email, 1)
        if not allowed:
            return _quota_exceeded_response(usage)

        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()
        payload = {"job_description": jd_text, "user_email": user_email}
        job_id = svc.create_job("extract_and_tailor", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "extract_and_tailor", payload)
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
        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()
        payload = {
            "jd_analysis": jd_analysis,
            "tailored_resume": tailored_resume,
            "user_feedback": user_feedback,
            "user_email": user_email,
        }
        job_id = svc.create_job("regenerate", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "regenerate", payload)
        return jsonify({"job_id": job_id}), 202

    except Exception as e:
        logger.error(f"Resume regenerate error: {e}")
        return jsonify({"error": "Failed to start resume regeneration. Please try again."}), 500


# ------------------------------------------------------------------
# POST /api/resume/rewrite-bullet — Rewrite a single bullet with JD context
# ------------------------------------------------------------------
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

    try:
        from services.gemini_client import gemini_json, GEMINI_FLASH

        keyword_ctx = f"Target keywords: {', '.join(jd_keywords[:20])}\n" if jd_keywords else ""
        role_ctx = (
            f"Target role: {job_title}" + (f" at {company}" if company else "") + "\n"
            if job_title
            else ""
        )

        from services.gemini_client import gemini_json, GEMINI_FLASH

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
        result = gemini_json(
            prompt, max_tokens=6000, temperature=0.4, model=GEMINI_FLASH, schema=schema
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
        allowed, usage = _quota_check_and_increment(user_email, len(jd_list))
        if not allowed:
            # Tell the user exactly how many would fit if they trimmed the batch.
            usage_msg = usage.copy()
            usage_msg["requested"] = len(jd_list)
            return _quota_exceeded_response(usage_msg)

        from services.resume_service import get_resume_service, ResumeService

        svc = get_resume_service()

        jobs = []
        for jd in jd_list:
            # Each JD goes through: extract_jd → tailor (handled by batch_tailor_item job type)
            payload = {
                "jd_text": jd["text"].strip()[:15000],
                "jd_title": jd.get("title", "").strip(),
                "user_email": user_email,
                # source_job_id ties the batch result back to a saved_jobs row
                # when the batch was launched from the Daily Pipeline handoff.
                # Without this the tailored result never updates the kanban
                # nor increments the streak — it just lives in the jobs table.
                "source_job_id": (jd.get("source_job_id") or "").strip() or None,
            }
            job_id = svc.create_job("batch_tailor_item", payload, user_email=user_email)
            ResumeService.invoke_async(job_id, "batch_tailor_item", payload)
            jobs.append({"job_id": job_id, "title": jd.get("title", "")})

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
    # `limit` query param tunable; bumped default from 100 → 1000 so the
    # kanban can show ALL of a user's records. The 100 cap was causing the
    # funnel summary (server aggregation) and kanban column counts (local
    # records) to disagree once a user exceeded 100 total tailoring records.
    try:
        limit = int(request.args.get("limit") or 1000)
    except (TypeError, ValueError):
        limit = 1000
    limit = max(50, min(limit, 5000))
    try:
        db = DBConnect().get_db()
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
        version, created = append_version(
            versions,
            tailored_resume,
            source=source,
            parent_version_id=parent_version_id,
            ats_scores=ats_scores,
            user_feedback=user_feedback,
        )
        update = {
            "$set": {
                "versions": versions,
                "updated_at": datetime.utcnow(),
            }
        }
        if set_current:
            update["$set"]["current_version_id"] = version["version_id"]
        db.tailoring_records.update_one(
            {"record_id": record_id, "user_email": user_email},
            update,
        )
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
# DELETE /api/resume/tailoring-records/<record_id> — Soft-delete a record
# ------------------------------------------------------------------
@resume_bp.route("/tailoring-records/<record_id>", methods=["DELETE"])
@jwt_required()
def delete_tailoring_record(record_id):
    user_email = get_jwt_identity()
    try:
        db = DBConnect().get_db()
        result = db.tailoring_records.delete_one(
            {"record_id": record_id, "user_email": user_email}
        )
        if not result.deleted_count:
            return jsonify({"error": "Record not found"}), 404
        return jsonify({"message": "Record deleted"}), 200
    except Exception as e:
        logger.exception("Delete tailoring record error: %s", e)
        return jsonify({"error": "Failed to delete record"}), 500


# ------------------------------------------------------------------
# PATCH /api/resume/tailoring-records/<record_id>/application
# Update application tracker fields for a record.
# ------------------------------------------------------------------
_APPLICATION_STATUSES = {
    "draft", "applied", "interviewing", "offer", "rejected", "withdrawn", "ghosted",
}


@resume_bp.route("/tailoring-records/<record_id>/application", methods=["PATCH"])
@jwt_required()
def update_application(record_id):
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}

    updates = {}
    status = data.get("status")
    # Read existing record once — we need to know if a batch-tailor confirmation
    # was pending (so we can fire the streak event when the user confirms).
    db_pre = DBConnect().get_db()
    existing_app = (db_pre.tailoring_records.find_one(
        {"record_id": record_id, "user_email": user_email},
        {"application": 1, "_id": 0},
    ) or {}).get("application") or {}
    was_confirmation_pending = bool(existing_app.get("batch_confirmation_pending"))
    was_status = existing_app.get("status")

    if status is not None:
        if status not in _APPLICATION_STATUSES:
            return jsonify({"error": f"Invalid status. Must be one of {sorted(_APPLICATION_STATUSES)}"}), 400
        updates["application.status"] = status
        # Stamp applied_at automatically when transitioning from draft → applied
        if status == "applied":
            updates["application.applied_at"] = updates.get("application.applied_at") or datetime.utcnow()

    # batch_confirmation_pending — accept boolean from frontend (typically
    # false, set by "I applied" / "Not yet" / "Dismiss" buttons on the board).
    if "batch_confirmation_pending" in data:
        updates["application.batch_confirmation_pending"] = bool(data.get("batch_confirmation_pending"))

    for field in ("recruiter_name", "recruiter_email", "recruiter_company",
                  "next_action_note", "notes", "job_url"):
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

        from services.gemini_client import gemini_json, GEMINI_PRO

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

        result = gemini_json(
            prompt, max_tokens=24000, temperature=0.55, model=GEMINI_PRO, schema=schema
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

        from services.gemini_client import gemini_json, GEMINI_PRO
        result = gemini_json(
            prompt, max_tokens=8000, temperature=0.75, model=GEMINI_PRO
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

        from services.gemini_client import gemini_json, GEMINI_FLASH
        result = gemini_json(
            prompt=prompt,
            max_tokens=5000,
            temperature=0.55,
            model=GEMINI_FLASH,
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
            '  "model_answer": "A concise, strong ideal answer tailored to this candidate (<=150 words)"\n'
            "}"
        )

        schema = {
            "score": int,
            "verdict": str,
            "strengths": list,
            "improvements": list,
            "missing_points": list,
            "model_answer": str,
        }

        from services.gemini_client import gemini_json, GEMINI_PRO
        result = gemini_json(
            prompt, max_tokens=6000, temperature=0.3, model=GEMINI_PRO, schema=schema
        )
        return jsonify({"evaluation": result}), 200
    except Exception as e:
        logger.exception("Mock evaluate error: %s", e)
        return jsonify({"error": f"Evaluation failed: {str(e)[:200]}"}), 500


# ------------------------------------------------------------------
# POST /api/resume/save-record — Store complete tailoring session
# ------------------------------------------------------------------


@resume_bp.route("/save-record", methods=["POST"])
@jwt_required()
def save_record():
    """Store a complete tailoring session record for admin analytics.
    Called automatically by the frontend after tailoring completes,
    and updated again after ATS scoring (if performed)."""
    user_email = get_jwt_identity()
    data = request.get_json(force=True) or {}

    record_id = data.get("record_id")  # None on first save, provided for ATS update
    jd_text = data.get("jd_text", "")
    jd_analysis = data.get("jd_analysis")
    tailored_resume = data.get("tailored_resume")
    ats_scores = data.get("ats_scores")
    base_resume_filename = data.get("base_resume_filename", "")
    base_resume_s3_key = data.get("base_resume_s3_key", "")

    if not record_id and (not jd_analysis or not tailored_resume):
        return jsonify({"error": "jd_analysis and tailored_resume are required"}), 400

    try:
        db = DBConnect().get_db()
        col = db.tailoring_records

        if record_id:
            # Update existing record — attach ATS scores to the current version
            # and mirror them at the top level for legacy list queries.
            existing = col.find_one({"record_id": record_id, "user_email": user_email})
            if not existing:
                return jsonify({"error": "Record not found"}), 404
            versions, _ = ensure_versions(existing)
            current_id = existing.get("current_version_id") or (
                latest_version(versions) or {}
            ).get("version_id")
            for v in versions:
                if v.get("version_id") == current_id:
                    if ats_scores:
                        v["ats_scores"] = ats_scores
                    break
            col.update_one(
                {"record_id": record_id, "user_email": user_email},
                {
                    "$set": {
                        "versions": versions,
                        "current_version_id": current_id,
                        "ats_scores": ats_scores,
                        "ats_scored_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            return jsonify({"record_id": record_id, "updated": True}), 200

        # Create new record with an initial version
        import uuid as _uuid
        from services.resume_versioning import build_initial_version

        rid = str(_uuid.uuid4())
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
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "ats_scored_at": datetime.utcnow() if ats_scores else None,
        }
        col.insert_one(record)
        logger.info(f"Tailoring record saved: {rid} for {user_email}")

        try:
            from services.streak_service import record_application
            user_tz = data.get("tz") or request.args.get("tz") or None
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
# POST /api/resume/download — Generate PDF or DOCX
# ------------------------------------------------------------------


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
                    target, _ = append_version(
                        versions,
                        tailored,
                        source=source if source in ("regenerated", "edited") else "edited",
                        parent_version_id=target.get("version_id") if target else None,
                    )

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
                    return send_file(
                        io.BytesIO(file_bytes),
                        mimetype=mimetype,
                        as_attachment=True,
                        download_name=cached.get("filename") or filename,
                    )
                except Exception as cache_err:
                    logger.warning("Cached S3 fetch failed, re-rendering: %s", cache_err)

            # Cache miss — render and upload.
            if fmt == "pdf":
                file_bytes = svc.renderer.generate_pdf(tailored)
            else:
                file_bytes = svc.renderer.generate_docx(tailored)
            if not file_bytes or len(file_bytes) < 100:
                return jsonify({"error": "Failed to generate document"}), 500

            s3_key = None
            try:
                user = db.users.find_one({"email": user_email})
                if user:
                    user_id = str(user["_id"])
                    job_title = (jd_analysis or {}).get("job_title") or data.get("job_title", "untitled")
                    s3_key = storage.upload_generated_resume(user_id, file_bytes, job_title, fmt)
            except Exception as s3_err:
                logger.warning(f"Failed to upload generated resume to S3: {s3_err}")

            # Attach file cache to the target version atomically.
            target.setdefault("files", {})
            target["files"][fmt] = {
                "s3_key": s3_key,
                "size_bytes": len(file_bytes),
                "filename": filename,
                "content_hash": content_hash,
                "render_version": svc.renderer.RENDER_VERSION,
                "rendered_at": datetime.utcnow(),
            }
            update_set = {
                "versions": versions,
                "updated_at": datetime.utcnow(),
            }
            if not record.get("current_version_id"):
                update_set["current_version_id"] = target.get("version_id")
            db.tailoring_records.update_one(
                {"record_id": record_id, "user_email": user_email},
                {"$set": update_set},
            )

            return send_file(
                io.BytesIO(file_bytes),
                mimetype=mimetype,
                as_attachment=True,
                download_name=filename,
            )

        # ── Legacy path (no record_id) — generate once, no DB dedup ──
        if fmt == "pdf":
            file_bytes = svc.renderer.generate_pdf(tailored)
        else:
            file_bytes = svc.renderer.generate_docx(tailored)
        if not file_bytes or len(file_bytes) < 100:
            return jsonify({"error": "Failed to generate document"}), 500

        return send_file(
            io.BytesIO(file_bytes),
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        logger.exception(f"Resume download error: {e}")
        return jsonify({"error": "Failed to generate document"}), 500


# ------------------------------------------------------------------
# POST /api/resume/upload — Upload and parse a new resume PDF
# ------------------------------------------------------------------


@resume_bp.route("/upload", methods=["POST"])
@jwt_required()
def upload():
    user_email = get_jwt_identity()
    client_ip = get_client_ip(request)
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(f"resume_upload:{client_ip}", max_requests=5, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded. Max 5 uploads per hour."}), 429

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    allowed_extensions = (".pdf", ".docx")
    if not file.filename or not any(
        file.filename.lower().endswith(ext) for ext in allowed_extensions
    ):
        return jsonify({"error": "Only PDF and DOCX files are accepted"}), 400

    file_bytes = file.read()
    if len(file_bytes) > 5 * 1024 * 1024:
        return jsonify({"error": "File too large (max 5 MB)"}), 400

    is_pdf = file.filename.lower().endswith(".pdf")
    if is_pdf and not file_bytes[:5].startswith(b"%PDF"):
        return jsonify({"error": "File is not a valid PDF"}), 400

    original_filename = file.filename

    try:
        import hashlib

        from services.resume_service import get_resume_service, ResumeService
        from services.resume_parser import ResumeParser

        # Validate file structure (lightweight, no text extraction)
        ResumeParser.validate_file(file_bytes, original_filename)

        # Content hash — used to skip LLM re-parse when the same file is
        # uploaded twice. Per-user (a user re-uploading the exact same bytes
        # gets back their existing parsed structured data instantly).
        content_hash = hashlib.sha256(file_bytes).hexdigest()

        db = DBConnect().get_db()
        svc = get_resume_service()

        # Cache hit: same file already uploaded by this user AND already parsed.
        # Cheap check FIRST: only load the (potentially large) parsed structured
        # doc when a base hit exists. On cache misses this avoids a full doc
        # fetch per upload.
        existing_base = db.user_resumes.find_one(
            {"user_email": user_email, "type": "base", "content_hash": content_hash}
        )
        existing_structured = (
            svc.parser.get_structured_resume(user_email=user_email)
            if existing_base and existing_base.get("s3_key")
            else None
        )
        if (
            existing_base
            and existing_base.get("s3_key")
            and existing_structured
            and existing_structured.get("structured")
            and existing_structured.get("content_hash") == content_hash
        ):
            # Reactivate this base doc; deactivate others.
            db.user_resumes.update_many(
                {"user_email": user_email, "type": "base"},
                {"$set": {"is_active": False}},
            )
            db.user_resumes.update_one(
                {"_id": existing_base["_id"]},
                {"$set": {"is_active": True}},
            )

            # Synthesize an already-completed job so the frontend can poll
            # the same /job/<id> endpoint and get the parsed result back
            # without invoking the LLM at all.
            job_id = svc.create_job(
                "upload_parse",
                {"cached": True, "content_hash": content_hash, "user_email": user_email},
                user_email=user_email,
            )
            svc.complete_job(job_id, {"parsed_resume": existing_structured})
            logger.info(
                "Resume upload cache HIT for %s (content_hash=%s) — skipping LLM parse",
                user_email,
                content_hash[:12],
            )
            return jsonify({"job_id": job_id, "cached": True}), 202

        # Upload file to S3 and store metadata
        try:
            storage = get_storage_service()
            user = db.users.find_one({"email": user_email})
            if user:
                user_id = str(user["_id"])
                s3_key = storage.upload_base_resume(user_id, file_bytes, original_filename)
                db.user_resumes.insert_one(
                    {
                        "user_email": user_email,
                        "user_id": user_id,
                        "type": "base",
                        "s3_key": s3_key,
                        "filename": original_filename,
                        "uploaded_at": datetime.utcnow(),
                        "size_bytes": len(file_bytes),
                        "content_hash": content_hash,
                        "is_active": True,
                    }
                )
                # Deactivate previous base resumes
                db.user_resumes.update_many(
                    {"user_email": user_email, "type": "base", "s3_key": {"$ne": s3_key}},
                    {"$set": {"is_active": False}},
                )
        except Exception as s3_err:
            logger.warning(f"Failed to save resume to S3: {s3_err}")

        # Async: create a job for multi-modal LLM parsing and return job_id immediately
        import base64

        file_b64 = base64.b64encode(file_bytes).decode("utf-8")
        mime_type = ResumeParser.get_mime_type(original_filename)

        payload = {
            "file_base64": file_b64,
            "mime_type": mime_type,
            "user_email": user_email,
            "content_hash": content_hash,
        }
        job_id = svc.create_job("upload_parse", payload, user_email=user_email)
        ResumeService.invoke_async(job_id, "upload_parse", payload)

        return jsonify({"job_id": job_id}), 202

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


@resume_bp.route("/status", methods=["GET"])
@jwt_required()
def status():
    user_email = get_jwt_identity()
    try:
        db = DBConnect().get_db()
        # Check for active base resume for this user
        active_resume = db.user_resumes.find_one(
            {"user_email": user_email, "type": "base", "is_active": True}
        )
        # Also check if any base file exists at all
        any_base = active_resume or db.user_resumes.find_one(
            {"user_email": user_email, "type": "base"}
        )

        from services.resume_service import get_resume_service

        svc = get_resume_service()
        resume = svc.get_base_resume(user_email=user_email)

        if not resume:
            # No structured/parsed resume found — but a base file might exist.
            # If so, attempt a re-parse from S3 (handles users whose parse
            # job failed or never completed after upload).
            if any_base and any_base.get("s3_key"):
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
                            "has_base_file": bool(any_base),
                        }
                    ),
                    200,
                )

        parsed_at = resume.get("parsed_at", "")
        if hasattr(parsed_at, "isoformat"):
            parsed_at = parsed_at.isoformat()
        response = {
            "has_resume": True,
            "has_base_file": bool(any_base),
            "skills": resume.get("skills", []),
            "experience_years": resume.get("experience_years"),
            "job_titles": resume.get("job_titles", []),
            "summary": resume.get("summary", ""),
            "parsed_at": parsed_at,
        }
        if active_resume:
            response["s3_key"] = active_resume.get("s3_key", "")
            response["filename"] = active_resume.get("filename", "")
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
    user_email = get_jwt_identity()
    db = DBConnect().get_db()
    resumes = list(
        db.user_resumes.find({"user_email": user_email, "type": "base"}, {"_id": 0}).sort(
            "uploaded_at", -1
        )
    )
    for r in resumes:
        if "uploaded_at" in r and hasattr(r["uploaded_at"], "isoformat"):
            r["uploaded_at"] = r["uploaded_at"].isoformat()
    return jsonify({"versions": resumes}), 200


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
    user_email = get_jwt_identity()
    data = request.get_json()
    s3_key = data.get("s3_key", "")
    db = DBConnect().get_db()
    # Deactivate all, then activate the chosen one
    db.user_resumes.update_many(
        {"user_email": user_email, "type": "base"}, {"$set": {"is_active": False}}
    )
    result = db.user_resumes.update_one(
        {"user_email": user_email, "s3_key": s3_key}, {"$set": {"is_active": True}}
    )
    if result.modified_count:
        return jsonify({"message": "Active resume updated"}), 200
    return jsonify({"error": "Resume not found"}), 404


# ------------------------------------------------------------------
# GET /api/resume/download-file/<s3_key> — Download resume from S3
# ------------------------------------------------------------------


@resume_bp.route("/download-file/<path:s3_key>", methods=["GET"])
@jwt_required()
def download_file(s3_key):
    user_email = get_jwt_identity()
    db = DBConnect().get_db()
    # Verify this resume belongs to the user
    resume = db.user_resumes.find_one({"user_email": user_email, "s3_key": s3_key})
    if not resume:
        return jsonify({"error": "Resume not found"}), 404
    storage = get_storage_service()
    file_bytes = storage.get_resume(s3_key)
    # Determine content type
    content_type = "application/pdf" if s3_key.endswith(".pdf") else "application/octet-stream"
    filename = resume.get("filename", "resume.pdf")
    # Business metric: classify base vs generated/tailored from the key prefix.
    variant = "generated" if "/generated/" in s3_key else "base"
    dd_metric(
        "portfolio.resume.download",
        1,
        tags=[f"variant:{variant}", f"format:{'pdf' if s3_key.endswith('.pdf') else 'other'}"],
    )
    return send_file(
        io.BytesIO(file_bytes), mimetype=content_type, as_attachment=True, download_name=filename
    )


# ------------------------------------------------------------------
# DELETE /api/resume/file/<s3_key> — Delete a resume
# ------------------------------------------------------------------


@resume_bp.route("/file/<path:s3_key>", methods=["DELETE"])
@jwt_required()
def delete_file(s3_key):
    user_email = get_jwt_identity()
    db = DBConnect().get_db()
    resume = db.user_resumes.find_one({"user_email": user_email, "s3_key": s3_key})
    if not resume:
        return jsonify({"error": "Resume not found"}), 404
    storage = get_storage_service()
    storage.delete_resume(s3_key)
    db.user_resumes.delete_one({"user_email": user_email, "s3_key": s3_key})
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

    configured = gmail_service.gmail_configured()
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

    # Quick guard so we don't bother dispatching when the user isn't linked.
    from services import gmail_service
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
                from services.gemini_client import gemini_json, GEMINI_FLASH
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
                out = gemini_json(
                    prompt=prompt, model=GEMINI_FLASH, temperature=0.3,
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
