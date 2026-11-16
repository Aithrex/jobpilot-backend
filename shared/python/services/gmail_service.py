"""
Gmail integration — OAuth, encrypted-token storage, message sync, and
evidence-based status suggestions for application tracking.

Design notes
------------
- Uses Google's OAuth 2.0 web flow with the readonly Gmail scope.
- Refresh tokens are encrypted at rest (Fernet); the encryption key comes
  from `GMAIL_TOKEN_ENCRYPTION_KEY` (SSM/env). Access tokens are never
  persisted — they are minted on demand from the refresh token.
- Sync is incremental through a frozen date query, page cursor and completed
  watermark. The initial scan covers 60 days in bounded batches.
- One press of Sync is a RUN: batch after batch until the window is finished,
  in job slices that chain (`run_slice`). The worker's `gmail_sync` job
  (`sync_due`) does the same for every connected user on a schedule and picks
  up a run a deploy interrupted. Progress lives on the connection (`sync_run`).
- A connection Google has stopped accepting (expired after 7 days in Testing
  mode, revoked, or missing the read permission) is marked `needs_reconnect`;
  `status_payload` reports it so the client offers Reconnect.
- Every failure a user can meet is a GmailSyncError / GmailLinkError with a
  stable code and a sentence that is safe to show.
- Deterministic evidence runs first; inconclusive matched mail is retained
  for review with no invented status. Model calls are disabled by default.
  An operator can enable at most three FLASH attempts per batch; model-only
  verdicts never automatically update an application.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import math
import re
import secrets
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from utils.config import _get_config_value
from utils.db_connect import DBConnect

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OAuth + Gmail constants
# ---------------------------------------------------------------------------
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "email",
]

# Confidence at/above which we auto-apply a status change vs. surface a suggestion.
# Tightened from 0.70 → 0.85 after users reported false positives — emails being
# silently moved to "interviewing" off automated boilerplate, or "applied"
# records being left stale because per-email classification couldn't see the
# rejection that arrived later. The per-record thread classifier is the real
# accuracy fix; this raised bar just keeps borderline calls in the user's
# review queue instead of auto-applying them.
# Raised 0.85 → 0.92 — false positives are dramatically worse than slow user
# review. A wrongly-moved card ("interview" → "rejected" off a boilerplate
# phrase) erodes trust in the auto-classification. The remaining bucket
# (0.60-0.92) lands in the user-review queue where they approve/reject.
AUTO_APPLY_CONFIDENCE = 0.92

# Gmail's current quota table charges 60 units for messages.get(format=full).
# Keep each resumable batch below the 6,000-unit/min/user default, with room
# for other clients. Pacing also avoids consuming the entire allowance at once.
# https://developers.google.com/workspace/gmail/api/reference/quota
MAX_MESSAGES_PER_SYNC = 50
GMAIL_REQUEST_INTERVAL_SECONDS = 1.0
# Bump when WHAT a scan reads, or HOW it reads it, changes. A connection whose
# last finished scan is older re-reads its whole window once with the new
# rules. Safe to replay: suggestion ids are deterministic, and evidence older
# than what an application already holds never moves it.
#   2 (2026-11-16): application mail only, HTML bodies read properly, replies
#                   and the user's own sent mail left out.
#   3 (2026-11-16): interview wording must be a stated, unconditional event about
#                   the person; automatic Interviewing moves the new rules no
#                   longer support are put back (`_retract_stale_interviews`).
SCAN_VERSION = 3
# Text kept per message for the rules. A decision sentence sits near the top;
# this is room for a long greeting and a quoted job description.
BODY_TEXT_LIMIT = 6000
GMAIL_FETCH_BUDGET_SECONDS = 180.0
GMAIL_MAX_REQUEST_ATTEMPTS = 7


def _model_call_limit() -> int:
    """Rules-only by default; an operator may enable at most three cheap calls."""
    try:
        return max(0, min(3, int(_get_config_value("GMAIL_MODEL_CALLS_PER_BATCH", "0"))))
    except (TypeError, ValueError):
        return 0

# A run is one press of Sync (or one scheduled pass): batch after batch until
# the mailbox window is finished. It is cut into slices so a deploy's graceful
# stop (5 minutes) never waits on a whole backlog; each slice starts the next.
RUN_SLICE_SECONDS = 90.0
# A run with no heartbeat for this long has lost its worker. It may be taken
# over, and the per-batch lease below expires on the same clock.
RUN_STALE_SECONDS = 480
ACTIVITY_DAYS = 30
# While the Google consent screen is in "Testing", Google ends every refresh
# token 7 days after consent. See docs/ops/gmail-google-cloud.md.
TESTING_ACCESS_DAYS = 7


class GmailSyncError(RuntimeError):
    """A sync failure with a stable code and a sentence written for the user.

    str(error) is always safe to show: no provider body, no exception text.
    """
    code = "gmail_error"

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        if code:
            self.code = code


class GmailReconnectRequired(GmailSyncError):
    """Google no longer accepts the saved access. Only a new consent fixes it."""
    code = "gmail_reconnect_required"
    MESSAGES = {
        "expired_or_revoked": ("Google ended this Gmail connection, so Aspirely can no longer read "
                               "your application emails. Reconnect Gmail to continue."),
        "permission_missing": ("Aspirely was not given permission to read Gmail. Reconnect Gmail and "
                               "tick the box that lets Aspirely view your email messages."),
    }

    def __init__(self, reason: str = "expired_or_revoked"):
        self.reason = reason if reason in self.MESSAGES else "expired_or_revoked"
        super().__init__(self.MESSAGES[self.reason])


class GmailLinkError(RuntimeError):
    """A failed connect attempt. `code` is stable; str(error) is safe to show."""
    MESSAGES = {
        "state_invalid": ("This Google sign-in link has expired or was already used. "
                          "Start again from the tracker."),
        "code_rejected": ("Google did not accept this sign-in, usually because it took longer than "
                          "10 minutes or the page was reloaded. Connect Gmail again."),
        "permission_missing": ("Gmail was not connected because the box that lets Aspirely view your "
                               "email messages was not ticked on Google's screen. Connect again and tick it."),
        "no_refresh_token": ("Google did not grant ongoing access. Connect Gmail again and approve "
                             "the request on Google's screen."),
        "gmail_unavailable": "Gmail connection is not available right now. Try again later.",
    }

    def __init__(self, code: str):
        self.code = code if code in self.MESSAGES else "gmail_unavailable"
        self.http_status = 503 if self.code == "gmail_unavailable" else 400
        super().__init__(self.MESSAGES[self.code])


class GmailUndoError(RuntimeError):
    MESSAGES = {
        "not_undoable": "This update cannot be undone here. Change the status on the application instead.",
        "status_changed": ("This application's status was changed again after Gmail updated it, "
                           "so there is nothing to undo."),
    }

    def __init__(self, code: str):
        self.code = code
        super().__init__(self.MESSAGES[code])


# Connection age beyond which we ignore older messages on the very first sync.
# 60d covers the typical "applied → first-round → final-round → outcome" arc
# so users see status backfill even on records they applied to weeks ago.
INITIAL_LOOKBACK_DAYS = 60


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _client_id() -> str:
    return _get_config_value("GOOGLE_OAUTH_CLIENT_ID", "")


def _client_secret() -> str:
    return _get_config_value("GOOGLE_OAUTH_CLIENT_SECRET", "")


def _redirect_uri() -> str:
    return _get_config_value("GOOGLE_OAUTH_REDIRECT_URI", "")


def _encryption_key() -> bytes:
    """Return a Fernet-compatible key derived from the configured secret.

    Accepts either a raw 32-byte urlsafe-base64 Fernet key or any string
    (we hash it to 32 bytes and base64-urlsafe-encode). This makes local
    setup as simple as `export GMAIL_TOKEN_ENCRYPTION_KEY=anything-secret`.
    """
    raw = _get_config_value("GMAIL_TOKEN_ENCRYPTION_KEY", "")
    if not raw:
        raise RuntimeError("GMAIL_TOKEN_ENCRYPTION_KEY is not configured")
    if len(raw) == 44 and raw.endswith("="):
        return raw.encode("utf-8")
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet():
    from cryptography.fernet import Fernet  # lazy import — only loaded for Gmail flows
    return Fernet(_encryption_key())


def gmail_configured() -> bool:
    """True if the deployment can run the OAuth flow AND keep its result.

    The token encryption key is part of that: without it Connect appeared,
    the user consented at Google, and the callback then failed.
    """
    return bool(_client_id() and _client_secret() and _redirect_uri()
                and _get_config_value("GMAIL_TOKEN_ENCRYPTION_KEY", ""))


def consent_mode() -> str:
    """`testing` until the owner has published the Google consent screen.

    Google cannot be asked which it is, so the deployment says so with
    GOOGLE_OAUTH_CONSENT_MODE=production. Until then users are told the truth:
    Google ends each connection after 7 days.
    """
    raw = (_get_config_value("GOOGLE_OAUTH_CONSENT_MODE", "") or "").strip().lower()
    return "production" if raw == "production" else "testing"


def _config_int(key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(_get_config_value(key, str(default)))))
    except (TypeError, ValueError):
        return default


def auto_sync_enabled() -> bool:
    raw = (_get_config_value("GMAIL_AUTO_SYNC_ENABLED", "") or "").strip().lower()
    return raw not in _FLAG_FALSE and gmail_oauth_enabled() and gmail_configured()


def auto_sync_interval_hours() -> int:
    return _config_int("GMAIL_AUTO_SYNC_HOURS", 6, 1, 72)


_FLAG_TRUE = {"1", "true", "yes", "on"}
_FLAG_FALSE = {"0", "false", "no", "off"}


def gmail_oauth_enabled() -> bool:
    """`GMAIL_OAUTH_ENABLED` kill-switch for the `gmail.readonly` scope (Track B,
    §2.4). INDEPENDENT of credential presence: even with valid Google OAuth creds
    configured, the readonly scope stays disabled until this flag is on — so a
    single env var disables mail sync/consent without unsetting secrets.

    Unset → ON in dev, OFF in prod (the scope needs Google verification + CASA
    Tier 2 before go-live — see docs/ops/manual-steps.md), mirroring how
    `VOICE_GRAPH_ENABLED` / `MAIL_INGRESS_ENABLED` are read.
    """
    import os
    raw = (_get_config_value("GMAIL_OAUTH_ENABLED", "") or "").strip().lower()
    if raw in _FLAG_TRUE:
        return True
    if raw in _FLAG_FALSE:
        return False
    return os.getenv("ENVIRONMENT", "development").strip().lower() == "development"


# ---------------------------------------------------------------------------
# Mongo collections (created on first use)
# ---------------------------------------------------------------------------

def _connections():
    return DBConnect().get_db().gmail_connections


def _suggestions():
    return DBConnect().get_db().gmail_suggestions


def _oauth_states():
    return DBConnect().get_db().gmail_oauth_states


_INDEXES_READY = False


def _ensure_indexes() -> None:
    """Idempotent, once per process, never fatal. Abandoned consent attempts
    used to stay in gmail_oauth_states for ever; the TTL removes them."""
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    _INDEXES_READY = True
    try:
        _oauth_states().create_index("expires_at", expireAfterSeconds=0, name="ttl_expires")
        _oauth_states().create_index("state", unique=True, name="state_unique")
        _suggestions().create_index([("user_email", 1), ("auto_applied", 1), ("applied_at", -1)],
                                    name="user_auto_applied_at")
        _suggestions().create_index([("user_email", 1), ("applied", 1), ("dismissed", 1), ("created_at", -1)],
                                    name="user_pending_created_at")
    except Exception as e:  # pragma: no cover - an index must never block a connect
        logger.warning("gmail index setup skipped: %s", type(e).__name__)


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def build_auth_url(user_email: str) -> str:
    """Generate a Google authorization URL bound to a one-time state token.

    State is stored server-side so the callback can validate it without
    trusting the client. State auto-expires after 10 minutes.
    """
    _ensure_indexes()
    state = secrets.token_urlsafe(24)
    _oauth_states().insert_one({
        "state": state,
        "user_email": user_email,
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
    })
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",  # ensure refresh_token is returned even on re-link
        "state": state,
    }
    # While the consent screen is in Testing only the address the owner added
    # at Google can pass it. Preselect that account; picking another one there
    # ends on Google's "Access blocked" page.
    try:
        from services import gmail_access_service
        hint = gmail_access_service.login_hint(user_email)
        if hint:
            params["login_hint"] = hint
    except Exception as e:  # pragma: no cover - a hint must never block a connect
        logger.warning("gmail login hint skipped: %s", type(e).__name__)
    from urllib.parse import urlencode
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def consume_state(state: str) -> Optional[str]:
    """Validate and burn a one-time OAuth state. Returns the user_email it was issued to."""
    if not state:
        return None
    doc = _oauth_states().find_one_and_delete({"state": state})
    if not doc:
        return None
    if doc.get("expires_at") and doc["expires_at"].replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        return None
    return doc.get("user_email")


def exchange_code_for_tokens(code: str) -> Dict[str, Any]:
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": _redirect_uri(),
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        reason = _oauth_error(resp)
        logger.warning("Google token exchange failed: status=%s error=%s", resp.status_code, reason)
        # invalid_grant = the code expired, was reused, or belongs to another
        # redirect URI. Anything else is our configuration or Google's outage.
        raise GmailLinkError("code_rejected" if reason == "invalid_grant" else "gmail_unavailable")
    return resp.json()


def _oauth_error(resp) -> str:
    """Google's OAuth error NAME only (invalid_grant ...). Never the body."""
    try:
        value = resp.json().get("error")
    except Exception:
        return "unknown"
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,40}", value) else "unknown"


def refresh_access_token(refresh_token: str) -> str:
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        reason = _oauth_error(resp)
        logger.warning("Google refresh failed: status=%s error=%s", resp.status_code, reason)
        if reason == "invalid_grant":
            # Expired (7 days in Testing mode, 6 months unused), revoked by the
            # user in their Google account, or ended by a password change.
            raise GmailReconnectRequired("expired_or_revoked")
        if reason in {"invalid_client", "unauthorized_client"}:
            raise GmailSyncError("Gmail sync is not available right now. This is a problem on "
                                 "Aspirely's side, and your connection has not been changed.",
                                 "gmail_unavailable")
        raise GmailSyncError("Google could not confirm your Gmail connection just now. "
                             "Try again in a few minutes.", "gmail_error")
    access = resp.json().get("access_token")
    if not access:
        raise GmailSyncError("Google could not confirm your Gmail connection just now. "
                             "Try again in a few minutes.", "gmail_error")
    return access


def revoke_refresh_token(refresh_token: str) -> None:
    try:
        requests.post(GOOGLE_REVOKE_URL, data={"token": refresh_token}, timeout=10)
    except Exception:
        # Revoke is best-effort; we still drop our local copy regardless.
        pass


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

def _userinfo(access_token: str) -> Dict[str, Any]:
    resp = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if resp.status_code != 200:
        return {}
    return resp.json()


