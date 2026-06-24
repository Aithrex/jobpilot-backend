"""GitHub "Connect & act" data-integration.

Lets a logged-in JobPilot user connect their GitHub account so the app can act
ON THEIR BEHALF (create repos, push files) — this is NOT scraping and is NOT the
sign-in OAuth in ``oauth_service.py``. It mirrors the data-provider pattern in
``gmail_service.py``:

  - Separate credentials (``GITHUB_CONNECT_*`` falling back to ``GITHUB_*``),
    separate scopes (``read:user public_repo``), and a dedicated redirect URI
    under the already-edge-routed ``/api/resume`` prefix.
  - The OAuth access token is encrypted at rest with Fernet and stored in a
    per-provider collection (``github_connections``), keyed by JobPilot
    ``user_email``.
  - A short-lived CSRF state doc (``connect_oauth_states``, TTL ~10min) binds
    the browser-redirect callback back to the user who started the flow — the
    callback itself carries no JWT.

Every action (create repo, push file, publish project) is HUMAN-APPROVED: it
only runs in response to an explicit user request from the UI, never
automatically.
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

PROVIDER = "github"

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_BASE = "https://api.github.com"
GITHUB_SCOPE = "read:user public_repo"

STATE_TTL_MINUTES = 10
HTTP_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Config helpers (separate creds from the sign-in OAuth; fall back to shared)
# ---------------------------------------------------------------------------

def _client_id() -> str:
    return _get_config_value("GITHUB_CONNECT_CLIENT_ID", "") or _get_config_value("GITHUB_CLIENT_ID", "")


def _client_secret() -> str:
    return _get_config_value("GITHUB_CONNECT_CLIENT_SECRET", "") or _get_config_value("GITHUB_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    return f"{AppUrlsConfig.BACKEND_PUBLIC_URL}/api/resume/connect/{PROVIDER}/callback"


def is_configured() -> bool:
    return bool(_client_id() and _client_secret())


# ---------------------------------------------------------------------------
# Token encryption (mirrors gmail_service._encryption_key / _fernet)
# ---------------------------------------------------------------------------

def _encryption_key() -> bytes:
    """Fernet key derived from INTEGRATION_TOKEN_ENCRYPTION_KEY (falling back to
    GMAIL_TOKEN_ENCRYPTION_KEY). Accepts a raw 44-char urlsafe-base64 Fernet key
    or any string (hashed to 32 bytes then urlsafe-base64-encoded)."""
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
    return DBConnect().get_db().github_connections


def _states():
    return DBConnect().get_db().connect_oauth_states


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# OAuth state (CSRF) — shared collection across connect providers
# ---------------------------------------------------------------------------

def build_authorize_url(user_email: str) -> str:
    """Generate a GitHub authorize URL bound to a one-time, user-scoped state."""
    state = secrets.token_urlsafe(24)
    _states().insert_one({
        "state": state,
        "provider": PROVIDER,
        "user_email": user_email,
        "created_at": _now(),
        "expires_at": _now() + timedelta(minutes=STATE_TTL_MINUTES),
    })
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "scope": GITHUB_SCOPE,
        "state": state,
        "allow_signup": "false",
    }
    return f"{GITHUB_AUTH_URL}?{urlencode(params)}"


def _consume_state(state: str) -> Optional[str]:
    """Validate + burn a one-time state. Returns the user_email it was issued to."""
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

def _exchange_code(code: str) -> str:
    resp = requests.post(
        GITHUB_TOKEN_URL,
        data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("GitHub token exchange failed: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError("Failed to exchange GitHub authorization code")
    body = resp.json()
    token = body.get("access_token")
    if not token:
        logger.warning("GitHub token exchange returned no access_token: %s", str(body)[:200])
        raise RuntimeError("GitHub did not return an access token")
    return token


def _gh_user(access_token: str) -> Dict[str, Any]:
    resp = requests.get(
        f"{GITHUB_API_BASE}/user",
        headers=_auth_headers(access_token),
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning("GitHub /user failed: %s %s", resp.status_code, resp.text[:200])
        return {}
    return resp.json()


def handle_callback(code: str, state: str) -> str:
    """Resolve the state→user, exchange the code, store the encrypted token +
    github login for that user. Returns the resolved user_email."""
    user_email = _consume_state(state)
    if not user_email:
        raise RuntimeError("invalid_state")
    token = _exchange_code(code)
    info = _gh_user(token)
    login = info.get("login") or ""

    encrypted = _fernet().encrypt(token.encode("utf-8")).decode("utf-8")
    _connections().update_one(
        {"user_email": user_email},
        {"$set": {
            "user_email": user_email,
            "access_token_enc": encrypted,
            "login": login,
            "scopes": GITHUB_SCOPE.split(),
            "linked_at": _now(),
        }},
        upsert=True,
    )
    return user_email


def get_status(user_email: str) -> Dict[str, Any]:
    conn = _connections().find_one({"user_email": user_email})
    if not conn:
        return {"connected": False}
    return {"connected": True, "login": conn.get("login") or None}


def disconnect(user_email: str) -> bool:
    doc = _connections().find_one_and_delete({"user_email": user_email})
    return bool(doc)


# ---------------------------------------------------------------------------
# Authenticated API access
# ---------------------------------------------------------------------------

def _auth_headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "JobPilot",
    }


def _get_connection(user_email: str) -> Dict[str, Any]:
    conn = _connections().find_one({"user_email": user_email})
    if not conn:
        raise RuntimeError("GitHub is not connected")
    return conn


def _access_token(conn: Dict[str, Any]) -> str:
    return _fernet().decrypt(conn["access_token_enc"].encode("utf-8")).decode("utf-8")


# ---------------------------------------------------------------------------
# Actions (human-approved only)
# ---------------------------------------------------------------------------

def create_repo(user_email: str, name: str, description: str, private: bool = False) -> Dict[str, Any]:
    conn = _get_connection(user_email)
    token = _access_token(conn)
    resp = requests.post(
        f"{GITHUB_API_BASE}/user/repos",
        headers=_auth_headers(token),
        json={
            "name": name,
            "description": (description or "")[:350],
            "private": bool(private),
            "auto_init": False,
        },
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        logger.warning("GitHub create_repo failed: %s %s", resp.status_code, resp.text[:300])
        if resp.status_code == 422:
            raise RuntimeError("A repository with that name already exists")
        raise RuntimeError("Failed to create GitHub repository")
    repo = resp.json()
    return {
        "login": repo.get("owner", {}).get("login") or conn.get("login"),
        "name": repo.get("name") or name,
        "repo_url": repo.get("html_url"),
        "default_branch": repo.get("default_branch") or "main",
    }


def push_file(user_email: str, repo: str, path: str, content: str, message: str) -> Dict[str, Any]:
    conn = _get_connection(user_email)
    token = _access_token(conn)
    login = conn.get("login")
    if not login:
        login = (_gh_user(token).get("login") or "")
    if not login:
        raise RuntimeError("Could not resolve GitHub account login")

    b64 = base64.b64encode((content or "").encode("utf-8")).decode("utf-8")
    url = f"{GITHUB_API_BASE}/repos/{login}/{repo}/contents/{path.lstrip('/')}"
    resp = requests.put(
        url,
        headers=_auth_headers(token),
        json={"message": (message or "Add file")[:200], "content": b64},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code not in (200, 201):
        logger.warning("GitHub push_file failed (%s): %s %s", path, resp.status_code, resp.text[:300])
        raise RuntimeError(f"Failed to push file: {path}")
    body = resp.json()
    return {"path": path, "html_url": (body.get("content") or {}).get("html_url")}


def publish_project(user_email: str, name: str, description: str, files: Dict[str, str]) -> Dict[str, Any]:
    """Create a repo then push each file. ``files`` maps repo-relative path → content.
    Returns {repo_url}."""
    if not files:
        raise RuntimeError("No files to publish")
    repo_meta = create_repo(user_email, name, description, private=False)
    repo_name = repo_meta["name"]
    pushed = []
    for path, content in files.items():
        push_file(user_email, repo_name, path, content, f"Add {path}")
        pushed.append(path)
    return {"repo_url": repo_meta["repo_url"], "files": pushed}
