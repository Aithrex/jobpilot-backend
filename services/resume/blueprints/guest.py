"""Guest Onboarding blueprint — pre-signup upload → parse → tailor → claim.

Mounted at /api/resume/guest so it rides the existing "ANY /api/resume/{proxy+}"
edge route (CLAUDE.md invariant #6 — no new Terraform route).

Contract (docs/specs/guest-onboarding.md):
  * POST /onboard          multipart resume (pdf/docx ≤5MB) → {guest_id, job_id}
  * GET  /job/<id>?guest_id= guest-scoped poll, same job-doc shape as /api/resume/job/<id>
  * GET  /session          recognition + quota summary (no auth)
  * POST /tailor           quota → circuit breaker → async PRO tailor → {job_id}
  * POST /claim            JWT required — atomic ownership flip after signup/login

Status codes: disabled feature → 503; quota exhausted → 429 {quota_exhausted:true};
invalid input → 400; unknown guest/job → 404. NEVER 401 except @jwt_required()
on /claim (auth error contract — the FE force-logs-out on any 401).

Security: every guest input is sanitized (InputSanitizer / UUID validation)
before any Mongo filter; guest_id is a capability token (generic 404 on
mismatch); upload size/type checked BEFORE the file is read into memory;
no resume PII is ever logged.
"""
import base64
import logging

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import InputSanitizer, get_rate_limiter, get_client_ip

from services import guest_service

guest_bp = Blueprint("guest", __name__)
logger = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _gate():
    """503 when the feature is off (env kill-switch AND admin runtime knob)."""
    if not guest_service.guest_onboarding_enabled():
        return (
            jsonify({
                "error": "guest_onboarding_disabled",
                "message": "Guest onboarding is currently unavailable. Please sign up to continue.",
            }),
            503,
        )
    return None


def _ip_throttled(bucket: str, max_requests: int, window_seconds: int = 3600) -> bool:
    limiter = get_rate_limiter()
    return limiter.is_rate_limited(
        f"guest:{bucket}:{get_client_ip(request)}",
        max_requests=max_requests,
        window_seconds=window_seconds,
    )


def _quota_exhausted_response(state: dict, global_breaker: bool = False):
    """429 + {quota_exhausted:true} — the FE renders the signup CTA (never 401)."""
    message = (
        "We've hit today's platform-wide limit for free guest tailoring. "
        "Sign up (free) to keep tailoring — your work here carries over."
        if global_breaker
        else (
            f"You've used all {state.get('tailors_limit', 0)} free tailors. "
            "Sign up (free) to unlock daily tailoring — everything you made "
            "here comes with you."
        )
    )
    body = {
        "error": "quota_exhausted",
        "quota_exhausted": True,
        "message": message,
        "tailors_used": state.get("tailors_used"),
        "tailors_limit": state.get("tailors_limit"),
    }
    if global_breaker:
        body["global"] = True
    return jsonify(body), 429


def _capacity_response(activity: str):
    """429 {error:"capacity", global:true} — the platform-wide daily budget
    for an anonymous activity is spent (NOT a per-user quota; never 401)."""
    return (
        jsonify({
            "error": "capacity",
            "global": True,
            "message": (
                f"We've hit today's platform-wide limit for free guest {activity}. "
                "Sign up (free) to continue — or try again tomorrow."
            ),
        }),
        429,
    )