def store_connection(user_email: str, token_payload: Dict[str, Any]) -> Dict[str, Any]:
    refresh = token_payload.get("refresh_token")
    if not refresh:
        # Google won't return a refresh_token if the user previously consented
        # and we didn't pass prompt=consent. We always pass prompt=consent, so
        # the only way to land here is a misconfiguration.
        raise GmailLinkError("no_refresh_token")
    scopes = str(token_payload.get("scope") or "").split()
    if scopes and GMAIL_SCOPES[0] not in scopes:
        # Google lets the user untick each permission. Without the read scope
        # every sync would fail, so do not store a connection that cannot work.
        revoke_refresh_token(refresh)
        raise GmailLinkError("permission_missing")
    access = token_payload.get("access_token") or ""
    info = _userinfo(access) if access else {}
    gmail_address = info.get("email") or ""

    try:
        encrypted = _fernet().encrypt(refresh.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error("gmail token encryption unavailable: %s", type(e).__name__)
        raise GmailLinkError("gmail_unavailable") from e
    previous = get_connection(user_email) or {}
    # Reconnecting the SAME mailbox keeps the finished-scan watermark, so a
    # weekly reconnect does not re-read 60 days of mail. Another mailbox starts
    # from nothing.
    same_mailbox = bool(gmail_address) and previous.get("gmail_address") == gmail_address
    doc = {
        "user_email": user_email,
        "gmail_address": gmail_address,
        "refresh_token_enc": encrypted,
        "scopes": scopes,
        "linked_at": datetime.now(timezone.utc),
        "last_synced_at": previous.get("last_synced_at") if same_mailbox else None,
        "last_history_id": None,
    }
    _connections().update_one(
        {"user_email": user_email},
        {"$set": doc, "$unset": {key: "" for key in (
            "scan_next_page_token", "scan_query", "scan_before", "scan_cursor_version",
            "last_sync_summary", "sync_lock_id", "sync_lock_until", "needs_reconnect",
            "reconnect_reason", "reconnect_at", "sync_run",
        )}},
        upsert=True,
    )
    return {"gmail_address": gmail_address, "linked_at": doc["linked_at"].isoformat()}


def get_connection(user_email: str) -> Optional[Dict[str, Any]]:
    return _connections().find_one({"user_email": user_email})


def disconnect(user_email: str) -> bool:
    doc = _connections().find_one_and_delete({"user_email": user_email})
    if not doc:
        return False
    enc = doc.get("refresh_token_enc")
    if enc:
        try:
            refresh = _fernet().decrypt(enc.encode("utf-8")).decode("utf-8")
            revoke_refresh_token(refresh)
        except Exception:
            pass
    # Drop pending suggestions too — they're meaningless without a connection.
    _suggestions().delete_many({"user_email": user_email, "applied": False, "dismissed": False})
    return True


def _get_access_token(user_email: str) -> Tuple[str, Dict[str, Any]]:
    conn = get_connection(user_email)
    if not conn:
        raise GmailSyncError("Gmail is not connected.", "gmail_not_connected")
    try:
        fernet = _fernet()
    except Exception as e:
        logger.error("gmail token key unavailable: %s", type(e).__name__)
        raise GmailSyncError("Gmail sync is not available right now. This is a problem on "
                             "Aspirely's side, and your connection has not been changed.",
                             "gmail_unavailable") from e
    try:
        refresh = fernet.decrypt(str(conn.get("refresh_token_enc") or "").encode("utf-8")).decode("utf-8")
    except Exception as e:
        # The stored token cannot be read with the configured key. That is far
        # more likely our key than anything the user did, and it would hit
        # every connection at once, so it is NOT recorded as needs_reconnect:
        # restoring the key must be enough to make every connection work again.
        logger.error("gmail stored token unreadable with the configured key: %s", type(e).__name__)
        raise GmailSyncError("Gmail sync is not available right now. This is a problem on "
                             "Aspirely's side, and your connection has not been changed.",
                             "gmail_unavailable") from e
    try:
        access = refresh_access_token(refresh)
    except requests.RequestException as e:
        raise GmailSyncError("Google could not be reached just now. Try again in a few minutes.",
                             "gmail_error") from e
    return access, conn


def mark_needs_reconnect(user_email: str, reason: str) -> None:
    """Remember that only a new consent can fix this connection, so the status
    endpoint stops saying "connected" while every sync fails."""
    _connections().update_one(
        {"user_email": user_email},
        {"$set": {"needs_reconnect": True, "reconnect_reason": reason,
                  "reconnect_at": datetime.now(timezone.utc)}})


# ---------------------------------------------------------------------------
# Gmail API helpers
# ---------------------------------------------------------------------------

class GmailAPIError(RuntimeError):
    """Classified provider error; raw bodies never become client text or logs."""
    def __init__(self, status: int, body: str, retry_after_seconds: Optional[float] = None):
        self.status = status
        self.body = body
        self.retry_after_seconds = retry_after_seconds
        try:
            error = json.loads(body).get("error", {})
            if not isinstance(error, dict):
                error = {}
        except (ValueError, AttributeError):
            error = {}
        reasons = {
            item.get("reason") for key in ("errors", "details")
            for item in (error.get(key) or []) if isinstance(item, dict)
            and isinstance(item.get("reason"), str)
        }
        known_reasons = (
            "accessNotConfigured", "SERVICE_DISABLED", "insufficientPermissions",
            "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "domainPolicy", "dailyLimitExceeded",
            "userRateLimitExceeded", "rateLimitExceeded", "RATE_LIMIT_EXCEEDED",
        )
        self.reason = next((reason for reason in known_reasons if reason in reasons), "unknown")
        quota_message = str(error.get("message") or "").lower()
        # Some newer Google quota responses omit the legacy errors[].reason.
        minute_quota = "quota exceeded" in quota_message and "per minute" in quota_message
        self.rate_limited = status == 429 or (status == 403 and (
            self.reason in {"userRateLimitExceeded", "rateLimitExceeded", "RATE_LIMIT_EXCEEDED"}
            or (self.reason == "unknown" and minute_quota)))
        self.retryable = self.rate_limited or status in {500, 502, 503, 504}
        super().__init__(self.user_message())

    def user_message(self) -> str:
        if self.rate_limited:
            delay = math.ceil(self.retry_after_seconds or 60)
            return ("Gmail temporarily reached its request limit. Your connection is still active. "
                    f"Try syncing again in {delay} seconds; your previous scan position is unchanged.")
        if self.reason in {"accessNotConfigured", "SERVICE_DISABLED"}:
            return "Gmail API is unavailable for this app. The app owner needs to enable the Gmail API."
        if self.reason in {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
            return "Gmail read permission was not granted. Reconnect Gmail and allow read access."
        if self.reason == "domainPolicy":
            return "Your Google Workspace policy blocks Gmail access. Contact your administrator."
        if self.reason == "dailyLimitExceeded":
            return "The app's daily Gmail quota has been reached. Try syncing again later."
        if self.status == 401:
            return "Your Gmail authorization has expired. Reconnect Gmail to continue syncing."
        return ("Gmail could not complete this sync. Try again in a few minutes; "
                "nothing already checked is lost.")

    def as_sync_error(self) -> "GmailSyncError":
        if self.status == 401:
            return GmailReconnectRequired("expired_or_revoked")
        if self.reason in {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
            return GmailReconnectRequired("permission_missing")
        if self.rate_limited or self.reason == "dailyLimitExceeded":
            code = "gmail_rate_limited"
        elif self.reason in {"accessNotConfigured", "SERVICE_DISABLED"}:
            code = "gmail_unavailable"
        elif self.reason == "domainPolicy":
            code = "gmail_policy_blocked"
        else:
            code = "gmail_error"
        return GmailSyncError(self.user_message(), code)


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class _GmailReadBudget:
    """One bounded read window across list pages, full reads, and every retry."""
    def __init__(self):
        self.deadline = time.monotonic() + GMAIL_FETCH_BUDGET_SECONDS
        self.next_request_at = time.monotonic()

    def before_request(self, last_error: Optional[GmailAPIError]) -> float:
        now = time.monotonic()
        delay = max(0.0, self.next_request_at - now)
        if now + delay + 1 >= self.deadline:
            if last_error:
                raise last_error
            raise GmailSyncError("Gmail sync reached its time limit. Try syncing again; "
                                 "your previous scan position is unchanged.", "gmail_time_limit")
        if delay:
            time.sleep(delay)
        self.next_request_at = time.monotonic() + GMAIL_REQUEST_INTERVAL_SECONDS
        return min(15.0, self.deadline - time.monotonic())

    def retry(self, error: GmailAPIError, attempt: int) -> bool:
        delay = max(min(2 ** attempt, 32) + secrets.randbelow(1001) / 1000,
                    error.retry_after_seconds or 0.0)
        # Never shorten Retry-After to fit our budget or sleep until Lambda dies.
        if time.monotonic() + delay + 1 >= self.deadline:
            return False
        time.sleep(delay)
        return True


def _gmail_get(path: str, access_token: str, params: Optional[Dict[str, Any]] = None,
               raise_on_error: bool = False, budget: Optional[_GmailReadBudget] = None) -> Dict[str, Any]:
    budget = budget or _GmailReadBudget()
    error = None
    for attempt in range(GMAIL_MAX_REQUEST_ATTEMPTS):
        resp = requests.get(
            f"{GMAIL_API_BASE}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params or {},
            timeout=budget.before_request(error),
        )
        if resp.status_code == 200:
            return resp.json()
        error = GmailAPIError(resp.status_code, resp.text,
                              _retry_after_seconds(resp.headers.get("Retry-After")))
        if not error.retryable or attempt + 1 == GMAIL_MAX_REQUEST_ATTEMPTS or not budget.retry(error, attempt):
            break
    logger.warning("Gmail request failed status=%s reason=%s retryable=%s",
                   error.status, error.reason, error.retryable)
    if raise_on_error:
        raise error
    return {}


class _MessageBatch(list):
    """List-compatible batch with a resumable, fixed-window Gmail cursor."""

    next_page_token: Optional[str] = None
    scan_query: Optional[str] = None
    scan_before: Optional[datetime] = None
    scan_version: int = SCAN_VERSION
    # Messages left out because the user wrote them (sent, draft, chat).
    skipped_own: int = 0


def _list_message_ids(access_token: str, query: str, max_results: int,
                      page_token: Optional[str] = None, budget: Optional[_GmailReadBudget] = None) -> List[str]:
    ids = _MessageBatch()
    while len(ids) < max_results:
        params: Dict[str, Any] = {"q": query, "maxResults": min(100, max_results - len(ids)),
                                  "includeSpamTrash": "true"}
        if page_token:
            params["pageToken"] = page_token
        # raise_on_error=True so a transient 4xx/5xx surfaces as an exception in
        # sync_user — otherwise we'd swallow the failure and poison last_synced_at.
        body = _gmail_get("/users/me/messages", access_token, params=params, raise_on_error=True, budget=budget)
        for m in body.get("messages") or []:
            if m.get("id"):
                ids.append(m["id"])
        page_token = body.get("nextPageToken")
        if not page_token:
            break
    ids.next_page_token = page_token
    return ids


def _b64url_bytes(data: str) -> bytes:
    if not data:
        return b""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except Exception:
        return b""


def _decode_part(data: str, charset: str = "") -> str:
    """A part's text in the charset it declared.

    Gmail hands back the sender's bytes. Read as UTF-8 regardless, a
    Windows-1252 apostrophe became U+FFFD and "we've decided" stopped matching.
    """
    raw = _b64url_bytes(data)
    if not raw:
        return ""
    for name in (charset.strip().strip('"\'').lower(), "utf-8", "cp1252"):
        if not name:
            continue
        try:
            return raw.decode(name)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _b64url_decode(data: str) -> str:
    return _decode_part(data)


def _part_charset(part: Dict[str, Any]) -> str:
    for header in part.get("headers") or []:
        if str(header.get("name") or "").lower() == "content-type":
            found = re.search(r"charset\s*=\s*\"?([\w.:-]+)", str(header.get("value") or ""), re.I)
            return found.group(1) if found else ""
    return ""


# Markup whose CONTENT is never something a person reads. A stylesheet alone
# used to fill the whole text budget of an HTML-only email.
_HTML_UNREAD = re.compile(r"(?is)<(style|script|head|title|svg|noscript|template)\b.*?</\1\s*>|<!--.*?-->")
# Everything a mail client adds when you press Reply. What follows is an older
# message, which was read as its own email when it arrived.
_HTML_QUOTED = re.compile(
    r"(?is)<(?:div|blockquote)\b[^>]*(?:class\s*=\s*[\"'][^\"']*\b(?:gmail_quote|yahoo_quoted|moz-cite-prefix)\b"
    r"|id\s*=\s*[\"'](?:divRplyFwdMsg|appendonsend|OLK_SRC_BODY_SECTION)[\"']|type\s*=\s*[\"']cite[\"']).*")
_HTML_BREAK = re.compile(r"(?i)<\s*(?:br|hr|/p|/div|/tr|/li|/h[1-6]|/table|/blockquote|/td|/th)\b[^>]*>")
_HTML_TAG = re.compile(r"<[^>]+>")
# Zero-width and soft-hyphen characters: invisible, used as preview padding,
# and enough to break a phrase match when one lands inside a word.
_INVISIBLE = re.compile("[​‌‍‎‏⁠﻿­͏᠎؜]")
_APOSTROPHES = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "′": "'", "´": "'",
                              "“": '"', "”": '"'})
_REPLY_HEADER = re.compile(
    r"(?im)^\s*(?:on\b[^\n]{6,200}\bwrote:\s*$|on\b[^\n]{6,200}\n[^\n]{0,80}\bwrote:\s*$"
    r"|-{2,}\s*original message\s*-{2,}\s*$|_{8,}\s*\n\s*from:[^\n]*$"
    r"|from:[^\n]{1,200}\n\s*sent:[^\n]{1,200}\n\s*to:[^\n]*$)")


# Words the writer put above a quoted message before it counts as a reply.
# With nothing above it, the quote IS the message (a forward).
_OWN_WORDS_BEFORE_QUOTE = 20


def _html_to_text(markup: str) -> str:
    markup = _HTML_UNREAD.sub(" ", markup)
    quoted = _HTML_QUOTED.search(markup)
    if quoted and len(_HTML_TAG.sub(" ", markup[:quoted.start()]).strip()) >= _OWN_WORDS_BEFORE_QUOTE:
        markup = markup[:quoted.start()]
    return _HTML_TAG.sub(" ", _HTML_BREAK.sub("\n", markup))


def _readable(text: str) -> str:
    """Entities decoded, the quoted older message removed, one space between words.

    Line breaks are folded on purpose: plain-text mail wraps at about 76
    characters, so "decided to move\nforward with other candidates" has to read
    as one sentence.
    """
    text = _INVISIBLE.sub("", html.unescape(text)).translate(_APOSTROPHES)
    header = _REPLY_HEADER.search(text)
    if header and len(text[:header.start()].strip()) >= _OWN_WORDS_BEFORE_QUOTE:
        text = text[:header.start()]
        text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    return re.sub(r"\s+", " ", text).strip()


def _extract_body_text(payload: Dict[str, Any]) -> str:
    """The words of an email as its reader saw them: the plain-text part when it
    carries the message, otherwise the HTML part with its markup removed."""
    if not payload:
        return ""
    plain: List[str] = []
    marked_up: List[str] = []

    def walk(part: Dict[str, Any]) -> None:
        mime = str(part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode_part(data, _part_charset(part)))
        elif data and mime == "text/html":
            marked_up.append(_decode_part(data, _part_charset(part)))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    plain_text = _readable("\n".join(plain))
    html_text = _readable(_html_to_text("\n".join(marked_up))) if marked_up else ""
    # Some senders ship a stub plain part ("open this in an HTML viewer") beside
    # the real message. Prefer plain text unless it is clearly the stub.
    if html_text and (len(plain_text) < 200 and len(plain_text) < len(html_text) / 2):
        return html_text[:BODY_TEXT_LIMIT]
    return (plain_text or html_text)[:BODY_TEXT_LIMIT]


def _headers_dict(headers: Iterable[Dict[str, str]]) -> Dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in headers or []}


def _parse_email_address(value: str) -> Tuple[str, str]:
    """Parse 'Recruiter Name <r@co.com>' → ('Recruiter Name', 'r@co.com')."""
    if not value:
        return "", ""
    m = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", value)
    if m:
        return m.group(1).strip().strip('"'), m.group(2).strip().lower()
    return "", value.strip().lower()


# What Gmail is asked for. An email about an application says so in ordinary
# words, so the search asks for those words and nothing else is ever opened:
# no receipts, no statements, no personal mail. That is also what makes a first
# scan take minutes instead of an hour, since Google allows roughly 100 full
# reads a minute per mailbox.
_SEARCH_TERMS = (
    "application", "applications", "applied", "applying", "candidate", "candidates", "candidacy",
    "interview", "interviews", "interviewing", "interviewer", "recruiter", "recruiters",
    "recruiting", "recruitment", "hiring", "position", "role", "requisition", "offer",
    "assessment", "resume", "opportunity", "opportunities", "job", "career", "careers", "talent",
    "calendly", "availability", "onsite", "screening", '"phone screen"', '"next steps"',
    '"not selected"', '"move forward"', '"moving forward"', "hackerrank", "codility",
    "codesignal", "coderpad",
)
_EMAIL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")
# Never a reply from an employer: what the user wrote, an unsent draft, a chat.
_OWN_LABELS = {"SENT", "DRAFT", "CHAT"}


def scan_scope() -> str:
    """`focused` (application mail only) unless the operator sets
    GMAIL_SCAN_SCOPE=all, which restores the search of every email."""
    raw = (_get_config_value("GMAIL_SCAN_SCOPE", "") or "").strip().lower()
    return "all" if raw == "all" else "focused"


def _search_query(after_epoch: int, before_epoch: int, recruiter_emails: Iterable[str] = ()) -> str:
    query = (f"in:anywhere after:{max(0, after_epoch - 1)} before:{before_epoch} "
             "-in:sent -in:chats -category:promotions -category:social")
    if scan_scope() == "all":
        return query
    # A saved recruiter may write without any of the words above.
    senders = sorted({addr.lower() for addr in recruiter_emails
                      if isinstance(addr, str) and _EMAIL_ADDRESS.fullmatch(addr.strip())})[:20]
    terms = list(_SEARCH_TERMS) + [f"from:{addr}" for addr in senders]
    return query + " {" + " ".join(terms) + "}"


def fetch_recent_messages(user_email: str, since: Optional[datetime],
                          recruiter_emails: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """Return a list of normalized message dicts since the given datetime."""
    access, conn = _get_access_token(user_email)
    budget = _GmailReadBudget()
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=INITIAL_LOOKBACK_DAYS)
    # Gmail accepts unix-seconds for `after:` queries.
    after_epoch = int(since.timestamp())
    # Freeze the upper boundary across pages. New mail belongs to the NEXT
    # scan; a completed watermark must never skip an unprocessed older page.
    before = conn.get("scan_before") or datetime.now(timezone.utc).replace(microsecond=0)
    resuming = bool(conn.get("scan_query"))
    query = conn.get("scan_query") or _search_query(after_epoch, int(before.timestamp()), recruiter_emails)
    ids = _list_message_ids(access, query, MAX_MESSAGES_PER_SYNC, conn.get("scan_next_page_token"), budget)
    out = _MessageBatch()
    out.next_page_token = ids.next_page_token
    out.scan_query = query
    out.scan_before = before
    # A scan that began under older rules finishes under them; the version it
    # earns is the one its query was written with.
    out.scan_version = int(conn.get("scan_cursor_version") or 1) if resuming else SCAN_VERSION
    mailbox = str(conn.get("gmail_address") or "").strip().lower()
    for mid in ids:
        full = _gmail_get(f"/users/me/messages/{mid}", access,
                          params={"format": "full"}, raise_on_error=True, budget=budget)
        if not full:
            continue
        payload = full.get("payload") or {}
        headers = _headers_dict(payload.get("headers") or [])
        from_name, from_addr = _parse_email_address(headers.get("from", ""))
        # The search already leaves these out. Checked again on the message
        # itself, because a decision must never rest on the user's own words.
        if _OWN_LABELS.intersection(full.get("labelIds") or []) or (mailbox and from_addr == mailbox):
            out.skipped_own += 1
            continue
        subject = headers.get("subject", "")
        date_hdr = headers.get("date", "")
        snippet = full.get("snippet", "") or ""
        body = _extract_body_text(payload)
        out.append({
            "message_id": mid,
            "thread_id": full.get("threadId"),
            "from_name": from_name,
            "from_address": from_addr,
            "from_domain": from_addr.split("@")[-1] if "@" in from_addr else "",
            "subject": subject,
            "date": date_hdr,
            "internal_date": int(full.get("internalDate") or 0),
            "snippet": snippet,
            "body": body,
        })
    return out


# ---------------------------------------------------------------------------
# Matching: link a message to one of the user's tailoring records
# ---------------------------------------------------------------------------

_STATUS_KEYWORDS = {
    "rejected": [
        "we regret", "unfortunately", "not be moving forward", "decided not to move forward",
        "other candidates", "will not be progressing", "we have decided to move forward with other",
        "no longer under consideration", "did not select",
    ],
    "interviewing": [
        "schedule", "interview", "phone screen", "next round", "would you be available",
        "calendly", "available to meet", "looking forward to speaking", "panel interview",
        "technical screen", "coding interview", "onsite",
    ],
    "applied": [
        "thank you for applying", "we received your application", "your application has been received",
        "application submitted", "thanks for your interest",
    ],
    "offer": [
        "pleased to offer", "offer of employment", "we'd like to offer", "we are excited to extend",
        "offer letter",
    ],
}


def _normalize(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


def _company_token(company: str) -> str:
    """Normalize the WHOLE employer name, dropping legal suffixes only."""
    if not company or company == "Not specified":
        return ""
    s = _normalize(company)
    return re.sub(r"(?:\s+(?:inc|llc|corp|corporation|ltd|limited|co|gmbh))+$", "", s).strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {text} "


# Other jobs listed under a message are never evidence about this application.
_FOOTER_START = re.compile(
    r"\b(?:(?:recommended|suggested|similar) (?:jobs|roles|opportunities)|"
    r"jobs you (?:might|may) (?:like|be interested in)|you may also like)\b", re.IGNORECASE)
# The legal lines under a message. At the very top of an email ("View in
# browser | Unsubscribe") they are a header and the message follows them.
_LEGAL_FOOTER = re.compile(
    r"\b(?:(?:follow|connect with) us (?:on|at)|privacy policy|unsubscribe|"
    r"manage (?:email |notification )?preferences)\b", re.IGNORECASE)
_LEGAL_FOOTER_FROM = 300
_PRIMARY_EMPLOYER_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:thank you|thanks) for (?:applying to|your application (?:to|at|with))\s+(.{2,120}?)(?=[.!?;\n|]|$)",
    r"\b(?:your )?application (?:to|at|with)\s+(.{2,120}?)(?=\s+(?:(?:has been|was|is)\s+)?(?:successfully\s+)?(?:submitted|received|rejected|accepted)\b|[.!?;\n|]|$)",
)]
_PLATFORM_DOMAINS = ("linkedin.com", "indeed.com", "wellfound.com", "greenhouse-mail.io",
                     "greenhouse.io", "lever.co", "myworkdayjobs.com", "smartrecruiters.com",
                     "myworkday.com", "workday.com", "ashbyhq.com", "icims.com", "jobvite.com",
                     "workable.com", "workablemail.com", "taleo.net", "successfactors.com",
                     "oraclecloud.com", "bamboohr.com", "recruitee.com", "teamtailor.com",
                     "rippling.com", "gem.com", "eightfold.ai", "avature.net", "applytojob.com",
                     "breezy.hr", "glassdoor.com", "ziprecruiter.com", "dice.com",
                     "joinhandshake.com", "hackerrank.com", "codility.com", "codesignal.com")


# Words a hiring sender puts after the employer's name: "Acme Recruiting Team".
_SENDER_FILLER = frozenset((
    "recruiting recruitment recruiter recruiters careers career talent acquisition hiring team "
    "jobs hr people university campus early notifications notification no reply noreply do not "
    "donotreply via at the workday greenhouse lever ashby icims smartrecruiters global us usa "
    "candidate experience support staffing ta").split())


def _sender_names_company(msg: Dict[str, Any], company: str, platform_sender: bool) -> bool:
    """True when the From line itself names this employer.

    Hiring systems send as "Acme Recruiting <no-reply@greenhouse-mail.io>" or
    as acme@myworkday.com, often with a subject and body that never repeat the
    name. Read alone that email matched nothing and was dropped.
    """
    if len(company) < 2:
        return False
    name = _normalize(msg.get("from_name") or "")
    if name:
        if _company_token(msg.get("from_name") or "") == company:
            return True
        if name.startswith(company + " ") and all(
                word in _SENDER_FILLER for word in name[len(company):].split()):
            return True
        if any(name.endswith(f" {joiner} {company}") for joiner in ("at", "from", "with")):
            return True
    local = str(msg.get("from_address") or "").split("@")[0].lower()
    return platform_sender and len(company) >= 3 and re.sub(r"[^a-z0-9]", "", local) == company.replace(" ", "")


