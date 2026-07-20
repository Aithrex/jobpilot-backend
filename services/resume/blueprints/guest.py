"""Guest Onboarding blueprint v2 — pre-signup upload → parse → match → tailor
→ polished download → claim.

Mounted at /api/resume/guest so it rides the existing "ANY /api/resume/{proxy+}"
edge route (CLAUDE.md invariant #6 — no new Terraform route).

Contract (docs/specs/guest-onboarding.md v2):
  * POST /onboard            multipart resume (pdf/docx ≤5MB) → 202 {guest_id, job_id, device_token}
  * GET  /job/<id>?guest_id= guest-scoped poll, same job-doc shape as /api/resume/job/<id>
  * GET  /session            recognition + quota summary (no auth; device_token aware)
  * POST /tailor             {guest_id, match_id, device_token?} ONLY → atomic
                             subject reservation → breaker → async PRO tailor
                             → 202 {job_id} | 200 {existing:true} | 429
  * POST /download           {guest_id, record_id, format} → {token, expires_at, formats}
  * GET  /download/<token>   streams the rendered PDF/DOCX (shared resume_renderer)
  * POST /claim              JWT required — atomic, idempotent, exact-capability claim

Status codes: disabled feature → 503; quota exhausted → 429 {quota_exhausted:true};
challenge → 429 {error:"challenge_required"}; invalid input → 400; unknown
guest/job/match/record/token → generic 404; expired download token → 410.
NEVER 401 except @jwt_required() on /claim (auth error contract — the FE
force-logs-out on any 401).

Security: every guest input is format-validated/sanitized (InputSanitizer /
UUID / match-id / token-charset checks) before any Mongo filter; guest_id and
download tokens are capabilities (generic 404 on mismatch, no existence
oracle); upload size/type checked BEFORE the file is read into memory; the
challenge hook and all budget reservations run BEFORE any spend; no resume
PII or raw tokens are ever logged.
"""
import base64
import io
import logging

from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import InputSanitizer, get_rate_limiter, get_client_ip

from services import guest_challenge_service
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


def _challenge(kind: str, subject_id=None):
    """Progressive challenge hook (v2 seam). Runs BEFORE any budget spend.
    Returns a 429 challenge response, or None to proceed. Default provider
    "none" makes this a complete no-op."""
    ch = guest_challenge_service.maybe_challenge(
        kind, guest_service.hash_ip(get_client_ip(request)), subject_id
    )
    if ch:
        return (
            jsonify({
                "error": "challenge_required",
                "provider": ch.get("provider"),
                "message": "Please complete a quick verification to continue.",
            }),
            429,
        )
    return None