# ------------------------------------------------------------------
# POST /api/resume/guest/onboard — upload + async FLASH parse
# ------------------------------------------------------------------
@guest_bp.route("/onboard", methods=["POST"])
def onboard():
    gated = _gate()
    if gated:
        return gated
    if _ip_throttled("onboard", max_requests=10, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded. Max 10 uploads per hour."}), 429

    # Size gate BEFORE reading the body into memory (spec §Security 2).
    if request.content_length and request.content_length > _MAX_UPLOAD_BYTES + 16 * 1024:
        return jsonify({"error": "File too large (max 5 MB)"}), 400

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    file = request.files["file"]
    if not file.filename or not any(
        file.filename.lower().endswith(ext) for ext in (".pdf", ".docx")
    ):
        return jsonify({"error": "Only PDF and DOCX files are accepted"}), 400

    file_bytes = file.read(_MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        return jsonify({"error": "File too large (max 5 MB)"}), 400

    fingerprint_hash = InputSanitizer.sanitize_string(
        request.form.get("fingerprint_hash") or "", max_length=128
    )
    requested_guest_id = request.form.get("guest_id") or ""

    try:
        from services.resume_parser import ResumeParser
        from services.resume_service import ResumeService

        ResumeParser.validate_file(file_bytes, file.filename)

        session = guest_service.create_or_get_session(
            requested_guest_id,
            fingerprint_hash,
            guest_service.hash_ip(get_client_ip(request)),
        )
        guest_id = session["guest_id"]

        # Global daily parse budget (atomic reservation) BEFORE dispatching
        # the FLASH parse — gate before you spend (invariant #3).
        if not guest_service.try_reserve_global_parse():
            return _capacity_response("resume parsing")

        payload = {
            "file_base64": base64.b64encode(file_bytes).decode("utf-8"),
            "mime_type": ResumeParser.get_mime_type(file.filename),
            "filename": InputSanitizer.sanitize_string(file.filename, max_length=255),
            "guest_id": guest_id,
            "fingerprint_hash": fingerprint_hash,
        }
        try:
            job_id = guest_service.create_guest_job("guest_parse", payload, guest_id)
            ResumeService.invoke_async(job_id, "guest_parse", payload)
        except Exception:
            # The job never dispatched — free the reserved global slot.
            guest_service.release_global_parse()
            raise
        return jsonify({"guest_id": guest_id, "job_id": job_id}), 202

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Guest onboard error: {e}")
        return jsonify({"error": "Failed to process resume upload"}), 500


# ------------------------------------------------------------------
# GET /api/resume/guest/job/<job_id>?guest_id= — guest-scoped polling
# ------------------------------------------------------------------
@guest_bp.route("/job/<job_id>", methods=["GET"])
def guest_job(job_id):
    gated = _gate()
    if gated:
        return gated
    if _ip_throttled("poll", max_requests=600, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429
    try:
        job = guest_service.get_guest_job(job_id, request.args.get("guest_id") or "")
        if not job:
            return jsonify({"error": "Job not found"}), 404
        for key in ("created_at", "completed_at"):
            if hasattr(job.get(key), "isoformat"):
                job[key] = job[key].isoformat()
        return jsonify(job), 200
    except Exception as e:
        logger.error(f"Guest job status error: {e}")
        return jsonify({"error": "Failed to check job status"}), 500


# ------------------------------------------------------------------
# GET /api/resume/guest/session — recognition + quota summary
# ------------------------------------------------------------------
@guest_bp.route("/session", methods=["GET"])
def guest_session():
    gated = _gate()
    if gated:
        return gated
    if _ip_throttled("session", max_requests=60, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429
    try:
        summary = guest_service.session_summary(
            request.args.get("guest_id") or "",
            request.args.get("fingerprint_hash") or "",
        )
        return jsonify(summary), 200
    except Exception as e:
        logger.error(f"Guest session error: {e}")
        return jsonify({"error": "Failed to load session"}), 500


# ------------------------------------------------------------------
# POST /api/resume/guest/tailor — quota → breaker → async PRO tailor
# ------------------------------------------------------------------
@guest_bp.route("/tailor", methods=["POST"])
def guest_tailor():
    gated = _gate()
    if gated:
        return gated
    if _ip_throttled("tailor", max_requests=10, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded. Try again in a bit."}), 429

    data = request.get_json(force=True, silent=True) or {}
    guest_id = guest_service.validate_guest_id(data.get("guest_id") or "")
    if not guest_id:
        return jsonify({"error": "A valid guest_id is required"}), 400

    job_raw = data.get("job") or {}
    if not isinstance(job_raw, dict):
        return jsonify({"error": "job must be an object"}), 400
    title = InputSanitizer.sanitize_string(str(job_raw.get("title") or ""), max_length=200)
    company = InputSanitizer.sanitize_string(str(job_raw.get("company") or ""), max_length=200)
    jd_text = InputSanitizer.sanitize_string(str(job_raw.get("jd_text") or ""), max_length=15000)
    url = InputSanitizer.sanitize_string(str(job_raw.get("url") or ""), max_length=600)
    if url and not (url.startswith("http://") or url.startswith("https://")):
        url = ""
    if not jd_text and not url:
        return jsonify({"error": "job.jd_text or job.url is required"}), 400

    session = guest_service.get_session(guest_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    if not ((session.get("resume") or {}).get("structured")):
        return jsonify({"error": "Upload your resume first"}), 400

    # Gate BEFORE we spend (invariant #3): the cluster lifetime quota is an
    # ATOMIC charge (increment-then-verify — no check-then-inc TOCTOU), then
    # the platform-wide daily circuit breaker (an atomic reservation).
    charged, state = guest_service.try_charge_guest_tailor(guest_id)
    if not charged:
        return _quota_exhausted_response(state)
    if not guest_service.try_reserve_global_tailor():
        guest_service.increment_guest_tailors(guest_id, -1)  # refund the charge
        return _quota_exhausted_response(
            guest_service.tailor_quota_state(guest_id), global_breaker=True
        )

    try:
        from services.resume_service import ResumeService

        payload = {
            "guest_id": guest_id,
            "job": {"title": title, "company": company, "jd_text": jd_text, "url": url},
        }
        job_id = guest_service.create_guest_job("guest_tailor", payload, guest_id)
        ResumeService.invoke_async(job_id, "guest_tailor", payload)
        return (
            jsonify({
                "job_id": job_id,
                "tailors_used": state["tailors_used"],
                "tailors_limit": state["tailors_limit"],
            }),
            202,
        )
    except Exception as e:
        # Roll back what we charged — the job never dispatched.
        guest_service.increment_guest_tailors(guest_id, -1)
        guest_service.release_global_tailor()
        logger.error(f"Guest tailor error: {e}")
        return jsonify({"error": "Failed to start tailoring. Please try again."}), 500


# ------------------------------------------------------------------
# POST /api/resume/guest/claim — JWT required; atomic ownership flip
# ------------------------------------------------------------------
@guest_bp.route("/claim", methods=["POST"])
@jwt_required()
def guest_claim():
    # Claim stays available even if the env flag was later turned off —
    # users must always be able to pull their guest work into their account.
    # Only require the flag for NEW guest activity, not for claiming.
    if _ip_throttled("claim", max_requests=20, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True, silent=True) or {}
    guest_id = guest_service.validate_guest_id(data.get("guest_id") or "")
    if not guest_id:
        return jsonify({"error": "A valid guest_id is required"}), 400

    user_email = get_jwt_identity()
    try:
        result = guest_service.claim_session(guest_id, user_email)
    except Exception as e:
        logger.error(f"Guest claim error: {e}")
        return jsonify({"error": "Failed to claim guest session"}), 500

    err = result.get("error")
    if err == "not_found":
        return jsonify({"error": "Session not found"}), 404
    if err == "already_claimed":
        # Capability token consumed by ANOTHER account — forbidden, not 401.
        return jsonify({"error": "already_claimed"}), 403
    if err:
        return jsonify({"error": "Invalid claim request"}), 400
    return jsonify(result), 200
