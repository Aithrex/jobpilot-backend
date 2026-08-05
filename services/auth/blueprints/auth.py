"""In-house authentication blueprint for JobPilot (/api/auth).

JobPilot was split out of a portfolio monorepo whose auth *service* (the part
that MINTS JWTs) stayed behind. This blueprint brings auth in-house so the
product runs standalone: email/password registration + login, password reset,
profile, and social sign-in (see services/oauth_service.py for the providers,
whose routes are registered at the bottom of this module).

Hard contract (verified against the frontend's src/lib/api.ts — do not change
shapes without changing the client):
  - register -> 201 {access_token, email}
  - login    -> 200 {access_token, email, role?, sector?}
  - reset payload field is snake_case `new_password`
  - AUTH FAILURES ARE NEVER 401. The frontend turns any 401 into a forced
    logout (auth:session-expired). Bad creds -> 400, exists -> 409,
    upgrade -> 403, throttle/lockout -> 429. 401 is reserved for "your token
    is genuinely invalid/expired" (enforced by @jwt_required).

JWTs are minted with identity = email and the SAME JWT_SECRET_KEY the old auth
service used (set in .env), so existing sessions stay valid and existing bcrypt
passwords keep working — zero forced re-logins after migration.
"""
import hashlib
import logging
import secrets
from datetime import datetime, timezone, timedelta

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity, create_access_token

from utils.db_connect import DBConnect
from utils.security import InputSanitizer, get_rate_limiter, get_client_ip
from models.user import User
from services import entitlements_service

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)

# Seed admins for fresh databases (the migration also sets these on existing DBs).
ADMIN_EMAILS = {"harshith@aithrex.tech", "mannesiddardha@gmail.com"}

# Durable brute-force protection (the in-memory limiter resets on Lambda cold
# start, so the real lockout lives in Mongo; the limiter is a soft IP throttle).
MAX_LOGIN_ATTEMPTS = 8
LOCKOUT_MINUTES = 15
RESET_TOKEN_TTL_MINUTES = 30


# ---------------------------------------------------------------------------
# Collections + helpers
# ---------------------------------------------------------------------------
def _users():
    return DBConnect().get_db().users


def _prefs():
    return DBConnect().get_db().user_preferences


def _reset_tokens():
    return DBConnect().get_db().password_reset_tokens


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v):
    if not hasattr(v, "isoformat"):
        return None
    s = v.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


