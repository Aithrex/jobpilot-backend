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
from urllib.parse import quote

from flask import Blueprint, request, jsonify, redirect, Response
from flask_jwt_extended import jwt_required, get_jwt_identity

from utils.security import InputSanitizer, get_rate_limiter, get_client_ip
from utils.db_connect import DBConnect
from services import entitlements_service
from services import auto_submit_service
from services import autonomy_service
from services import application_ledger_service
from services import approval_token_service
from services import ats_credential_service
from services import today_inbox_service
from services import apply_onboarding_service

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


def _charge_feature(user_email: str, feature: str, *, idempotency_key=None):
    """Charge ONE unit of a metered feature, returning ``(allowed, usage)``.

    Defect G: when ``AUTO_APPLY_RESERVE`` is on, spend via the ATOMIC
    reserve+complete ledger (``quota_ledger_service`` — a single guarded ``$inc``,
    no check-then-increment race) so ``auto_submit`` / ``autoapply_answer`` can
    never be double-charged under concurrent Lambdas. Dark by default: the switch
    OFF keeps the legacy ``check_and_increment`` metering byte-identical. The
    per-plan cap is entitlements_service's, either way (never a second copy)."""
    from utils.config import _get_config_value
    reserve_on = str(
        _get_config_value("AUTO_APPLY_RESERVE", "false") or "false"
    ).strip().lower() in ("1", "true", "yes", "on")
    if not reserve_on:
        return entitlements_service.check_and_increment(user_email, feature)
    from services import quota_ledger_service as quota
    res = quota.reserve(user_email, feature, n=1, idempotency_key=idempotency_key)
    if not res.get("reserved"):
        return False, res.get("usage") or {}
    # Immediately settle — this is a synchronous spend (no async job to attach to),
    # so the reserved slot is consumed on the spot.
    for eid in (res.get("entry_ids") or []):
        quota.complete_entry(eid)
    return True, res.get("usage") or {}


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
        # G: atomic reserve+complete (no check-then-increment race) when enabled.
        allowed, usage = _charge_feature(user_email, "autoapply_answer")
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
        settings = auto_submit_service.get_settings(user_email)
        # Merge the per-user autonomy threshold (confidence gate for hands-off
        # submit). Kept in a separate store, surfaced on the same settings blob.
        settings["autonomy_threshold"] = autonomy_service.get_autonomy_threshold(user_email)
        # Explicit autonomy mode (copilot|approval|autopilot) — the SINGLE source
        # of truth. null until onboarding forces the pick (no implicit default).
        settings["mode"] = autonomy_service.get_mode(user_email)
        return jsonify(settings), 200
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
    # Canonical key is `auto_submit_enabled` (matches GET + the FE type + storage).
    # Accept the legacy `enabled` key transitionally so FE/BE deploy ordering can
    # never strand the settings PUT at 400 during rollout (defect A). Remove the
    # fallback once the FE ships the canonical key everywhere.
    enabled = data.get("auto_submit_enabled")
    if enabled is None:
        enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        return jsonify({"error": "auto_submit_enabled (boolean) is required"}), 400

    # Optional autonomy threshold — confidence gate in [0, 1]. Validate BEFORE
    # persisting the opt-in so a bad value never half-applies. bool is a subclass
    # of int/float, so reject it explicitly.
    threshold = data.get("autonomy_threshold")
    if threshold is not None:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            return jsonify({"error": "autonomy_threshold must be a number in [0, 1]"}), 400
        if not (0.0 <= float(threshold) <= 1.0):
            return jsonify({"error": "autonomy_threshold must be a number in [0, 1]"}), 400

    # Optional explicit autonomy mode (copilot|approval|autopilot) — the SINGLE
    # source of truth. Validate the enum BEFORE persisting; a bad value is a
    # business error (400 — never 401).
    #
    # FRONTEND HANDOFF (frontend-engineer): no implicit default is ever written.
    # Onboarding must add a step that FORCES the user to pick one of the three
    # modes and PUTs it here; until it is set, GET returns mode:null and the
    # auto-apply gather path REFUSES to auto-act (mode_required fail-safe). The FE
    # should treat a mode_required/needs_onboarding signal (and a null mode) as
    # "route the user to the onboarding mode picker".
    mode = data.get("mode")
    if mode is not None and mode not in autonomy_service.VALID_MODES:
        return jsonify({
            "error": "invalid_mode",
            "message": f"mode must be one of {list(autonomy_service.VALID_MODES)}",
        }), 400

    try:
        result = auto_submit_service.set_settings(user_email, enabled)
        if threshold is not None:
            result["autonomy_threshold"] = autonomy_service.set_autonomy_threshold(
                user_email, float(threshold)
            )
        else:
            result["autonomy_threshold"] = autonomy_service.get_autonomy_threshold(user_email)
        if mode is not None:
            autonomy_service.set_mode(user_email, mode)
        result["mode"] = autonomy_service.get_mode(user_email)
        return jsonify(result), 200
    except Exception as e:
        logger.exception("apply/auto-submit settings PUT error: %s", e)
        return jsonify({"error": "Failed to save auto-submit settings"}), 500