def _primary_employer_token(label: str, index: List[Dict[str, Any]]) -> str:
    """The employer a "thank you for applying to ..." phrase names, or "" when
    the phrase names a role and no employer."""
    label = label.strip()
    token = _company_token(label)
    if token in _index_facts(index)["company_tokens"]:
        return token
    # "the Data Engineer position at Acme": the employer is what follows "at".
    placed = re.match(r"^(.{2,120}?)\s+(?:at|with)\s+(.{2,80})$", label, re.IGNORECASE)
    if placed and (_ROLE_NOUN.search(placed.group(1))
                   or re.search(r"\b(?:role|position|opening|job|team)\b", placed.group(1), re.I)):
        return _company_token(placed.group(2))
    # Common subject templates append the role after the employer. Only split
    # a dash/"for" suffix when it contains a tracked role, not a company suffix.
    parts = re.split(r"\s+(?:for\s+(?:the\s+|an?\s+)?|[-–—]\s+)", label, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2 and any(_contains_phrase(_normalize(parts[1]), rec["title_norm"])
                              for rec in index if rec["title_norm"]):
        return _company_token(parts[0])
    # "the Data Engineer role" names a role and nobody. Treating it as an
    # untracked employer hid the email from every application.
    if re.match(r"^(?:the|our|a|an|this|your)\s", label, re.I) and (
            _ROLE_NOUN.search(label) or re.search(r"\b(?:role|position|opening|job)\s*$", label, re.I)):
        return ""
    return token


def _application_body(msg: Dict[str, Any]) -> str:
    """Only application content can establish identity or an outcome."""
    text = html.unescape(str(msg.get("body") or msg.get("snippet") or ""))[:12000]
    cuts = [found.start() for found in (_FOOTER_START.search(text),
                                        _LEGAL_FOOTER.search(text, _LEGAL_FOOTER_FROM)) if found]
    return text[:min(cuts)] if cuts else text


def _application_text(msg: Dict[str, Any]) -> str:
    return html.unescape(str(msg.get("subject") or "")) + "\n" + _application_body(msg)


def _primary_employers(msg: Dict[str, Any], index: List[Dict[str, Any]]) -> Optional[set]:
    subject = html.unescape(str(msg.get("subject") or ""))
    for pattern in _PRIMARY_EMPLOYER_PATTERNS:
        match = pattern.search(subject)
        token = _primary_employer_token(match.group(1), index) if match else ""
        if token:
            # Preserve even an UNTRACKED employer: never fall back to a footer
            # brand simply because the true application isn't in the tracker.
            return {token}
    # Product names can also be employers (e.g. a Guidewire Developer at Acme).
    # A company word inside the role title cannot establish the employer.
    subject_without_roles = _normalize(subject)
    for title in _index_facts(index)["titles"]:
        # A word-bounded match needs the title to be in the text at all. Testing
        # that first keeps this to the one or two titles a subject can hold;
        # compiling a pattern per tracked title made every request cost
        # (emails x applications) and timed the review queue out.
        if title in subject_without_roles:
            subject_without_roles = re.sub(r"\b" + re.escape(title) + r"\b", " ", subject_without_roles)
    remaining = _normalize(subject_without_roles)
    named = {token for token in _index_facts(index)["company_tokens"]
             if _contains_phrase(remaining, token)}
    if named:
        return named
    for pattern in _PRIMARY_EMPLOYER_PATTERNS:
        match = pattern.search(_application_body(msg))
        token = _primary_employer_token(match.group(1), index) if match else ""
        if token:
            return {token}
    return None


def _is_job_recommendation(msg: Dict[str, Any]) -> bool:
    text = _application_text(msg)
    # Actual application acknowledgements/outcomes remain valid even when the
    # platform appends ads. Conditional interest alone is never a receipt.
    if any(_scan_patterns(text, patterns) for patterns in
           (_APPLICATION_ACK_REGEX, _REJECTION_REGEX, _OFFER_REGEX, _INTERVIEW_REGEX)):
        return False
    sender = str(msg.get("from_address") or "").lower()
    return sender.endswith("@match.indeed.com") or bool(re.search(
        r"\b(?:(?:strong|great|good|top) match|(?:job|role) recommendations?|"
        r"(?:jobs|roles|opportunities) for you|job alerts?|apply now|view (?:this |the )?job|"
        r"if (?:you['’]?re|you are) interested|new jobs? (?:at|for|in|matching)|similar jobs|"
        r"jobs similar to|top job picks|job picks|recommended for you|easy apply|"
        r"based on your (?:profile|resume|search|activity)|see (?:all|more) jobs)\b",
        text, re.IGNORECASE))


_TALENT_MARKETING = re.compile(
    r"\b(?:webinar|open house|tech talk|info(?:rmation)? session|career fair|hiring event|meetup|"
    r"newsletter|talent (?:community|network)|register (?:now|here|today)|rsvp|"
    r"save your (?:seat|spot)|join us (?:for|at|on)|(?:monthly|weekly) (?:digest|update))\b",
    re.IGNORECASE)


def _is_talent_marketing(msg: Dict[str, Any]) -> bool:
    """An event invitation or a talent-community mailing: from an employer,
    full of hiring words, and nothing to do with an application's outcome."""
    text = _application_text(msg)
    if not _TALENT_MARKETING.search(text):
        return False
    return not _states_an_outcome(text)


def _states_an_outcome(text: str) -> bool:
    return any(_scan_patterns(text, patterns) for patterns in
               (_APPLICATION_ACK_REGEX, _REJECTION_REGEX, _OFFER_REGEX, _WITHDRAWN_REGEX)) or bool(
        _scan_patterns(text, _INTERVIEW_REGEX, stated=True))


# Mail a hiring portal sends that says nothing about where an application
# stands: a reminder to finish one that was never submitted, or a survey.
_HOUSEKEEPING = re.compile(
    r"\b(?:(?:finish|complete|continue|resume) your application|started an application|"
    r"application is (?:incomplete|not (?:yet )?(?:complete|submitted))|"
    r"has not (?:yet )?been submitted|candidate (?:experience )?survey|"
    r"(?:feedback on|rate|how was) your (?:interview |candidate |application )?experience)\b",
    re.IGNORECASE)


def _is_application_housekeeping(msg: Dict[str, Any]) -> bool:
    text = _application_text(msg)
    if not _HOUSEKEEPING.search(text):
        return False
    return not any(_scan_patterns(text, patterns) for patterns in
                   (_REJECTION_REGEX, _OFFER_REGEX, _WITHDRAWN_REGEX))


# An email is ABOUT an application when it uses the vocabulary of hiring. This
# is the gate between "names a company I applied to" and "is a reply to my
# application": without it every order receipt, security alert and newsletter
# from a tracked employer became a review item.
_CLEARLY_HIRING = re.compile(
    r"\b(?:candidate|candidates|candidacy|recruiter|recruiters|recruiting|recruitment|"
    r"talent acquisition|hiring (?:team|manager|process|committee)|interview(?:s|ing|er|ed)?|"
    r"phone screen|requisition|r[eé]sum[eé]|job application|offer letter|offer of employment)\b",
    re.IGNORECASE)
_APPLICATION_WORDS = re.compile(
    r"\b(?:your application|application (?:for|to|at|with|status|update|received|portal|process|"
    r"decision|submitted)|appl(?:ied|ying) (?:for|to|at|with)|thank(?:s| you) for applying)\b",
    re.IGNORECASE)
# Words a newsletter uses as freely as a recruiter does ("the role of AI",
# "a new opening"). They count only when the email also names the tracked role.
_LOOSE_HIRING_WORDS = re.compile(
    r"\b(?:position|opening|job|career|careers|role|assessment|next steps?|offer|opportunity)\b",
    re.IGNORECASE)
_NOT_HIRING = re.compile(
    r"\b(?:credit card|debit card|card application|loan|mortgage|lease|rental|apartment|"
    r"insurance (?:policy|claim|quote)|bank account|checking account|savings account|"
    r"credit (?:limit|line|score)|order (?:number|confirmation|has shipped|is on its way)|"
    r"your order|shipment|shipped|out for delivery|tracking number|invoice|billing statement|"
    r"payment (?:received|due|confirmation|method)|subscription|refund|app store|google play|"
    r"sign[- ]in (?:attempt|alert)|new sign[- ]in|security alert|two[- ]factor|"
    r"2[- ]step verification|password (?:reset|change))\b", re.IGNORECASE)


def _about_an_application(msg: Dict[str, Any]) -> str:
    """"yes", "maybe" (only loose words: the role must be named too) or ""."""
    text = _application_text(msg)
    if _CLEARLY_HIRING.search(text):
        return "yes"
    if _NOT_HIRING.search(text):
        return ""
    if _APPLICATION_WORDS.search(text):
        return "yes"
    return "maybe" if _LOOSE_HIRING_WORDS.search(text) else ""


def _is_irrelevant_mail(msg: Dict[str, Any]) -> bool:
    """Known authentication notices never describe an application outcome.

    Keep this subject-only and narrow: application portal updates, interview
    logistics and consent documents still need evidence review.
    """
    subject = html.unescape(str(msg.get("subject") or "")).strip()
    auth_notice = any(re.search(pattern, subject, re.I) for pattern in (
        r"^you shared some google account data with\b",
        r"^(?:your\s+.{1,100}\s+)?(?:verification|security|authentication|sign[- ]in|login|one[- ]time) code(?:\b.*)?$",
        r"^please confirm your identity[.!]?$",
        r"^verify your candidate account[.!]?$",
        r"^reset your password[.!]?$",
    ))
    return (auth_notice or _is_job_recommendation(msg) or _is_talent_marketing(msg)
            or _is_application_housekeeping(msg))


def _candidate_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    candidate = {key: rec.get(key, "") for key in ("record_id", "company", "title")}
    for key in ("applied_date", "created_at"):
        value = rec.get(key)
        if value:
            candidate[key] = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return candidate


class _RecordIndex(list):
    """The tracked applications, plus the lists every email is tested against.

    Still a plain list of record facts to every caller. `facts` is worked out
    once per index instead of once per email.
    """
    facts: Optional[Dict[str, List[str]]] = None


def _index_facts(index: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    cached = getattr(index, "facts", None)
    if cached is not None:
        return cached
    facts = {
        # Longest first: "senior data engineer" is removed before "data engineer".
        "titles": sorted({rec["title_norm"] for rec in index if rec.get("title_norm")},
                         key=lambda title: (-len(title), title)),
        "company_tokens": sorted({rec["company_token"] for rec in index if rec.get("company_token")}),
        "companies": sorted({rec["company"] for rec in index if rec.get("company")}),
    }
    if isinstance(index, _RecordIndex):
        index.facts = facts
    return facts


def _build_record_index(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    index = _RecordIndex()
    for r in records:
        jd = r.get("jd_analysis") or {}
        app = r.get("application") or {}
        company = jd.get("company") or ""
        title = jd.get("job_title") or ""
        recruiter_email = (app.get("recruiter_email") or "").lower()
        recruiter_domain = recruiter_email.split("@")[-1] if "@" in recruiter_email else ""
        index.append({
            "record_id": r.get("record_id"),
            "company": company,
            "company_token": _company_token(company),
            "title": title,
            "title_norm": _normalize(title),
            "requisition_ids": _requisition_ids(" ".join(str(value or "") for value in (
                app.get("job_url"), jd.get("job_url"), r.get("job_url"),
                app.get("requisition_id"), jd.get("requisition_id")))),
            "recruiter_email": recruiter_email,
            "recruiter_domain": recruiter_domain,
            "current_status": app.get("status") or "draft",
            "gmail_evidence_ms": app.get("gmail_evidence_ms") or 0,
            "status_updated_at": app.get("updated_at"),
            "gmail_updated_at": app.get("gmail_updated_at"),
            "applied_date": app.get("applied_date"),
            "created_at": r.get("created_at"),
        })
    return index


def _requisition_ids(text: str) -> set:
    ids = {re.sub(r"[^A-Z0-9]", "", match.upper()) for match in re.findall(
        r"(?<![A-Za-z0-9])(?:JR|R)[-_ ]?\d{4,}(?!\d)", text, re.I)}
    ids.update(re.findall(r"\b(?:requisition|req|job)\.?\s*(?:id|number|#)?\s*[:#-]?\s*(\d{5,})\b", text, re.I))
    return ids


_ROLE_NOUN = re.compile(r"\b(?:engineer|developer|analyst|architect|scientist|manager|designer|specialist|consultant|administrator|researcher|director|intern|technician)\b", re.I)
_EXPLICIT_ROLE_PATTERNS = [re.compile(pattern, re.I) for pattern in (
    r"\b(?:application|applying|interest)\s+(?:for|in)\s+(?:the\s+)?(.{2,140}?)(?=\s+(?:at|with)\s|\s+(?:position|role|was|has|is)\b|[.!?\n]|$)",
    r"\b(?:regarding|for)\s+(?:the\s+)?(?!applying\b)(.{2,140}?)\s+(?:role|position)\b",
    r"\b(?:regarding|for)\s+(?:the\s+)?(.{2,140}?)\s+at\s",
    r"\b(?:role|position|job title)\s*(?:of|:)\s*(.{2,140}?)(?=\s+at\s|[.!?\n]|$)",
    # Lever, Ashby: "thanks for applying to the Data Engineer role".
    r"\bappl(?:y|ying|ied)\s+(?:to|for)\s+(?:the\s+|our\s+|an?\s+)?(.{2,140}?)\s+(?:role|position|opening|job)\b",
)]


def _clean_role_title(role: str) -> str:
    """Remove ATS sentence wrappers, never role levels/specialties/locations.

    Also accepts normalized titles retained by earlier scans, so a parsing
    correction can recover manual review choices without re-fetching mail.
    """
    role = re.split(r"\b(?:thank you|thanks|please do not reply|do not reply|this is an automated)\b",
                    str(role), maxsplit=1, flags=re.I)[0]
    role = re.sub(r"^(?:(?:taking the time to )?(?:apply|applying)\s+(?:for|to)\s+)", "", role, flags=re.I)
    role = re.sub(r"^(?:your\s+)?interview logistics for\s+", "", role, flags=re.I)
    role = re.sub(r"^(?:(?:the|our|current|available)\s+)+", "", role, flags=re.I)
    role = re.sub(r"^(?:(?:req(?:uisition)?\.?\s*(?:id|number|#)?\s*[:#-]?\s*)?(?:JR|R)[- ]?\d+\s*[-:]?\s*)", "", role, flags=re.I)
    role = re.sub(r"[\[(]?\b(?:req(?:uisition)?\.?)\s*(?:id|number|#)?\s*[:#-]?\s*(?:(?:JR|R)[- ]?)?\d+\s*[\])]?", "", role, flags=re.I)
    role = re.sub(r"\s+(?:role|position|opening|open)\s*[.,:;–—-]*$", "", role.strip(), flags=re.I)
    return _normalize(role)


# Words of the sentence around a title, never of a title itself.
_ROLE_WRAPPER = re.compile(
    r"\b(?:your|you|we|our|thank|thanks|applying|applied|interest|interested|speaking|talking)\b", re.I)
_ROLE_LEAD_IN = re.compile(r".*\b(?:for|in|to|about|regarding)\s+(?:the|our|an?)\s+", re.I)
_ROLE_BARE_LEAD_IN = re.compile(r".*\b(?:for|in|to|about|regarding)\s+", re.I)


def _role_from_capture(capture: str) -> str:
    """The title inside a captured phrase.

    The role patterns are greedy about where a title starts: "thank you for
    your interest in the Data Engineer role" captures "your interest in the
    Data Engineer". Kept whole, that phantom second role made every such email
    look as if it named two roles, and nothing was ever applied.
    """
    role = _clean_role_title(capture)
    wrapped = bool(_ROLE_WRAPPER.search(role))
    for lead_in in ((_ROLE_LEAD_IN, _ROLE_BARE_LEAD_IN) if wrapped else (_ROLE_LEAD_IN,)):
        if lead_in.match(capture.strip()):
            tail = _clean_role_title(lead_in.sub("", capture.strip(), count=1))
            if tail and _ROLE_NOUN.search(tail) and not _ROLE_WRAPPER.search(tail):
                return tail
    return "" if wrapped else role


def _explicit_role_titles(msg: Dict[str, Any], index: List[Dict[str, Any]]) -> List[str]:
    """Extract complete application titles, preserving seniority and specialty.

    A generic title occurring inside a different title is not exact evidence.
    Only bounded application prose is considered, never recommendation footers.
    """
    text = _application_text(msg)
    # Req. contains a period but is not the end of a role sentence. Preserve
    # the identifier separately from the original email during matching.
    text = re.sub(r"[\[(]\s*req(?:uisition)?\.?\s*(?:id|number|#)?\s*[:#-]?\s*(?:(?:JR|R)[- ]?)?\d+\s*[\])]", "", text, flags=re.I)
    roles = []
    for pattern in _EXPLICIT_ROLE_PATTERNS:
        roles.extend(match.group(1).strip(" ,:–—-\"'") for match in pattern.finditer(text))
    # Common ATS subjects: "Acme Senior Engineer application update" and
    # "Your application to Acme - Senior Engineer".
    subject = html.unescape(str(msg.get("subject") or ""))
    title_subject = re.search(r"^(.{2,140}?)\s*[-–—:]\s*(?:application (?:decision|update)|application|decision|update)\b", subject, re.I)
    if title_subject:
        roles.append(title_subject.group(1))
    subject_folded = subject.lower()
    for company in _index_facts(index)["companies"]:
        # Both patterns hold the company name literally, so a subject without
        # it cannot match. Non-ASCII names skip the shortcut: their case rules
        # differ between str.lower() and the regex engine.
        if company.isascii() and company.lower() not in subject_folded:
            continue
        for pattern in (
            r"^" + re.escape(company) + r"\s+(.{2,140}?)\s+application\b",
            r"\bapplication to\s+" + re.escape(company) + r"\s*[-–—:]\s*(.{2,140})$",
        ):
            found = re.search(pattern, subject, re.I)
            if found:
                roles.append(found.group(1))
    cleaned = (_role_from_capture(role) for role in roles if _ROLE_NOUN.search(role))
    return list(dict.fromkeys(role for role in cleaned if role))


def _exact_role_in_text(text: str, title: str, company: str = "") -> bool:
    if not title:
        return False
    text = "\n".join(_normalize(part) for part in re.split(r"[\n.!?:]", text))
    # Fallback for nonstandard templates. Reject titles embedded in a longer
    # level/specialty phrase even when the explicit-role parser missed it.
    for match in re.finditer(r"\b" + re.escape(title) + r"\b", text):
        before = text[:match.start()].rsplit("\n", 1)[-1]
        after = text[match.end():].split("\n", 1)[0]
        if re.search(r"\b(?:senior|junior|staff|principal|lead|sr|jr|associate)\s+$", before):
            continue
        if re.match(r"\s+(?:i{1,3}|iv|[1-9]|senior|junior|lead)\b", after):
            continue
        before_word = before.strip().split()[-1:] or [""]
        after_word = after.strip().split()[:1] or [""]
        if before_word[0] not in {"", "for", "the", "of", "as", "regarding", "role", "position", "application", "a", "an", "is", "our", "your", *company.split()[-1:]}:
            continue
        if after_word[0] not in {"", "at", "with", "position", "role", "application", "we", "your", "thank", "thanks", "is", "was", "has", "have", "our", "to", "and", "the",
                                 "interview", "interviews", "opening", "job", "opportunity", "candidate",
                                 "candidacy", "phone", "onsite", "coding", "assessment", "screen"}:
            continue
        return True
    return False


def _match_message_to_record(msg: Dict[str, Any], index: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], float, str]]:
    """Pick the best record candidate for this email. Returns (record_meta, score, reason).

    A status classifier cannot establish WHICH application an email belongs
    to. Company-only mail stays a review item; automatic changes require an
    exact employer and role match with no equally plausible application.
    """
    if not index or _is_irrelevant_mail(msg):
        return None
    sender_addr = (msg.get("from_address") or "").lower()
    # Naming a company is not being about a job there. Only hiring mail, or a
    # recruiter the user saved on an application, goes any further.
    about = _about_an_application(msg)
    if not about and not any(
            rec["recruiter_email"] and rec["recruiter_email"] == sender_addr for rec in index):
        return None
    haystack = _normalize(_application_text(msg))
    primary_employers = _primary_employers(msg, index)
    explicit_roles = _explicit_role_titles(msg, index)
    email_requisitions = _requisition_ids(_application_text(msg))
    sender_domain = (msg.get("from_domain") or "").lower()
    platform_sender = any(sender_domain == domain or sender_domain.endswith("." + domain)
                          for domain in _PLATFORM_DOMAINS)

    candidates = []
    employer_records = []
    named_in_text: Dict[str, bool] = {}

    def company_is_named(company: str) -> bool:
        if len(company) < 2:
            return False
        if company in (primary_employers or set()):
            return True
        if company not in named_in_text:
            # Containment first: a tracker of several hundred applications must
            # not compile several hundred patterns for every email.
            named_in_text[company] = company in haystack and bool(re.search(
                r"(?:\b(?:at|to|with|from|company|joining|join|interest in|interested in|behalf of)\s+"
                + re.escape(company) + r"\b|\b" + re.escape(company)
                + r"\s+(?:application|careers|recruiting|recruitment|hiring|talent|team)\b)", haystack))
        return named_in_text[company]

    for rec in index:
        company = rec["company_token"]
        if primary_employers is not None and company not in primary_employers:
            continue
        company_named = company_is_named(company)
        # Exact DNS label only: Box != Dropbox, and United Airlines != United
        # Healthcare. Shared ATS/recruiter domains never establish identity.
        company_domain = not platform_sender and len(company) >= 3 and company.replace(" ", "") in sender_domain.split(".")[:-1]
        recruiter = bool(rec["recruiter_email"] and rec["recruiter_email"] == sender_addr)
        sender_named = _sender_names_company(msg, company, platform_sender)
        if not (company_named or company_domain or recruiter or sender_named):
            continue
        # A From name is the employer's own word only where the employer set
        # it: on a hiring platform, or on their own domain.
        identified = company_named or (sender_named and (platform_sender or company_domain))
        employer_records.append(rec)
        if explicit_roles and rec["title_norm"] not in explicit_roles:
            continue
        if email_requisitions and rec["requisition_ids"] and not email_requisitions.intersection(rec["requisition_ids"]):
            continue
        title_exact = (rec["title_norm"] in explicit_roles if explicit_roles
                       else _exact_role_in_text(_application_text(msg), rec["title_norm"], company))
        if about == "maybe" and not title_exact and not recruiter:
            continue
        reasons = []
        if company_named:
            reasons.append("Company identified in application content")
        if company_domain:
            reasons.append("Company name matches sender domain label")
        if recruiter:
            reasons.append("Saved recruiter email matches sender")
        if sender_named and not company_named:
            reasons.append("Sent in the company's name")
        if title_exact:
            reasons.append("Exact role title in email")
        else:
            reasons.append("Role title not confirmed; review before updating")
        # A recruiter alone can handle many unrelated companies, so needs review.
        score = 0.97 if title_exact and identified else 0.85 if title_exact else 0.6
        if email_requisitions:
            if len(email_requisitions) > 1 or not email_requisitions.intersection(rec["requisition_ids"]):
                score = min(score, 0.85)
                reasons.append("Email requisition is not uniquely confirmed in this application")
            else:
                reasons.append("Application requisition matches email")
        if len(explicit_roles) > 1:
            score = min(score, 0.85)
        candidates.append((rec, score, "; ".join(reasons)))
    if not candidates:
        if about == "yes" and employer_records and (explicit_roles or email_requisitions):
            # Keep true application mail whose role is not tracked, but never
            # offer an unrelated same-company record as a plausible match.
            meta = dict(employer_records[0])
            role = explicit_roles[0] if explicit_roles else "Application in email"
            meta.update(record_id=None, title=role, email_role=role,
                        current_status=None, candidate_records=[], requires_record_selection=True,
                        requires_application_match=True, gmail_evidence_ms=0,
                        status_updated_at=None, gmail_updated_at=None)
            return meta, 0.0, "Email role or requisition does not match a tracked application"
        return None
    candidates.sort(key=lambda item: (-item[1], str(item[0]["record_id"])))
    best_rec, score, reason = candidates[0]
    close = [item for item in candidates if score - item[1] < 0.15]
    ambiguous = len(close) > 1
    meta = dict(best_rec)
    meta["candidate_records"] = [_candidate_record(item[0]) for item in close]
    meta["requires_record_selection"] = ambiguous
    if ambiguous:
        score = min(score, 0.6)
        reason += "; Several applications match; choose the correct role"
    return meta, score, reason


# ---------------------------------------------------------------------------
# Classification: deterministic signals + LLM, reconciled
# ---------------------------------------------------------------------------
#
# Architecture
# ------------
# Pure-LLM classifiers drift on adversarial-looking-but-benign emails (e.g. an
# automated "thanks for applying — next steps if you're a fit" reads as
# "interviewing" to a small model). To get production-grade accuracy we run a
# two-tier pipeline per record:
#
#   Tier 1 — DETERMINISTIC SIGNALS (this module).
#     Regex-extracted phrases that are unambiguous in plain English: explicit
#     decline language ("we have decided to move forward with other
#     candidates"), explicit offer language ("pleased to extend an offer"),
#     scheduled-time signals (Calendly link, "are you available <date>"). When
#     a deterministic signal fires we treat it as ground truth — no LLM can
#     override a verbatim "we will not be moving forward".
#
#   Tier 2 — LLM THREAD CLASSIFIER (`classify_record_thread`).
#     Reads the full email thread for nuanced cases the regex layer can't
#     parse: politely-phrased rejections without the canonical phrase, soft
#     interview signals via context, etc. Outputs a status + confidence.
#
#   RECONCILIATION (`reconcile_classification`).
#     Combines both:
#       - deterministic rejection/offer  → final = that, conf = 0.97 (override LLM)
#       - deterministic interview + LLM agrees → final = interviewing, conf = 0.95
#       - LLM confident (>=0.85) and no contradicting deterministic signal → trust LLM
#       - everything else → suggestion for user approval (never auto-apply)
#
# This gives 100% precision on the explicit-phrase cases (the regex layer
# never produces false positives because the patterns are quoted decline
# language), with the LLM serving as a recall layer for everything else and
# the user as the safety net for ambiguity.

VALID_STATUSES = {"applied", "interviewing", "offer", "rejected", "ghosted", "withdrawn"}


# ── Deterministic signal patterns ─────────────────────────────────────────
# Each pattern is a phrase that, when present in any email body for the
# record, is taken as ground truth for that status. Patterns are quoted from
# real recruiter mail; we deliberately avoid loose patterns ("not a fit"
# alone is too risky — it can appear in a "you'd be a great fit" rebuttal).

# Pattern library curated from real recruiter mail across major ATS platforms
# (Workday, Greenhouse, Lever, Ashby, iCIMS, SmartRecruiters, Taleo, Eightfold,
# Phenom, Avature) and FAANG / unicorn careers teams. Patterns are quoted from
# templates these platforms ship by default. Each was added in response to a
# real false-negative or false-positive incident.

_REJECTION_PATTERNS = [
    # ── "moving forward with other candidates" family ────────────────────
    r"\bwe (?:have )?decided to (?:move|go) forward with other (?:candidates|applicants)\b",
    r"\b(?:decided|chosen|elected|opted) to (?:move|go|proceed) (?:forward|ahead) with (?:other|another|a different|different) (?:candidates?|applicants?)\b",
    r"\bmoving forward with (?:other|another|different) (?:candidate|applicant)",
    r"\bgoing (?:to (?:move|go) )?forward with (?:other|another) (?:candidate|applicant)",
    r"\b(?:we have )?selected (?:other|another) (?:candidate|applicant)",
    r"\bwe(?:['’]ve| have) (?:chosen|selected) (?:to (?:move|go) forward with )?(?:another|other) (?:candidate|applicant)",
    r"\bother candidates whose (?:experience|background|qualifications|skills) (?:more closely|better|more) (?:match|align|fit)",
    r"\b(?:proceed|continue|advance) (?:with|in)\s+other (?:candidates|applicants)\b",
    r"\bmov(?:ed|ing) forward with (?:a |an )?(?:different|another|other) (?:candidate|applicant|direction)",
    r"\b(?:go|going|gone|went) in (?:a|another) different direction\b",

    # ── "we will not be moving/proceeding forward" family ────────────────
    r"\bwe (?:will|won['’]t|will not|are not|won['’]?t be) (?:moving|proceeding|progressing|going) (?:forward|further|ahead) (?:with|in)\b",
    r"\bnot (?:be |going to be )?(?:moving|proceeding|progressing|advancing) (?:forward|further) (?:with your|in (?:our|the))",
    # The plain forms the two above miss: "we will not be moving forward.",
    # "unable to move your application forward", "can't proceed further".
    r"\b(?:will not|won['’]?t|are not|aren['’]?t|is not|isn['’]?t|not going to|unable to|not able to|cannot|can['’]?t|not in a position to) "
    r"(?:be )?(?:mov(?:e|ing)|proceed(?:ing)?|progress(?:ing)?|advanc(?:e|ing)|continu(?:e|ing)) "
    r"(?:you |with you |your (?:application|candidacy) )?(?:forward|further|ahead)\b(?! (?:until|unless|without|before))",

    # ── "decision not to" / "decision (was) made" family — Veeva, Workday ─
    r"\b(?:made (?:the |a )?)?decision (?:not |against )to (?:move|go|proceed|continue) forward\b",
    r"\bwe['’]?ve (?:made the |come to a |reached a )?decision (?:not |against )to\b",
    r"\b(?:we have )?decided (?:not |against )to (?:offer|proceed|continue|move forward)\b",
    r"\bnot to (?:move|go|proceed) forward (?:at this time|with your|in (?:our|the))",
    r"\bdecision has been made not to\b",

    # ── "after careful review" follow-up phrases ─────────────────────────
    r"\bafter (?:careful |thorough |a careful |much )?(?:consideration|review|deliberation)[,]?\s+we\b.{0,80}\b(?:not|other|unable|unfortunately|cannot|won['’]?t|will not)\b",
    r"\bafter (?:reviewing|evaluating) your (?:application|resume|profile)[,]? we\b.{0,80}\b(?:not|unable|other|won['’]?t|cannot)\b",

    # ── "your application was not selected" family ───────────────────────
    r"\byour application (?:was|has been) (?:not selected|unsuccessful|declined|rejected)\b",
    r"\byour application for [^.!?\n]{2,180}? (?:was|has been) (?:not selected|unsuccessful|declined|rejected)\b",
    r"\b(?:application|candidacy) (?:has been |was |is )(?:unsuccessful|declined|rejected)\b",
    r"\bnot (?:been )?selected (?:for (?:this|the|that|an?|our|further)|to (?:move|proceed|advance|continue))\b",
    r"\b(?:we are|we['’]?re|i am|i['’]?m) unable to (?:move forward|proceed|offer|continue|advance)\b",
    r"\b(?:unable|not able) to offer you (?:a|an|the|this) (?:position|role|job|interview|offer)\b",
    r"\bwill not be (?:extending|making) (?:you )?an offer\b",

    # ── "no longer / not progressing" ────────────────────────────────────
    r"\bno longer (?:under |being )?consider(?:ed|ation)\b",
    r"\bdid not select(?:ed)? (?:your|you for)\b",
    r"\bnot (?:moving|proceeding|progressing|advancing) (?:forward|further) with your\b",

    # ── "position filled" family ─────────────────────────────────────────
    r"\b(?:the |this |that )?position (?:has been |was )?(?:already )?filled\b",
    r"\b(?:position|role)\b[^.!?\n]{0,80}?\b(?:has been|was|is now) (?:already )?filled\b",
    r"\bwe (?:have )?filled (?:the|this|that) (?:position|role|opening|vacancy)\b",
    r"\b(?:the |this )?role (?:has been |is now )?(?:closed|filled|no longer (?:open|available))\b",
    r"\b(?:requisition|req) (?:has been |is )?(?:closed|filled|cancelled)\b",
    r"\b(?:position|role|job|requisition|opening) (?:has been|is|was) (?:cancell?ed|put on hold|placed on hold|no longer available)\b",
    r"\bno longer (?:hiring|recruiting) for (?:this|the|that)\b",
    r"\bdecided to (?:close|cancel|pause) (?:this|the|that) (?:position|role|search|requisition|opening)\b",

    # ── "not a match / not a fit" with negative framing ──────────────────
    r"\bnot (?:a (?:strong |good |the right |an ideal )?(?:match|fit)|the (?:right|best) fit)\b.{0,40}\b(?:for (?:the|this)|at this time)\b",
    r"\b(?:experience|background|skills?|qualifications?) (?:does not|did not|do not) (?:closely )?(?:match|align|meet)\b.{0,40}\b(?:requirements|needs|criteria)\b",
    r"\b(?:closer|stronger|better)(?:ly)? (?:match|aligned|aligned with) (?:to |with )?(?:our (?:current )?(?:requirements|needs))\b",

    # ── ATS-specific rejection phrases ───────────────────────────────────
    # Workday: "after careful consideration we are no longer considering your candidacy"
    r"\bno longer (?:considering|pursuing) your (?:candidacy|application)\b",
    # Greenhouse: "we've decided to pursue other candidates whose qualifications…"
    r"\bdecided to pursue other (?:candidates|applicants)\b",
    # Generic ATS bulk rejection: "Thank you for your interest. We have decided not to proceed."
    r"\bthank you for your (?:interest|application|time)[\.\,]?\s+we (?:have )?decided not to (?:proceed|continue|move forward)\b",
    # iCIMS: "we have determined that other candidates are better suited"
    r"\b(?:determined|concluded) that (?:other|another) candidate",
    # Lever: "we won't be progressing with your application"
    r"\bwon['’]?t be (?:progressing|advancing) (?:with|in) your\b",
    # Ashby: "After review, your application has not been selected"
    r"\b(?:after review|upon review)[,]?\s+your (?:application|profile|candidacy)\s+(?:has |was )?not\b",
    # SmartRecruiters: "we are pursuing other candidates"
    r"\bwe (?:are|are currently) pursuing (?:other|another) candidate",
]

# Decline-sounding words that also open ordinary messages ("Unfortunately, we
# need to reschedule"). One of these alone is a question for the user, never a
# status change. Two, or one beside a closing line below, read as a decline.
_SOFT_REJECTION_PATTERNS = [
    r"\bunfortunately[,]?\s+(?:we|after|at this time|the (?:hiring|recruiting) team|your)\b",
    r"\b(?:i|we)(?:['’]?m|['’]?re| am| are) sorry (?:to (?:inform|let you know|share)|but)\b",
    r"\bregretfully\b",
    r"\bwe regret to (?:inform|let you know|share)\b",
    r"\b(?:application|candidacy) (?:has been |is no longer )?(?:closed|inactive)\b",
]
# Lines that end a decline and also end plenty of receipts ("we will keep your
# resume on file"). They support a soft decline; alone they mean nothing.
_CLOSING_LINE_PATTERNS = [
    r"\bwe['’]ll keep your (?:resume|application|profile|materials) on file\b",
    r"\b(?:keep|hold) your (?:resume|application|profile) (?:on file )?for future\b",
    r"\b(?:resume|application) (?:on file|in our (?:talent|candidate) (?:pool|community|database))\b",
    r"\bwish you (?:the best|all the best|success|continued success) (?:(?:in|with) (?:your|future)\s*)?(?:job\s+search|future endeavors|career|search|future)\b",
    r"\bbest (?:of luck|wishes) (?:in|with) (?:your|the)\s*(?:job\s+search|future|search|endeavors)\b",
]
# "Unfortunately we need to move our call" is scheduling, not a decision.
_RESCHEDULE_PATTERN = re.compile(
    r"\b(?:reschedul\w+|postpon\w+|push(?:ed|ing)? (?:back|out)|"
    r"mov(?:e|ed|ing) (?:our|your|the|this) (?:call|interview|meeting|chat|conversation|time|slot)|"
    r"running (?:late|behind)|another time|new time|different time|delay(?:ed)?|"
    r"technical (?:issue|difficult\w+)|out (?:sick|of (?:the )?office)|scheduling conflict)\b", re.IGNORECASE)

# The candidate left the process. That is "withdrawn", never "rejected".
_WITHDRAWN_PATTERNS = [
    r"\b(?:application|candidacy) (?:has been|was|is) (?:successfully )?withdrawn\b",
    r"\b(?:application|candidacy)\b[^.!?\n]{0,120}?\b(?:has been|was) (?:successfully )?withdrawn\b",
    r"\byou (?:have )?(?:successfully )?withdr(?:awn|ew) (?:your|from)\b",
    r"\b(?:received|confirm(?:ing|ed)?|processed) your (?:request to withdraw|withdrawal)\b",
    r"\bremoved you from (?:this|the|our) (?:process|consideration)[^.!?\n]{0,40}\bat your request\b",
]

# An offer names what is being offered. "Pleased to offer you the chance to
# interview" and "any offer of employment is contingent on a background check"
# both used to read as offers.
_OFFERED = r"(?:the |a |an |this |our )?(?:full[- ]time |part[- ]time |summer |internship |contract |permanent )*(?:position|role|job|internship|employment|opportunity to join)"
_OFFER_PATTERNS = [
    # ── canonical offer phrasing ─────────────────────────────────────────
    r"\b(?:pleased|delighted|excited|thrilled|happy|glad|like|love) to (?:formally )?offer you " + _OFFERED + r"\b",
    r"\b(?:pleased|delighted|excited|thrilled|happy|glad|like|love|want(?:ed)?) to (?:formally )?extend (?:you |to you )?(?:an |the |a |our )(?:formal |written |verbal |conditional |contingent )?offer\b",
    r"\bextend(?:ing|ed)? (?:you |to you )?(?:an |the |a |our )(?:formal |written |verbal |conditional |contingent )?offer of employment\b",
    r"\b(?:we are|we['’]re|i am|i['’]m) (?:making|extending) you (?:an |a formal )?offer\b",

    # ── offer letter mechanics ───────────────────────────────────────────
    r"\boffer letter (?:attached|enclosed|below|is ready|has been (?:sent|generated)|for your review)\b",
    r"\bplease (?:find |review |see )?(?:your |the |attached )?offer (?:letter |of employment |package )?(?:attached|enclosed|below)\b",
    r"\b(?:reviewing|signing|countersigning|accepting) (?:your |the )?offer (?:letter|package)\b",
    r"\b(?:e[- ]?signature|docusign).{0,30}\boffer (?:letter|package)\b",
    r"\boffer (?:package|letter|details) (?:include[ds]?|outlines?|contains?)\b",

    # ── compensation / start-date discussion in offer context ────────────
    r"\b(?:your |the )(?:base salary|annual salary|total compensation|comp package|starting salary|sign[- ]on bonus)\b.{0,60}\b(?:would be|will be|of \$|is \$)",
    r"\bstart(?:ing)? (?:date|day) (?:would be|will be|of) (?:[A-Z][a-z]+\s+\d|the \d|on)\b",
    r"\bcongratulations[!.,]\s*[a-z\s]{0,80}\boffer\b",

    # ── ATS-specific offer phrasing ──────────────────────────────────────
    # Greenhouse: "you've been extended an offer"
    r"\byou(?:['’]ve)? been (?:extended|sent|presented) (?:an |the |our )?offer\b",
    # Workday: "a formal offer has been extended"
    r"\b(?:formal|written|verbal) offer (?:has been|is being) (?:extended|sent|prepared|approved|issued)\b",
]

# Interview signals require *human-authored* progression — a scheduled time,
# Calendly link, hiring-manager intro, or explicit availability ask. We are
# DELIBERATELY conservative here: any pattern that could match conditional
# "if you're selected for an interview" boilerplate is excluded, and a match
# that follows "if" or "should" in its own sentence is never counted.
# Each pattern has a kind:
#   link      a scheduling, calendar or assessment link the person is asked to use
#   strong    a stated event or a direct ask: "are you available on", "your
#             interview is scheduled", "we'd like to invite you to interview"
#   progress  being moved on; counts only when the sentence is about YOU, so
#             "candidates selected for the next step will be contacted" is not
#   soft      friendly outreach ("let's connect", "I'd love to chat"). Worth a
#             question, never a move: it is how recruiters, newsletters and
#             cold outreach all talk.
# Every non-link match is also dropped when its sentence states a selection
# condition ("if your background is a match", "only candidates selected") or
# describes the process in general ("our process includes a phone screen with").
_INTERVIEW_PATTERN_KINDS = [
    # ── scheduling links (almost certainly interview-intent) ─────────────
    (r"\bcalendly\.com/", "link"),
    (r"\bsavvycal\.com/", "link"),
    (r"\b(?:meetings|book)\.hubspot\.com/", "link"),
    (r"\bcal\.com/", "link"),
    (r"\b(?:lu\.ma|chronograph\.io|x\.ai)/", "link"),
    (r"\bgoodtime\.io/", "link"),

    # ── a calendar invitation for an interview ───────────────────────────
    (r"\b(?:updated )?invitation(?: from [^:\n]{1,60})?: [^\n]{0,120}\b(?:interview|phone screen|technical screen|recruiter screen|onsite|hiring manager)\b", "link"),

    # ── direct availability / scheduling asks ────────────────────────────
    # Note: "are you available" must be followed by "on/for/to/this/next" so
    # we don't catch "are you available for any of the listed roles" boilerplate.
    (r"\b(?:are|would|when (?:are|would)) you (?:available|free) (?:on |for |to |this |next |the |a (?:few|couple|brief))", "strong"),
    (r"\bdo you have (?:any |some |a few )?(?:availability|time|times|slots) (?:on |this |next |for |to )", "strong"),
    (r"\bplease (?:share|let me know|send) (?:your )?(?:availability|times|time slots)\b", "strong"),
    (r"\b(?:share|provide|send) (?:me )?(?:a few |some )?(?:times?|slots?) (?:that work|when you|you['’]re)", "strong"),
    (r"\blet['’]s (?:schedule|set up|find a time|coordinate)\b", "strong"),
    (r"\bschedule (?:a|the|your|our) (?:call|interview|screen|chat|conversation|time|meeting|sync)\b", "strong"),
    (r"\b(?:I['’]?d|I would|we['’]?d|we would) (?:love|like) to (?:schedule|set up)\b", "strong"),

    # ── confirmed / scheduled interview events ──────────────────────────
    # Crucially these require a confirmation verb, NOT just the word "interview"
    (r"\b(?:phone|technical|behavioral|hiring manager|recruiter|initial|introductory|first[- ]round|second[- ]round|final[- ]round|culture) (?:screen|interview|call|chat|conversation)\s+(?:with|on|at|scheduled|confirmed|is set|has been booked)\b", "strong"),
    (r"\b(?:your |the )?(?:phone screen|technical screen|video interview|virtual interview|interview) (?:is |has been |will be )?(?:scheduled|confirmed|booked|set|happening|on)\b", "strong"),
    (r"\binterview (?:scheduled|confirmed|booked|set up) (?:for|on|with|at)\b", "strong"),
    (r"\b(?:confirming|to confirm) (?:your |our |the )?(?:interview|screen|call|chat|meeting)\b", "strong"),
    (r"\bcalendar invite (?:for|to) (?:your |the |our )?(?:interview|screen|call|chat|meeting)\b", "strong"),

    # ── invitations to interviews / next rounds ──────────────────────────
    # Must have a clear "we want to invite you" — not "if invited"
    (r"\b(?:we['’]?d like|we would like|i['’]?d like|i would like) to (?:invite|set up|schedule)\s+(?:you )?(?:for |to )?(?:an? |the )?(?:interview|screen|call|chat|conversation|next (?:round|step|stage))\b", "strong"),
    (r"\b(?:we['’]?re|i['’]?m|we are) (?:inviting|extending an invitation to) you (?:to|for)\b", "strong"),
    (r"\b(?:onsite|virtual onsite|panel|loop) (?:loop|interview|round)\s+(?:with|on|scheduled|confirmed|invitation)\b", "strong"),
    # A receipt's "the next step in our process is a review" is not progress;
    # being moved to the next step is.
    (r"\b(?:advanc\w+|mov\w+|progress\w+|proceed\w+|invit\w+|selected)\b[^.!?\n]{0,40}\bnext (?:round|step|stage) (?:of|in) (?:the|your|our) (?:interview|hiring|recruiting|recruitment|selection )?\s?process\b", "progress"),
    (r"\bmove(?:d)? (?:you )?(?:forward|on|ahead) to (?:the next|our|a) (?:round|step|stage|interview)\b", "progress"),

    # ── take-home / coding assessments ───────────────────────────────────
    # Only when actively assigned (link to it, deadline, etc.) — not
    # mentioning that one exists in the process
    (r"\btake[- ]home (?:assessment|assignment|exercise|challenge|project|coding (?:test|assignment))\s+(?:link|attached|to complete|due|deadline|below|here|at)\b", "strong"),
    (r"\b(?:please )?(?:complete|submit|return) (?:the |your |this |attached )?(?:take[- ]home|coding|technical) (?:assessment|assignment|exercise|challenge|task)\b", "strong"),
    (r"\b(?:hackerrank|codility|coderpad|coderbyte|codesignal|leetcode|hackerearth)\.(?:com|io)/", "link"),
    (r"\b(?:assessment|coding test) (?:link|invitation) (?:below|above|attached|here)\b", "strong"),

    # ── hiring manager / recruiter intros ────────────────────────────────
    (r"\bhiring manager (?:would like|wants|will|is excited|reached out)\b.{0,40}\b(?:to (?:speak|chat|meet|connect|interview|talk)|with you|to set up)\b", "soft"),
    (r"\b(?:i['’]?m|hi[,]?) (?:reaching out|writing) (?:about|regarding|to discuss|to chat about) your (?:application|interest|background)\b.{0,80}\b(?:next step|chat|call|interview|screen|connect)\b", "soft"),
    (r"\bafter review(?:ing)? your (?:application|resume|profile)[,]? (?:we['’]?d like|i['’]?d like|we would like)\b", "progress"),
    (r"\blet['’]s (?:connect|chat|talk)\b", "soft"),
    (r"\b(?:I['’]?d|I would) (?:love|like) to (?:chat|connect|hop on|jump on)\b", "soft"),
    (r"\b(?:like|love) to (?:hop|jump|get) on a (?:quick )?(?:call|chat|video)\b", "soft"),

    # ── direct interview-platform links ──────────────────────────────────
    (r"\b(?:hirevue|sparkhire|willo|videoask|odro|interviewing\.io)\.(?:com|io)/", "link"),
    (r"\b(?:karat|byteboard|coderbyte interview|techscreen)\.(?:com|io)/", "link"),
]
_INTERVIEW_PATTERNS = [p for p, kind in _INTERVIEW_PATTERN_KINDS if kind != "soft"]
# A video-call link is an interview only beside interview words. Alone it is
# just as likely a webinar from a company the user happens to track.
_MEETING_LINK = re.compile(
    r"\b(?:(?:[a-z0-9-]+\.)?zoom\.us/(?:j|my|w|s)/|meet\.google\.com/|teams\.microsoft\.com/l/meetup-join|"
    r"teams\.live\.com/meet/|(?:[a-z0-9-]+\.)?webex\.com/(?:meet|join|[a-z0-9-]+/j\.php))[A-Za-z0-9/_?=&\-.]*",
    re.IGNORECASE)
_INTERVIEW_CONTEXT = re.compile(
    r"\b(?:interview|phone screen|technical screen|recruiter screen|hiring manager|onsite|your candidacy)\b",
    re.IGNORECASE)
# A link the candidate is asked to use counts even inside an acknowledgement.
_ACTION_LINK = re.compile(
    r"\b(?:calendly|savvycal|hubspot|cal|goodtime|hackerrank|codility|coderpad|coderbyte|codesignal|"
    r"hackerearth|hirevue|sparkhire|willo|videoask|karat|byteboard|zoom|meet\.google|teams\.microsoft)"
    r"\.?[a-z.]*/", re.IGNORECASE)

_REJECTION_REGEX = [re.compile(p, re.IGNORECASE) for p in _REJECTION_PATTERNS]
_SOFT_REJECTION_REGEX = [re.compile(p, re.IGNORECASE) for p in _SOFT_REJECTION_PATTERNS]
_CLOSING_LINE_REGEX = [re.compile(p, re.IGNORECASE) for p in _CLOSING_LINE_PATTERNS]
_WITHDRAWN_REGEX = [re.compile(p, re.IGNORECASE) for p in _WITHDRAWN_PATTERNS]
_OFFER_REGEX = [re.compile(p, re.IGNORECASE) for p in _OFFER_PATTERNS]
_INTERVIEW_REGEX = [re.compile(p, re.IGNORECASE) for p in _INTERVIEW_PATTERNS]
_INTERVIEW_KIND_REGEX = [(re.compile(p, re.IGNORECASE), kind) for p, kind in _INTERVIEW_PATTERN_KINDS]


# Boilerplate can prevent a false interview, but cannot prove a submission.
# A receipt requires an acknowledgement specifically about the application.
_APPLICATION_ACK_REGEX = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:thanks|thank you|many thanks) for (?:applying|your application|submitting (?:your|an) application|taking the time to apply)\b",
    r"\bwe(?: have|['’]ve)? (?:received|got) your application\b",
    r"\byour application (?:has been |is currently |was )?(?:successfully )?(?:received|submitted|on file|in our system)\b",
    r"\bapplication (?:successfully )?(?:received|submitted|completed)\b",
    r"\byour application (?:is currently |will be |has been )?(?:being |under |in )?(?:evaluated|reviewed|considered|assessed|in review)\b",
    r"\byour application was sent to\b",
    # "Acme viewed your application": nothing changed, but it was submitted.
    r"\b(?:viewed|opened|is reviewing|has reviewed) your application\b",
    r"\byour application (?:was|has been) viewed\b",
)]

