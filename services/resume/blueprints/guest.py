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
import hashlib
import io
import logging
import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, request, jsonify, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity

from urllib.parse import urlsplit

from utils.security import InputSanitizer, check_rate_limit, get_client_ip

from services import guest_challenge_service
from services import guest_service

guest_bp = Blueprint("guest", __name__)
logger = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# ------------------------------------------------------------------
# Guest capability transport (cookie > header > DEPRECATED query string)
# ------------------------------------------------------------------
# guest_id is a bearer capability: it grants read access to the parsed resume,
# the match snapshot, the tailored record and the download. Carried in a QUERY
# STRING it leaks into CloudFront/API Gateway access logs, browser history,
# bookmarks and the Referer header of every outbound link on the page - none of
# which are places a capability may live.
#
# Transport, most to least preferred:
#   1. Secure, HttpOnly, SameSite cookie (set on /onboard). HttpOnly also puts
#      it out of reach of any XSS that manages to run on the page.
#   2. X-Guest-Id / X-Guest-Device-Token request headers, for non-cookie
#      clients (native apps, curl, tests). Headers are not logged by the edge
#      and never appear in a Referer.
#   3. DEPRECATED query string. Kept ONLY so the currently-shipped frontend
#      (jobpilot-frontend src/lib/api.ts) keeps working during the migration.
#      REMOVE once the frontend stops sending it - see the report.
GUEST_ID_COOKIE = "jp_guest_id"
DEVICE_TOKEN_COOKIE = "jp_guest_device"
GUEST_ID_HEADER = "X-Guest-Id"
DEVICE_TOKEN_HEADER = "X-Guest-Device-Token"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # matches the 30-day guest asset retention

SOURCE_COOKIE = "cookie"
SOURCE_QUERY = "query"
SOURCE_NONE = "none"


def _allowed_origins() -> list:
    raw = os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://localhost:3000",
    )
    return [o.strip() for o in raw.split(",") if o.strip()]


def _cookies_secure() -> bool:
    """Secure flag on everywhere except explicit local HTTP development."""
    if os.getenv("GUEST_COOKIE_SECURE"):
        return os.getenv("GUEST_COOKIE_SECURE", "").strip().lower() == "true"
    return os.getenv("ENVIRONMENT", "development").strip().lower() != "development"


def _cookie_samesite() -> str:
    """Lax by default: the SPA and /api share a registrable domain in prod, so
    Lax is sufficient and blocks the classic cross-site POST. Deployments that
    split the API onto a third-party domain must set None (which also forces
    Secure) AND rely on the Origin check below."""
    value = (os.getenv("GUEST_COOKIE_SAMESITE", "Lax") or "Lax").strip().capitalize()
    return value if value in ("Lax", "Strict", "None") else "Lax"


def _read_capability(header: str, cookie: str, explicit: str = "") -> tuple:
    """Resolve one guest capability. Returns (raw_value, source).

    Precedence is EXPLICIT-FIRST: body, then header, then the deprecated query
    string, and only then the ambient cookie. An explicitly-supplied value must
    never be silently overridden by a cookie the browser happened to attach -
    that would make ``?guest_id=<someone-elses-id>`` quietly resolve to the
    caller's own session and mask exactly the cross-session checks this code
    exists to enforce.

    ``explicit`` is a value the caller already pulled out of the JSON/form body.
    A body/header/query value is actively supplied, so it does not need the
    CSRF check that an ambient cookie does.
    """
    if explicit:
        return explicit, "body"
    from_header = request.headers.get(header) or ""
    if from_header:
        return from_header, "header"
    # DEPRECATED query-string transport - remove with the frontend migration.
    arg_name = "guest_id" if header == GUEST_ID_HEADER else "device_token"
    from_query = request.args.get(arg_name) or ""
    if from_query:
        return from_query, SOURCE_QUERY
    from_cookie = request.cookies.get(cookie) or ""
    # No value anywhere: source SOURCE_NONE, not SOURCE_COOKIE - otherwise a
    # request that simply omits the capability would be treated as a
    # cookie-authorized write and answered 403 instead of the correct 400/404.
    return (from_cookie, SOURCE_COOKIE if from_cookie else SOURCE_NONE)


def _guest_id_from_request(explicit: str = "") -> tuple:
    return _read_capability(GUEST_ID_HEADER, GUEST_ID_COOKIE, explicit)