# ==================================================================
# AUTO-APPLY ONBOARDING (QT2) — the ask-once declared-profile surface.
#
# GET returns the canonical question bank grouped by category (each field with
# its sensitivity/options/required/default/current/missing) + ready_for_autoapply
# + the autonomy mode. PUT validates each answer against the catalog and persists
# it to the SAME declared store answer_library resolves from (user_preferences),
# so a value declared here is immediately fillable by the executor — and never
# fabricated (protected/EEO/legal are only ever user-declared). Rides the existing
# /api/apply/* edge route — no new prefix. Business failures are 400 — never 401.
# ==================================================================
@apply_bp.route("/onboarding/questions", methods=["GET"])
@jwt_required()
def onboarding_questions():
    user_email = get_jwt_identity()
    try:
        return jsonify(apply_onboarding_service.build_questions_payload(user_email)), 200
    except Exception as e:
        logger.exception("apply/onboarding questions error: %s", e)
        return jsonify({"error": "Failed to load onboarding questions"}), 500


@apply_bp.route("/onboarding/answers", methods=["PUT"])
@jwt_required()
def onboarding_answers():
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return jsonify({
            "error": "invalid_answers",
            "message": "answers (an object of {field_id: value}) is required",
        }), 400
    # Optional autonomy mode pick (copilot|approval|autopilot) — validated in the
    # service against autonomy_service.VALID_MODES (a bad value is a 400).
    mode = data.get("mode")
    try:
        payload = apply_onboarding_service.save_answers(user_email, answers, mode=mode)
        return jsonify(payload), 200
    except apply_onboarding_service.OnboardingError as e:
        return jsonify({"error": e.error, "message": e.message}), e.status
    except Exception as e:
        logger.exception("apply/onboarding answers error: %s", e)
        return jsonify({"error": "Failed to save onboarding answers"}), 500


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
        # G: atomic reserve+complete (no check-then-increment race). Keyed on the
        # application so a duplicate approve (already short-circuited above) can
        # never double-charge even if it slips through.
        allowed, usage = _charge_feature(
            user_email, "auto_submit",
            idempotency_key=f"route-approve:{user_email}:{application_id}")
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
def _ledger_ready_as_pending(user_email: str) -> list:
    """Bridge System B → the extension: auto-apply LEDGER rows that are APPROVED
    (F: `approved` — a manual go-ahead or a clean `auto` system-approval, snapshot
    frozen) mapped to the extension's pending-record shape. This is what lets the
    extension auto-submit pipeline applications in the USER's own browser — the
    reliable path past reCAPTCHA the headless fleet cannot clear. Best-effort;
    never raises past the caller."""
    try:
        from services import application_ledger_service as ledger
        rows = ledger.list_applications(user_email, status=ledger.STATUS_APPROVED, limit=100)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("auto-submit pending: ledger bridge failed: %s", e)
        return []
    out = []
    for r in rows:
        url = r.get("apply_url")
        if not url:
            continue
        out.append({
            "application_id": r.get("application_id"),
            "apply_url": url, "url": url,
            "status": "approved",       # `approved` == cleared-to-submit for the extension
            "eligible": True,
            "ats": r.get("ats_family"),
            "title": r.get("title"), "company": r.get("company"),
            "source": "ledger",
        })
    return out