# What an outcome phrase must not be the tail of: "if you are selected for an
# interview, we will schedule a call" describes a possibility, not an event.
_CONDITIONAL_LEAD = re.compile(
    r"\b(?:if|unless|whether|in case|in the event|provided that|assuming)\b|(?:^\s*|[,:]\s*)should\b",
    re.IGNORECASE)


def _sentence_lead(text: str, start: int) -> str:
    """The part of the sentence that comes before position `start`."""
    return text[max(text.rfind(mark, 0, start) for mark in ".!?;\n") + 1:start]


# A sentence that makes the event depend on being chosen is boilerplate,
# wherever the condition sits: "a recruiter will reach out to schedule a call
# if your background is a match", "only candidates selected ... will be contacted".
_SELECTION_CONDITION = re.compile(
    r"\b(?:if|should|once|unless|in the event|provided)\b[^.!?\n]{0,80}?\b(?:selected|shortlisted|chosen|"
    r"qualif\w*|(?:a |good |strong |great )?(?:match|fit)\b|move (?:you )?forward|moving forward|"
    r"proceed\w*|advance\w*|align\w*|meet (?:our|the|all) (?:requirements|criteria|needs|qualifications))"
    r"|\bwhen (?:you are |you['’]re )?(?:selected|shortlisted|chosen)\b"
    r"|\b(?:only |those |the )?(?:candidates|applicants|individuals|people)\b[^.!?\n]{0,40}?"
    r"\b(?:selected|shortlisted|chosen|qualified|who (?:best )?(?:meet|match))\b",
    re.IGNORECASE)