def _device_token_from_request(explicit: str = "") -> tuple:
    return _read_capability(DEVICE_TOKEN_HEADER, DEVICE_TOKEN_COOKIE, explicit)


def _set_capability_cookies(response, guest_id: str, device_token: str = ""):
    """Attach the guest capability cookies to a Flask response object."""
    common = {
        "max_age": _COOKIE_MAX_AGE,
        "secure": _cookies_secure(),
        "httponly": True,
        "samesite": _cookie_samesite(),
        "path": "/api",
    }
    if guest_id:
        response.set_cookie(GUEST_ID_COOKIE, guest_id, **common)
    if device_token:
        response.set_cookie(DEVICE_TOKEN_COOKIE, device_token, **common)
    return response


def _origin_allowed() -> bool:
    """CSRF defense for any write authorized by an ambient cookie.

    A cookie is attached by the browser automatically, so a cookie-authorized
    write needs proof the request was actually issued by our own origin. We
    accept only an Origin (or, if absent, a Referer) whose scheme+host+port is
    in the SAME explicit ALLOWED_ORIGINS list CORS uses - never a wildcard, and
    a missing Origin AND Referer is a rejection, not a pass.
    """
    allowed = set(_allowed_origins())
    origin = (request.headers.get("Origin") or "").strip()
    if origin:
        return origin in allowed
    referer = (request.headers.get("Referer") or "").strip()
    if referer:
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}" in allowed
    return False


def _csrf_blocked(source: str):
    """403 when a cookie-borne capability is used for a write from an
    untrusted (or absent) origin. 403, never 401 - the FE logs out on 401."""
    if source != SOURCE_COOKIE:
        return None
    if _origin_allowed():
        return None
    logger.warning("guest cookie write rejected: origin not allowed")
    return (
        jsonify({
            "error": "origin_not_allowed",
            "message": "This request could not be verified. Please retry from the app.",
        }),
        403,
    )


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


def _ip_throttled(
    bucket: str,
    max_requests: int,
    window_seconds: int = 3600,
    fail_open: bool = False,
) -> bool:
    """Distributed (cross-container) per-client throttle - see
    utils.security.check_rate_limit for the fail-open/fail-closed policy.

    fail_open=True is used ONLY for the cheap read endpoints (job poll,
    session) so a Mongo blip degrades to unthrottled reads instead of breaking
    every in-flight guest session. Everything that spends money or storage
    (onboard, tailor, download, claim) fails CLOSED.
    """
    return check_rate_limit(
        f"guest:{bucket}:{get_client_ip(request)}",
        max_requests=max_requests,
        window_seconds=window_seconds,
        fail_open=fail_open,
    )