@apply_bp.route("/auto-submit/pending", methods=["GET"])
@jwt_required()
def auto_submit_pending():
    disabled = _auto_submit_disabled()
    if disabled is not None:
        return disabled
    user_email = get_jwt_identity()
    try:
        resp = auto_submit_service.list_pending_eligible(user_email)  # {"pending":[...],"waiting":[...]}
        pending = list(resp.get("pending") or [])
        seen = {p.get("application_id") for p in pending}
        # E2 (defect E): when the approval fold is flipped on, surface CANONICAL
        # ledger rows (ready_for_review + approved/queued_for_execution) LABELED
        # with `status` + `already_approved` so H's FE can render approved rows
        # read-only. Dark by default → the legacy `approved`-only bridge below
        # runs unchanged until AUTO_APPLY_APPROVAL_FOLD is on.
        if auto_submit_service.approval_fold_enabled():
            bridge = auto_submit_service.list_canonical_approvals(user_email)
        else:
            bridge = _ledger_ready_as_pending(user_email)
        for row in bridge:
            if row["application_id"] not in seen:
                pending.append(row)
        resp["pending"] = pending
        return jsonify(resp), 200
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
    # Bridge: if this is an auto-apply LEDGER row (the extension submitted a
    # pipeline application in the user's browser), record it on the ledger.
    try:
        from services import application_ledger_service as ledger
        app = ledger.get_application(user_email, application_id)
        # F: the extension submits an APPROVED row in the user's own browser. Walk
        # it through the execution lease (approved → queued_for_execution →
        # running → submitted) so the ledger reflects a real submission.
        _bridge_states = (ledger.STATUS_APPROVED, ledger.STATUS_QUEUED_FOR_EXECUTION,
                          ledger.STATUS_RUNNING)
        if app and app.get("status") in _bridge_states:
            st = app.get("status")
            if st == ledger.STATUS_APPROVED:
                ledger.transition(user_email, application_id,
                                  ledger.STATUS_QUEUED_FOR_EXECUTION)
                st = ledger.STATUS_QUEUED_FOR_EXECUTION
            if st == ledger.STATUS_QUEUED_FOR_EXECUTION:
                ledger.transition(user_email, application_id, ledger.STATUS_RUNNING)
            ledger.mark_submitted(user_email, application_id)
            return jsonify({"approval": {"application_id": application_id,
                                         "status": "submitted", "source": "ledger"}}), 200
    except Exception as e:  # fall through to System A on any ledger miss
        logger.info("auto-submit mark: ledger bridge miss for %s (%s)", application_id, e)
    try:
        record = auto_submit_service.mark_submitted(user_email, application_id)
        return jsonify({"approval": record}), 200
    except auto_submit_service.AutoSubmitError as e:
        return _auto_submit_error(e)
    except Exception as e:
        logger.exception("apply/auto-submit mark-submitted error: %s", e)
        return jsonify({"error": "Failed to mark submitted"}), 500


