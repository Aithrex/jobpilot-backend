"""In-house authentication blueprint for JobPilot (/api/auth).

JobPilot was split out of a portfolio monorepo whose auth *service* (the part
that MINTS JWTs) stayed behind. This blueprint brings auth in-house so the
product runs standalone: email/password registration + login, password reset,
profile, and social sign-in (see services/oauth_service.py for the providers,
whose routes are registered at the bottom of this module).

Hard contract (verified against the frontend's src/lib/api.ts — do not change
shapes without changing the client):
  - login, otp/verify -> 200 {access_token, email, role?, sector?} (flat)
  - register -> 202 {verification_required, email}: it mails a code and
    stores nothing; /otp/verify creates the account (docs/specs/sign-in.md)
  - every failure carries `code` (machine) and `message` (human); every 429
    carries `retry_after` and the Retry-After header
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
from utils.security import InputSanitizer, check_rate_limit, get_client_ip
from models.user import User
from services import entitlements_service
from services import email_code_service

auth_bp = Blueprint("auth", __name__)
logger = logging.getLogger(__name__)

# Seed admins for fresh databases (the migration also sets these on existing DBs).
ADMIN_EMAILS = {"harshith@aithrex.tech", "mannesiddardha@gmail.com"}

# Durable brute-force protection (the in-memory limiter resets on Lambda cold
# start, so the real lockout lives in Mongo; the limiter is a soft IP throttle).
MAX_LOGIN_ATTEMPTS = 8
LOCKOUT_MINUTES = 15
RESET_TOKEN_TTL_MINUTES = 30
# Emailed codes. With 5 tries a code (email_code_service.MAX_ATTEMPTS) the
# per-address cap bounds guessing at 25 in a million per hour.
OTP_PER_EMAIL_PER_HOUR = 5
OTP_REQUESTS_PER_IP_PER_HOUR = 20


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


def _lock_seconds_left(user: dict) -> int:
    """Seconds until a password lockout ends (0 = not locked)."""
    lu = _aware(user.get("lockout_until"))
    if not lu or lu <= _now():
        return 0
    return max(1, int((lu - _now()).total_seconds() + 0.999))


def _burn_reset_tokens(email: str, reason: str) -> None:
    """Invalidate reset links already in an inbox. Best effort."""
    try:
        _reset_tokens().update_many(
            {"user_email": email, "used": False},
            {"$set": {"used": True, "used_at": _now(), "invalidated_by": reason}},
        )
    except Exception as e:  # pragma: no cover - best effort
        logger.warning("reset-token invalidation failed for %s: %s", email, e)


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
    """Count a wrong password; lock the account at the threshold. The count
    is an atomic $inc, so parallel guesses cannot share one slot."""
    from pymongo import ReturnDocument
    user = _users().find_one_and_update(
        {"email": email}, {"$inc": {"login_attempts": 1}},
        projection={"login_attempts": 1}, return_document=ReturnDocument.AFTER)
    if user and int(user.get("login_attempts") or 0) >= MAX_LOGIN_ATTEMPTS:
        _users().update_one(
            {"email": email},
            {"$set": {"lockout_until": _now() + timedelta(minutes=LOCKOUT_MINUTES)}})


def _retry_after(window_seconds: int) -> int:
    """Seconds until the current fixed rate-limit window ends."""
    import time
    return max(1, int(window_seconds - (int(time.time()) % window_seconds)))


def _throttle(bucket: str, max_requests: int, window_seconds: int = 600, subject: str = None):
    """Count this request against `bucket`. Returns the seconds to wait when
    the limit is spent, else None.

    Shared across every process (`check_rate_limit`: a local first pass, then
    one Mongo counter). The per-process limiter this replaced multiplied each
    limit by the number of workers and again by blue/green. Keyed by client
    IP unless `subject` names something else (an email hash)."""
    subject = subject if subject is not None else get_client_ip(request)
    if check_rate_limit(f"auth:{bucket}:{subject}", max_requests=max_requests,
                        window_seconds=window_seconds):
        return _retry_after(window_seconds)
    return None


def _fail(status: int, code: str, message: str, **extra):
    """One failure shape for sign-in: `code` for the client to switch on,
    `message` to show. `error` repeats the message because older clients
    render that key. Never 401 (the frontend logs out on any 401)."""
    body = {"error": message, "code": code, "message": message}
    body.update(extra)
    return jsonify(body), status


def _too_many(retry_after: int, message: str, code: str = "rate_limited"):
    """The one 429 shape: a body `retry_after` and the `Retry-After` header."""
    retry_after = max(1, int(retry_after))
    resp = jsonify({"error": "rate_limited", "code": code, "message": message,
                    "retry_after": retry_after})
    resp.headers["Retry-After"] = str(retry_after)
    return resp, 429


def _rate_limited(retry_after: int, message: str):
    return _too_many(retry_after, message)


TOO_MANY_ATTEMPTS = "Too many attempts. Please try again in a few minutes."
ACCOUNT_CLOSED = "This account has been closed."


def _note_gpc(email: str) -> None:
    """Record a Global Privacy Control signal (``Sec-GPC: 1``). Best-effort:
    a privacy-store hiccup must never fail the request carrying the signal."""
    try:
        from services import privacy_service
        if privacy_service.gpc_signal(request.headers):
            privacy_service.record_gpc(email)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("GPC record failed: %s", type(e).__name__)


# ---------------------------------------------------------------------------
# Who has proven what
# ---------------------------------------------------------------------------
SOCIAL_PROVIDERS = ("google", "github", "linkedin", "microsoft", "apple")


def _email_is_verified(user: dict) -> bool:
    """Has anyone proven they receive mail at this account's address?

    True after an emailed code or reset link, or when a sign-in provider that
    vouches for this same address is linked. Password accounts created before
    2026-11 were never verified; they pass through a code once."""
    if user.get("email_verified") is True:
        return True
    email = user.get("email")
    for identity in (user.get("oauth_identities") or {}).values():
        if isinstance(identity, dict) and identity.get("email_verified") is True \
                and identity.get("email") == email:
            return True
    return False


def _sign_in_methods(user: dict) -> list:
    """What this account can sign in with, for "use Google instead" hints.
    An emailed code always works, so it is not listed."""
    methods = ["password"] if user.get("password_hash") else []
    linked = set(user.get("auth_providers") or []) | set((user.get("oauth_identities") or {}).keys())
    methods.extend(p for p in SOCIAL_PROVIDERS if p in linked)
    return methods


def _session_payload(user: dict) -> dict:
    """The flat success payload: {access_token, email, role?, sector?}."""
    email = user["email"]
    payload = {"access_token": create_access_token(identity=email), "email": email}
    if user.get("role"):
        payload["role"] = user["role"]
    if user.get("sector"):
        payload["sector"] = user["sector"]
    return payload


def _console_delivery_allowed() -> bool:
    """Local development with no mail provider: the code is in the server log
    (`[email:dev-fallback]`), and that counts as delivered. Never in prod."""
    import os
    from services import email_service
    return (os.getenv("ENVIRONMENT", "development").lower() == "development"
            and not email_service.email_configured())


def _send_code(email: str):
    """Mail a fresh code. Returns None when it went, else the error response.

    Production mail has one provider and `send_email` swallows its failures,
    so the boolean is the only signal: a False here is told to the person
    instead of leaving them waiting for an email that is not coming."""
    from services import email_service

    wait = email_code_service.cooldown_remaining(email)
    if wait:
        return _too_many(wait, "We just sent a code. You can ask for another one shortly.",
                         code="code_cooldown")
    wait = _throttle("otp-email", OTP_PER_EMAIL_PER_HOUR, 3600, subject=_hash_token(email)[:32])
    if wait:
        return _too_many(wait, "Too many codes were requested for this address. Please try again later.")

    code = email_code_service.issue(email)
    sent = False
    try:
        sent = bool(email_service.send_login_code(email, code, email_code_service.CODE_TTL_MINUTES))
    except Exception as e:
        logger.error("login-code send raised for %s: %s", email, type(e).__name__)
    if sent or _console_delivery_allowed():
        return None

    email_code_service.withdraw(email)
    logger.error(
        "login-code-not-sent email=%s provider=%s configured=%s suppressed=%s",
        email, email_service.provider_name(),
        email_service.email_configured(), email_service.is_suppressed(email),
    )
    return _fail(503, "email_send_failed",
                 "We could not send an email to that address. Check it for typos, or try again in a minute.")


def _code_sent_body() -> dict:
    return {
        "ok": True,
        "message": "We sent a 6-digit code to that address.",
        "resend_after": email_code_service.RESEND_COOLDOWN_SECONDS,
        "expires_in": email_code_service.CODE_TTL_MINUTES * 60,
    }


def _signup_consent(data: dict):
    """Clean the clickwrap fields. Returns (terms_version, error_response)."""
    from services import privacy_service
    if data.get("age_confirmed") is False:
        return None, _fail(400, "age_requirement", privacy_service.age_requirement_message(),
                           error="age_requirement")
    try:
        return privacy_service.clean_terms_version(data.get("terms_version")), None
    except privacy_service.PrivacyError as e:
        return None, _fail(400, e.code, e.message, error=e.code)


def _create_account(email: str, data: dict, *, password: str, terms_version) -> dict:
    """Insert a new account whose email has just been proven by a code, with
    everything sign-up has always done (preferences seed, clickwrap record,
    welcome mail, managed mailbox). Raises DuplicateKeyError on a lost race."""
    from services import privacy_service

    role = InputSanitizer.sanitize_string(data.get("role") or "", 120) or None
    sector = InputSanitizer.sanitize_string(data.get("sector") or "", 120) or None
    fingerprint = InputSanitizer.sanitize_string(data.get("fingerprint_hash") or "", 128) or None
    # Optional at sign-up. When present it seeds the managed mailbox's readable
    # local part; when absent the alias is an opaque stopgap that repairs
    # itself later (managed_mailbox_service.repair_local_part).
    name = InputSanitizer.sanitize_string(data.get("name") or "", 120) or None

    now = _now()
    doc = {
        "email": email,
        "name": name,
        "role": role,
        "sector": sector,
        "plan": entitlements_service.DEFAULT_PLAN,
        "plan_since": now,
        "auth_providers": ["password"] if password else ["email_code"],
        "email_verified": True,
        "email_verified_at": now,
        "is_admin": email in ADMIN_EMAILS,
        "status": "active",
        "login_attempts": 0,
        "created_at": now,
        "updated_at": now,
        "last_login": now,
    }
    if password:
        doc["password_hash"] = User.hash_password(password)
    if fingerprint:
        doc["fingerprints"] = [fingerprint]
    _users().insert_one(doc)
    # Seed an empty preferences doc so the frontend shows onboarding.
    _prefs().update_one(
        {"user_email": email},
        {"$setOnInsert": {"user_email": email, "onboarding_complete": False, "created_at": now}},
        upsert=True,
    )

    # Clickwrap facts + the erased-address marker. Runs BEFORE the welcome
    # email: a new account on an address that was erased earlier is a new
    # relationship, and the marker would otherwise block that email.
    try:
        privacy_service.on_account_created(email)
        privacy_service.record_registration(
            email, age_confirmed=data.get("age_confirmed") is True or None,
            terms_version=terms_version)
    except Exception as e:
        logger.warning("clickwrap record failed at signup: %s", type(e).__name__)

    # Welcome email: best-effort. A mail failure must never break signup.
    try:
        from utils.config import AppUrlsConfig
        from services import email_service
        email_service.send_welcome(email, AppUrlsConfig.FRONTEND_URL)
    except Exception as e:
        logger.warning("welcome email dispatch failed for %s: %s", email, e)

    # Managed application mailbox: AUTO-PROVISION at signup (spec §6). The user
    # must NEVER be asked to "create a private email": the address is minted
    # here and forwarding defaults to this very address. Idempotent, a pure DB
    # write, and wrapped so an unconfigured domain or a Mongo blip can never
    # fail signup.
    try:
        from services import managed_mailbox_service
        managed_mailbox_service.provision(email, name)
    except Exception as e:
        logger.warning("mailbox auto-provision failed for %s: %s", email, e)
    return doc


# ---------------------------------------------------------------------------
# Sign-up
# ---------------------------------------------------------------------------
@auth_bp.route("/register", methods=["POST"])
def register():
    """Step 1 of password sign-up: check the request and mail a code.

    Nothing is stored. The account is created by `/otp/verify`, which carries
    the code AND the password, so an account never exists (and a password
    never works) for an address nobody has proven they own. Before 2026-11
    this route created the account at once, which let anyone register someone
    else's address and keep a working password on it.
    """
    wait = _throttle("register", 15)
    if wait:
        return _too_many(wait, TOO_MANY_ATTEMPTS)

    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    password = data.get("password") or ""
    if not email:
        return _fail(400, "invalid_email", "A valid email is required")

    ok, msg = User.validate_password(password)
    if not ok:
        return _fail(400, "weak_password", msg)

    _terms, bad = _signup_consent(data)
    if bad:
        return bad

    existing = _users().find_one({"email": email})
    if existing:
        if existing.get("is_deleted"):
            return _fail(403, "account_closed", ACCOUNT_CLOSED)
        return _fail(409, "account_exists", "An account with this email already exists",
                     methods=_sign_in_methods(existing))

    # A new relationship on an address that was erased earlier: without this
    # the erasure marker would block the code email itself.
    try:
        from services import privacy_service
        privacy_service.clear_account_deleted_marker(email)
    except Exception as e:  # pragma: no cover - best effort
        logger.warning("erasure marker clear failed: %s", type(e).__name__)

    failed = _send_code(email)
    if failed:
        return failed
    body = _code_sent_body()
    body.update({"email": email, "verification_required": True})
    return jsonify(body), 202


# ---------------------------------------------------------------------------
# Emailed one-time code: sign in, sign up, and prove an address
# ---------------------------------------------------------------------------
@auth_bp.route("/otp/request", methods=["POST"])
def otp_request():
    """Mail a 6-digit code. The answer is the same whether or not the address
    has an account (a code signs in an existing one and creates a new one)."""
    wait = _throttle("otp-request", OTP_REQUESTS_PER_IP_PER_HOUR, 3600)
    if wait:
        return _too_many(wait, "Too many codes requested. Please try again later.")

    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    if not email:
        return _fail(400, "invalid_email", "Enter a valid email address.")

    user = _users().find_one({"email": email}, {"is_deleted": 1})
    if user and user.get("is_deleted"):
        return _fail(403, "account_closed", ACCOUNT_CLOSED)
    if not user:
        try:
            from services import privacy_service
            privacy_service.clear_account_deleted_marker(email)
        except Exception as e:  # pragma: no cover - best effort
            logger.warning("erasure marker clear failed: %s", type(e).__name__)

    failed = _send_code(email)
    if failed:
        return failed
    return jsonify(_code_sent_body()), 200


@auth_bp.route("/otp/verify", methods=["POST"])
def otp_verify():
    """Trade a code for a session.

    Existing account: signs in and marks the email verified.
    New address: creates the account. That needs the same age and terms
    consent the register form records (`consent_required` otherwise), and
    takes an optional `password`.

    An unverified account that already holds a password is the one delicate
    case. That password was set by someone who never proved the address, so
    it survives only if this request repeats it (the person signing up, or a
    pre-2026-11 account finishing verification). Otherwise it is removed and
    the response says so with `password_removed`.
    """
    wait = _throttle("otp-verify", 40)
    if wait:
        return _too_many(wait, TOO_MANY_ATTEMPTS)

    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    code = email_code_service.normalize_code(data.get("code"))
    password = data.get("password") if isinstance(data.get("password"), str) else ""
    if not email or len(code) != 6:
        return _fail(400, "code_invalid", "Enter the 6-digit code from the email.")

    user = _users().find_one({"email": email})
    if user and user.get("is_deleted"):
        return _fail(403, "account_closed", ACCOUNT_CLOSED)

    # Everything that can be wrong with the REQUEST is answered before the
    # code is looked at, so fixing it does not cost one of the five tries.
    terms_version = None
    if not user:
        terms_version, bad = _signup_consent(data)
        if bad:
            return bad
        if data.get("age_confirmed") is not True:
            from services import privacy_service
            return _fail(400, "consent_required",
                         f"Confirm you are at least {privacy_service.minimum_age()} and agree to the Terms "
                         "to create your account.")
        if password:
            ok, msg = User.validate_password(password)
            if not ok:
                return _fail(400, "weak_password", msg)

    try:
        row = email_code_service.check(email, code)
    except email_code_service.CodeError as e:
        return _fail(400, e.code, e.message, **e.extra)
    if not email_code_service.consume(row):
        return _fail(400, "code_expired", "That code has expired. Request a new one.")

    created = False
    password_removed = False
    if not user:
        try:
            user = _create_account(email, data, password=password, terms_version=terms_version)
            created = True
        except Exception as e:
            # Two verifies for one new address: the other one created it.
            user = _users().find_one({"email": email})
            if not user:
                logger.error("account create failed for %s: %s", email, e)
                return _fail(500, "signup_failed", "Could not create account")

    if not created:
        now = _now()
        update = {
            "$set": {"email_verified": True, "email_verified_at": now, "last_login": now,
                     "login_attempts": 0, "updated_at": now},
            "$unset": {"lockout_until": ""},
        }
        unproven_password = bool(user.get("password_hash")) and not _email_is_verified(user)
        if unproven_password and not (password and User.verify_password(password, user["password_hash"])):
            update["$unset"]["password_hash"] = ""
            update["$pull"] = {"auth_providers": "password"}
            password_removed = True
        fingerprint = InputSanitizer.sanitize_string(data.get("fingerprint_hash") or "", 128) or None
        if fingerprint:
            update["$addToSet"] = {"fingerprints": fingerprint}
        _users().update_one({"email": email}, update)
        if password_removed:
            _burn_reset_tokens(email, "email-verified")

    _note_gpc(email)
    body = _session_payload(user)
    body["created"] = created
    if password_removed:
        body["password_removed"] = True
    return jsonify(body), 200


# ---------------------------------------------------------------------------
# Password sign-in
# ---------------------------------------------------------------------------
@auth_bp.route("/login", methods=["POST"])
def login():
    wait = _throttle("login", 30)
    if wait:
        return _too_many(wait, TOO_MANY_ATTEMPTS)

    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    password = data.get("password") or ""
    fingerprint = InputSanitizer.sanitize_string(data.get("fingerprint_hash") or "", 128) or None

    # One answer for a wrong password and an unknown address.
    invalid = _fail(400, "invalid_credentials", "Invalid email or password")
    if not email or not password:
        return invalid

    user = _users().find_one({"email": email})
    if not user:
        return invalid

    if user.get("is_deleted"):
        # Account is soft-deleted and pending erasure (AUDIT-P1-004). Refuse
        # re-auth so it can't be resurrected mid-purge. 403, never 401.
        return _fail(403, "account_closed", ACCOUNT_CLOSED)

    locked_for = _lock_seconds_left(user)
    if locked_for:
        return _too_many(locked_for,
                         "Too many wrong passwords, so password sign-in is paused for this account. "
                         "You can still sign in with an emailed code.",
                         code="account_locked")

    if not user.get("password_hash"):
        # No password on this account: it was made with a sign-in provider or
        # an emailed code. `social_only` and `auth_providers` let the client
        # offer the method that works instead of a dead end.
        methods = _sign_in_methods(user)
        if methods:
            message = ("This account signs in with " + " or ".join(m.capitalize() for m in methods)
                       + " and has no password yet.")
        else:
            message = "This account has no password yet. Sign in with an emailed code."
        return _fail(400, "no_password", message, social_only=bool(methods),
                     auth_providers=methods, methods=methods)

    if not User.verify_password(password, user["password_hash"]):
        _register_failed_attempt(email)
        return invalid

    if not _email_is_verified(user):
        # Right password, but nobody has proven this address. The client asks
        # for a code and sends this same password with it (/otp/verify).
        return _fail(403, "email_unverified",
                     "Confirm your email to finish signing in. We will send you a 6-digit code.")

    _touch_login(email, fingerprint)
    _note_gpc(email)
    return jsonify(_session_payload(user)), 200


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
    _note_gpc(get_jwt_identity())
    return jsonify({"email": get_jwt_identity(), "valid": True}), 200


# ---------------------------------------------------------------------------
# Privacy rights (US launch: CCPA/CPRA, GPC, sensitive data, clickwrap).
# Thin routes; the logic is services/privacy_service.py. Failures are
# 400 / 403 / 429, never 401 (the frontend force-logs-out on any 401).
# ---------------------------------------------------------------------------
@auth_bp.route("/privacy", methods=["GET"])
@jwt_required()
def get_privacy_settings():
    from services import privacy_service
    email = get_jwt_identity()
    _note_gpc(email)
    return jsonify(privacy_service.get_privacy(email)), 200


@auth_bp.route("/privacy", methods=["PUT"])
@jwt_required()
def update_privacy_settings():
    from services import privacy_service
    email = get_jwt_identity()
    _note_gpc(email)
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid_request", "message": "Send a JSON object."}), 400
    user = _users().find_one({"email": email}, {"is_deleted": 1}) or {}
    if user.get("is_deleted"):
        return jsonify({"error": "account_closed", "message": "This account has been closed."}), 403
    try:
        return jsonify(privacy_service.update_privacy(email, data)), 200
    except privacy_service.PrivacyError as e:
        return jsonify({"error": e.code, "message": e.message}), e.status
    except Exception as e:
        logger.exception("privacy update failed: %s", type(e).__name__)
        return jsonify({"error": "privacy_update_failed",
                        "message": "We could not save your privacy settings. Please try again."}), 500


@auth_bp.route("/account/export", methods=["GET"])
@jwt_required()
def export_account_data():
    """Right to know / portability: every user-owned record as one JSON file.

    Streamed (one document in memory at a time). Secrets are never included.
    Five exports per account per day on the shared limiter."""
    from flask import Response, stream_with_context
    from services import privacy_service
    from utils.security import check_rate_limit

    email = get_jwt_identity()
    window = privacy_service.EXPORT_WINDOW_SECONDS
    key = "privacy:export:" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]
    if check_rate_limit(key, max_requests=privacy_service.EXPORT_DAILY_LIMIT,
                        window_seconds=window):
        return _rate_limited(_retry_after(window),
                             "You can download your data 5 times a day. Please try again later.")

    user = _users().find_one({"email": email}, {"is_deleted": 1})
    if not user:
        return jsonify({"error": "account_not_found", "message": "Account not found."}), 400
    if user.get("is_deleted"):
        return jsonify({"error": "account_closed", "message": "This account has been closed."}), 403

    filename = privacy_service.export_filename()
    logger.info("data export started for %s", privacy_service.mask_email(email))
    return Response(
        stream_with_context(privacy_service.export_stream(email)),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# One-click unsubscribe (RFC 8058) + the frontend confirmation page. PUBLIC:
# no login, by law (CAN-SPAM 15 U.S.C. 7704(a)(3)). The token is the only
# credential: an HMAC over (email, list), verified in constant time. A GET
# never changes anything, so link scanners that prefetch the URL are harmless.
UNSUBSCRIBE_IP_LIMIT = 120
UNSUBSCRIBE_IP_WINDOW_SECONDS = 600


@auth_bp.route("/email/unsubscribe", methods=["GET", "POST"])
def email_unsubscribe():
    from services import privacy_service
    from utils.security import check_rate_limit

    if check_rate_limit(f"auth:unsubscribe:{get_client_ip(request)}",
                        max_requests=UNSUBSCRIBE_IP_LIMIT,
                        window_seconds=UNSUBSCRIBE_IP_WINDOW_SECONDS, fail_open=True):
        return _rate_limited(_retry_after(UNSUBSCRIBE_IP_WINDOW_SECONDS),
                             "Too many requests. Please try again in a few minutes.")

    token = request.args.get("t") or ""
    if request.method == "POST" and not token:
        body = request.get_json(silent=True) if request.is_json else None
        if isinstance(body, dict):
            token = body.get("token") or body.get("t") or ""
        if not token:
            token = request.form.get("t") or request.form.get("token") or ""
    invalid = (jsonify({"error": "invalid_token",
                        "message": "This unsubscribe link is not valid."}), 400)
    if not isinstance(token, str) or not token:
        return invalid

    if request.method == "GET":
        info = privacy_service.describe_token(token)
        return (jsonify(info), 200) if info else invalid

    # POST: the RFC 8058 body is `List-Unsubscribe=One-Click` (form-encoded);
    # the frontend page sends JSON {token}. Either way the token decides.
    try:
        result = privacy_service.unsubscribe(token)
    except Exception as e:
        logger.exception("unsubscribe failed: %s", type(e).__name__)
        return jsonify({"error": "unsubscribe_failed",
                        "message": "We could not process this request. Please try again."}), 500
    return (jsonify(result), 200) if result else invalid


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
    wait = _throttle("check-user", 30)
    if wait:
        return _too_many(wait, "Too many requests. Please try again in a few minutes.")
    data = request.get_json(force=True, silent=True) or {}
    fp = InputSanitizer.sanitize_string(data.get("fingerprint_hash") or "", 128)
    if not fp:
        return jsonify({"exists": False}), 200
    user = _users().find_one({"fingerprints": fp, "is_deleted": {"$ne": True}}, {"email": 1})
    if user:
        return jsonify({"exists": True, "email": user.get("email")}), 200
    return jsonify({"exists": False}), 200


@auth_bp.route("/check-email", methods=["POST"])
def check_email():
    """What the email-first sign-in screen needs to offer the right next step:
    does the address have an account, and how does that account sign in."""
    wait = _throttle("check-email", 30)
    if wait:
        return _too_many(wait, "Too many requests. Please try again in a few minutes.")
    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    if not email:
        return jsonify({"exists": False}), 200
    user = _users().find_one({"email": email})
    if not user:
        return jsonify({"exists": False}), 200
    if user.get("is_deleted"):
        return jsonify({"exists": True, "closed": True, "methods": []}), 200
    methods = _sign_in_methods(user)
    return jsonify({"exists": True, "has_password": "password" in methods,
                    "methods": methods}), 200


# `/validate-password` was removed on 2026-11-15. It answered "is this the
# password?" for any address without touching the lockout counter, which made
# it a free password-guessing oracle. `/login` is the only route that tests a
# password, and it counts every miss.


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------
@auth_bp.route("/request-password-reset", methods=["POST"])
def request_password_reset():
    # Always return ok:true regardless of whether the email exists — never leak
    # account existence through this endpoint.
    wait = _throttle("reset-request", 10)
    if wait:
        return _too_many(wait, "Too many requests. Please try again in a few minutes.")
    data = request.get_json(force=True, silent=True) or {}
    email = InputSanitizer.sanitize_email(data.get("email", ""))
    generic = jsonify({"ok": True, "message": "If an account exists for that email, a reset link is on its way."})

    if not email:
        return generic, 200
    user = _users().find_one({"email": email}, {"_id": 1, "is_deleted": 1})
    if not user or user.get("is_deleted"):
        return generic, 200
    # Per address as well as per IP, so one inbox cannot be flooded from many.
    if _throttle("reset-email", 5, 3600, subject=_hash_token(email)[:32]):
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
    wait = _throttle("reset-password", 10)
    if wait:
        return _too_many(wait, TOO_MANY_ATTEMPTS)
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
    # Single use, decided atomically: two requests with one link, one winner.
    claimed = _reset_tokens().update_one(
        {"_id": rec["_id"], "used": False}, {"$set": {"used": True, "used_at": _now()}})
    if claimed.modified_count != 1:
        return jsonify({"error": "Invalid or expired reset link"}), 400
    user = _users().find_one({"email": email}, {"is_deleted": 1})
    if not user:
        return jsonify({"error": "Invalid or expired reset link"}), 400
    if user.get("is_deleted"):
        return _fail(403, "account_closed", ACCOUNT_CLOSED)
    # The link was read from this address's inbox, which proves the address.
    _users().update_one(
        {"email": email},
        {
            "$set": {"password_hash": User.hash_password(new_password), "updated_at": _now(),
                     "login_attempts": 0, "email_verified": True, "email_verified_at": _now()},
            "$unset": {"lockout_until": ""},
            "$addToSet": {"auth_providers": "password"},
        },
    )
    _burn_reset_tokens(email, "reset-password")
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
        wait = _throttle("set-password", 20)
        if wait:
            return _too_many(wait, TOO_MANY_ATTEMPTS)
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
    _burn_reset_tokens(email, "set-password")

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
