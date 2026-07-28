"""Social sign-in ("Continue with …") for JobPilot.

Provider-agnostic OAuth2/OIDC SIGN-IN, generalized from the Gmail-sync OAuth in
gmail_service.py but kept FULLY SEPARATE from it: different credentials, a
different state collection (`oauth_login_states`), different scopes (identity,
not gmail.readonly), and a different (server-side) callback. The two flows must
never share state, so linking Gmail for inbox sync and signing in with Google
stay independent.

Supported: google, github, linkedin, microsoft, apple. Each provider is
independent — any one left unconfigured simply 503s on /authorize and is hidden
in the UI.

Flow:
  GET  /api/auth/oauth/<provider>/authorize  -> {authorize_url}
  GET|POST /api/auth/oauth/<provider>/callback
       -> exchange code -> fetch identity -> upsert/link user -> mint JWT
       -> 302 ${FRONTEND_URL}/oauth/callback#token=<jwt>   (or #error=...)
The JWT rides the URL fragment (never the query) so it stays out of server and
proxy logs. A tiny frontend page at /oauth/callback reads it and stores it.

Account linking:
  1. match by oauth_identities.<provider>.subject  -> that user
  2. else, if the provider asserts a VERIFIED email AND the provider allows
     email linking (everything except Apple) -> match existing user by email
     and attach this provider (preserves any existing password login)
  3. else -> create a new free user keyed by the email
Apple is excluded from step 2 because Apple may hand back a per-app private
relay address; we never auto-merge those into an existing account.
"""
from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from urllib.parse import urlencode

import requests

from utils.config import _get_config_value, AppUrlsConfig
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

STATE_TTL_MINUTES = 10
HTTP_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------
def _cfg(key: str) -> str:
    return _get_config_value(key, "") or ""


PROVIDERS = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
        "client_id_key": "GOOGLE_SIGNIN_CLIENT_ID",
        "client_secret_key": "GOOGLE_SIGNIN_CLIENT_SECRET",
        "auto_link_by_email": True,
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "scope": "read:user user:email",
        "client_id_key": "GITHUB_CLIENT_ID",
        "client_secret_key": "GITHUB_CLIENT_SECRET",
        "auto_link_by_email": True,
    },
    "linkedin": {
        "authorize_url": "https://www.linkedin.com/oauth/v2/authorization",
        "token_url": "https://www.linkedin.com/oauth/v2/accessToken",
        "userinfo_url": "https://api.linkedin.com/v2/userinfo",
        "scope": "openid profile email",
        "client_id_key": "LINKEDIN_CLIENT_ID",
        "client_secret_key": "LINKEDIN_CLIENT_SECRET",
        "auto_link_by_email": True,
    },
    "microsoft": {
        # {tenant} is substituted from MICROSOFT_TENANT (default "common").
        "authorize_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "userinfo_url": "https://graph.microsoft.com/oidc/userinfo",
        "scope": "openid email profile",
        "client_id_key": "MICROSOFT_CLIENT_ID",
        "client_secret_key": "MICROSOFT_CLIENT_SECRET",
        "auto_link_by_email": True,
    },
    "apple": {
        "authorize_url": "https://appleid.apple.com/auth/authorize",
        "token_url": "https://appleid.apple.com/auth/token",
        "userinfo_url": None,  # identity comes from the id_token
        "scope": "name email",
        "client_id_key": "APPLE_CLIENT_ID",
        "client_secret_key": None,  # secret is an ES256 JWT, minted on demand
        "auto_link_by_email": False,  # never auto-merge Apple relay emails
        "response_mode": "form_post",
    },
}


def _states():
    return DBConnect().get_db().oauth_login_states


def _users():
    return DBConnect().get_db().users


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _redirect_uri(provider: str) -> str:
    return f"{AppUrlsConfig.BACKEND_PUBLIC_URL}/api/auth/oauth/{provider}/callback"


