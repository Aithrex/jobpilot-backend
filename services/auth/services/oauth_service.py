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

Account linking (see `upsert_user`, which enforces it):
  1. match by oauth_identities.<provider>.subject  -> that user
  2. else, the address belongs to an account -> link only when the provider
     VOUCHES for the address (`email_is_vouched`) and may link by email
     (everything except Apple); otherwise the sign-in is refused
  3. else -> create a new free user, again only for a vouched address
Apple is excluded from step 2 because Apple may hand back a per-app private
relay address; we never auto-merge those into an existing account.
Closed accounts are refused on every path.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
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
        "pkce": True,
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
        "pkce": True,
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
# State: CSRF token, bound to the browser that started the flow
# ---------------------------------------------------------------------------
BROWSER_COOKIE = "aspirely_oauth"
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")


class SignInRefused(Exception):
    """A provider sign-in we will not complete. `code` goes to the frontend
    as `#error=<code>`, which maps it to a sentence."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sha256(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def clean_browser_nonce(value) -> Optional[str]:
    return value if isinstance(value, str) and _NONCE_RE.match(value) else None


def binds_browser(provider: str) -> bool:
    """Apple answers with a cross-site POST, which a SameSite=Lax cookie does
    not ride. Until Apple is switched on (it needs its own SameSite=None
    cookie), its state is not bound to the browser."""
    return PROVIDERS[provider].get("response_mode") != "form_post"


def build_authorize_url(provider: str, fingerprint_hash: Optional[str] = None,
                        browser_nonce: Optional[str] = None) -> str:
    """The provider URL to send the browser to.

    `state` alone only proves the callback belongs to SOME flow we started. An
    attacker can start one, stop before the callback, and hand the victim that
    callback URL: the victim is then signed in to the attacker's account
    (login CSRF). `browser_nonce` closes it: its hash is stored with the state
    and the callback must present the same value from a cookie only the
    starting browser holds. PKCE additionally ties the code to this flow.
    """
    p = PROVIDERS[provider]
    state = secrets.token_urlsafe(24)
    doc = {
        "state": state,
        "provider": provider,
        "fingerprint_hash": fingerprint_hash,
        "nonce_hash": _sha256(browser_nonce) if (browser_nonce and binds_browser(provider)) else None,
        "created_at": _now(),
        "expires_at": _now() + timedelta(minutes=STATE_TTL_MINUTES),
    }
    params = {
        "client_id": _client_id(provider),
        "redirect_uri": _redirect_uri(provider),
        "response_type": "code",
        "scope": p["scope"],
        "state": state,
    }
    if p.get("pkce"):
        verifier = secrets.token_urlsafe(48)
        doc["code_verifier"] = verifier
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        params["code_challenge"] = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        params["code_challenge_method"] = "S256"
    if provider == "google":
        params["access_type"] = "online"
        params["prompt"] = "select_account"
    if p.get("response_mode"):
        params["response_mode"] = p["response_mode"]
    _states().insert_one(doc)
    tenant = _cfg("MICROSOFT_TENANT") or "common"
    authorize_url = p["authorize_url"].format(tenant=tenant)
    return f"{authorize_url}?{urlencode(params)}"


def consume_state(state: str, provider: str, browser_nonce: Optional[str] = None) -> Optional[Dict]:
    """Single use. Returns the state row when it is ours, unexpired, for this
    provider and (where bound) presented by the browser that started it."""
    if not state or not isinstance(state, str):
        return None
    doc = _states().find_one_and_delete({"state": state})
    if not doc or doc.get("provider") != provider:
        return None
    exp = doc.get("expires_at")
    if exp and (exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)) < _now():
        return None
    if binds_browser(provider):
        expected = doc.get("nonce_hash")
        if not expected or not browser_nonce \
                or not hmac.compare_digest(expected, _sha256(browser_nonce)):
            return None
    return doc


# ---------------------------------------------------------------------------
# Token exchange + identity
# ---------------------------------------------------------------------------
def exchange_code(provider: str, code: str, code_verifier: Optional[str] = None) -> Dict:
    p = PROVIDERS[provider]
    tenant = _cfg("MICROSOFT_TENANT") or "common"
    data = {
        "code": code,
        "client_id": _client_id(provider),
        "client_secret": _client_secret(provider),
        "redirect_uri": _redirect_uri(provider),
        "grant_type": "authorization_code",
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
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


def _truthy(value) -> bool:
    return value is True or str(value).lower() in ("true", "1")


def email_is_vouched(provider: str, info: Dict, claims: Dict) -> bool:
    """Did the provider itself confirm the person receives mail at `email`?

    Google, LinkedIn and Apple say so with `email_verified`. Microsoft does
    not: the `email` claim of a work or school account is whatever its tenant
    administrator typed, so anyone with their own tenant can present any
    address. Microsoft's statement that the address's domain owner verified it
    is the optional claim `xms_edov`; without it the address is unproven.
    """
    if provider == "microsoft":
        return _truthy(claims.get("xms_edov"))
    return _truthy(info.get("email_verified"))


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
    claims: Dict = _decode_id_token(id_token) if id_token else {}
    for k in ("sub", "email", "email_verified", "name"):
        if info.get(k) is None and claims.get(k) is not None:
            info[k] = claims.get(k)

    email = (info.get("email") or "").strip().lower() or None
    name = info.get("name") or info.get("given_name")
    subject = info.get("sub")
    return {"subject": str(subject) if subject is not None else None, "email": email,
            "email_verified": bool(email) and email_is_vouched(provider, info, claims),
            "name": name}


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
def _account_email_proven(user: Dict) -> bool:
    if user.get("email_verified") is True:
        return True
    for identity in (user.get("oauth_identities") or {}).values():
        if isinstance(identity, dict) and identity.get("email_verified") is True \
                and identity.get("email") == user.get("email"):
            return True
    return False


def upsert_user(provider: str, identity: Dict) -> str:
    """Sign in, link or create from a provider identity. Returns the account
    email (the JWT identity). Raises SignInRefused with the reason otherwise.

    The rules, in order:
      1. The provider subject is already linked -> that account.
      2. The address belongs to an existing account -> link ONLY when the
         provider vouches for the address and may link by email (never Apple).
         Otherwise refuse: linking on an unproven address hands the account to
         whoever typed it into a provider profile.
      3. No account -> create one, again only for a vouched address. An
         account created on an unproven address would be an open door the
         moment its real owner signs in and is linked into it.
    A closed account is refused on every path.
    """
    subject = identity.get("subject")
    email = identity.get("email")
    vouched = bool(identity.get("email_verified"))
    if not subject:
        raise SignInRefused("oauth_failed")

    users = _users()
    now = _now()
    subject_field = f"oauth_identities.{provider}.subject"
    identity_doc = {
        "subject": subject,
        "email": email,
        "email_verified": vouched,
        "linked_at": now,
    }

    # 1. Existing link by provider subject.
    existing = users.find_one({subject_field: subject})
    if existing:
        if existing.get("is_deleted"):
            raise SignInRefused("account_closed")
        identity_doc["linked_at"] = (existing.get("oauth_identities") or {}).get(provider, {}).get("linked_at") or now
        users.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {f"oauth_identities.{provider}": identity_doc, "last_login": now, "updated_at": now},
                "$addToSet": {"auth_providers": provider},
            },
        )
        return existing["email"]

    if not email:
        logger.warning("%s sign-in returned no email; cannot create account", provider)
        raise SignInRefused("no_email")

    # 2. An account already owns this address.
    by_email = users.find_one({"email": email})
    if by_email:
        if by_email.get("is_deleted"):
            raise SignInRefused("account_closed")
        if not vouched:
            raise SignInRefused("email_unverified")
        if not PROVIDERS[provider].get("auto_link_by_email"):
            raise SignInRefused("link_not_allowed")
        update = {
            "$set": {f"oauth_identities.{provider}": identity_doc, "last_login": now,
                     "updated_at": now, "email_verified": True, "email_verified_at": now},
            "$addToSet": {"auth_providers": provider},
        }
        # A password on an account nobody had proven was set by someone who
        # never showed they own the address (anyone could register it before
        # 2026-11). The provider has now vouched for the real owner, so that
        # password stops working; they can set a new one in Settings.
        strip_password = bool(by_email.get("password_hash")) and not _account_email_proven(by_email)
        if strip_password:
            update["$unset"] = {"password_hash": ""}
        users.update_one({"_id": by_email["_id"]}, update)
        if strip_password:
            users.update_one({"_id": by_email["_id"]}, {"$pull": {"auth_providers": "password"}})
            try:
                DBConnect().get_db().password_reset_tokens.update_many(
                    {"user_email": email, "used": False},
                    {"$set": {"used": True, "used_at": now, "invalidated_by": "provider-link"}})
            except Exception as e:  # pragma: no cover - best effort
                logger.warning("reset-token invalidation failed: %s", type(e).__name__)
        return by_email["email"]

    # 3. Create a new free user.
    if not vouched:
        raise SignInRefused("email_unverified")

    from services import entitlements_service
    doc = {
        "email": email,
        "name": identity.get("name"),
        "plan": entitlements_service.DEFAULT_PLAN,
        "plan_since": now,
        "auth_providers": [provider],
        "oauth_identities": {provider: identity_doc},
        "email_verified": True,
        "email_verified_at": now,
        "is_admin": email in {"harshith@aithrex.tech", "mannesiddardha@gmail.com"},
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "last_login": now,
    }
    try:
        users.insert_one(doc)
    except Exception as e:
        # Two callbacks for one new person (a double click): the unique index
        # let one insert through. Sign in to the row that exists.
        raced = users.find_one({subject_field: subject})
        if raced and not raced.get("is_deleted"):
            return raced["email"]
        logger.warning("%s sign-in: account insert failed: %s", provider, type(e).__name__)
        raise SignInRefused("oauth_failed")
    DBConnect().get_db().user_preferences.update_one(
        {"user_email": email},
        {"$setOnInsert": {"user_email": email, "onboarding_complete": False, "created_at": now}},
        upsert=True,
    )
    # A new account on an address erased earlier is a new relationship: drop
    # the hashed "account deleted" marker (bounce/complaint rows stay).
    # Clickwrap is not recorded here; the client shows the agreement line and
    # can PUT /api/auth/privacy {accept_terms_version, age_confirmed}.
    try:
        from services import privacy_service
        privacy_service.on_account_created(email)
    except Exception as e:
        logger.warning("%s sign-in: erasure marker clear failed: %s", provider, type(e).__name__)

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
    from utils.security import check_rate_limit, get_client_ip

    def _frontend_redirect(fragment: str):
        return redirect(f"{AppUrlsConfig.FRONTEND_URL}/oauth/callback#{fragment}")

    def _refused(code: str, provider: str):
        return _frontend_redirect(urlencode({"error": code, "provider": provider}))

    def _throttled(bucket: str, max_requests: int) -> bool:
        return check_rate_limit(f"auth:{bucket}:{get_client_ip(request)}",
                                max_requests=max_requests, window_seconds=600)

    @bp.route("/oauth/providers", methods=["GET"])
    def oauth_providers():
        return jsonify({"providers": available_providers()}), 200

    @bp.route("/oauth/<provider>/authorize", methods=["GET"])
    def oauth_authorize(provider):
        """`?redirect=1` answers with a 302 to the provider (the browser
        navigates here directly, so the binding cookie is always stored).
        Without it the answer is JSON {authorize_url}, for older clients."""
        as_redirect = request.args.get("redirect") in ("1", "true")

        def _error(code: str, status: int, **extra):
            if as_redirect:
                return _refused(code, provider if provider in PROVIDERS else "")
            return jsonify({"error": code, "code": code, **extra}), status

        if provider not in PROVIDERS:
            return _error("unknown_provider", 404)
        if not provider_configured(provider):
            return _error("provider_not_configured", 503, provider=provider)
        if _throttled("oauth-authorize", 30):
            return _error("rate_limited", 429)
        fp = (request.args.get("fingerprint_hash") or "")[:128] or None
        nonce = clean_browser_nonce(request.cookies.get(BROWSER_COOKIE)) or secrets.token_urlsafe(24)
        try:
            url = build_authorize_url(provider, fp, nonce)
        except Exception as e:
            logger.exception("authorize build failed for %s: %s", provider, e)
            return _error("authorize_failed", 500)
        resp = redirect(url) if as_redirect else jsonify({"authorize_url": url})
        resp.set_cookie(
            BROWSER_COOKIE, nonce, max_age=STATE_TTL_MINUTES * 60, httponly=True,
            secure=AppUrlsConfig.BACKEND_PUBLIC_URL.startswith("https://"),
            samesite="Lax", path="/api/auth/oauth",
        )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/oauth/<provider>/callback", methods=["GET", "POST"])
    def oauth_callback(provider):
        if provider not in PROVIDERS:
            return _refused("unknown_provider", "")
        if _throttled("oauth-callback", 60):
            return _refused("rate_limited", provider)
        # Apple posts form-encoded; others use query params on a GET redirect.
        src = request.form if request.method == "POST" else request.args
        if src.get("error"):
            # The provider's own code (access_denied, user_cancelled_authorize
            # ...), reduced to a safe token before it enters a URL.
            code = re.sub(r"[^a-z0-9_]", "", str(src.get("error")).lower())[:60] or "oauth_failed"
            return _refused(code, provider)
        code = src.get("code")
        state = consume_state(src.get("state"), provider,
                              clean_browser_nonce(request.cookies.get(BROWSER_COOKIE)))
        if not code or not state:
            return _refused("invalid_state", provider)
        try:
            tokens = exchange_code(provider, code, state.get("code_verifier"))
            identity = fetch_identity(provider, tokens)
            email = upsert_user(provider, identity)
            token = create_access_token(identity=email)
            return _frontend_redirect(f"token={token}")
        except SignInRefused as refusal:
            logger.info("%s sign-in refused: %s", provider, refusal.code)
            return _refused(refusal.code, provider)
        except Exception as e:
            logger.exception("%s sign-in failed: %s", provider, e)
            return _refused("oauth_failed", provider)