# ==================================================================
# AUTO-APPLY LEDGER (Tranche 1 §1) — enqueue a job into the applications ledger.
#
# The dedup gate + the unique (user_email, job_identity) index make a duplicate
# application STRUCTURALLY impossible. All business failures are 400/404/409 —
# never 401. Long work (form recon / answer resolution) is NOT done here: this
# only writes the ledger row; the Tranche 2 gather job does the rest.
# ==================================================================
@apply_bp.route("/queue", methods=["POST"])
@jwt_required()
def queue_application():
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    # Sanitize every untrusted field BEFORE it reaches the ledger / a Mongo
    # filter (job_identity is derived from company+title+jd_text).
    company = InputSanitizer.sanitize_string(data.get("company", "") or "", max_length=300)
    title = InputSanitizer.sanitize_string(data.get("title", "") or "", max_length=300)
    jd_text = InputSanitizer.sanitize_string(data.get("jd_text", "") or "", max_length=20000)
    if not company or not title:
        return jsonify({"error": "company and title are required"}), 400

    source = InputSanitizer.sanitize_string(data.get("source", "") or "", max_length=40) or "manual"
    apply_url = InputSanitizer.sanitize_string(data.get("apply_url", "") or "", max_length=2000)

    try:
        # Fast pre-enqueue dedup: if a live/handled (or user-skipped) ledger row
        # already covers this (user, posting), do NOT re-offer it.
        identity = application_ledger_service.normalized_job_identity(company, title, jd_text)
        skip, existing = application_ledger_service.dedup_gate(user_email, identity)
        if skip:
            return jsonify({
                "application": existing,
                "skipped": True,
                "reason": "already_handled",
            }), 200

        # create_application re-sanitizes ats_family/tenant/posting_closes_at and
        # resolves the insert-conflict race deterministically (unique index).
        record, outcome = application_ledger_service.create_application(
            user_email,
            company=company,
            title=title,
            jd_text=jd_text,
            source=source,
            apply_url=apply_url,
            ats_family=data.get("ats_family"),
            tenant=data.get("tenant"),
            posting_closes_at=data.get("posting_closes_at"),
            # Pins the canonical inventory row so the posting survives the
            # rolling 7-day retention window (archived to S3, verified before
            # any delete). Absent for postings that did not come from inventory.
            posting_job_id=data.get("posting_job_id") or data.get("job_id"),
        )

        # Enqueue recon/gather for freshly-created (or requeued) rows so the
        # form is reconned → answers resolved → row transitioned without waiting
        # for the hourly sweep. Skipped for already-handled duplicates. The
        # _process_job "auto_apply_gather" branch now exists; failures here are
        # non-fatal (the sweep still picks the row up).
        if outcome in ("created", "requeued"):
            try:
                from services.resume_service import get_resume_service, ResumeService
                aid = record.get("application_id")
                payload = {"application_id": aid, "user_email": user_email}
                job_id = get_resume_service().create_job("auto_apply_gather", payload,
                                                         user_email=user_email)
                ResumeService.invoke_async(job_id, "auto_apply_gather", payload)
            except Exception as _e:
                logger.warning("apply/queue: gather enqueue failed for %s: %s",
                               record.get("application_id"), _e)
        return jsonify({"application": record, "outcome": outcome}), 201
    except application_ledger_service.ApplicationLedgerError as e:
        return jsonify({"error": e.error, "message": e.message}), e.status
    except Exception as e:
        logger.exception("apply/queue error: %s", e)
        return jsonify({"error": "Failed to queue application"}), 500


# ==================================================================
# ATS CREDENTIAL VAULT (Tranche 1 §4) — server-managed ATS logins.
#
# The password is ALWAYS server-generated: the client never sends one (a PUT
# carrying a password is rejected). Reveal is a single, audited plaintext egress
# gated by a FRESH account-password re-auth + tight rate limit. Every route is
# @jwt_required and user_email-scoped; typed CredentialError maps to
# 400/403/404/409/429/503 — never 401.
# ==================================================================
def _credential_error(e: "ats_credential_service.CredentialError"):
    return jsonify({"error": e.error, "message": e.message}), e.status


@apply_bp.route("/credentials", methods=["GET"])
@jwt_required()
def list_ats_credentials():
    """Metadata only — the encrypted password_blob NEVER leaves the service."""
    user_email = get_jwt_identity()
    try:
        return jsonify({"credentials": ats_credential_service.list_credentials(user_email)}), 200
    except ats_credential_service.CredentialError as e:
        return _credential_error(e)
    except Exception as e:
        logger.exception("apply/credentials list error: %s", e)
        return jsonify({"error": "Failed to load credentials"}), 500