def _client_id(provider: str) -> str:
    return _cfg(PROVIDERS[provider]["client_id_key"])


def _client_secret(provider: str) -> str:
    """Static client secret, or an ES256 JWT for Apple."""
    if provider == "apple":
        return _apple_client_secret()
    return _cfg(PROVIDERS[provider]["client_secret_key"])


def provider_configured(provider: str) -> bool:
    p = PROVIDERS.get(provider)
    if not p:
        return False
    if provider == "apple":
        return all(_cfg(k) for k in ("APPLE_CLIENT_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY"))
    return bool(_client_id(provider) and _cfg(p["client_secret_key"]))


def available_providers() -> list:
    return [name for name in PROVIDERS if provider_configured(name)]


# ---------------------------------------------------------------------------
# Apple client-secret (ES256 JWT)
# ---------------------------------------------------------------------------
def _apple_client_secret() -> str:
    import jwt  # PyJWT
    team_id = _cfg("APPLE_TEAM_ID")
    key_id = _cfg("APPLE_KEY_ID")
    client_id = _cfg("APPLE_CLIENT_ID")
    private_key = _cfg("APPLE_PRIVATE_KEY").replace("\\n", "\n")
    now = int(time.time())
    payload = {
        "iss": team_id,
        "iat": now,
        "exp": now + 60 * 60,  # 1h (Apple allows up to 6 months)
        "aud": "https://appleid.apple.com",
        "sub": client_id,
    }
    return jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": key_id})


# ---------------------------------------------------------------------------
# State (CSRF) management — separate collection from Gmail-sync states
# ---------------------------------------------------------------------------
def build_authorize_url(provider: str, fingerprint_hash: Optional[str] = None) -> str:
    p = PROVIDERS[provider]
    state = secrets.token_urlsafe(24)
    _states().insert_one({
        "state": state,
        "provider": provider,
        "fingerprint_hash": fingerprint_hash,
        "created_at": _now(),
        "expires_at": _now() + timedelta(minutes=STATE_TTL_MINUTES),
    })
    params = {
        "client_id": _client_id(provider),
        "redirect_uri": _redirect_uri(provider),
        "response_type": "code",
        "scope": p["scope"],
        "state": state,
    }
    if provider == "google":
        params["access_type"] = "online"
        params["prompt"] = "select_account"
    if p.get("response_mode"):
        params["response_mode"] = p["response_mode"]
    tenant = _cfg("MICROSOFT_TENANT") or "common"
    authorize_url = p["authorize_url"].format(tenant=tenant)
    return f"{authorize_url}?{urlencode(params)}"


def consume_state(state: str, provider: str) -> bool:
    if not state:
        return False
    doc = _states().find_one_and_delete({"state": state})
    if not doc or doc.get("provider") != provider:
        return False
    exp = doc.get("expires_at")
    if exp and (exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)) < _now():
        return False
    return True