# A sentence describing how hiring works in general, not what is happening.
_GENERAL_PROCESS = re.compile(
    r"\b(?:typically|usually|generally|normally|what to expect|tips for|how to prepare|frequently asked|"
    r"learn (?:more )?about|explore our|"
    r"our (?:interview|hiring|recruiting|recruitment|selection) process|"
    r"(?:the|this) (?:interview|hiring|recruiting|selection) process (?:consists|includes|involves|typically|may|will|is)|"
    r"(?:process|steps?|stages?) (?:includes?|involves?|consists? of|may include|might include|can include|will include))\b",
    re.IGNORECASE)
_SECOND_PERSON = re.compile(r"\b(?:you|your|you['’](?:ve|re|ll))\b", re.IGNORECASE)
# "Let's connect: follow us on LinkedIn" is a social plug, not outreach.
_SOCIAL_PLUG = re.compile(
    r"\b(?:follow us|on (?:linkedin|twitter|instagram|facebook|youtube)|subscribe|newsletter|"
    r"talent (?:community|network)|join our community)\b", re.IGNORECASE)
_INTERVIEW_RANK = {"soft": 1, "strong": 2}


def _sentence_around(text: str, start: int, end: int) -> str:
    """The whole sentence a match sits in."""
    lo = max(text.rfind(mark, 0, start) for mark in ".!?;\n") + 1
    tails = [i for i in (text.find(mark, end) for mark in "!?;\n") if i != -1]
    # A full stop ends the sentence only when a space or the end follows it,
    # so "calendly.com/x" or "Inc." mid-sentence does not cut it short.
    stop = re.search(r"\.(?:\s|$)", text[end:])
    if stop:
        tails.append(end + stop.start())
    return text[lo:min(tails) if tails else len(text)]


def _interview_signal(text: str) -> Optional[Tuple[str, str]]:
    """(phrase, "strong" | "soft") for the clearest interview signal in one
    email, or None. A link counts by itself; wording counts only when its
    sentence states it as fact, about this person, unconditionally."""
    if not text:
        return None
    soft: Optional[str] = None
    for rx, kind in _INTERVIEW_KIND_REGEX:
        for m in rx.finditer(text):
            if _CONDITIONAL_LEAD.search(_sentence_lead(text, m.start())):
                continue
            phrase = m.group(0)[:160]
            if kind == "link":
                return phrase, "strong"
            sentence = _sentence_around(text, m.start(), m.end())
            if _SELECTION_CONDITION.search(sentence) or _GENERAL_PROCESS.search(sentence):
                continue
            if kind == "progress" and not _SECOND_PERSON.search(sentence):
                continue
            if kind == "soft":
                if not _SOCIAL_PLUG.search(sentence):
                    soft = soft or phrase
                continue
            return phrase, "strong"
    # A real meeting link beside interview wording that is itself a statement.
    link = _MEETING_LINK.search(text)
    if link:
        for m in _INTERVIEW_CONTEXT.finditer(text):
            sentence = _sentence_around(text, m.start(), m.end())
            if (_CONDITIONAL_LEAD.search(_sentence_lead(text, m.start()))
                    or _SELECTION_CONDITION.search(sentence) or _GENERAL_PROCESS.search(sentence)):
                continue
            return link.group(0)[:160], "strong"
    return (soft, "soft") if soft else None


def _scan_patterns(text: str, regexes: List[re.Pattern], stated: bool = False) -> Optional[str]:
    """Return the first matching pattern's source phrase, or None.

    We surface the matched phrase so reconciliation can include it as the
    `reason` shown in the suggestions UI — users trust the call more when
    they can see the literal sentence that drove it.

    `stated=True` skips a phrase that follows "if"/"should" in its own
    sentence: only something the email states as fact is an outcome.
    """
    if not text:
        return None
    for rx in regexes:
        for m in rx.finditer(text):
            if stated and _CONDITIONAL_LEAD.search(_sentence_lead(text, m.start())):
                continue
            return m.group(0)[:160]
    return None


def _rejection_signal(text: str) -> Optional[Tuple[str, str]]:
    """(phrase, strength) for decline wording in one email.

    strong        an explicit decision about the application
    corroborated  soft wording twice, or soft wording with a closing line
    weak          one soft phrase: worth asking the user, never acted on
    """
    phrase = _scan_patterns(text, _REJECTION_REGEX, stated=True)
    if phrase:
        return phrase, "strong"
    soft = [found for found in (_scan_patterns(text, [rx], stated=True) for rx in _SOFT_REJECTION_REGEX)
            if found]
    if not soft or _RESCHEDULE_PATTERN.search(text):
        return None
    if len(soft) > 1 or _scan_patterns(text, _CLOSING_LINE_REGEX):
        return soft[0], "corroborated"
    return soft[0], "weak"


_REJECTION_RANK = {"weak": 1, "corroborated": 2, "strong": 3}