@apply_bp.route("/credentials", methods=["PUT"])
@jwt_required()
def put_ats_credential():
    """Create or rotate a credential. The vault generates the password — reject
    ANY client-supplied secret so a phished/reused personal password can't land."""
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}

    if any(k in data for k in ("password", "secret", "password_blob")):
        return jsonify({
            "error": "password_not_accepted",
            "message": "The vault generates and manages the password; do not send one.",
        }), 400

    try:
        result = ats_credential_service.store_or_rotate(
            user_email,
            data.get("ats_family"),
            data.get("tenant", ""),
            data.get("username", ""),
        )
        return jsonify(result), 200
    except ats_credential_service.CredentialError as e:
        return _credential_error(e)
    except Exception as e:
        logger.exception("apply/credentials put error: %s", e)
        return jsonify({"error": "Failed to store credential"}), 500


@apply_bp.route("/credentials/reveal", methods=["POST"])
@jwt_required()
def reveal_ats_credential():
    """Reveal-on-demand: fresh bcrypt re-auth against the account password, a
    tight per-user rate limit, and a hard-required audit write in the service."""
    user_email = get_jwt_identity()

    # Tight rate limit — a reveal is a sensitive plaintext egress.
    limiter = get_rate_limiter()
    if limiter.is_rate_limited(
        f"cred_reveal:{user_email}", max_requests=5, window_seconds=300
    ):
        return jsonify({
            "error": "rate_limited",
            "message": "Too many reveal attempts. Try again in a few minutes.",
        }), 429

    data = request.get_json(silent=True) or {}
    password = data.get("password")
    if not password or not isinstance(password, str):
        return jsonify({
            "error": "reauth_required",
            "message": "Your account password is required to reveal a stored credential.",
        }), 400

    # Fresh re-auth against users.password_hash (never logged).
    from models.user import User
    try:
        db = DBConnect().get_db()
        user = db.users.find_one({"email": user_email}, {"password_hash": 1})
    except Exception as e:
        logger.exception("apply/credentials reveal reauth lookup error: %s", e)
        return jsonify({"error": "Failed to reveal credential"}), 500
    if not user or not user.get("password_hash") or not User.verify_password(password, user["password_hash"]):
        # 403 (not 401): a failed re-auth is a business failure, not a missing JWT.
        return jsonify({
            "error": "reauth_failed",
            "message": "Password verification failed.",
        }), 403

    try:
        return jsonify(ats_credential_service.reveal(
            user_email, data.get("ats_family"), data.get("tenant", "")
        )), 200
    except ats_credential_service.CredentialError as e:
        return _credential_error(e)
    except Exception as e:
        logger.exception("apply/credentials reveal error: %s", e)
        return jsonify({"error": "Failed to reveal credential"}), 500


@apply_bp.route("/credentials/reset", methods=["POST"])
@jwt_required()
def reset_ats_credential():
    """User-initiated reset (Tranche 1 stub): rotates the stored secret NOW and
    flags pending_ats_reset for the Tranche 2 Oracle forgot-password flow."""
    user_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(ats_credential_service.request_reset(
            user_email, data.get("ats_family"), data.get("tenant", "")
        )), 200
    except ats_credential_service.CredentialError as e:
        return _credential_error(e)
    except Exception as e:
        logger.exception("apply/credentials reset error: %s", e)
        return jsonify({"error": "Failed to reset credential"}), 500


# ==================================================================
# EMAIL MAGIC-LINK APPROVAL (Tranche 1 §2) — PUBLIC route.
#
# Authorization is the signed, single-use token (verified via
# approval_token_service.verify_and_claim), NOT a JWT — same trust model as the
# SES/Stripe webhooks. Deliberately NO @jwt_required: a 401 here would wrongly
# trip the FE force-logout. All failures return a self-contained HTML page with
# 403/409/503 — NEVER 401. Every action is idempotent.
# ==================================================================
def _approve_link_page(title: str, message: str, status: int = 200) -> Response:
    """A tiny, self-contained (no external assets) confirmation page. Title and
    message are STATIC strings here — no untrusted input is interpolated."""
    html = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title></head>"
        "<body style='font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "max-width:480px;margin:64px auto;padding:0 20px;text-align:center;color:#1a1a2e'>"
        f"<h2 style='margin:0 0 12px'>{title}</h2>"
        f"<p style='color:#444;line-height:1.5'>{message}</p>"
        "</body></html>"
    )
    return Response(html, status=status, mimetype="text/html")


