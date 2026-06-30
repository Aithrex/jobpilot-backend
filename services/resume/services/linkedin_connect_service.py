"""LinkedIn "Connect & act" data-integration.

Lets a logged-in JobPilot user connect their LinkedIn account so the app can
PUBLISH POSTS on their behalf (via the official "Share on LinkedIn" product) —
this is NOT scraping and is NOT the sign-in OAuth in ``oauth_service.py``. It
mirrors the data-provider pattern in ``gmail_service.py`` and the sibling
``github_connect_service.py``:

  - Separate credentials (``LINKEDIN_CONNECT_*`` falling back to ``LINKEDIN_*``),
    its own scopes (``openid profile w_member_social``), and a dedicated redirect
    URI under the already-edge-routed ``/api/resume`` prefix.
  - The OAuth access token is encrypted at rest with Fernet and stored in a
    per-provider collection (``linkedin_connections``), keyed by JobPilot
    ``user_email``, alongside the member ``sub`` and token expiry.
  - A short-lived CSRF state doc (``connect_oauth_states``, TTL ~10min) binds
    the browser-redirect callback back to the user who started the flow.

Posting is HUMAN-APPROVED: it only runs in response to an explicit user request
from the UI, never automatically.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

from utils.config import _get_config_value, AppUrlsConfig
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)

PROVIDER = "linkedin"

LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
LINKEDIN_UGC_URL = "https://api.linkedin.com/v2/ugcPosts"
LINKEDIN_SCOPE = "openid profile w_member_social"

STATE_TTL_MINUTES = 10
HTTP_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Config helpers (separate creds from sign-in OAuth; fall back to shared)
# ---------------------------------------------------------------------------

def _client_id() -> str:
    return _get_config_value("LINKEDIN_CONNECT_CLIENT_ID", "") or _get_config_value("LINKEDIN_CLIENT_ID", "")


def _client_secret() -> str:
    return _get_config_value("LINKEDIN_CONNECT_CLIENT_SECRET", "") or _get_config_value("LINKEDIN_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    return f"{AppUrlsConfig.BACKEND_PUBLIC_URL}/api/resume/connect/{PROVIDER}/callback"


def is_configured() -> bool:
    return bool(_client_id() and _client_secret())


# ---------------------------------------------------------------------------
# Token encryption (mirrors github_connect_service / gmail_service)
# ---------------------------------------------------------------------------

def _encryption_key() -> bytes:
    raw = _get_config_value("INTEGRATION_TOKEN_ENCRYPTION_KEY", "") or _get_config_value("GMAIL_TOKEN_ENCRYPTION_KEY", "")
    if not raw:
        raise RuntimeError("INTEGRATION_TOKEN_ENCRYPTION_KEY (or GMAIL_TOKEN_ENCRYPTION_KEY) is not configured")
    if len(raw) == 44 and raw.endswith("="):
        return raw.encode("utf-8")
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet():
    from cryptography.fernet import Fernet  # lazy import — only for connect flows
    return Fernet(_encryption_key())


# ---------------------------------------------------------------------------
# Mongo collections
# ---------------------------------------------------------------------------

def _connections():
    return DBConnect().get_db().linkedin_connections


def _states():
    return DBConnect().get_db().connect_oauth_states


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# OAuth state (CSRF) — shared collection across connect providers
# ---------------------------------------------------------------------------

def build_authorize_url(user_email: str) -> str:
    state = secrets.token_urlsafe(24)
    _states().insert_one({
        "state": state,
        "provider": PROVIDER,
        "user_email": user_email,
        "created_at": _now(),
        "expires_at": _now() + timedelta(minutes=STATE_TTL_MINUTES),
    })
    params = {
        "response_type": "code",
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": LINKEDIN_SCOPE,
        "state": state,
    }
    return f"{LINKEDIN_AUTH_URL}?{urlencode(params)}"


def _consume_state(state: str) -> Optional[str]:
    if not state:
        return None
    doc = _states().find_one_and_delete({"state": state, "provider": PROVIDER})
    if not doc:
        return None
    exp = doc.get("expires_at")
    if exp and (exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)) < _now():
        return None
    return doc.get("user_email")


# ---------------------------------------------------------------------------
# Token exchange + connection storage
# ---------------------------------------------------------------------------

def _exchange_code(code: str) -> Dict[str, Any]:
    resp = requests.post(
        LINKEDIN_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("LinkedIn token exchange failed: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError("Failed to exchange LinkedIn authorization code")
    return resp.json()


def _userinfo(access_token: str) -> Dict[str, Any]:
    resp = requests.get(
        LINKEDIN_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("LinkedIn userinfo failed: %s %s", resp.status_code, resp.text[:200])
        return {}
    return resp.json()


def handle_callback(code: str, state: str) -> str:
    """Resolve state→user, exchange code, fetch member sub, store encrypted token."""
    user_email = _consume_state(state)
    if not user_email:
        raise RuntimeError("invalid_state")
    token_resp = _exchange_code(code)
    access_token = token_resp.get("access_token")
    if not access_token:
        raise RuntimeError("LinkedIn did not return an access token")
    expires_in = int(token_resp.get("expires_in") or 0)
    expires_at = _now() + timedelta(seconds=expires_in) if expires_in else None

    info = _userinfo(access_token)
    member_sub = info.get("sub") or ""
    name = info.get("name") or ""
    if not member_sub:
        raise RuntimeError("Could not resolve LinkedIn member identity")

    encrypted = _fernet().encrypt(access_token.encode("utf-8")).decode("utf-8")
    _connections().update_one(
        {"user_email": user_email},
        {"$set": {
            "user_email": user_email,
            "access_token_enc": encrypted,
            "member_sub": member_sub,
            "name": name,
            "scopes": LINKEDIN_SCOPE.split(),
            "expires_at": expires_at,
            "linked_at": _now(),
        }},
        upsert=True,
    )
    return user_email


def get_status(user_email: str) -> Dict[str, Any]:
    conn = _connections().find_one({"user_email": user_email})
    if not conn:
        return {"connected": False}
    return {"connected": True, "name": conn.get("name") or None}


def disconnect(user_email: str) -> bool:
    doc = _connections().find_one_and_delete({"user_email": user_email})
    return bool(doc)


# ---------------------------------------------------------------------------
# Authenticated access
# ---------------------------------------------------------------------------

def _get_connection(user_email: str) -> Dict[str, Any]:
    conn = _connections().find_one({"user_email": user_email})
    if not conn:
        raise RuntimeError("LinkedIn is not connected")
    exp = conn.get("expires_at")
    if exp and (exp if getattr(exp, "tzinfo", None) else exp.replace(tzinfo=timezone.utc)) < _now():
        raise RuntimeError("LinkedIn connection expired — please reconnect")
    return conn


def _access_token(conn: Dict[str, Any]) -> str:
    return _fernet().decrypt(conn["access_token_enc"].encode("utf-8")).decode("utf-8")


# ---------------------------------------------------------------------------
# Action (human-approved only)
# ---------------------------------------------------------------------------

def create_post(user_email: str, text: str) -> Dict[str, Any]:
    conn = _get_connection(user_email)
    token = _access_token(conn)
    sub = conn.get("member_sub")
    if not sub:
        raise RuntimeError("LinkedIn member identity missing — please reconnect")

    author = f"urn:li:person:{sub}"
    payload = {
        "author": author,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": (text or "")[:3000]},
                "shareMediaCategory": "NONE",
            }
        },
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    resp = requests.post(
        LINKEDIN_UGC_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        logger.warning("LinkedIn create_post failed: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError("Failed to publish LinkedIn post")

    post_id = resp.headers.get("x-restli-id") or resp.headers.get("X-RestLi-Id")
    if not post_id:
        try:
            post_id = resp.json().get("id")
        except Exception:
            post_id = None
    post_url = f"https://www.linkedin.com/feed/update/{post_id}" if post_id else None
    return {"post_url": post_url, "post_id": post_id}