def extract_deterministic_signals(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run regex extractors across an application's email bundle.

    Returns a dict with any of: rejected, withdrawn, offer, interviewing,
    receipt — each with the matched phrase and the message it came from for
    traceability. Order of precedence on conflict is decided by
    `reconcile_classification`.

    Literal phrases are evidence, not proof of application identity. The
    matcher separately establishes identity before any automatic update.
    """
    # Newest-first processing so an older "applied" receipt doesn't shadow a
    # later rejection — but we still scan ALL messages for any signal.
    found: Dict[str, Dict[str, Any]] = {}

    def keep(kind: str, phrase: str, msg: Dict[str, Any], order: int, **extra: Any) -> None:
        found[kind] = {"phrase": phrase, "message_id": msg.get("message_id"),
                       "internal_date": msg.get("internal_date") or 0, "order": order, **extra}

    ordered = sorted(messages, key=lambda m: m.get("internal_date") or 0, reverse=True)
    for order, msg in enumerate(ordered):
        if _is_irrelevant_mail(msg):
            continue
        text = _application_text(msg)[:BODY_TEXT_LIMIT]
        rejection = _rejection_signal(text)
        # The newest decline wins, unless an older email says it more plainly.
        if rejection and _REJECTION_RANK[rejection[1]] > _REJECTION_RANK.get(
                (found.get("rejected") or {}).get("strength"), 0):
            keep("rejected", rejection[0], msg, order, strength=rejection[1])
        if "withdrawn" not in found:
            phrase = _scan_patterns(text, _WITHDRAWN_REGEX, stated=True)
            if phrase:
                keep("withdrawn", phrase, msg, order)
        if "offer" not in found:
            phrase = _scan_patterns(text, _OFFER_REGEX, stated=True)
            if phrase:
                keep("offer", phrase, msg, order)
        # The newest interview signal wins, unless an older email says it
        # more plainly (a soft "let's chat" never hides a real invitation).
        interview = _interview_signal(text)
        if interview and _INTERVIEW_RANK[interview[1]] > _INTERVIEW_RANK.get(
                (found.get("interviewing") or {}).get("strength"), 0):
            keep("interviewing", interview[0], msg, order,
                 link=bool(_ACTION_LINK.search(interview[0]) or _MEETING_LINK.search(interview[0])),
                 strength=interview[1])
        if "receipt" not in found:
            phrase = _scan_patterns(text, _APPLICATION_ACK_REGEX)
            if phrase:
                keep("receipt", phrase, msg, order)

    return {kind: found[kind] for kind in ("rejected", "withdrawn", "offer", "interviewing", "receipt")
            if kind in found}


def reconcile_classification(
    deterministic: Dict[str, Any],
    llm: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Merge deterministic signals with the LLM verdict into a final call.

    Returns {status, confidence, reason, source} or None if neither layer
    produced anything actionable.

    Precedence (highest → lowest):
      0. WITHDRAWAL confirmed      → status=withdrawn, conf=0.95, unless an explicit decline
                                     is also present (then the decline rules decide).
      1. Deterministic REJECTION   → status=rejected. conf=0.97 for an explicit decision,
                                     0.93 for corroborated soft wording, 0.85 (review only)
                                     for one soft phrase.
      2. Deterministic OFFER       → status=offer, conf=0.96 (overrides everything but rejection)
      3. RECEIPT-ONLY GUARD        → if the thread is just an application receipt (no real
                                     interview/decline/offer signal) the status is "applied",
                                     period. This is what blocks LLM false positives where
                                     "if you're selected for an interview" boilerplate gets
                                     misread as a real interview invite.
      4. Deterministic INTERVIEW + LLM=interviewing/applied → status=interviewing, conf=0.95.
         Interview wording inside the SAME email as the receipt is a question for the user
         (conf=0.80) unless it is a scheduling or assessment link.
      5. LLM verdict alone         → trust at LLM's stated confidence (capped 0.90 without
                                     deterministic backing — never auto-apply on LLM-only
                                     unless the LLM is genuinely confident)
      6. Deterministic INTERVIEW only (LLM disagreed) → status=interviewing, conf=0.80
         (we trust the regex but flag for review since LLM saw something else)
    """
    rejection = deterministic.get("rejected")
    strength = (rejection or {}).get("strength", "strong")

    # 0. The candidate withdrew. An explicit decline still outranks it.
    if "withdrawn" in deterministic and not (rejection and strength == "strong"):
        signal = deterministic["withdrawn"]
        return {"status": "withdrawn", "confidence": 0.95, "source": "deterministic",
                "evidence_message_id": signal.get("message_id"),
                "reason": f'Withdrawal confirmed: "{signal["phrase"]}"'}

    # A historical rejection must not erase a newer outcome. Conflicting
    # terminal/positive evidence is retained for review instead of guessing.
    outcomes = {k: v for k, v in deterministic.items() if k in ("rejected", "offer", "interviewing")}
    if "rejected" in outcomes and len(outcomes) > 1:
        latest = max(outcomes, key=lambda k: outcomes[k].get("internal_date") or 0)
        times = [v.get("internal_date") or 0 for v in outcomes.values()]
        # Equal/missing timestamps cannot establish chronology.
        uncertain_order = not all(times) or len(set(times)) < len(times)
        unusual_transition = latest != "rejected" or "offer" in outcomes
        if uncertain_order or unusual_transition:
            signal = outcomes[latest]
            return {"status": latest, "confidence": 0.85, "source": "deterministic",
                    "evidence_message_id": signal.get("message_id"),
                    "reason": f'Conflicting outcomes need review. Latest evidence: "{signal["phrase"]}"'}

    # 1. Current rejection.
    if rejection:
        return {
            "status": "rejected",
            "confidence": {"strong": 0.97, "corroborated": 0.93}.get(strength, 0.85),
            "reason": (f'Possible decline, please check: "{rejection["phrase"]}"' if strength == "weak"
                       else f'Decline phrase: "{rejection["phrase"]}"'),
            "source": "deterministic",
            "evidence_message_id": rejection.get("message_id"),
        }

    # 2. Offer.
    if "offer" in deterministic:
        return {
            "status": "offer",
            "confidence": 0.96,
            "reason": f'Offer phrase: "{deterministic["offer"]["phrase"]}"',
            "source": "deterministic",
            "evidence_message_id": deterministic["offer"].get("message_id"),
        }

    llm_status = (llm or {}).get("status") if llm else None
    llm_conf = float((llm or {}).get("confidence") or 0.0) if llm else 0.0
    llm_reason = (llm or {}).get("reason", "") if llm else ""

    # 3. Receipt-only guard. If the thread is JUST an application receipt
    #    (with conditional "if you're selected" boilerplate) and no real
    #    interview/decline/offer signal fired, force status=applied. This is
    #    the killer rule that prevents the CDW/WorkWhile false positives
    #    where the LLM reads "Should you be selected to interview, a member
    #    of our Talent team will reach out" and misclassifies as interviewing.
    if "receipt" in deterministic and "interviewing" not in deterministic:
        return {
            "status": "applied",
            "confidence": 0.95,
            "reason": f'Receipt only: "{deterministic["receipt"]["phrase"]}"',
            "source": "deterministic",
            "evidence_message_id": deterministic["receipt"].get("message_id"),
        }

    # 4. Deterministic interview + LLM agrees (or LLM still on applied — which
    #    is fine, the regex caught a signal LLM may have under-weighted).
    if "interviewing" in deterministic:
        interview = deterministic["interviewing"]
        receipt = deterministic.get("receipt")
        if interview.get("strength") == "soft":
            # Friendly outreach reads the same from a recruiter, a newsletter
            # and a cold message: ask, never move the card by itself.
            return {
                "status": "interviewing",
                "confidence": 0.85 if llm_status == "interviewing" else 0.80,
                "reason": f'Possible interview, please check: "{interview["phrase"]}"',
                "source": "deterministic+llm" if llm_status == "interviewing" else "deterministic",
                "evidence_message_id": interview.get("message_id"),
            }
        if receipt and receipt.get("order") == interview.get("order") and not interview.get("link"):
            # "Thanks for applying ... our team will schedule a call" is how
            # receipts are written. It may be an invitation; the user decides.
            return {
                "status": "interviewing",
                "confidence": 0.80,
                "reason": ("Interview wording inside an application receipt, please check: "
                           f'"{interview["phrase"]}"'),
                "source": "deterministic",
                "evidence_message_id": interview.get("message_id"),
            }
        if llm_status in ("interviewing", "applied", None):
            return {
                "status": "interviewing",
                "confidence": 0.95,
                "reason": f'Interview signal: "{interview["phrase"]}"',
                "source": "deterministic+llm" if llm_status == "interviewing" else "deterministic",
                "evidence_message_id": interview.get("message_id"),
            }
        # LLM said something contradictory (e.g. "rejected" but no decline
        # phrase fired — possible polite rejection). Fall through to LLM.
        if llm_status in ("rejected", "offer"):
            return {
                "status": llm_status,
                "confidence": min(llm_conf, 0.85),  # cap — interview signal is mildly contradicting
                "reason": llm_reason,
                "source": "llm",
                "evidence_message_id": None,
            }
        # 5. Interview signal but no useful LLM verdict — trust regex but lower conf.
        return {
            "status": "interviewing",
            "confidence": 0.80,
            "reason": f'Interview signal: "{interview["phrase"]}"',
            "source": "deterministic",
            "evidence_message_id": interview.get("message_id"),
        }

    # 4. LLM-only verdict.
    if llm_status in VALID_STATUSES:
        return {
            "status": llm_status,
            # Cap LLM-only confidence so we don't auto-apply on a hallucinated
            # high-confidence call without any deterministic backing.
            "confidence": min(llm_conf, 0.90),
            "reason": llm_reason,
            "source": "llm",
            "evidence_message_id": None,
        }
    if llm_status == "ignore":
        return {"status": "ignore", "confidence": llm_conf, "reason": llm_reason, "source": "llm"}

    return None


def _heuristic_status(msg: Dict[str, Any]) -> Tuple[Optional[str], float]:
    """Quick keyword pass — if nothing matches we still call the LLM."""
    if _is_irrelevant_mail(msg):
        return None, 0.0
    text = _application_text(msg).lower()
    for status, phrases in _STATUS_KEYWORDS.items():
        for phrase in phrases:
            if phrase in text:
                return status, 0.7
    return None, 0.0


def classify_record_thread(messages: List[Dict[str, Any]], record_meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Classify an application's CURRENT status by reading the full set of
    related emails together (newest first).

    This is dramatically more accurate than per-message classification because
    the LLM can see the timeline as one story. A thread that goes
    "applied → interview invite → rejection" is unambiguous when read in
    order — but classifying each email in isolation can leave a record stuck
    on whichever message happened to be processed last.
    """
    messages = [msg for msg in messages if not _is_irrelevant_mail(msg)]
    if not messages:
        return None

    blocks: List[str] = []
    for i, msg in enumerate(messages):
        blocks.append(
            f"--- Email {i+1} (most recent first) ---\n"
            f"Date: {msg.get('date', '')}\n"
            f"From: {msg.get('from_name','')} <{msg.get('from_address','')}>\n"
            f"Subject: {msg.get('subject', '')}\n"
            f"Body: {_application_body(msg)[:900]}"
        )
    emails_text = "\n\n".join(blocks)

    prompt = f"""You are auditing a SINGLE job application by reading every related email
together. Your job is to decide the application's CURRENT status with high precision.
False positives (e.g. promoting "applied" → "interviewing" off automated boilerplate, or
marking "rejected" off a vague "we'll be in touch") are worse than being conservative.

Application:
- Company: {record_meta.get('company') or 'unknown'}
- Role: {record_meta.get('title') or 'unknown'}
- Tracker currently shows: {record_meta.get('current_status')}

Below are the related emails, MOST RECENT FIRST. Read them as one timeline and decide the
status reflected by the most-recent meaningful event.

{emails_text}

CRITICAL ANTI-FALSE-POSITIVE GUARDS (read these BEFORE the decision rules):

A. If NONE of the emails are addressed to this specific application (e.g. the
   subject mentions a different role at a different company, OR the body
   talks about an unrelated job/event), pick "ignore". Do NOT bend signals
   from unrelated mail toward this record.

B. The phrase "we will keep your resume on file" is rejection UNLESS it
   appears inside a positive context ("…we're moving you to the next round
   and will keep your resume on file for the full process"). Read context.

C. "We're moving forward" is AMBIGUOUS in isolation:
     • "we're moving forward with other candidates" → rejection ✓
     • "we're moving forward to schedule your next interview" → interviewing ✓
     • "we're moving forward in the process" — needs more context, don't
       infer rejection unless an explicit decline phrase follows.

D. Mass/newsletter signals — DO NOT classify as interviewing or applied:
     • "join our talent community" / "career community update"
     • job-alert digests ("3 new roles at <company>")
     • generic event invites ("Tech Talk", "Open House", "AMA")
     • surveys / feedback requests after an interview
   These are "ignore" status; the record's tracker status stays unchanged.

E. Automated platform reminders (HackerRank/Codility/CodeSignal) about a
   take-home are interviewing-signal ONLY when the user clearly received
   the invite for THIS role at THIS company. Standalone platform marketing
   or reminder emails for OTHER assessments are "ignore".

F. Withdrawal language ("we received your withdrawal", "we've removed you
   from this process at your request") → status="withdrawn", NOT rejected.

G. Don't conflate "next steps" boilerplate in an application-receipt email
   with progression. "We've received your application. If you move forward,
   next steps will be shared" is still "applied", not interviewing.

DECISION RULES (apply in order — first match wins):

1. rejected — pick this if ANY email contains explicit decline language directed at the
   user's application:
     • "we have decided to move forward with other candidates"
     • "we will not be moving forward"
     • "we regret to inform you"
     • "unfortunately, we are not able to offer"
     • "your application was not selected"
     • "we'll keep your resume on file" (with rejection context — see Guard B)
     • "no longer under consideration"
   A rejection email overrides any earlier interview/applied state — the final status is
   "rejected" even if there was a prior interview.

2. offer — pick this only if an email explicitly extends an offer of employment or
   discusses compensation/start date in an offer context (e.g. "we are pleased to extend",
   "offer letter attached", "your starting salary would be").

3. interviewing — pick this ONLY if an email contains an explicit, human-authored
   progression signal directed at this user:
     • a specific scheduled interview time/date or Calendly link addressed to them
     • "would you be available on <date>" / "let's schedule a call"
     • take-home/coding assessment INVITATION (not just a reminder of one received elsewhere)
     • hiring manager / recruiter introducing themselves to set up a screen
     • panel/onsite invite or confirmation
   Do NOT pick "interviewing" off:
     • generic "next steps will be shared if you're a fit" boilerplate in an application
       receipt
     • mass newsletter / event invitations
     • assessment platforms emails the user clearly didn't engage with for THIS role
     • "thanks for applying — our team will review" (this is still "applied")

4. applied — pick this if there is an application-received / acknowledgement email but
   NO clear progression signal (rule 3) and NO decline (rule 1). This is the safe default
   when the thread is just "we got your application" + automated check-ins.

5. ignore — pick this only if NONE of the emails are actually about this user's
   application for THIS role at THIS company (e.g. unrelated recruiter outreach for a
   different role, newsletters, job alert digests, generic marketing).

Funnel direction is draft → applied → interviewing → offer, with rejected as a terminal
side branch. NEVER pick a status earlier in the funnel than the tracker's current status
unless the most recent email genuinely re-opens the process (rare — flag it with low
confidence so the user reviews).

Confidence calibration (be honest — low confidence is fine, it just keeps it as a
suggestion the user reviews instead of auto-applying):
- 0.90+   unambiguous: an explicit phrase from the rules above appears verbatim
- 0.75-0.89  clear from human-written context across the thread
- 0.60-0.74  leaning that way but a reasonable person could read it differently
- below 0.60  genuinely uncertain — use this when the emails are vague or boilerplate

Output strict JSON: {{"status": "...", "confidence": 0.0-1.0, "reason": "one short sentence quoting the specific phrase or email that drove the decision"}}.
"""

    # Optional bounded assistance only. Inconclusive mail is retained for
    # review, so neither a premium model nor retry spending is necessary.
    result: Optional[Dict[str, Any]] = None
    try:
        from services.claude_client import claude_json, CLAUDE_FLASH
        result = claude_json(prompt=prompt, max_tokens=400, temperature=0.0,
                             model=CLAUDE_FLASH, max_retries=0)
    except Exception:
        logger.warning("classify_record_thread model unavailable; retaining evidence for review")
        return None
    if not isinstance(result, dict):
        return None

    status = str(result.get("status") or "").lower()
    try:
        confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(confidence):
        return None
    confidence = max(0.0, min(1.0, confidence))
    reason = str(result.get("reason") or "")[:240]

    if status == "ignore":
        return {"status": "ignore", "confidence": confidence, "reason": reason}
    if status not in VALID_STATUSES:
        return None
    return {"status": status, "confidence": confidence, "reason": reason}


# ---------------------------------------------------------------------------
# Sync orchestrator
# ---------------------------------------------------------------------------

# Funnel direction. Lower index = earlier in the funnel. "rejected"/"ghosted"/
# "withdrawn" are terminal — moving OUT of them needs strong evidence.
_FUNNEL_ORDER = {"saved": 0, "draft": 1, "applied": 2, "interviewing": 3, "offer": 4}


def _is_funnel_backwards(current: str, proposed: str) -> bool:
    """True if `proposed` would move the record backwards in the funnel.

    Used as a soft safety net — a backwards move still goes through, but only
    as a suggestion the user has to confirm, never an auto-apply.
    """
    if current == "offer" and proposed != "offer":
        return True
    if current in ("rejected", "ghosted", "withdrawn"):
        # Terminal states — only reopen if very confident, and never auto.
        return proposed not in ("rejected", "ghosted", "withdrawn")
    cur_rank = _FUNNEL_ORDER.get(current)
    new_rank = _FUNNEL_ORDER.get(proposed)
    if cur_rank is None or new_rank is None:
        return False
    return new_rank < cur_rank

def _records_for_user(user_email: str) -> List[Dict[str, Any]]:
    db = DBConnect().get_db()
    cursor = db.tailoring_records.find(
        {"user_email": user_email},
        {
            "record_id": 1,
            "jd_analysis.job_title": 1,
            "jd_analysis.company": 1,
            "jd_analysis.job_url": 1,
            "jd_analysis.requisition_id": 1,
            "job_url": 1,
            "application.status": 1,
            "application.recruiter_email": 1,
            "application.job_url": 1,
            "application.requisition_id": 1,
            "application.gmail_evidence_ms": 1,
            "application.updated_at": 1,
            "application.gmail_updated_at": 1,
            "application.applied_date": 1,
            "created_at": 1,
            "_id": 0,
        },
    )
    return list(cursor)


def _apply_status(user_email: str, record_id: str, status: str,
                  expected_status: Optional[str] = None,
                  evidence_ms: Optional[int] = None) -> bool:
    """Persist a status change directly. Mirrors the PATCH /application logic minimally."""
    if status not in VALID_STATUSES:
        return False
    db = DBConnect().get_db()
    record = db.tailoring_records.find_one({"record_id": record_id, "user_email": user_email}, {"application": 1})
    if not record:
        return False
    application = record.get("application") or {}
    updates: Dict[str, Any] = {
        "application.status": status,
        "application.updated_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    if evidence_ms:
        updates["application.gmail_updated_at"] = updates["application.updated_at"]
    if status == "applied" and not application.get("applied_at"):
        applied_at = updates.setdefault(
            "application.applied_at",
            datetime.fromtimestamp(evidence_ms / 1000, timezone.utc) if evidence_ms else datetime.now(timezone.utc))
        if evidence_ms:
            updates["application.applied_at_source"] = "gmail_receipt"
        # Label the civil day so Gmail-detected applications are visible to the
        # tracker's applied-date filter. No browser here, so the zone comes from
        # the user's stored preferences. Best-effort — never fail a sync on it.
        try:
            from services.user_time import applied_day_for_user
            (updates["application.applied_date"],
             updates["application.applied_tz"]) = applied_day_for_user(
                applied_at, user_email)
        except Exception as e:
            logger.warning("[gmail-sync] applied_date derivation failed: %s", e)
    query = {"record_id": record_id, "user_email": user_email}
    if expected_status is not None:
        if expected_status == "draft":
            query["$or"] = [{"application.status": "draft"}, {"application.status": {"$exists": False}}]
        else:
            query["application.status"] = expected_status
    update = {"$set": updates}
    if evidence_ms:
        update["$max"] = {"application.gmail_evidence_ms": evidence_ms}
    # The stage was reached when the employer wrote, not when this sync ran.
    from services import application_stages
    reached = application_stages.first_reached(
        status, datetime.fromtimestamp(evidence_ms / 1000, timezone.utc) if evidence_ms else None)
    if reached:
        update["$min"] = reached
    res = db.tailoring_records.update_one(
        query,
        update,
    )
    matched = bool(res.matched_count)
    if matched:
        # E1: also project onto the canonical applications OUTCOME axis (the
        # tailoring_records write above stays the authoritative mirror until E3).
        # gmail's VALID_STATUSES are all members of OUTCOME_STATUSES, so the value
        # maps 1:1. Lazy import avoids an import cycle + cold-start cost; any
        # canonical-write hiccup is swallowed so it can never break the gmail flow.
        try:
            from services import application_ledger_service as _ledger
            _ledger.set_outcome(
                user_email, record_id=record_id, outcome=status, source="gmail"
            )
        except Exception as _e:  # pragma: no cover - defensive
            logger.warning("gmail: canonical set_outcome failed (mirror stands): %s", _e)
    return matched


def sync_user(user_email: str) -> Dict[str, Any]:
    """One bounded batch. One active batch per account, including retries.

    Raises GmailSyncError (a RuntimeError) with a code and a user-safe message.
    A connection Google has stopped accepting is marked `needs_reconnect`.
    """
    if not get_connection(user_email):
        raise GmailSyncError("Gmail is not connected.", "gmail_not_connected")
    now = datetime.now(timezone.utc)
    lock_id = uuid.uuid4().hex
    claimed = _connections().update_one(
        {"user_email": user_email, "$or": [
            {"sync_lock_until": {"$exists": False}}, {"sync_lock_until": {"$lte": now}}]},
        {"$set": {"sync_lock_id": lock_id,
                  "sync_lock_until": now + timedelta(seconds=RUN_STALE_SECONDS)}},
    )
    if not claimed.modified_count:
        raise GmailSyncError("A Gmail sync is already running. Try again after it finishes.",
                             "gmail_sync_running")
    try:
        return _sync_user(user_email, lock_id)
    except GmailReconnectRequired as e:
        mark_needs_reconnect(user_email, e.reason)
        raise
    finally:
        _connections().update_one({"user_email": user_email, "sync_lock_id": lock_id},
                                  {"$unset": {"sync_lock_id": "", "sync_lock_until": ""}})


def _sync_user(user_email: str, lock_id: str) -> Dict[str, Any]:
    """Pull recent Gmail, match to records, classify, and write applies/suggestions.

    Returns a summary dict for the API response.
    """
    conn = get_connection(user_email)
    if not conn:
        raise RuntimeError("Gmail is not connected")

    run_id = (conn.get("sync_run") or {}).get("run_id")
    records = _records_for_user(user_email)
    index = _build_record_index(records)
    if not index:
        # Mail is only ever matched against tracked applications, so with none
        # there is nothing to read. Say so, and leave the watermark alone: the
        # first scan after an application is added still covers the full window.
        summary = {"messages_scanned": 0, "auto_applied": 0, "suggested": 0,
                   "ignored": 0, "ambiguous_matches": 0, "has_more": False,
                   "deferred": 0, "model_calls": 0, "records_with_mail": 0,
                   "tracked_applications": 0, "outcome": "no_applications"}
        _connections().update_one({"user_email": user_email, "sync_lock_id": lock_id},
                                  {"$set": {"last_sync_summary": summary}})
        return summary

    since = conn.get("last_synced_at")
    if isinstance(since, datetime) and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    # The last finished scan used older rules: read the whole window again so
    # mail those rules could not read is not lost behind the watermark.
    if since is not None and not conn.get("scan_query") and int(conn.get("scan_version") or 1) < SCAN_VERSION:
        since = None
    first_scan = since is None
    if since is not None and not conn.get("scan_query"):
        since = _since_for_new_records(since, records)
    recruiter_emails = [rec["recruiter_email"] for rec in index if rec["recruiter_email"]]
    try:
        messages = fetch_recent_messages(user_email, since, recruiter_emails)
    except GmailAPIError as e:
        # Don't poison last_synced_at — caller (route) can show a user-facing
        # error and the next sync will retry the same window.
        logger.warning("[gmail-sync] fetch_failed status=%s reason=%s retryable=%s",
                       e.status, e.reason, e.retryable)
        raise e.as_sync_error() from e
    except requests.RequestException as e:
        logger.warning("[gmail-sync] fetch_failed network=%s", type(e).__name__)
        raise GmailSyncError("Gmail could not be reached just now. Try again in a few minutes; "
                             "nothing already checked is lost.", "gmail_error") from e

    def require_same_connection():
        if not _connections().find_one({"user_email": user_email, "sync_lock_id": lock_id}, {"_id": 1}):
            raise GmailSyncError("Gmail connection changed during sync. Start a new sync for the "
                                 "connected mailbox.", "gmail_connection_changed")

    require_same_connection()

    auto_applied = 0
    suggested = 0
    ignored = 0
    skipped_no_match = 0
    skipped_already_in_status = 0
    ambiguous_matches = 0
    deferred = 0
    model_calls = 0
    model_call_limit = _model_call_limit()
    seen_message_ids = set()

    logger.info(
        "[gmail-sync] user=%s records=%d messages_fetched=%d since=%s",
        user_email, len(index), len(messages), since.isoformat() if since else "initial",
    )

    # ── Phase 1: bucket matched messages by record ────────────────────────
    # We classify per-record (not per-message) so the LLM sees the full
    # thread and can resolve sequences like applied → interview → rejected
    # correctly regardless of the order Gmail returned them in.
    record_buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    record_meta_by_id: Dict[str, Dict[str, Any]] = {}

    for msg in messages:
        mid = msg["message_id"]
        if mid in seen_message_ids:
            continue
        seen_message_ids.add(mid)

        match = _match_message_to_record(msg, index)
        if not match:
            skipped_no_match += 1
            ignored += 1
            continue
        record_meta, match_score, match_reason = match
        ambiguous_matches += int(record_meta["requires_record_selection"])
        # Weak/ambiguous emails get their OWN review item. They must never
        # contaminate a confidently matched thread for one of those roles.
        bucket = (record_meta["record_id"] if match_score >= AUTO_APPLY_CONFIDENCE
                  else f"review:{mid}")
        record_meta_by_id[bucket] = record_meta
        record_buckets[bucket].append({
            "msg": msg, "score": match_score, "reason": match_reason,
        })

    # ── Phase 2: classify each record's thread as a whole ─────────────────
    # Re-run cheap deterministic rules for matched mail on replay, allowing
    # corrected rules to recover an earlier uncertain classification. Model
    # attempts are optional, bounded, and skipped for already-reviewed mail.
    for bucket, items in record_buckets.items():
        require_same_connection()
        rec_meta = record_meta_by_id.get(bucket)
        if not rec_meta:
            continue
        record_id = rec_meta["record_id"]

        # Newest first — the LLM weighs the most recent email most heavily.
        items.sort(key=lambda x: x["msg"].get("internal_date", 0), reverse=True)
        msg_ids = [it["msg"]["message_id"] for it in items]

        msgs_for_llm = [it["msg"] for it in items[:10]]  # cap at 10 emails for token budget

        # Tier 1: deterministic regex extraction. Runs even if the LLM call
        # later fails, so a clear "we have decided to move forward with other
        # candidates" still resolves to rejected.
        deterministic = extract_deterministic_signals([it["msg"] for it in items])
        _retract_stale_interviews(user_email, rec_meta, msg_ids, deterministic)
        if rec_meta.get("requires_application_match") and not any(
                kind in deterministic for kind in ("receipt", "rejected", "offer", "withdrawn")):
            # An email about a role that is not tracked is worth showing only
            # when it shows the user applied. A stranger asking "are you
            # available for a call" about some other job is not that.
            ignored += len(items)
            continue

        # Tier 2: LLM thread classification. May return None on transient
        # failure — that's fine, deterministic alone can carry the decision.
        # Clear deterministic outcomes need no model call. Optional assistance
        # cannot exceed the batch budget; all other wording stays reviewable.
        llm_cls = None
        # Existing evidence must not repeatedly consume model budget on cursor
        # replays. Rules still run, allowing improved deterministic verdicts.
        previously_reviewed = None
        if not deterministic:
            previously_reviewed = _suggestions().find_one({"user_email": user_email,
                "supporting_message_ids": {"$all": msg_ids},
                "candidate_records.record_id": {"$all": [r["record_id"] for r in rec_meta["candidate_records"]]}})
            if previously_reviewed:
                continue
        if not deterministic and not previously_reviewed and model_calls < model_call_limit:
            model_calls += 1
            llm_cls = classify_record_thread(msgs_for_llm, rec_meta)

        # Reconcile both tiers into a single verdict.
        cls = reconcile_classification(deterministic, llm_cls)
        require_same_connection()
        if not cls or cls["status"] == "ignore":
            # The page cursor can safely advance only after uncertain matched
            # application evidence is durable. No status is invented here.
            cls = {"status": None, "confidence": 0.0, "source": "review",
                   "reason": "Outcome is unclear. Review this email and choose the application status.",
                   "evidence_message_id": items[0]["msg"]["message_id"]}

        new_status = cls["status"]
        confidence = cls["confidence"]
        match_confidence = min(it["score"] for it in items)
        evidence = next((it["msg"] for it in items
                         if it["msg"]["message_id"] == cls.get("evidence_message_id")), items[0]["msg"])
        evidence_ms = evidence.get("internal_date") or 0
        if (not rec_meta["requires_record_selection"] and evidence_ms
                and evidence_ms < rec_meta["gmail_evidence_ms"]):
            ignored += len(items)
            continue
        def epoch_ms(value):
            if not isinstance(value, datetime):
                return 0
            return int(value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp() * 1000)
        status_updated_ms = epoch_ms(rec_meta["status_updated_at"])
        gmail_updated_ms = epoch_ms(rec_meta["gmail_updated_at"])
        if evidence_ms and status_updated_ms > evidence_ms and status_updated_ms > gmail_updated_ms:
            confidence = min(confidence, 0.85)
            cls["reason"] = "Email predates your latest status change. " + cls.get("reason", "")
        identity = "|".join([user_email, new_status or "needs_review", *sorted(msg_ids),
                             *sorted(r["record_id"] for r in rec_meta["candidate_records"])])
        suggestion_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        if _suggestions().find_one({"_id": suggestion_id, "user_email": user_email}, {"_id": 1}):
            # Already recorded. If it was written before the scan could tell
            # hiring mail from other mail, this read has just vouched for it.
            _suggestions().update_one(
                {"_id": suggestion_id, "user_email": user_email, "scan_version": {"$exists": False}},
                {"$set": {"scan_version": SCAN_VERSION}})
            continue

        if new_status and rec_meta["current_status"] == new_status and not rec_meta["requires_record_selection"]:
            skipped_already_in_status += 1
            logger.info(
                "[gmail-sync] already_in_status record=%s status=%s confidence=%.2f",
                record_id, new_status, confidence,
            )
            # Confirmation is new chronological evidence even without a status
            # change. Older pages must not undo it. Do not stamp updated_at: the
            # user has not changed their application and its age still matters.
            if match_confidence >= AUTO_APPLY_CONFIDENCE and confidence >= AUTO_APPLY_CONFIDENCE and evidence_ms:
                DBConnect().get_db().tailoring_records.update_one(
                    {"record_id": record_id, "user_email": user_email,
                     "application.status": new_status},
                    {"$max": {"application.gmail_evidence_ms": evidence_ms}},
                )
            # Only supersede verdicts for messages actually reclassified here.
            # An older receipt must not dismiss a newer pending outcome.
            if match_confidence >= AUTO_APPLY_CONFIDENCE:
                _suggestions().update_many(
                    {"user_email": user_email, "record_id": record_id,
                     "applied": False, "dismissed": False,
                     "suggested_status": {"$ne": new_status},
                     "requires_record_selection": {"$ne": True},
                     "gmail_message_id": {"$in": msg_ids}},
                    {"$set": {"dismissed": True, "dismissed_at": datetime.now(timezone.utc),
                              "dismissed_reason": "superseded by re-classification"}},
                )
            continue

        # Funnel-backwards moves are usually wrong (rejected→interviewing,
        # offer→applied). We still SUGGEST them but never auto-apply, so the
        # user has a chance to confirm.
        backwards = bool(new_status and _is_funnel_backwards(rec_meta["current_status"], new_status))
        if backwards:
            logger.info(
                "[gmail-sync] backwards_move record=%s %s→%s — suggesting only",
                record_id, rec_meta["current_status"], new_status,
            )

        # Before writing a fresh suggestion, supersede any prior unresolved
        # suggestions on the same record. This prevents the suggestions panel
        # from accumulating duplicates as we re-classify on every sync, and
        # ensures the user always sees the *current* verdict.
        if new_status and match_confidence >= AUTO_APPLY_CONFIDENCE:
            _suggestions().update_many(
                {"user_email": user_email, "record_id": record_id,
                 "applied": False, "dismissed": False,
                 "requires_record_selection": {"$ne": True},
                 "gmail_message_id": {"$in": msg_ids}},
                {"$set": {"dismissed": True, "dismissed_at": datetime.now(timezone.utc),
                          "dismissed_reason": "superseded by re-classification"}},
            )

        # Use the newest message as the canonical reference; the suggestion
        # row carries supporting_message_ids so we can show the thread later.
        primary = evidence
        suggestion = {
            "_id": suggestion_id,
            "suggestion_id": suggestion_id,
            "user_email": user_email,
            "record_id": record_id,
            "company": rec_meta["company"],
            "title": rec_meta["title"],
            "gmail_message_id": primary["message_id"],
            "gmail_thread_id": primary.get("thread_id"),
            "from_name": primary.get("from_name"),
            "from_address": primary.get("from_address"),
            "subject": primary.get("subject"),
            "snippet": primary.get("snippet"),
            "current_status": rec_meta["current_status"],
            "suggested_status": new_status,
            "requires_status_selection": new_status is None,
            "requires_application_match": rec_meta.get("requires_application_match", False),
            "email_role": rec_meta.get("email_role"),
            "email_roles": _explicit_role_titles(primary, index),
            "email_requisition_ids": sorted(_requisition_ids(_application_text(primary))),
            "confidence": confidence,
            "match_confidence": match_confidence,
            "match_reason": items[0]["reason"],
            "candidate_records": rec_meta["candidate_records"],
            "requires_record_selection": rec_meta["requires_record_selection"],
            "email_received_at": (datetime.fromtimestamp(primary["internal_date"] / 1000, timezone.utc).isoformat()
                                  if primary.get("internal_date") else None),
            "reason": cls.get("reason", ""),
            "source": cls.get("source", "llm"),
            "evidence_message_id": cls.get("evidence_message_id"),
            "supporting_message_ids": msg_ids,
            "applied": False,
            "dismissed": False,
            "auto_applied": False,
            "sync_run_id": run_id,
            "scan_version": SCAN_VERSION,
            "created_at": datetime.now(timezone.utc),
        }

        inserted = _suggestions().update_one(
            {"_id": suggestion_id}, {"$setOnInsert": suggestion}, upsert=True)
        if not inserted.upserted_id:
            continue
        deferred += int(new_status is None)
        if confidence >= AUTO_APPLY_CONFIDENCE and match_confidence >= AUTO_APPLY_CONFIDENCE \
                and not backwards and not rec_meta["requires_record_selection"] \
                and _apply_status(user_email, record_id, new_status,
                                  expected_status=rec_meta["current_status"], evidence_ms=evidence_ms):
            suggestion["applied"] = True
            suggestion["auto_applied"] = True
            suggestion["applied_at"] = datetime.now(timezone.utc)
            rec_meta["current_status"] = new_status
            auto_applied += 1
            logger.info(
                "[gmail-sync] auto_applied record=%s %s→%s confidence=%.2f",
                record_id, suggestion["current_status"], new_status, confidence,
            )
        else:
            suggested += 1
            logger.info(
                "[gmail-sync] suggested record=%s %s→%s confidence=%.2f%s",
                record_id, suggestion["current_status"], new_status, confidence,
                " (backwards)" if backwards else "",
            )

        _suggestions().update_one({"_id": suggestion_id, "user_email": user_email},
                                  {"$set": {k: v for k, v in suggestion.items() if k != "_id"}})

    logger.info(
        "[gmail-sync] done user=%s scanned=%d records_with_mail=%d auto=%d suggest=%d "
        "ignored=%d skipped_nomatch=%d skipped_same_status=%d",
        user_email, len(messages), len(record_buckets), auto_applied, suggested, ignored,
        skipped_no_match, skipped_already_in_status,
    )

    summary = {
        "messages_scanned": len(messages),
        "records_with_mail": len(record_buckets),
        "auto_applied": auto_applied,
        "suggested": suggested,
        "ignored": ignored,
        "skipped_no_match": skipped_no_match,
        "skipped_already_in_status": skipped_already_in_status,
        "ambiguous_matches": ambiguous_matches,
        "deferred": deferred,
        "model_calls": model_calls,
        "has_more": bool(getattr(messages, "next_page_token", None)),
        "tracked_applications": len(index),
        "skipped_own": int(getattr(messages, "skipped_own", 0) or 0),
    }
    if first_scan:
        summary["window_days"] = INITIAL_LOOKBACK_DAYS
    summary["outcome"] = _run_outcome(summary)
    state = {"last_sync_summary": summary}
    batch_version = int(getattr(messages, "scan_version", SCAN_VERSION) or SCAN_VERSION)
    if summary["has_more"]:
        state.update({"scan_next_page_token": messages.next_page_token,
                      "scan_query": messages.scan_query, "scan_before": messages.scan_before,
                      "scan_cursor_version": batch_version})
        update = {"$set": state}
    else:
        state["last_synced_at"] = getattr(messages, "scan_before", None) or datetime.now(timezone.utc)
        state["scan_version"] = batch_version
        update = {"$set": state, "$unset": {"scan_next_page_token": "", "scan_query": "",
                                            "scan_before": "", "scan_cursor_version": ""}}
    completed = _connections().update_one({"user_email": user_email, "sync_lock_id": lock_id}, update)
    if not completed.matched_count:
        raise GmailSyncError("Gmail connection changed during sync. Start a new sync for the "
                             "connected mailbox.", "gmail_connection_changed")
    return summary


def _as_utc(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _since_for_new_records(since: datetime, records: List[Dict[str, Any]]) -> datetime:
    """Start earlier when an application was added after the last finished scan.

    Its emails may be older than the watermark (apply on Monday, track it on
    Wednesday), and mail before the watermark is otherwise never read again.
    Bounded by the first-scan window. Replayed mail is safe: suggestion ids are
    deterministic, and evidence older than a record's newest is ignored.
    """
    floor = datetime.now(timezone.utc) - timedelta(days=INITIAL_LOOKBACK_DAYS)
    start = since
    for record in records:
        created = _as_utc(record.get("created_at"))
        if not created or created <= since:
            continue
        applied = _as_utc((record.get("application") or {}).get("applied_date"))
        start = min(start, min(created, applied or created) - timedelta(days=1))
    return max(start, floor)


def _run_outcome(totals: Dict[str, Any]) -> str:
    """Why a finished scan ended the way it did, in one word for the client."""
    if not totals.get("tracked_applications"):
        return "no_applications"
    if not totals.get("messages_scanned"):
        return "no_mail"
    if totals.get("auto_applied") or totals.get("suggested"):
        return "updated"
    if not totals.get("records_with_mail"):
        return "no_matches"
    return "up_to_date"


# ---------------------------------------------------------------------------
# Runs: one press of Sync, read to completion in slices
# ---------------------------------------------------------------------------

_RUN_COUNTERS = ("messages_scanned", "auto_applied", "suggested", "deferred", "ignored",
                 "records_with_mail")


def _run_is_live(run: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> bool:
    if not run or run.get("state") != "running":
        return False
    beat = _as_utc(run.get("updated_at"))
    now = now or datetime.now(timezone.utc)
    return bool(beat and (now - beat).total_seconds() < RUN_STALE_SECONDS)


def start_run(user_email: str, trigger: str = "manual") -> Dict[str, Any]:
    """Claim the account's run. Exactly one caller gets `already_running: False`."""
    now = datetime.now(timezone.utc)
    run = {"run_id": uuid.uuid4().hex, "trigger": trigger, "state": "running",
           "started_at": now, "updated_at": now, "finished_at": None, "batches": 0,
           "outcome": None, "error_code": None, "error": None,
           **{key: 0 for key in _RUN_COUNTERS}}
    claimed = _connections().update_one(
        {"user_email": user_email, "$or": [
            {"sync_run": {"$exists": False}},
            {"sync_run.state": {"$ne": "running"}},
            {"sync_run.updated_at": {"$lt": now - timedelta(seconds=RUN_STALE_SECONDS)}}]},
        {"$set": {"sync_run": run}})
    if claimed.modified_count:
        return {"run_id": run["run_id"], "already_running": False}
    conn = get_connection(user_email)
    if not conn:
        raise GmailSyncError("Gmail is not connected.", "gmail_not_connected")
    return {"run_id": (conn.get("sync_run") or {}).get("run_id"), "already_running": True}


def _finish_run(user_email: str, run_id: str, state: str, *, outcome: Optional[str] = None,
                error: Optional[GmailSyncError] = None, summary: Optional[Dict[str, Any]] = None) -> None:
    now = datetime.now(timezone.utc)
    update: Dict[str, Any] = {"sync_run.state": state, "sync_run.updated_at": now,
                              "sync_run.finished_at": None if state == "paused" else now,
                              "sync_run.outcome": outcome,
                              "sync_run.error_code": error.code if error else None,
                              "sync_run.error": str(error) if error else None}
    if summary is not None:
        update["last_sync_summary"] = summary
    _connections().update_one({"user_email": user_email, "sync_run.run_id": run_id}, {"$set": update})


def pause_run(user_email: str, run_id: str) -> None:
    """Mail remains and nothing is reading it. The scheduled sync continues it."""
    _finish_run(user_email, run_id, "paused")


def run_slice(user_email: str, run_id: Optional[str] = None, *, trigger: str = "manual",
              budget_seconds: Optional[float] = None) -> Dict[str, Any]:
    """Read batches for one run until the mailbox window is finished or the
    slice's time is used. Returns the run totals plus `continue` when more mail
    remains; the caller starts the next slice or pauses the run.

    Every failure is recorded on the run and raised as GmailSyncError.
    """
    budget = RUN_SLICE_SECONDS if budget_seconds is None else budget_seconds
    if not run_id:
        claimed = start_run(user_email, trigger)
        if claimed["already_running"]:
            return {"run_id": claimed["run_id"], "already_running": True, "continue": False}
        run_id = claimed["run_id"]
    started = time.monotonic()
    try:
        while True:
            conn = get_connection(user_email)
            run = (conn or {}).get("sync_run") or {}
            if run.get("run_id") != run_id or run.get("state") not in {"running", "paused"}:
                # Disconnected, reconnected, or replaced by a newer run.
                return {"run_id": run_id, "superseded": True, "continue": False}
            batch = sync_user(user_email)
            inc = {f"sync_run.{key}": int(batch.get(key) or 0) for key in _RUN_COUNTERS}
            inc["sync_run.batches"] = 1
            _connections().update_one(
                {"user_email": user_email, "sync_run.run_id": run_id},
                {"$inc": inc, "$set": {"sync_run.state": "running",
                                       "sync_run.updated_at": datetime.now(timezone.utc)}})
            totals = _run_totals(user_email, run_id, batch)
            if not batch.get("has_more"):
                totals["has_more"] = False
                totals["outcome"] = _run_outcome(totals)
                _finish_run(user_email, run_id, "completed", outcome=totals["outcome"], summary=totals)
                return {**totals, "run_id": run_id, "continue": False}
            if time.monotonic() - started >= budget:
                return {**totals, "run_id": run_id, "has_more": True, "continue": True}
    except GmailSyncError as e:
        _finish_run(user_email, run_id, "failed", error=e)
        raise
    except Exception as e:
        logger.exception("[gmail-sync] run failed user=%s: %s", user_email, type(e).__name__)
        safe = GmailSyncError("Gmail sync stopped because of a problem on Aspirely's side. "
                              "Try again in a few minutes; nothing already checked is lost.")
        _finish_run(user_email, run_id, "failed", error=safe)
        raise safe from e


def _run_totals(user_email: str, run_id: str, batch: Dict[str, Any]) -> Dict[str, Any]:
    run = ((get_connection(user_email) or {}).get("sync_run") or {})
    totals = {key: int(run.get(key) or 0) for key in _RUN_COUNTERS}
    totals["batches"] = int(run.get("batches") or 0)
    for key in ("tracked_applications", "window_days"):
        if key in batch:
            totals[key] = batch[key]
    return totals


def sync_due(budget_seconds: float = 600.0, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The scheduled background sync: every connection that is due, one user at
    a time, round-robin inside the budget. Never raises for one user's failure.
    """
    if not auto_sync_enabled():
        return {"enabled": False}
    now = now or datetime.now(timezone.utc)
    started = time.monotonic()
    db = DBConnect().get_db()
    stale_before = now - timedelta(hours=auto_sync_interval_hours())
    active_since = now - timedelta(days=_config_int("GMAIL_AUTO_SYNC_ACTIVE_DAYS", 30, 1, 365))
    due: List[Tuple[int, datetime, str]] = []
    skipped = 0
    for conn in _connections().find({"needs_reconnect": {"$ne": True}}, {
            "user_email": 1, "last_synced_at": 1, "scan_next_page_token": 1, "sync_run": 1}):
        email = conn.get("user_email")
        last = _as_utc(conn.get("last_synced_at"))
        unfinished = bool(conn.get("scan_next_page_token"))
        if not email or _run_is_live(conn.get("sync_run"), now):
            continue
        if not (unfinished or last is None or last < stale_before):
            continue
        # Reading the mail of someone who has stopped using the product, or who
        # tracks nothing to match it against, serves no feature.
        user = db.users.find_one({"email": email}, {"last_login": 1}) or {}
        seen = _as_utc(user.get("last_login"))
        if (seen and seen < active_since) or not db.tailoring_records.find_one(
                {"user_email": email}, {"_id": 1}):
            skipped += 1
            continue
        due.append((0 if unfinished else 1, last or datetime.min.replace(tzinfo=timezone.utc), email))
    due.sort()
    result = {"enabled": True, "due": len(due), "skipped": skipped, "synced": 0,
              "finished": 0, "failed": 0, "needs_reconnect": 0, "behind": 0}
    pending = [(email, None) for _, _, email in due]
    touched = set()
    while pending:
        carry = []
        for email, run_id in pending:
            # At least one user per tick, so a slow box still makes progress.
            if touched and time.monotonic() - started > budget_seconds:
                carry.append((email, run_id))
                continue
            touched.add(email)
            try:
                out = run_slice(email, run_id, trigger="scheduled", budget_seconds=60.0)
            except GmailReconnectRequired:
                result["needs_reconnect"] += 1
                continue
            except GmailSyncError as e:
                result["failed"] += 1
                logger.info("[gmail-sync] scheduled user failed code=%s", e.code)
                continue
            if out.get("continue"):
                carry.append((email, out["run_id"]))
            elif not out.get("already_running") and not out.get("superseded"):
                result["finished"] += 1
        if carry and time.monotonic() - started > budget_seconds:
            for email, run_id in carry:
                if run_id:
                    pause_run(email, run_id)
            result["behind"] = len(carry)
            break
        pending = carry
    result["synced"] = len(touched)
    return result


# ---------------------------------------------------------------------------
# Status, activity and undo (used by the blueprint)
# ---------------------------------------------------------------------------

def _iso(value: Any) -> Optional[str]:
    value = _as_utc(value)
    return value.isoformat() if value else None


def status_payload(user_email: str, light: bool = False) -> Dict[str, Any]:
    """Everything a client needs to pick a screen. `state` is the one field
    that decides it; see docs/specs/gmail-tracker-contract.md."""
    configured = gmail_configured() and gmail_oauth_enabled()
    conn = get_connection(user_email) if configured else None
    mode = consent_mode()
    payload: Dict[str, Any] = {
        "configured": configured, "connected": bool(conn), "state": "unavailable",
        "gmail_address": None, "linked_at": None, "last_synced_at": None,
        "pending_suggestions": 0, "has_more": False, "last_sync_summary": None,
        "needs_reconnect": False, "reconnect_reason": None, "tracked_applications": 0,
        "sync": None,
        "auto_sync": {"enabled": auto_sync_enabled(), "interval_hours": auto_sync_interval_hours()},
        "access": {"mode": mode, "expires_at": None},
    }
    if not configured:
        return payload
    # Whether this person may start Google's screen at all (see
    # gmail_access_service): `gate`, `state`, and the address they asked for.
    from services import gmail_access_service
    payload["access"].update(gmail_access_service.access_for(user_email, bool(conn)))
    payload["tracked_applications"] = DBConnect().get_db().tailoring_records.count_documents(
        {"user_email": user_email})
    if not conn:
        payload["state"] = "not_connected"
        return payload
    run = conn.get("sync_run") or None
    live = _run_is_live(run)
    has_more = bool(conn.get("scan_next_page_token"))
    linked = _as_utc(conn.get("linked_at"))
    if mode == "testing" and linked:
        payload["access"]["expires_at"] = (linked + timedelta(days=TESTING_ACCESS_DAYS)).isoformat()
    sync = None
    if run:
        state = run.get("state")
        if state == "running" and not live:
            state = "paused"  # the worker is gone; the scheduled sync takes it over
        sync = {key: run.get(key) for key in (
            "run_id", "trigger", "batches", "outcome", "error_code", "error", *_RUN_COUNTERS)}
        sync.update(state=state, started_at=_iso(run.get("started_at")),
                    updated_at=_iso(run.get("updated_at")), finished_at=_iso(run.get("finished_at")))
    payload.update({
        "gmail_address": conn.get("gmail_address"),
        "linked_at": _iso(linked),
        "last_synced_at": _iso(conn.get("last_synced_at")),
        # The validated review list, with the same evidence exclusions as
        # GET /gmail/suggestions. Skipped while a client is polling progress.
        "pending_suggestions": None if light else len(list_suggestions(user_email, limit=None)),
        "has_more": has_more,
        "last_sync_summary": conn.get("last_sync_summary"),
        "needs_reconnect": bool(conn.get("needs_reconnect")),
        "reconnect_reason": conn.get("reconnect_reason") if conn.get("needs_reconnect") else None,
        "sync": sync,
    })
    if payload["needs_reconnect"]:
        payload["state"] = "needs_reconnect"
    elif live:
        payload["state"] = "syncing"
    elif has_more or (sync and sync["state"] == "paused"):
        payload["state"] = "paused"
    elif not conn.get("last_synced_at") and not conn.get("last_sync_summary"):
        payload["state"] = "never_synced"
    else:
        payload["state"] = "synced"
    return payload


def health_report() -> Dict[str, Any]:
    """Read-only. Which settings are present (never their values) and how the
    connections stand. For the owner, on the box:

        docker compose --env-file /etc/jobpilot/compose.env exec worker python -c \
          "from services.gmail_service import print_health; print_health()"
    """
    now = datetime.now(timezone.utc)
    connections = list(_connections().find({}, {
        "needs_reconnect": 1, "last_synced_at": 1, "linked_at": 1, "scan_next_page_token": 1}))
    week = now - timedelta(days=TESTING_ACCESS_DAYS)
    return {
        "settings_present": {key: bool(_get_config_value(key, "")) for key in (
            "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI",
            "GMAIL_TOKEN_ENCRYPTION_KEY")},
        "redirect_uri": _redirect_uri(),
        "gmail_oauth_enabled": gmail_oauth_enabled(),
        "connect_is_offered": gmail_configured() and gmail_oauth_enabled(),
        "consent_mode": consent_mode(),
        "auto_sync": {"enabled": auto_sync_enabled(), "interval_hours": auto_sync_interval_hours()},
        "connections": {
            "total": len(connections),
            "needs_reconnect": sum(bool(c.get("needs_reconnect")) for c in connections),
            "never_finished_a_scan": sum(not c.get("last_synced_at") for c in connections),
            "scan_unfinished": sum(bool(c.get("scan_next_page_token")) for c in connections),
            "linked_over_7_days_ago": sum(
                bool(_as_utc(c.get("linked_at")) and _as_utc(c.get("linked_at")) < week)
                for c in connections),
        },
    }


def print_health() -> None:
    print(json.dumps(health_report(), indent=2, default=str))


def list_activity(user_email: str, limit: int = 20) -> List[Dict[str, Any]]:
    """What sync changed by itself: which application moved, from what to
    what, and the email that caused it. Newest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ACTIVITY_DAYS)
    docs = list(_suggestions().find(
        {"user_email": user_email, "auto_applied": True, "applied_at": {"$gte": cutoff}},
    ).sort([("applied_at", -1), ("_id", -1)]).limit(max(1, min(int(limit), 50))))
    record_ids = [d.get("record_id") for d in docs if d.get("record_id")]
    current = {r["record_id"]: (r.get("application") or {}).get("status") or "draft"
               for r in DBConnect().get_db().tailoring_records.find(
                   {"user_email": user_email, "record_id": {"$in": record_ids}},
                   {"record_id": 1, "application.status": 1, "_id": 0})}
    changes = []
    for d in docs:
        undone = bool(d.get("undone"))
        changes.append({
            "suggestion_id": d.get("suggestion_id"), "record_id": d.get("record_id"),
            "company": d.get("company") or "", "title": d.get("title") or "",
            "from_status": d.get("current_status") or "draft",
            "to_status": d.get("suggested_status"),
            "subject": html.unescape(d.get("subject") or ""),
            "from_address": d.get("from_address") or "",
            "email_received_at": d.get("email_received_at"),
            "applied_at": _iso(d.get("applied_at")), "sync_run_id": d.get("sync_run_id"),
            "undone": undone,
            "undoable": not undone and current.get(d.get("record_id")) == d.get("suggested_status"),
        })
    return changes


def _retract_stale_interviews(user_email: str, rec_meta: Dict[str, Any], msg_ids: List[str],
                              deterministic: Dict[str, Any]) -> int:
    """Put back an automatic Interviewing move its own email no longer supports.

    Before scan version 3, receipt boilerplate ("a recruiter will reach out to
    schedule a call if your background is a match") moved cards by itself. On
    the re-read, when the same email now carries no stated interview, the move
    is undone exactly as the user's Undo would, unless the person has changed
    the application since. Returns how many were put back.
    """
    if (deterministic.get("interviewing") or {}).get("strength") == "strong" or not msg_ids:
        return 0
    record_id = rec_meta.get("record_id")
    if not record_id or rec_meta.get("current_status") != "interviewing":
        return 0
    moved = list(_suggestions().find({
        "user_email": user_email, "record_id": record_id, "auto_applied": True, "applied": True,
        "undone": {"$ne": True}, "suggested_status": "interviewing",
        "$or": [{"gmail_message_id": {"$in": msg_ids}}, {"evidence_message_id": {"$in": msg_ids}}]}))
    retracted = 0
    for s in moved:
        applied_at = _as_utc(s.get("applied_at"))
        changed_at = _as_utc(rec_meta.get("status_updated_at"))
        if applied_at and changed_at and (changed_at - applied_at).total_seconds() > 5:
            continue  # the person has touched this application since
        try:
            undo_auto_apply(user_email, s["suggestion_id"])
        except Exception as e:  # GmailUndoError, LookupError: leave it as it is
            logger.info("[gmail-sync] retraction skipped record=%s: %s", record_id, type(e).__name__)
            continue
        _suggestions().update_one(
            {"suggestion_id": s["suggestion_id"], "user_email": user_email},
            {"$set": {"dismissed_reason": "not an interview on a closer read",
                      "retracted_by_rules": True}})
        rec_meta["current_status"] = s.get("current_status") or "applied"
        retracted += 1
        logger.info("[gmail-sync] retracted interviewing record=%s back to %s",
                    record_id, rec_meta["current_status"])
    return retracted


def undo_auto_apply(user_email: str, suggestion_id: str) -> Dict[str, Any]:
    """Put an application back where it was before an automatic move.

    Written as the user's own status change, so the same email (and anything
    older) can only ever come back as a suggestion, never as another move.
    """
    s = _suggestions().find_one({"suggestion_id": suggestion_id, "user_email": user_email})
    if not s:
        raise LookupError("Suggestion not found")
    if not s.get("auto_applied") or s.get("undone") or not s.get("applied") or not s.get("record_id"):
        raise GmailUndoError("not_undoable")
    to_status = s.get("suggested_status")
    from_status = s.get("current_status") or "draft"
    db = DBConnect().get_db()
    record = db.tailoring_records.find_one(
        {"record_id": s["record_id"], "user_email": user_email}, {"application": 1}) or {}
    application = record.get("application") or {}
    now = datetime.now(timezone.utc)
    update: Dict[str, Any] = {"$set": {"application.status": from_status,
                                       "application.updated_at": now, "updated_at": now}}
    if to_status == "applied" and application.get("applied_at_source") == "gmail_receipt":
        # The applied date came from this email too.
        update["$unset"] = {key: "" for key in (
            "application.applied_at", "application.applied_at_source",
            "application.applied_date", "application.applied_tz")}
    # Forget the stage date this move wrote, and only that one: an application
    # that had reached the stage before keeps its earlier date.
    stage_date = _as_utc((application.get("stage_dates") or {}).get(to_status))
    moved_at = _as_utc(s.get("email_received_at"))
    if stage_date and moved_at and abs((stage_date - moved_at).total_seconds()) < 2:
        update.setdefault("$unset", {})[f"application.stage_dates.{to_status}"] = ""
    restored = db.tailoring_records.update_one(
        {"record_id": s["record_id"], "user_email": user_email, "application.status": to_status},
        update)
    if not restored.matched_count:
        raise GmailUndoError("status_changed")
    try:
        from services import application_ledger_service as _ledger
        outcome = from_status if from_status in _ledger.OUTCOME_STATUSES else _ledger.OUTCOME_NONE
        _ledger.set_outcome(user_email, record_id=s["record_id"], outcome=outcome, source="manual")
    except Exception as _e:  # pragma: no cover - the mirror above stands
        logger.warning("gmail: canonical undo failed (mirror stands): %s", _e)
    changes = {"applied": False, "dismissed": True, "dismissed_at": now,
               "dismissed_reason": "undone by user", "undone": True, "undone_at": now}
    _suggestions().update_one({"suggestion_id": suggestion_id, "user_email": user_email},
                              {"$set": changes})
    s.update(changes)
    return {"suggestion": _serialize_suggestion(s), "restored_status": from_status}


# ---------------------------------------------------------------------------
# Suggestion CRUD (used by the blueprint)
# ---------------------------------------------------------------------------

def _serialize_suggestion(s: Dict[str, Any]) -> Dict[str, Any]:
    s.pop("_id", None)
    for key in ("resolution_lock_id", "resolution_lock_until", "resolution_record_id", "resolution_status"):
        s.pop(key, None)
    for k in ("created_at", "applied_at", "dismissed_at", "undone_at"):
        v = s.get(k)
        if hasattr(v, "isoformat"):
            s[k] = v.isoformat()
    for k in ("subject", "snippet", "from_name"):
        if isinstance(s.get(k), str):
            s[k] = html.unescape(s[k])
    return s


def _unmatched_suggestion(s: Dict[str, Any], index: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Clear stale application metadata without guessing the email's employer."""
    primary = _primary_employers({"subject": s.get("subject")}, index)
    names = {rec["company"] for rec in index
             if primary and len(primary) == 1 and rec["company_token"] in primary}
    # Use a readable owned-record name only for one evidenced employer. A
    # legacy primary candidate may be the footer brand that was just excluded.
    company = sorted(names, key=lambda name: (len(name), name.casefold()))[0] if names else ""
    result = dict(s)
    result.update(record_id=None, company=company, current_status=None,
                  candidate_records=[], requires_record_selection=True,
                  requires_application_match=True, match_confidence=0.0,
                  match_reason="No tracked application matches this email's role or requisition")
    return result


def _validated_suggestion(s: Dict[str, Any], index: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Read-only repair of review choices whose retained evidence contradicts them.

    Old rows have subject/sender/snippet, not full bodies. Only clear employer
    contradictions and recommendations are excluded; absence of evidence alone
    does not invalidate a legacy review item. Nothing here rewrites history.
    """
    if s.get("applied") or s.get("dismissed"):
        return dict(s)
    if _is_irrelevant_mail(s):
        return None
    # A row from before scans could tell hiring mail from other mail. If what
    # was kept of the email (subject and excerpt) never mentions hiring, it
    # would not be written today, so it is not shown. Nothing is deleted: the
    # next scan re-reads the email and marks the row if it belongs.
    if not s.get("scan_version") and s.get("subject") and s.get("snippet") and not _about_an_application(s):
        return None
    recovering_match = bool(s.get("requires_application_match"))
    # Snippets are partial; only a subject employer can reassign legacy choices.
    primary = _primary_employers({"subject": s.get("subject")}, index)
    # A legacy snippet may end halfway through a title. Only complete prose
    # sentences can eliminate a candidate; subject titles are already bounded.
    snippet = str(s.get("snippet") or "")
    complete = re.match(r"(?s)^(.*[.!?\n])", snippet)
    explicit_roles = list(dict.fromkeys(_clean_role_title(role) for role in s.get("email_roles", [])
                                       if isinstance(role, str) and _ROLE_NOUN.search(role)))
    explicit_roles = explicit_roles or _explicit_role_titles(
        {"subject": s.get("subject"), "body": complete.group(1) if complete else ""}, index)
    partial = re.search(r"\b(?:application for|forward for)\s+(?:the\s+)?([^.!?\n]{2,140})$", snippet, re.I)
    partial_role = ""
    if partial and _ROLE_NOUN.search(partial.group(1)):
        partial_role = _clean_role_title(partial.group(1))
    email_requisitions = set(s.get("email_requisition_ids") or _requisition_ids(_application_text(s)))
    email_requisitions.update(_requisition_ids(" ".join(
        role for role in s.get("email_roles", []) if isinstance(role, str))))
    records = {rec["record_id"]: rec for rec in index}
    old_candidates = s.get("candidate_records") or [{"record_id": s.get("record_id"),
                                                    "company": s.get("company", ""), "title": s.get("title", "")}]
    if recovering_match:
        # Parser fixes may recover choices, but only from retained role evidence
        # and one evidenced employer. GET never applies a recovered verdict.
        if not explicit_roles or not primary or len(primary) != 1:
            return _unmatched_suggestion(s, index)
        old_candidates = [_candidate_record(rec) for rec in index if rec["company_token"] in primary]
    candidates = []
    eligible_employer_records = []
    for candidate in old_candidates:
        rec = records.get(candidate.get("record_id"))
        if rec is None:
            continue
        value = _candidate_record(rec)
        if primary is not None and _company_token(value.get("company", "")) not in primary:
            continue
        eligible_employer_records.append(rec)
        if (
                not explicit_roles or _normalize(value.get("title", "")) in explicit_roles) and (
                not partial_role or _normalize(value.get("title", "")).startswith(partial_role)) and (
                not email_requisitions or not rec["requisition_ids"] or email_requisitions.intersection(rec["requisition_ids"])):
            candidates.append(value)
    if not candidates:
        if (explicit_roles or partial_role or email_requisitions) and eligible_employer_records:
            result = _unmatched_suggestion(s, index)
            role = explicit_roles[0] if explicit_roles else (partial_role + "…" if partial_role else "Application in email")
            result.update(title=role, email_role=role)
            return result
        if recovering_match:
            return _unmatched_suggestion(s, index)
        return None
    result = dict(s)
    selected = next((r for r in candidates if r["record_id"] == s.get("record_id")), candidates[0])
    result.update({key: selected.get(key, "") for key in ("record_id", "company", "title")})
    result["candidate_records"] = candidates
    result["requires_record_selection"] = recovering_match or len(candidates) > 1
    if recovering_match:
        selected_meta = records[selected["record_id"]]
        result.update(requires_application_match=False, current_status=selected_meta["current_status"],
                      match_confidence=0.85,
                      match_reason="Role evidence identifies a possible application; confirm it before updating")
    return result


def list_suggestions(user_email: str, include_resolved: bool = False,
                     limit: Optional[int] = 100) -> List[Dict[str, Any]]:
    q: Dict[str, Any] = {"user_email": user_email}
    if not include_resolved:
        q["applied"] = False
        q["dismissed"] = False
    docs = _suggestions().find(q).sort([("created_at", -1), ("_id", -1)])
    index = _build_record_index(_records_for_user(user_email))
    visible = []
    for doc in docs:
        validated = _validated_suggestion(doc, index)
        if validated is not None:
            visible.append(_serialize_suggestion(validated))
            if limit is not None and len(visible) >= limit:
                break
    return visible


def apply_suggestion(user_email: str, suggestion_id: str,
                     record_id: Optional[str] = None, status: Optional[str] = None) -> Dict[str, Any]:
    s = _suggestions().find_one({"suggestion_id": suggestion_id, "user_email": user_email})
    if not s:
        raise LookupError("Suggestion not found")
    if s.get("applied") or s.get("dismissed"):
        return _serialize_suggestion(s)
    if status is not None and (not isinstance(status, str) or status not in VALID_STATUSES):
        raise ValueError("Choose a valid application status")
    if s.get("requires_status_selection"):
        if not status:
            raise ValueError("Choose the application status shown by this email")
        if not record_id:
            raise ValueError("Choose the application that this email belongs to")
        selected_status = status
    else:
        selected_status = s.get("suggested_status")
        if selected_status not in VALID_STATUSES or (status and status != selected_status):
            raise ValueError("The selected status does not match this suggestion")
    # Retain ownership validation for explicitly selected legacy candidates.
    if record_id and record_id in {r.get("record_id") for r in s.get("candidate_records", [])}:
        if not DBConnect().get_db().tailoring_records.find_one(
                {"record_id": record_id, "user_email": user_email}, {"_id": 1}):
            raise LookupError("Application not found")
    s = _validated_suggestion(s, _build_record_index(_records_for_user(user_email)))
    if s is None:
        raise ValueError("This email's evidence does not support the suggested application update")
    if s.get("requires_application_match"):
        raise ValueError("This email has no matching application in the tracker")
    if s.get("requires_record_selection") and not record_id:
        raise ValueError("Choose the application that this email belongs to")
    selected_id = record_id or s["record_id"]
    candidates = {r["record_id"] for r in s.get("candidate_records", [])} or {s["record_id"]}
    if selected_id not in candidates:
        raise ValueError("Choose one of the matching applications")
    selected = DBConnect().get_db().tailoring_records.find_one(
        {"record_id": selected_id, "user_email": user_email}, {"jd_analysis": 1})
    if not selected:
        raise LookupError("Application not found")
    # Claim the decision before touching a record. Two tabs may choose two
    # different valid roles concurrently; one email must resolve to only one.
    # Keep the selected role across a failed write so retries cannot apply a
    # possibly-completed decision to a second application.
    now = datetime.now(timezone.utc)
    lock_id = uuid.uuid4().hex
    claimed = _suggestions().update_one(
        {"suggestion_id": suggestion_id, "user_email": user_email,
         "applied": False, "dismissed": False,
         "$and": [
             {"$or": [{"resolution_lock_until": {"$exists": False}},
                       {"resolution_lock_until": {"$lte": now}}]},
             {"$or": [{"resolution_record_id": {"$exists": False}},
                       {"resolution_record_id": selected_id}]},
             {"$or": [{"resolution_status": {"$exists": False}},
                       {"resolution_status": selected_status}]},
         ]},
        {"$set": {"resolution_lock_id": lock_id, "resolution_lock_until": now + timedelta(minutes=2),
                  "resolution_record_id": selected_id, "resolution_status": selected_status}},
    )
    if not claimed.modified_count:
        raise ValueError("This email is already being resolved. Refresh the tracker before trying again.")
    evidence_ms = None
    if s.get("email_received_at"):
        try:
            evidence_ms = int(datetime.fromisoformat(s["email_received_at"]).timestamp() * 1000)
        except (TypeError, ValueError):
            pass
    try:
        if not _apply_status(user_email, selected_id, selected_status, evidence_ms=evidence_ms):
            raise RuntimeError("Could not apply the status. The record may have been deleted.")
        s.update({"record_id": selected_id, "company": (selected.get("jd_analysis") or {}).get("company", ""),
                  "title": (selected.get("jd_analysis") or {}).get("job_title", ""),
                  "applied": True, "applied_at": datetime.now(timezone.utc),
                  "suggested_status": selected_status, "requires_status_selection": False,
                  "requires_application_match": False, "requires_record_selection": False})
        _suggestions().update_one(
            {"suggestion_id": suggestion_id, "user_email": user_email, "resolution_lock_id": lock_id},
            {"$set": {k: s[k] for k in ("record_id", "company", "title", "applied", "applied_at",
                                      "suggested_status", "requires_status_selection",
                                      "requires_application_match", "requires_record_selection")}},
        )
    finally:
        _suggestions().update_one(
            {"suggestion_id": suggestion_id, "user_email": user_email, "resolution_lock_id": lock_id},
            {"$unset": {"resolution_lock_id": "", "resolution_lock_until": ""}},
        )
    return _serialize_suggestion(s)


def dismiss_suggestion(user_email: str, suggestion_id: str) -> bool:
    res = _suggestions().update_one(
        {"suggestion_id": suggestion_id, "user_email": user_email, "applied": False,
         "$or": [{"resolution_lock_until": {"$exists": False}},
                   {"resolution_lock_until": {"$lte": datetime.now(timezone.utc)}}]},
        {"$set": {"dismissed": True, "dismissed_at": datetime.now(timezone.utc)}},
    )
    return bool(res.matched_count)