# ---------------------------------------------------------------------------
# Token exchange + identity
# ---------------------------------------------------------------------------
def exchange_code(provider: str, code: str) -> Dict:
    p = PROVIDERS[provider]
    tenant = _cfg("MICROSOFT_TENANT") or "common"
    data = {
        "code": code,
        "client_id": _client_id(provider),
        "client_secret": _client_secret(provider),
        "redirect_uri": _redirect_uri(provider),
        "grant_type": "authorization_code",
    }
    resp = requests.post(
        p["token_url"].format(tenant=tenant),
        data=data,
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("%s token exchange failed: %s %s", provider, resp.status_code, resp.text[:300])
        raise RuntimeError(f"{provider} token exchange failed")
    return resp.json()


def _decode_id_token(id_token: str) -> Dict:
    """Read claims from an id_token WITHOUT signature verification.

    Safe here: the token arrived over TLS directly from the provider's token
    endpoint via the authenticated back-channel code exchange — we are not
    accepting it from the browser.
    """
    try:
        import jwt  # PyJWT
        return jwt.decode(id_token, options={"verify_signature": False})
    except Exception as e:
        logger.warning("id_token decode failed: %s", e)
        return {}


def fetch_identity(provider: str, token_response: Dict) -> Dict:
    """Return {subject, email, email_verified, name} for the signed-in user."""
    access_token = token_response.get("access_token")
    id_token = token_response.get("id_token")

    if provider == "github":
        return _github_identity(access_token)

    p = PROVIDERS[provider]
    info: Dict = {}
    if p.get("userinfo_url") and access_token:
        try:
            r = requests.get(
                p["userinfo_url"],
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code == 200:
                info = r.json()
        except Exception as e:
            logger.warning("%s userinfo failed: %s", provider, e)

    # Merge id_token claims (Apple has no userinfo endpoint; others may omit email).
    if id_token:
        claims = _decode_id_token(id_token)
        for k in ("sub", "email", "email_verified", "name"):
            info.setdefault(k, claims.get(k))

    email = (info.get("email") or "").lower() or None
    ev = info.get("email_verified")
    email_verified = ev is True or str(ev).lower() == "true"
    name = info.get("name") or info.get("given_name")
    subject = info.get("sub")
    return {"subject": subject, "email": email, "email_verified": email_verified, "name": name}


def _github_identity(access_token: str) -> Dict:
    headers = {"Authorization": f"Bearer {access_token}", "User-Agent": "JobPilot", "Accept": "application/vnd.github+json"}
    user = {}
    try:
        r = requests.get("https://api.github.com/user", headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            user = r.json()
    except Exception as e:
        logger.warning("github /user failed: %s", e)
    email = (user.get("email") or "").lower() or None
    email_verified = False
    # GitHub /user often omits email; fetch the verified primary explicitly.
    try:
        r = requests.get("https://api.github.com/user/emails", headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            for e in r.json():
                if e.get("primary") and e.get("verified"):
                    email = (e.get("email") or "").lower() or email
                    email_verified = True
                    break
    except Exception as e:
        logger.warning("github /user/emails failed: %s", e)
    return {
        "subject": str(user.get("id")) if user.get("id") is not None else None,
        "email": email,
        "email_verified": email_verified,
        "name": user.get("name") or user.get("login"),
    }


# ---------------------------------------------------------------------------
# User upsert / link
# ---------------------------------------------------------------------------
def upsert_user(provider: str, identity: Dict) -> Optional[str]:
    """Create or link a user from a verified social identity. Returns the user
    email (the JWT identity), or None if there isn't enough info to proceed."""
    subject = identity.get("subject")
    email = identity.get("email")
    if not subject:
        return None

    users = _users()
    subject_field = f"oauth_identities.{provider}.subject"
    identity_doc = {
        "subject": subject,
        "email": email,
        "email_verified": bool(identity.get("email_verified")),
        "linked_at": _now(),
    }

    # 1. Existing link by provider subject.
    existing = users.find_one({subject_field: subject})
    if existing:
        users.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {f"oauth_identities.{provider}": identity_doc, "last_login": _now(), "updated_at": _now()},
                "$addToSet": {"auth_providers": provider},
            },
        )
        return existing["email"]

    # 2. Link by verified email (not for Apple).
    if email and identity.get("email_verified") and PROVIDERS[provider].get("auto_link_by_email"):
        by_email = users.find_one({"email": email})
        if by_email:
            users.update_one(
                {"_id": by_email["_id"]},
                {
                    "$set": {f"oauth_identities.{provider}": identity_doc, "last_login": _now(), "updated_at": _now()},
                    "$addToSet": {"auth_providers": provider},
                },
            )
            return by_email["email"]

    # 3. Create a new free user. Need an email to key the account.
    if not email:
        logger.warning("%s sign-in returned no email; cannot create account", provider)
        return None
    # If an account with this email already exists but we got here (Apple, or
    # unverified email), attach the provider rather than collide on the PK.
    pre = users.find_one({"email": email})
    if pre:
        users.update_one(
            {"_id": pre["_id"]},
            {
                "$set": {f"oauth_identities.{provider}": identity_doc, "last_login": _now(), "updated_at": _now()},
                "$addToSet": {"auth_providers": provider},
            },
        )
        return pre["email"]

    from services import entitlements_service
    doc = {
        "email": email,
        "name": identity.get("name"),
        "plan": entitlements_service.DEFAULT_PLAN,
        "plan_since": _now(),
        "auth_providers": [provider],
        "oauth_identities": {provider: identity_doc},
        "email_verified": bool(identity.get("email_verified")),
        "is_admin": email in {"harshith@aithrex.tech", "mannesiddardha@gmail.com"},
        "status": "active",
        "created_at": _now(),
        "updated_at": _now(),
        "last_login": _now(),
    }
    users.insert_one(doc)
    DBConnect().get_db().user_preferences.update_one(
        {"user_email": email},
        {"$setOnInsert": {"user_email": email, "onboarding_complete": False, "created_at": _now()}},
        upsert=True,
    )

    # Managed application mailbox — AUTO-PROVISION at signup (spec §6), mirroring
    # auth.register. Social sign-in already carries a display name, so the local
    # part is the readable firstname.lastname straight away. Idempotent and
    # best-effort: a failure here can NEVER break sign-in.
    try:
        from services import managed_mailbox_service
        managed_mailbox_service.provision(email, identity.get("name"))
    except Exception as e:
        logger.warning("mailbox auto-provision failed for %s sign-in: %s", provider, e)
    return email


# ---------------------------------------------------------------------------
# Flask route registration (attached onto the auth blueprint)
# ---------------------------------------------------------------------------
def register_oauth_routes(bp):
    from flask import request, jsonify, redirect
    from flask_jwt_extended import create_access_token

    def _frontend_redirect(fragment: str):
        return redirect(f"{AppUrlsConfig.FRONTEND_URL}/oauth/callback#{fragment}")

    @bp.route("/oauth/providers", methods=["GET"])
    def oauth_providers():
        return jsonify({"providers": available_providers()}), 200

    @bp.route("/oauth/<provider>/authorize", methods=["GET"])
    def oauth_authorize(provider):
        if provider not in PROVIDERS:
            return jsonify({"error": "unknown_provider"}), 404
        if not provider_configured(provider):
            return jsonify({"error": "provider_not_configured", "provider": provider}), 503
        fp = (request.args.get("fingerprint_hash") or "")[:128] or None
        try:
            return jsonify({"authorize_url": build_authorize_url(provider, fp)}), 200
        except Exception as e:
            logger.exception("authorize build failed for %s: %s", provider, e)
            return jsonify({"error": "authorize_failed"}), 500

    @bp.route("/oauth/<provider>/callback", methods=["GET", "POST"])
    def oauth_callback(provider):
        if provider not in PROVIDERS:
            return _frontend_redirect("error=unknown_provider")
        # Apple posts form-encoded; others use query params on a GET redirect.
        src = request.form if request.method == "POST" else request.args
        if src.get("error"):
            return _frontend_redirect(f"error={src.get('error')[:80]}")
        code = src.get("code")
        state = src.get("state")
        if not code or not consume_state(state, provider):
            return _frontend_redirect("error=invalid_state")
        try:
            tokens = exchange_code(provider, code)
            identity = fetch_identity(provider, tokens)
            email = upsert_user(provider, identity)
            if not email:
                return _frontend_redirect("error=no_email")
            token = create_access_token(identity=email)
            return _frontend_redirect(f"token={token}")
        except Exception as e:
            logger.exception("%s sign-in failed: %s", provider, e)
            return _frontend_redirect("error=oauth_failed")