def _aware(dt):
    """Mongo may hand back naive datetimes; treat them as UTC for comparisons."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _is_locked(user: dict) -> bool:
    lu = _aware(user.get("lockout_until"))
    return bool(lu and lu > _now())


def _preferences_for(email: str) -> dict:
    doc = _prefs().find_one({"user_email": email}) or {}
    doc.pop("_id", None)
    doc.pop("user_email", None)
    return doc


def _profile_payload(email: str) -> dict:
    """Build the GET /profile response: identity + plan + preferences + flags."""
    user = _users().find_one({"email": email}) or {}
    prefs = _preferences_for(email)
    return {
        "email": email,
        "name": user.get("name"),
        "role": user.get("role"),
        "sector": user.get("sector"),
        "created_at": _iso(user.get("created_at")),
        "plan": user.get("plan") or entitlements_service.DEFAULT_PLAN,
        "onboarded": bool(prefs.get("onboarding_complete")),
        "preferences": prefs,
        "entitlements": entitlements_service.summary(email),
        "is_admin": bool(user.get("is_admin")) or email in ADMIN_EMAILS,
        "auth_providers": user.get("auth_providers", []),
    }


def _touch_login(email: str, fingerprint_hash: str = None):
    """Record a successful login: reset lockout, stamp last_login, track device."""
    update = {
        "$set": {"last_login": _now(), "login_attempts": 0},
        "$unset": {"lockout_until": ""},
    }
    if fingerprint_hash:
        update["$addToSet"] = {"fingerprints": fingerprint_hash}
    _users().update_one({"email": email}, update)


def _register_failed_attempt(email: str):
    """Increment failed-login counter; lock the account past the threshold."""
    user = _users().find_one({"email": email}, {"login_attempts": 1})
    attempts = int((user or {}).get("login_attempts", 0)) + 1
    update = {"$set": {"login_attempts": attempts}}
    if attempts >= MAX_LOGIN_ATTEMPTS:
        update["$set"]["lockout_until"] = _now() + timedelta(minutes=LOCKOUT_MINUTES)
    _users().update_one({"email": email}, update)


def _ip_throttled(bucket: str, max_requests: int, window_seconds: int = 600) -> bool:
    limiter = get_rate_limiter()
    return limiter.is_rate_limited(
        f"auth:{bucket}:{get_client_ip(request)}", max_requests=max_requests, window_seconds=window_seconds
    )


# ---------------------------------------------------------------------------
# Email / password
# ---------------------------------------------------------------------------
@auth_bp.route("/register", methods=["POST"])
def register():
    if _ip_throttled("register", max_requests=15):
        return jsonify({"error": "Too many attempts. Please try again later."}), 429

    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    password = data.get("password") or ""
    if not email:
        return jsonify({"error": "A valid email is required"}), 400

    ok, msg = User.validate_password(password)
    if not ok:
        return jsonify({"error": msg}), 400

    if _users().find_one({"email": email}, {"_id": 1}):
        return jsonify({"error": "An account with this email already exists"}), 409

    role = InputSanitizer.sanitize_string(data.get("role") or "", 120) or None
    sector = InputSanitizer.sanitize_string(data.get("sector") or "", 120) or None
    fingerprint = InputSanitizer.sanitize_string(data.get("fingerprint_hash") or "", 128) or None
    # Optional at sign-up (the current form doesn't collect it; OAuth does). When
    # present it seeds the managed mailbox's readable local part below; when
    # absent the alias is minted as an opaque stopgap and self-repairs later
    # (managed_mailbox_service.repair_local_part).
    name = InputSanitizer.sanitize_string(data.get("name") or "", 120) or None

    doc = {
        "email": email,
        "password_hash": User.hash_password(password),
        "name": name,
        "role": role,
        "sector": sector,
        "plan": entitlements_service.DEFAULT_PLAN,
        "plan_since": _now(),
        "auth_providers": ["password"],
        "email_verified": False,
        "is_admin": email in ADMIN_EMAILS,
        "status": "active",
        "login_attempts": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }
    if fingerprint:
        doc["fingerprints"] = [fingerprint]
    try:
        _users().insert_one(doc)
        # Seed an empty preferences doc so the frontend shows onboarding.
        _prefs().update_one(
            {"user_email": email},
            {"$setOnInsert": {"user_email": email, "onboarding_complete": False, "created_at": _now()}},
            upsert=True,
        )
    except Exception as e:
        logger.error("register failed for %s: %s", email, e)
        return jsonify({"error": "Could not create account"}), 500

    # Welcome email — best-effort, mirrors the password-reset dispatch. A mail
    # failure (or unconfigured provider in dev) must never break signup.
    try:
        from utils.config import AppUrlsConfig
        from services import email_service
        email_service.send_welcome(email, AppUrlsConfig.FRONTEND_URL)
    except Exception as e:
        logger.warning("welcome email dispatch failed for %s: %s", email, e)

    # Managed application mailbox — AUTO-PROVISION at signup (spec §6). The user
    # must NEVER be asked to "create a private email": the address is minted here
    # and forwarding defaults to this very address, enabled + verified.
    # Best-effort, EXACTLY like the welcome email above: idempotent (safe to
    # re-run), a pure DB write, and wrapped so an unconfigured domain, a Mongo
    # blip or a collision-exhaustion can NEVER fail signup.
    try:
        from services import managed_mailbox_service
        managed_mailbox_service.provision(email, name)
    except Exception as e:
        logger.warning("mailbox auto-provision failed for %s: %s", email, e)

    # Return a token (the frontend deliberately discards it and asks the user to
    # log in explicitly — see AuthContext register()). Shape stays {access_token,email}.
    token = create_access_token(identity=email)
    return jsonify({"access_token": token, "email": email}), 201


@auth_bp.route("/login", methods=["POST"])
def login():
    if _ip_throttled("login", max_requests=30):
        return jsonify({"error": "Too many attempts. Please try again later."}), 429

    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    password = data.get("password") or ""
    fingerprint = InputSanitizer.sanitize_string(data.get("fingerprint_hash") or "", 128) or None

    # Uniform invalid-credentials response (no enumeration via timing/message).
    invalid = (jsonify({"error": "Invalid email or password"}), 400)
    if not email or not password:
        return invalid

    user = _users().find_one({"email": email})
    if not user:
        return invalid

    if user.get("is_deleted"):
        # Account is soft-deleted and pending erasure (AUDIT-P1-004). Refuse
        # re-auth so it can't be resurrected mid-purge. 403, never 401.
        return jsonify({"error": "This account has been closed."}), 403

    if _is_locked(user):
        return jsonify({"error": "Account temporarily locked due to failed attempts. Try again later."}), 429

    if not user.get("password_hash"):
        # Social-only account with no password set. `social_only` lets the client
        # steer to the in-session "add a password" flow (Settings > Profile)
        # instead of the reset email, which is the reliable path.
        return jsonify({
            "error": "This account was created with social sign-in and has no password yet. "
                     "Sign in with your provider, then add a password under Settings > Profile.",
            "social_only": True,
            "auth_providers": user.get("auth_providers", []),
        }), 400

    if not User.verify_password(password, user["password_hash"]):
        _register_failed_attempt(email)
        return invalid

    _touch_login(email, fingerprint)
    token = create_access_token(identity=email)
    resp = {"access_token": token, "email": email}
    if user.get("role"):
        resp["role"] = user["role"]
    if user.get("sector"):
        resp["sector"] = user["sector"]
    return jsonify(resp), 200


@auth_bp.route("/logout", methods=["POST"])
def logout():
    # Stateless JWT: logout is client-side (token discard). Endpoint exists so
    # the frontend's apiService.logout() has something to call without erroring.
    return jsonify({"ok": True}), 200


@auth_bp.route("/profile", methods=["GET"])
@jwt_required()
def get_profile():
    return jsonify(_profile_payload(get_jwt_identity())), 200


@auth_bp.route("/profile", methods=["PUT"])
@jwt_required()
def update_profile():
    email = get_jwt_identity()
    data = request.get_json(force=True, silent=True) or {}
    updates = {}
    if "name" in data:
        updates["name"] = InputSanitizer.sanitize_string(data.get("name") or "", 120) or None
    if "role" in data:
        updates["role"] = InputSanitizer.sanitize_string(data.get("role") or "", 120) or None
    if "sector" in data:
        updates["sector"] = InputSanitizer.sanitize_string(data.get("sector") or "", 120) or None
    if updates:
        updates["updated_at"] = _now()
        _users().update_one({"email": email}, {"$set": updates})
    return jsonify(_profile_payload(email)), 200


@auth_bp.route("/preferences", methods=["PUT"])
@jwt_required()
def update_preferences():
    """Onboarding + personalization preferences (Pillar A)."""
    email = get_jwt_identity()
    data = request.get_json(force=True, silent=True) or {}

    def _str_list(v, n=40, item_len=80):
        if not isinstance(v, list):
            return None
        return [InputSanitizer.sanitize_string(str(x), item_len) for x in v[:n] if str(x).strip()]

    allowed = {}
    for key in ("target_roles", "target_locations", "current_skills", "learning_interests"):
        if key in data:
            allowed[key] = _str_list(data.get(key)) or []
    for key in ("seniority", "experience_level", "remote_pref", "salary_target", "intensity"):
        if key in data:
            allowed[key] = InputSanitizer.sanitize_string(str(data.get(key) or ""), 120) or None
    if data.get("onboarding_complete") is not None:
        allowed["onboarding_complete"] = bool(data.get("onboarding_complete"))

    allowed["updated_at"] = _now()
    _prefs().update_one(
        {"user_email": email},
        {"$set": allowed, "$setOnInsert": {"user_email": email, "created_at": _now()}},
        upsert=True,
    )
    return jsonify(_profile_payload(email)), 200


@auth_bp.route("/verify", methods=["GET"])
@jwt_required()
def verify():
    return jsonify({"email": get_jwt_identity(), "valid": True}), 200


@auth_bp.route("/account", methods=["DELETE"])
@jwt_required()
def delete_account():
    """Erase the caller's account — GDPR Art.17 / CCPA (AUDIT-P1-004).

    Requires an explicit {"confirm": "DELETE"} body. Soft-deletes synchronously
    (flags the users doc + clears reset tokens) so the client can log out at
    once, then enqueues the async hard purge on the resume Lambda. The client
    polls GET /api/resume/job/<job_id>. A missing/wrong confirmation is a 400 —
    never 401 (401 is reserved for a genuinely bad token, enforced by
    @jwt_required).
    """
    email = get_jwt_identity()
    data = request.get_json(force=True, silent=True) or {}
    if str(data.get("confirm", "")).strip() != "DELETE":
        return jsonify({
            "error": "confirmation_required",
            "message": 'Send {"confirm": "DELETE"} to erase your account.',
        }), 400

    from services import account_service
    from services.resume_service import get_resume_service, ResumeService

    # Phase 1: neutralize now (login-blocking flag + kill pending reset tokens).
    account_service.soft_delete(email)

    # Phase 2: enqueue the heavy purge. invoke_async rewrites -auth- -> -resume-,
    # so this runs on the resume Lambda (the only one wired for long jobs).
    payload = {"user_email": email}
    job_id = get_resume_service().create_job("account_purge", payload, email)
    ResumeService.invoke_async(job_id, "account_purge", payload)

    logger.info("account deletion requested; purge job %s enqueued", job_id)
    return jsonify({
        "deleted": True,
        "job_id": job_id,
        "message": "Your account is being erased.",
    }), 200


# ---------------------------------------------------------------------------
# Lightweight lookups used by the frontend (returning-user UX)
# ---------------------------------------------------------------------------
@auth_bp.route("/check-user", methods=["POST"])
def check_user():
    if _ip_throttled("check-user", max_requests=30):
        return jsonify({"error": "Too many requests"}), 429
    data = request.get_json(force=True, silent=True) or {}
    fp = InputSanitizer.sanitize_string(data.get("fingerprint_hash") or "", 128)
    if not fp:
        return jsonify({"exists": False}), 200
    user = _users().find_one({"fingerprints": fp}, {"email": 1})
    if user:
        return jsonify({"exists": True, "email": user.get("email")}), 200
    return jsonify({"exists": False}), 200


@auth_bp.route("/check-email", methods=["POST"])
def check_email():
    if _ip_throttled("check-email", max_requests=30):
        return jsonify({"error": "Too many requests"}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    if not email:
        return jsonify({"exists": False}), 200
    exists = bool(_users().find_one({"email": email}, {"_id": 1}))
    return jsonify({"exists": exists}), 200


@auth_bp.route("/validate-password", methods=["POST"])
def validate_password():
    if _ip_throttled("validate-password", max_requests=30):
        return jsonify({"error": "Too many requests"}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"valid": False}), 200
    user = _users().find_one({"email": email}, {"password_hash": 1})
    valid = bool(user and user.get("password_hash") and User.verify_password(password, user["password_hash"]))
    return jsonify({"valid": valid}), 200


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------
@auth_bp.route("/request-password-reset", methods=["POST"])
def request_password_reset():
    # Always return ok:true regardless of whether the email exists — never leak
    # account existence through this endpoint.
    if _ip_throttled("reset-request", max_requests=10):
        return jsonify({"error": "Too many requests"}), 429
    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    generic = jsonify({"ok": True, "message": "If an account exists for that email, a reset link is on its way."})

    if not email:
        return generic, 200
    user = _users().find_one({"email": email}, {"_id": 1})
    if not user:
        return generic, 200

    token = secrets.token_urlsafe(32)
    try:
        _reset_tokens().insert_one({
            "token_hash": _hash_token(token),
            "user_email": email,
            "created_at": _now(),
            "expires_at": _now() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES),
            "used": False,
        })
    except Exception as e:
        logger.error("reset token store failed for %s: %s", email, e)
        return generic, 200

    try:
        from utils.config import AppUrlsConfig
        from services import email_service
        link = f"{AppUrlsConfig.FRONTEND_URL}/reset-password?token={token}"
        sent = email_service.send_password_reset(email, link)
        if not sent:
            # The response to the caller stays generic (no enumeration), but a
            # silent no-send is an outage, not a non-event: without this line a
            # misconfigured provider / suppressed address looks identical to a
            # happy path in the logs. Grep `reset-email-not-sent` to find it.
            logger.error(
                "reset-email-not-sent email=%s provider=%s configured=%s suppressed=%s",
                email, email_service.provider_name(),
                email_service.email_configured(), email_service.is_suppressed(email),
            )
    except Exception as e:
        # Don't fail the request if email dispatch fails — log it (the dev
        # fallback also logs the link to the console).
        logger.warning("reset email dispatch failed for %s: %s", email, e)
    return generic, 200


@auth_bp.route("/reset-password", methods=["POST"])
def reset_password():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token") or ""
    new_password = data.get("new_password") or ""
    if not token:
        return jsonify({"error": "Invalid or expired reset link"}), 400

    ok, msg = User.validate_password(new_password)
    if not ok:
        return jsonify({"error": msg}), 400

    rec = _reset_tokens().find_one({"token_hash": _hash_token(token)})
    if not rec or rec.get("used"):
        return jsonify({"error": "Invalid or expired reset link"}), 400
    if _aware(rec.get("expires_at")) and _aware(rec["expires_at"]) < _now():
        return jsonify({"error": "This reset link has expired. Please request a new one."}), 400

    email = rec.get("user_email")
    _users().update_one(
        {"email": email},
        {
            "$set": {"password_hash": User.hash_password(new_password), "updated_at": _now(), "login_attempts": 0},
            "$unset": {"lockout_until": ""},
            "$addToSet": {"auth_providers": "password"},
        },
    )
    _reset_tokens().update_one({"_id": rec["_id"]}, {"$set": {"used": True, "used_at": _now()}})
    return jsonify({"ok": True, "message": "Your password has been reset. You can now log in."}), 200


@auth_bp.route("/set-password", methods=["POST"])
@jwt_required()
def set_password():
    """Set or change the password for the SIGNED-IN account.

    This is the in-session counterpart to the emailed reset link, and the answer
    for social-only accounts: someone who signed up with "Continue with Google"
    has no password_hash, so /login rejects them and the only way in was a reset
    email — which leaves them locked out of password login whenever mail delivery
    is broken. A valid JWT already proves control of the account, so no email
    round-trip is needed.

    Rules:
      - Account already HAS a password  -> `current_password` is required and
        must verify (400 otherwise). Prevents a stolen/borrowed session from
        silently taking the account over.
      - Social-only account (no hash)   -> `current_password` is not required;
        the JWT (minted by the OAuth callback) is the proof.

    Errors are 400/403/429 per the auth contract — never 401 (the frontend
    force-logs-out on any 401, which would be wrong for a bad current password).
    """
    email = get_jwt_identity()
    data = request.get_json(force=True, silent=True) or {}
    new_password = data.get("new_password") or ""
    current_password = data.get("current_password") or ""

    user = _users().find_one({"email": email})
    if not user:
        return jsonify({"error": "Account not found"}), 400
    if user.get("is_deleted"):
        return jsonify({"error": "This account has been closed."}), 403

    has_password = bool(user.get("password_hash"))
    if has_password:
        # Throttle only the branch that verifies a secret.
        if _ip_throttled("set-password", max_requests=20):
            return jsonify({"error": "Too many attempts. Please try again later."}), 429
        if not current_password:
            return jsonify({"error": "Enter your current password."}), 400
        if not User.verify_password(current_password, user["password_hash"]):
            return jsonify({"error": "Current password is incorrect."}), 400

    ok, msg = User.validate_password(new_password)
    if not ok:
        return jsonify({"error": msg}), 400
    if has_password and User.verify_password(new_password, user["password_hash"]):
        return jsonify({"error": "Choose a password you haven't used here before."}), 400

    _users().update_one(
        {"email": email},
        {
            "$set": {"password_hash": User.hash_password(new_password), "updated_at": _now(), "login_attempts": 0},
            "$unset": {"lockout_until": ""},
            "$addToSet": {"auth_providers": "password"},
        },
    )
    # Any reset link already in flight is now stale — burn it so an old email
    # sitting in an inbox can't overwrite the password just chosen.
    try:
        _reset_tokens().update_many(
            {"user_email": email, "used": False},
            {"$set": {"used": True, "used_at": _now(), "invalidated_by": "set-password"}},
        )
    except Exception as e:  # pragma: no cover - best effort
        logger.warning("reset-token invalidation failed for %s: %s", email, e)

    providers = list(user.get("auth_providers") or [])
    if "password" not in providers:
        providers.append("password")
    return jsonify({
        "ok": True,
        "message": "Password set. You can now sign in with your email and password.",
        "auth_providers": providers,
    }), 200


# ---------------------------------------------------------------------------
# SES bounce / complaint webhook (SNS → this endpoint).
#
# PUBLIC (no @jwt_required) — SNS delivers here. It is the single dangerous
# consequence of a forged POST (suppress a victim's address, blocking their
# password resets), so we verify the SNS signature (utils.sns_message) before
# trusting any field, and we NEVER return 5xx on a parse error (SNS treats
# non-2xx as failure and retries). Only a failed signature / wrong topic ->
# 403; everything else -> 200. A matching Terraform edge route is required for
# this to be reachable in prod.
# ---------------------------------------------------------------------------
@auth_bp.route("/ses-notifications", methods=["POST"])
def ses_notifications():
    from utils.sns_message import verify_sns_message, expected_topic_arn

    msg = request.get_json(force=True, silent=True) or {}
    if not msg:
        # Empty/garbage body — ack so SNS doesn't retry-storm us.
        return ("", 200)

    # 1. Cryptographic authenticity. A forged message is a 403 (never trusted).
    if not verify_sns_message(msg):
        logger.warning("[ses-webhook] rejected: SNS signature verification failed")
        return ("forbidden", 403)

    # 2. Bind to our topic (if configured) so a valid signature from an
    #    unrelated topic can't drive suppressions.
    topic = expected_topic_arn()
    if topic and msg.get("TopicArn") != topic:
        logger.warning("[ses-webhook] rejected: TopicArn mismatch")
        return ("forbidden", 403)

    try:
        import json as _json
        from utils.sns_message import SNS_HOST_RE
        from services import email_suppression

        msg_type = msg.get("Type")

        if msg_type == "SubscriptionConfirmation":
            sub_url = msg.get("SubscribeURL", "")
            # Same SSRF guard as SigningCertURL — only ever GET a real AWS SNS host.
            from urllib.parse import urlparse
            parts = urlparse(sub_url)
            host = (parts.hostname or "").lower()
            if parts.scheme != "https" or not SNS_HOST_RE.match(host):
                logger.warning("[ses-webhook] refusing to confirm: bad SubscribeURL host")
                return ("", 200)
            import requests
            requests.get(sub_url, timeout=5)
            logger.info("[ses-webhook] SNS subscription confirmed")
            return ("", 200)

        if msg_type == "Notification":
            event = _json.loads(msg.get("Message") or "{}")
            notification_type = event.get("notificationType") or event.get("eventType")

            if notification_type == "Bounce":
                bounce = event.get("bounce", {})
                if bounce.get("bounceType") == "Permanent":
                    subtype = bounce.get("bounceSubType", "")
                    for r in bounce.get("bouncedRecipients", []):
                        addr = r.get("emailAddress")
                        if addr:
                            email_suppression.add_suppression(addr, "bounce", subtype)

            elif notification_type == "Complaint":
                complaint = event.get("complaint", {})
                feedback = complaint.get("complaintFeedbackType", "")
                for r in complaint.get("complainedRecipients", []):
                    addr = r.get("emailAddress")
                    if addr:
                        email_suppression.add_suppression(addr, "complaint", feedback)

            return ("", 200)

        # Unknown/other type (e.g. UnsubscribeConfirmation) — ack and ignore.
        return ("", 200)
    except Exception as e:
        # Never 5xx on a parse issue: SNS would retry-storm. We already proved
        # authenticity above, so acking a malformed body is safe.
        logger.warning("[ses-webhook] error handling notification: %s", e)
        return ("", 200)


# ---------------------------------------------------------------------------
# Social sign-in routes (Continue with Google / GitHub / LinkedIn / …).
# Implemented in services/oauth_service.py and attached here so they share the
# /api/auth prefix. Registered lazily so a missing/optional dependency can't
# break the core email/password auth above.
# ---------------------------------------------------------------------------
try:
    from services.oauth_service import register_oauth_routes
    register_oauth_routes(auth_bp)
except Exception as e:  # pragma: no cover - oauth is optional/independent
    logger.warning("Social sign-in routes not registered: %s", e)