def _quota_exhausted_response(state: dict, global_breaker: bool = False):
    """429 + {quota_exhausted:true} — the FE renders the signup CTA (never 401)."""
    message = (
        "We've hit today's platform-wide limit for free guest tailoring. "
        "Sign up (free) to keep tailoring, your work here carries over."
        if global_breaker
        else (
            "You've used your free guest tailor. Sign up (free) to unlock "
            "daily tailoring, everything you made here comes with you."
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


def _capacity_response(activity: str, status: int = 429):
    """{error:"capacity", global:true} — a platform-wide budget or capability
    is unavailable (NOT a per-user quota; never 401)."""
    return (
        jsonify({
            "error": "capacity",
            "global": True,
            "message": (
                f"We've hit today's platform-wide capacity for free guest {activity}. "
                "Sign up (free) to continue, or try again later."
            ),
        }),
        status,
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
    # Echoed server-issued device token (anti-abuse signal, not a capability).
    echoed_device_token = guest_service.validate_device_token(
        request.form.get("device_token") or ""
    )

    # Progressive challenge BEFORE any budget spend (default provider: no-op).
    challenged = _challenge("onboard")
    if challenged:
        return challenged

    try:
        from services.resume_parser import ResumeParser
        from services.resume_service import ResumeService

        ResumeParser.validate_file(file_bytes, file.filename)

        device_token = echoed_device_token or guest_service.mint_device_token()
        session = guest_service.create_or_get_session(
            requested_guest_id,
            fingerprint_hash,
            guest_service.hash_ip(get_client_ip(request)),
            device_token_hash=guest_service.hash_device_token(device_token),
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
        return (
            jsonify({"guest_id": guest_id, "job_id": job_id, "device_token": device_token}),
            202,
        )

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
            device_token=request.args.get("device_token") or "",
        )
        return jsonify(summary), 200
    except Exception as e:
        logger.error(f"Guest session error: {e}")
        return jsonify({"error": "Failed to load session"}), 500


# ------------------------------------------------------------------
# POST /api/resume/guest/tailor — {guest_id, match_id} → reservation →
# breaker → async PRO tailor (v2: served match_ids ONLY, no client JDs)
# ------------------------------------------------------------------
@guest_bp.route("/tailor", methods=["POST"])
def guest_tailor():
    gated = _gate()
    if gated:
        return gated
    if _ip_throttled("tailor", max_requests=10, window_seconds=3600):
        return jsonify({"error": "Rate limit exceeded. Try again in a bit."}), 429

    data = request.get_json(force=True, silent=True) or {}

    # v1 payload shape is REMOVED (v2): a raw job/jd_text/url can no longer be
    # tailored — only a match_id the server itself served. Self-explanatory 400.
    if any(k in data for k in ("job", "jd_text", "url")):
        return (
            jsonify({
                "error": (
                    "This endpoint now tailors one of your matched jobs. "
                    "Send {guest_id, match_id} using a match_id from your "
                    "matches list. Custom job descriptions require a free account."
                ),
            }),
            400,
        )

    guest_id = guest_service.validate_guest_id(data.get("guest_id") or "")
    if not guest_id:
        return jsonify({"error": "A valid guest_id is required"}), 400
    match_id = guest_service.validate_match_id(data.get("match_id") or "")
    if not match_id:
        return jsonify({"error": "A valid match_id from your matches list is required"}), 400
    # device_token is an optional anti-abuse echo — validated, never trusted.
    guest_service.validate_device_token(data.get("device_token") or "")

    session = guest_service.get_session(guest_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404
    if not ((session.get("resume") or {}).get("structured")):
        return jsonify({"error": "Upload your resume first"}), 400

    # Progressive challenge BEFORE any reservation/breaker spend.
    challenged = _challenge("tailor", session.get("quota_subject_id"))
    if challenged:
        return challenged

    # The match must belong to the subject's CURRENT cached matches; the JD is
    # resolved server-side from the stored match (never a client value).
    source = guest_service.find_cached_match(guest_id, match_id)
    if not source:
        return jsonify({"error": "Match not found"}), 404

    # Gate BEFORE we spend (invariant #3): the subject's single-reservation
    # slot (atomic find_one_and_update), then the platform-wide daily breaker.
    outcome = guest_service.try_reserve_tailor(guest_id, match_id)
    state = outcome.get("state") or {}
    if outcome.get("existing"):
        # Idempotent same-match retry — hand back the in-flight/completed job.
        return (
            jsonify({
                "job_id": outcome["job_id"],
                "existing": True,
                "tailors_used": state.get("tailors_used"),
                "tailors_limit": state.get("tailors_limit"),
            }),
            200,
        )
    if not outcome.get("reserved"):
        return _quota_exhausted_response(state)

    if not guest_service.try_reserve_global_tailor():
        guest_service.release_reservation(guest_id)  # refund the slot
        return _quota_exhausted_response(
            guest_service.tailor_quota_state(guest_id), global_breaker=True
        )

    try:
        from services.resume_service import ResumeService

        payload = {
            "guest_id": guest_id,
            "match_id": match_id,
            "job": {
                "title": InputSanitizer.sanitize_string(str(source.get("title") or ""), max_length=200),
                "company": InputSanitizer.sanitize_string(str(source.get("company") or ""), max_length=200),
                "jd_text": str(source.get("jd_text") or "")[:15000],
                "url": str(source.get("url") or "")[:600],
            },
        }
        job_id = guest_service.create_guest_job("guest_tailor", payload, guest_id)
        guest_service.attach_reservation_job(guest_id, match_id, job_id)
        ResumeService.invoke_async(job_id, "guest_tailor", payload)
        return (
            jsonify({
                "job_id": job_id,
                "tailors_used": state.get("tailors_used"),
                "tailors_limit": state.get("tailors_limit"),
            }),
            202,
        )
    except Exception as e:
        # Roll back everything we reserved — the job never dispatched.
        guest_service.release_reservation(guest_id)
        guest_service.release_global_tailor()
        logger.error(f"Guest tailor error: {e}")
        return jsonify({"error": "Failed to start tailoring. Please try again."}), 500


# ------------------------------------------------------------------
# POST /api/resume/guest/download — mint a short-lived download token
# ------------------------------------------------------------------
@guest_bp.route("/download", methods=["POST"])
def guest_download_token():
    gated = _gate()
    if gated:
        return gated
    if _ip_throttled("download", max_requests=60, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True, silent=True) or {}
    guest_id = guest_service.validate_guest_id(data.get("guest_id") or "")
    record_id = guest_service.validate_guest_id(data.get("record_id") or "")
    fmt = data.get("format") or "pdf"
    if not guest_id or not record_id:
        return jsonify({"error": "Not found"}), 404  # generic — no oracle
    if fmt not in guest_service.DOWNLOAD_FORMATS:
        return jsonify({"error": "format must be 'pdf' or 'docx'"}), 400

    try:
        from utils.db_connect import DBConnect

        # Owned-by-this-exact-guest_id, still-unclaimed records only; anything
        # else (missing, other guest, claimed) → the SAME generic 404.
        record = DBConnect().get_db().tailoring_records.find_one(
            {"record_id": record_id, "guest_id": guest_id, "user_email": None},
            {"_id": 1},
        )
        if not record:
            return jsonify({"error": "Not found"}), 404

        # Unlimited re-issue; downloads NEVER consume quota (both formats of
        # the one tailored resume are the same single tailoring event).
        token, expires_at = guest_service.mint_download_token(record_id, guest_id, fmt)
        return (
            jsonify({
                "token": token,
                "expires_at": expires_at,
                "formats": list(guest_service.DOWNLOAD_FORMATS),
            }),
            200,
        )
    except guest_service.DownloadTokensUnavailable:
        # Fail closed (secret unset) — capacity-style 503, NEVER 401.
        return (
            jsonify({
                "error": "downloads_unavailable",
                "message": (
                    "Downloads are temporarily unavailable at our end. "
                    "Please try again shortly, your tailored resume is safe."
                ),
            }),
            503,
        )
    except guest_service.DownloadTokenInvalid:
        return jsonify({"error": "Not found"}), 404
    except Exception as e:
        logger.error(f"Guest download token error: {e}")
        return jsonify({"error": "Failed to prepare download"}), 500


# ------------------------------------------------------------------
# GET /api/resume/guest/download/<token> — stream the rendered file
# ------------------------------------------------------------------
@guest_bp.route("/download/<path:token>", methods=["GET"])
def guest_download_file(token):
    gated = _gate()
    if gated:
        return gated
    if _ip_throttled("download_get", max_requests=60, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    try:
        claims = guest_service.verify_download_token(token)
    except guest_service.DownloadTokenExpired:
        # 410 ONLY for a well-formed, signature-valid, expired token.
        return (
            jsonify({
                "error": "download_link_expired",
                "message": "This download link expired. Request a fresh one.",
            }),
            410,
        )
    except guest_service.DownloadTokensUnavailable:
        return (
            jsonify({
                "error": "downloads_unavailable",
                "message": "Downloads are temporarily unavailable. Please try again shortly.",
            }),
            503,
        )
    except guest_service.DownloadTokenInvalid:
        return jsonify({"error": "Not found"}), 404  # forged/malformed — generic

    try:
        from utils.db_connect import DBConnect

        record = DBConnect().get_db().tailoring_records.find_one(
            {"record_id": claims["record_id"], "guest_id": claims["guest_id"]}
        )
        if not record:
            return jsonify({"error": "Not found"}), 404

        fmt = claims["format"]
        # The EXISTING shared resume_renderer + versioned-dedup path — the
        # same polished output as the authed /api/resume/download.
        file_bytes, filename = guest_service.render_record_file(record, fmt)
        mimetype = (
            "application/pdf" if fmt == "pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        return send_file(
            io.BytesIO(file_bytes),
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename,
        )
    except Exception as e:
        logger.error(f"Guest download stream error: {e}")
        return jsonify({"error": "Failed to generate document"}), 500


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