def _challenge(kind: str, subject_id=None):
    """Progressive challenge hook. Runs BEFORE any budget spend. Returns a 429
    challenge response, or None to proceed. Default provider "none" makes this
    a complete no-op, and a provider is only ever presented when it is
    actually solvable (guest_challenge_service.active_provider)."""
    ch = guest_challenge_service.maybe_challenge(
        kind, guest_service.hash_ip(get_client_ip(request)), subject_id
    )
    if ch:
        return (
            jsonify({
                "error": "challenge_required",
                "provider": ch.get("provider"),
                # The client solves the provider widget, then POSTs
                # {challenge_id, solution} to /guest/challenge/verify and
                # retries. Without this id there is no way to clear the trip.
                "challenge_id": ch.get("challenge_id"),
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
    # A returning guest may present the capability in the form body (current
    # FE), a header, or the cookie set by a previous onboard.
    requested_guest_id, guest_id_source = _guest_id_from_request(
        request.form.get("guest_id") or ""
    )
    if guest_id_source == SOURCE_COOKIE and not _origin_allowed():
        # Onboard is the one place where the capability is only a "continue my
        # session" HINT, not an access grant: create_or_get_session mints a
        # fresh session when it is absent. So an unverifiable cross-origin
        # cookie is DROPPED (the upload becomes a brand-new guest) rather than
        # 403'd. That kills the CSRF - an attacker cannot steer an upload into
        # the victim's session - without breaking any legitimate first upload.
        requested_guest_id = ""
    # Echoed server-issued device token (anti-abuse signal, not a capability).
    echoed_device_token = guest_service.validate_device_token(
        _device_token_from_request(request.form.get("device_token") or "")[0]
    )

    # Progressive challenge BEFORE any budget spend (default provider: no-op).
    challenged = _challenge("onboard")
    if challenged:
        return challenged

    try:
        from services import resume_access
        from services.resume_parser import ResumeParser
        from services.resume_service import ResumeService

        ResumeParser.validate_file(file_bytes, file.filename)
        # DOCX zip-bomb guard BEFORE anything opens the archive (spec §4).
        if file.filename.lower().endswith(".docx"):
            from utils.zip_guard import guard_zip_archive

            guard_zip_archive(file_bytes)

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

        mime_type = ResumeParser.get_mime_type(file.filename)
        content_hash = hashlib.sha256(file_bytes).hexdigest()

        # Resume Platform v3 (spec 1.1/2): Mongo asset doc FIRST
        # (parse_status='pending'), S3 write second under
        # resume-assets/{resume_id}/original, then $set object_key. A failed
        # S3 write marks the asset failed. The async payload carries ONLY the
        # key (never bytes) on Lambda; file_bytes_b64_dev is the local-dev
        # fallback for bucketless environments.
        try:
            resume_id = resume_access.create_pending_asset(
                owner_type="guest",
                guest_id=guest_id,
                quota_subject_id=session.get("quota_subject_id"),
                filename=file.filename,
                mime_type=mime_type,
                size_bytes=len(file_bytes),
                content_hash=content_hash,
                purge_after=datetime.now(timezone.utc) + timedelta(days=30),
            )
        except Exception:
            guest_service.release_global_parse()
            raise

        payload = {
            "resume_id": resume_id,
            "mime_type": mime_type,
            "content_hash": content_hash,
            "guest_id": guest_id,
            "fingerprint_hash": fingerprint_hash,
        }
        try:
            from services.s3_service import get_storage_service

            object_key = get_storage_service().upload_resume_asset(
                resume_id, file_bytes, mime_type
            )
            try:
                resume_access.attach_object_key(resume_id, object_key)
            except Exception:
                # Orphan rule: S3 write landed but the Mongo ref didn't —
                # compensating delete in the same request path (best effort).
                try:
                    get_storage_service().delete_resume(object_key)
                except Exception:
                    pass
                raise
            payload["object_key"] = object_key
            # Track the object on the session so the legacy retention sweep
            # keeps deleting it with the expiring unclaimed session (Phase C
            # moves this fully onto the asset TTL).
            guest_service.record_resume_file_key(guest_id, object_key)
        except Exception as s3_err:
            if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
                # On Lambda there is no inline fallback (spec 1.3): storage
                # down means the upload cannot proceed.
                resume_access.mark_asset_failed(resume_id, "storage_unavailable")
                guest_service.release_global_parse()
                logger.error(
                    "Guest onboard: S3 unavailable for asset %s: %s",
                    resume_id, type(s3_err).__name__,
                )
                return jsonify({"error": "storage_unavailable"}), 503
            logger.warning(
                "Guest onboard: S3 upload unavailable, dev inline fallback for asset %s: %s",
                resume_id, type(s3_err).__name__,
            )
            payload["file_bytes_b64_dev"] = base64.b64encode(file_bytes).decode("utf-8")
        try:
            job_id = guest_service.create_guest_job("guest_parse", payload, guest_id)
            ResumeService.invoke_async(job_id, "guest_parse", payload)
        except Exception:
            # The job never dispatched — free the reserved global slot.
            guest_service.release_global_parse()
            raise
        # The capability also goes back as HttpOnly cookies so the client can
        # stop putting it in URLs. The body still carries guest_id/device_token
        # for the currently-shipped FE and for non-cookie clients.
        response = jsonify({
            "guest_id": guest_id,
            "job_id": job_id,
            "device_token": device_token,
            "resume_id": resume_id,
        })
        _set_capability_cookies(response, guest_id, device_token)
        return response, 202

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
    # Cheap read on the hot polling path: fail OPEN on a storage error.
    if _ip_throttled("poll", max_requests=600, window_seconds=300, fail_open=True):
        return jsonify({"error": "Rate limit exceeded"}), 429
    try:
        # Cookie/header first, DEPRECATED ?guest_id= last. A GET carries no
        # side effect, so no CSRF check - and an unknown/foreign guest_id still
        # gets the SAME generic 404 as a missing job (no existence oracle).
        job = guest_service.get_guest_job(job_id, _guest_id_from_request()[0])
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
    # Cheap read: fail OPEN on a storage error (see _ip_throttled).
    if _ip_throttled("session", max_requests=60, window_seconds=300, fail_open=True):
        return jsonify({"error": "Rate limit exceeded"}), 429
    try:
        summary = guest_service.session_summary(
            _guest_id_from_request()[0],
            request.args.get("fingerprint_hash") or "",
            device_token=_device_token_from_request()[0],
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

    raw_guest_id, guest_id_source = _guest_id_from_request(data.get("guest_id") or "")
    blocked = _csrf_blocked(guest_id_source)
    if blocked:
        return blocked
    guest_id = guest_service.validate_guest_id(raw_guest_id)
    if not guest_id:
        return jsonify({"error": "A valid guest_id is required"}), 400
    match_id = guest_service.validate_match_id(data.get("match_id") or "")
    if not match_id:
        return jsonify({"error": "A valid match_id from your matches list is required"}), 400
    # device_token is an optional anti-abuse echo — validated, never trusted.
    guest_service.validate_device_token(_device_token_from_request(data.get("device_token") or "")[0])

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
    raw_guest_id, guest_id_source = _guest_id_from_request(data.get("guest_id") or "")
    blocked = _csrf_blocked(guest_id_source)
    if blocked:
        return blocked
    guest_id = guest_service.validate_guest_id(raw_guest_id)
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

        # REDEMPTION re-checks the record is STILL guest-owned (user_email
        # null) — after /guest/claim flips ownership, outstanding pre-claim
        # tokens die here with the same generic 404 (spec 1.7 step 4). The
        # claimed copy is served by the authed /api/resume/download path.
        record = DBConnect().get_db().tailoring_records.find_one(
            {
                "record_id": claims["record_id"],
                "guest_id": claims["guest_id"],
                "user_email": None,
            }
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
# POST /api/resume/guest/challenge/verify — redeem a solved challenge
# ------------------------------------------------------------------
# Closes the loop opened by the 429 {error:"challenge_required", challenge_id}
# returned by onboard / matches / tailor. On success the velocity counter that
# produced the trip is reset, so the client's retry proceeds normally.
#
# Every failure is a 400 with a code (never 401 - the FE force-logs-out on any
# 401, and a failed CAPTCHA is not a session problem). The challenge record
# itself carries the replay/expiry/binding checks; see
# services.guest_challenge_service.verify_challenge.
@guest_bp.route("/challenge/verify", methods=["POST"])
def guest_challenge_verify():
    gated = _gate()
    if gated:
        return gated
    # Verification is cheap but brute-forceable in principle; fail CLOSED.
    if _ip_throttled("challenge_verify", max_requests=60, window_seconds=300):
        return jsonify({"error": "Rate limit exceeded"}), 429

    data = request.get_json(force=True, silent=True) or {}
    raw_guest_id, guest_id_source = _guest_id_from_request(data.get("guest_id") or "")
    blocked = _csrf_blocked(guest_id_source)
    if blocked:
        return blocked

    # The subject binding must be rebuilt exactly as it was at issue time:
    # onboard has no session yet (subject_id None), matches/tailor do.
    guest_id = guest_service.validate_guest_id(raw_guest_id)
    subject_id = None
    if guest_id:
        session = guest_service.get_session(guest_id)
        subject_id = (session or {}).get("quota_subject_id")

    client_ip = get_client_ip(request)
    result = guest_challenge_service.verify_challenge(
        data.get("challenge_id"),
        data.get("solution"),
        guest_service.hash_ip(client_ip),
        subject_id,
        remote_ip=client_ip,
    )
    if result.get("ok"):
        return jsonify({"verified": True}), 200
    return (
        jsonify({
            "error": result.get("error") or guest_challenge_service.ERR_INVALID,
            "verified": False,
            "message": "That verification could not be confirmed. Please try again.",
        }),
        400,
    )


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
    raw_guest_id, guest_id_source = _guest_id_from_request(data.get("guest_id") or "")
    blocked = _csrf_blocked(guest_id_source)
    if blocked:
        return blocked
    guest_id = guest_service.validate_guest_id(raw_guest_id)
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