_APPROVE_LINK_ERRORS = {
    "invalid_link": ("Link not valid",
                     "This approval link isn't valid. It may have been altered, "
                     "or replaced by a newer email."),
    "link_expired": ("Link expired",
                     "This approval link has expired. Open the app to review your matches."),
    "link_already_used": ("Already done",
                          "You've already used this link — no further action was taken."),
    "approval_links_unavailable": ("Temporarily unavailable",
                                   "Approval links are briefly unavailable. Please try again "
                                   "shortly, or open the app."),
}


def _app_applications_url(token: str = "") -> str:
    """The in-app applications view an approve/review link lands on.

    J (stale deep links): the canonical applications route on the migrated `/next`
    shell is ``/next/applications`` (jobpilot-frontend App.tsx — its index child
    redirects to the `tracker` tab). The legacy bare ``/applications`` no longer
    routes, so email approve/review links must target the `/next` route. Matches
    the mailbox forward convention (``/next/applications/<thread>``).

    P1-APPROVE-GET: for an approve/skip link the (unconsumed) ``token`` is passed
    through as ``?approve=<token>`` so the authenticated confirmation surface can
    submit it back via the CSRF-safe POST — the GET itself never mutates."""
    from utils.config import AppUrlsConfig
    base = f"{AppUrlsConfig.FRONTEND_URL}/next/applications"
    if token:
        return f"{base}?approve={quote(token, safe='')}"
    return base


def _perform_approval_action(action: str, user_email: str,
                             application_ids: list) -> dict:
    """The MUTATION behind an approve/skip magic-link — invoked ONLY from the
    authenticated, CSRF-safe POST (never a bare GET). Returns a JSON-able result.

    Mirrors the pre-P1-APPROVE-GET GET-handler body verbatim (F snapshot freeze +
    instant submit), but now guarded by a real JWT identity + owner-bound token."""
    if action in ("approve_all", "approve_one"):
        # F: approve ONLY rows that reached `ready_for_review`, freezing an
        # immutable snapshot per row (approved_by="user"). A row still gathering /
        # awaiting answers is NOT approvable — `moved` is 0 and the client shows a
        # plain "not ready yet" state (never an error, never 401).
        from services import auto_apply_service as _aas
        moved = application_ledger_service.approve(
            user_email, application_ids, approved_by="user",
            snapshot_provider=lambda aid: _aas.build_approval_snapshot(
                user_email, aid))
        if moved:
            # Fire submission immediately on approval instead of waiting for the
            # hourly sweep (best-effort; gated by AUTO_SUBMIT_ENABLED, idempotent
            # with the sweep). This is what makes "approve → it applies" instant.
            try:
                _aas.request_immediate_submit(user_email, application_ids)
            except Exception as _e:  # never let the response fail on this
                logger.warning("approve-link immediate submit enqueue failed: %s", _e)
        return {"action": action, "approved": moved, "ready": bool(moved)}

    if action == "skip":
        moved = application_ledger_service.user_skip(user_email, application_ids)
        return {"action": action, "skipped": moved}

    # Unreachable: the token action set is whitelisted at verify time.
    return {"action": action, "noop": True}


@apply_bp.route("/approve-link", methods=["GET"])
def approve_link():
    """PUBLIC, NON-MUTATING magic-link landing (P1-APPROVE-GET, control C17).

    A GET must never mutate state (a prefetch, an email-scanner, a shared/leaked
    link, or a CSRF cannot approve anything). This route only VERIFIES the signed
    token (peek — the single use is NOT consumed) and 302-redirects into the app:
      * `review`                      → the applications view;
      * `approve_all|approve_one|skip`→ the applications view carrying the token as
        ``?approve=<token>`` so the authenticated SPA can complete the action via
        the CSRF-safe ``POST /api/apply/approve-link``.
    Invalid/expired links show a self-contained page. NEVER 401."""
    token = request.args.get("token", "")
    try:
        claim = approval_token_service.verify(token)
    except approval_token_service.ApprovalTokenError as e:
        title, message = _APPROVE_LINK_ERRORS.get(
            e.error, ("Link problem", "This link could not be used.")
        )
        return _approve_link_page(title, message, status=e.status)

    action = claim.get("action")
    if action == "review":
        return redirect(_app_applications_url(), code=302)
    # approve_all / approve_one / skip: hand the still-unclaimed token to the
    # authenticated confirmation surface — the GET itself performs NO mutation.
    return redirect(_app_applications_url(token), code=302)


@apply_bp.route("/approve-link", methods=["POST"])
@jwt_required()
def approve_link_confirm():
    """AUTHENTICATED, CSRF-safe completion of a magic-link approval
    (P1-APPROVE-GET). CSRF-safe because authorization is a JWT Bearer in the
    Authorization header — an attacker page cannot forge it cross-site (no
    ambient cookie). The single-use token is consumed HERE (verify_and_claim),
    and the claim's owner MUST match the JWT identity. Business failures are
    400/403/409 — NEVER 401."""
    jwt_email = get_jwt_identity()
    data = request.get_json(silent=True) or {}
    token = data.get("token") or request.args.get("token") or ""
    if not token or not isinstance(token, str):
        return jsonify({"error": "token is required"}), 400

    try:
        claim = approval_token_service.verify_and_claim(token)
    except approval_token_service.TokenAlreadyUsed as e:
        # Idempotent single-use: the link already did its job. `review` is a
        # harmless no-op; other actions report "already done" (409, never 401).
        if (e.payload or {}).get("action") == "review":
            return jsonify({"ok": True, "action": "review", "already_used": True}), 200
        return jsonify({"error": "link_already_used",
                        "message": "You've already used this link — no further "
                                   "action was taken."}), 409
    except approval_token_service.ApprovalTokenError as e:
        # Signature/shape/expiry failure (403/503) — a broken link, not a bad JWT.
        return jsonify({"error": e.error, "message": e.message}), e.status

    action = claim.get("action")
    token_email = claim.get("user_email")
    application_ids = claim.get("application_ids") or []

    # Owner binding: the token's subject MUST be the authenticated caller. A
    # mismatch (a link claimed by the wrong signed-in account) is a business
    # authorization failure — 403, never 401.
    if not token_email or token_email != jwt_email:
        logger.warning("approve-link POST owner mismatch for jti-scoped action")
        return jsonify({"error": "not_authorized",
                        "message": "This approval link is for a different account."}), 403

    if action == "review":
        return jsonify({"ok": True, "action": "review"}), 200

    try:
        result = _perform_approval_action(action, jwt_email, application_ids)
        return jsonify({"ok": True, **result}), 200
    except application_ledger_service.ApplicationLedgerError as e:
        status = e.status if e.status in (400, 403, 409) else 409
        return jsonify({"error": e.error, "message": e.message}), status
    except Exception as e:
        logger.exception("apply/approve-link POST action error: %s", e)
        return jsonify({"error": "action_failed",
                        "message": "We couldn't complete that action. Please open "
                                   "the app."}), 500


# ==================================================================
# TODAY INBOX — the unified "N items need you today" feed.
#
# Read-only aggregator over four pending-decision surfaces (ledger needs_input,
# auto-submit approvals, Gmail suggestions, batch "did you apply?"). Each source
# is guarded + error-isolated inside the service, so a disabled feature or a
# failing source contributes nothing and never 500s the feed. No entitlement
# spend, no LLM. Scoped to the JWT identity. Rides the existing /api/apply/*
# edge route (jobs Lambda) — no new Terraform route needed.
# ==================================================================
@apply_bp.route("/today-inbox", methods=["GET"])
@jwt_required()
def today_inbox():
    user_email = get_jwt_identity()
    try:
        return jsonify(today_inbox_service.get_today_inbox(user_email)), 200
    except Exception as e:
        logger.exception("apply/today-inbox error: %s", e)
        return jsonify({"error": "Failed to load today inbox"}), 500
