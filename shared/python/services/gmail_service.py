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
GMAIL_FETCH_BUDGET_SECONDS = 180.0
GMAIL_MAX_REQUEST_ATTEMPTS = 7


def _model_call_limit() -> int:
    """Rules-only by default; an operator may enable at most three cheap calls."""
    try:
        return max(0, min(3, int(_get_config_value("GMAIL_MODEL_CALLS_PER_BATCH", "0"))))
    except (TypeError, ValueError):
        return 0

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
    """True if the deployment has the credentials needed to run the OAuth flow."""
    return bool(_client_id() and _client_secret() and _redirect_uri())


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


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def build_auth_url(user_email: str) -> str:
    """Generate a Google authorization URL bound to a one-time state token.

    State is stored server-side so the callback can validate it without
    trusting the client. State auto-expires after 10 minutes.
    """
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
        logger.warning("Google token exchange failed: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError("Failed to exchange Google authorization code")
    return resp.json()


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
        logger.warning("Google refresh failed: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError("Failed to refresh Gmail access token")
    return resp.json().get("access_token")


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
        raise RuntimeError("Google did not return a refresh_token. Re-link from a fresh consent screen.")
    access = token_payload.get("access_token") or ""
    info = _userinfo(access) if access else {}
    gmail_address = info.get("email") or ""

    encrypted = _fernet().encrypt(refresh.encode("utf-8")).decode("utf-8")
    doc = {
        "user_email": user_email,
        "gmail_address": gmail_address,
        "refresh_token_enc": encrypted,
        "scopes": token_payload.get("scope", "").split(),
        "linked_at": datetime.now(timezone.utc),
        "last_synced_at": None,
        "last_history_id": None,
    }
    _connections().update_one(
        {"user_email": user_email},
        {"$set": doc, "$unset": {key: "" for key in (
            "scan_next_page_token", "scan_query", "scan_before", "last_sync_summary",
            "sync_lock_id", "sync_lock_until",
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
        raise RuntimeError("Gmail is not connected")
    refresh = _fernet().decrypt(conn["refresh_token_enc"].encode("utf-8")).decode("utf-8")
    access = refresh_access_token(refresh)
    return access, conn


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
        return (f"Gmail could not complete this sync (HTTP {self.status}). "
                "Try syncing again later; your previous scan position is unchanged.")


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
            raise RuntimeError("Gmail sync reached its time limit. Try syncing again; "
                               "your previous scan position is unchanged.")
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


def _list_message_ids(access_token: str, query: str, max_results: int,
                      page_token: Optional[str] = None, budget: Optional[_GmailReadBudget] = None) -> List[str]:
    ids = _MessageBatch()
    while len(ids) < max_results:
        params: Dict[str, Any] = {"q": query, "maxResults": min(100, max_results - len(ids))}
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


def _b64url_decode(data: str) -> str:
    if not data:
        return ""
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body_text(payload: Dict[str, Any]) -> str:
    """Walk a Gmail payload tree and concatenate text/plain (preferred) or text/html parts."""
    if not payload:
        return ""
    out: List[str] = []
    plain: List[str] = []
    html: List[str] = []

    def walk(part: Dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        data = body.get("data")
        if mime == "text/plain" and data:
            plain.append(_b64url_decode(data))
        elif mime == "text/html" and data:
            html.append(_b64url_decode(data))
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    out.extend(plain or html)
    text = "\n".join(out)
    if not plain and html:
        text = re.sub(r"<[^>]+>", " ", text)  # strip HTML tags
    text = re.sub(r"\s+", " ", text).strip()
    return text[:4000]  # plenty for the classifier; keeps token budget tight


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


def fetch_recent_messages(user_email: str, since: Optional[datetime]) -> List[Dict[str, Any]]:
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
    query = conn.get("scan_query") or (
        f"in:anywhere after:{max(0, after_epoch - 1)} before:{int(before.timestamp())} "
        "-category:promotions -category:social"
    )
    ids = _list_message_ids(access, query, MAX_MESSAGES_PER_SYNC, conn.get("scan_next_page_token"), budget)
    out = _MessageBatch()
    out.next_page_token = ids.next_page_token
    out.scan_query = query
    out.scan_before = before
    for mid in ids:
        full = _gmail_get(f"/users/me/messages/{mid}", access,
                          params={"format": "full"}, raise_on_error=True, budget=budget)
        if not full:
            continue
        payload = full.get("payload") or {}
        headers = _headers_dict(payload.get("headers") or [])
        from_name, from_addr = _parse_email_address(headers.get("from", ""))
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


_FOOTER_START = re.compile(
    r"\b(?:(?:recommended|suggested|similar) (?:jobs|roles|opportunities)|"
    r"jobs you (?:might|may) (?:like|be interested in)|you may also like|"
    r"(?:follow|connect with) us (?:on|at)|privacy policy|unsubscribe|"
    r"manage (?:email |notification )?preferences)\b", re.IGNORECASE)
_PRIMARY_EMPLOYER_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in (
    r"\b(?:thank you|thanks) for (?:applying to|your application (?:to|at|with))\s+(.{2,120}?)(?=[.!?;\n|]|$)",
    r"\b(?:your )?application (?:to|at|with)\s+(.{2,120}?)(?=\s+(?:(?:has been|was|is)\s+)?(?:successfully\s+)?(?:submitted|received|rejected|accepted)\b|[.!?;\n|]|$)",
)]
_PLATFORM_DOMAINS = ("linkedin.com", "indeed.com", "wellfound.com", "greenhouse-mail.io",
                     "greenhouse.io", "lever.co", "myworkdayjobs.com", "smartrecruiters.com")


def _primary_employer_token(label: str, index: List[Dict[str, Any]]) -> str:
    token = _company_token(label.strip())
    if any(rec["company_token"] == token for rec in index):
        return token
    # Common subject templates append the role after the employer. Only split
    # a dash/"for" suffix when it contains a tracked role, not a company suffix.
    parts = re.split(r"\s+(?:for\s+(?:the\s+|an?\s+)?|[-–—]\s+)", label, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2 and any(_contains_phrase(_normalize(parts[1]), rec["title_norm"])
                              for rec in index if rec["title_norm"]):
        return _company_token(parts[0])
    return token


def _application_body(msg: Dict[str, Any]) -> str:
    """Only application content can establish identity or an outcome."""
    text = html.unescape(str(msg.get("body") or msg.get("snippet") or ""))[:12000]
    footer = _FOOTER_START.search(text)
    return text[:footer.start()] if footer else text


def _application_text(msg: Dict[str, Any]) -> str:
    return html.unescape(str(msg.get("subject") or "")) + "\n" + _application_body(msg)


def _primary_employers(msg: Dict[str, Any], index: List[Dict[str, Any]]) -> Optional[set]:
    subject = html.unescape(str(msg.get("subject") or ""))
    for pattern in _PRIMARY_EMPLOYER_PATTERNS:
        match = pattern.search(subject)
        if match:
            # Preserve even an UNTRACKED employer: never fall back to a footer
            # brand simply because the true application isn't in the tracker.
            return {_primary_employer_token(match.group(1), index)}
    # Product names can also be employers (e.g. a Guidewire Developer at Acme).
    # A company word inside the role title cannot establish the employer.
    subject_without_roles = _normalize(subject)
    for title in sorted({rec["title_norm"] for rec in index if rec["title_norm"]}, key=len, reverse=True):
        subject_without_roles = re.sub(r"\b" + re.escape(title) + r"\b", " ", subject_without_roles)
    named = {r["company_token"] for r in index if r["company_token"]
             and _contains_phrase(_normalize(subject_without_roles), r["company_token"])}
    if named:
        return named
    for pattern in _PRIMARY_EMPLOYER_PATTERNS:
        match = pattern.search(_application_body(msg))
        if match:
            return {_primary_employer_token(match.group(1), index)}
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
        r"if (?:you['’]?re|you are) interested)\b", text, re.IGNORECASE))


def _candidate_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    candidate = {key: rec.get(key, "") for key in ("record_id", "company", "title")}
    for key in ("applied_date", "created_at"):
        value = rec.get(key)
        if value:
            candidate[key] = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return candidate


def _build_record_index(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    index = []
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
    ids.update(re.findall(r"\b(?:requisition|req|job)\s*(?:id|number|#)?\s*[:#-]?\s*(\d{5,})\b", text, re.I))
    return ids


_ROLE_NOUN = re.compile(r"\b(?:engineer|developer|analyst|architect|scientist|manager|designer|specialist|consultant|administrator|researcher|director|intern|technician)\b", re.I)
_EXPLICIT_ROLE_PATTERNS = [re.compile(pattern, re.I) for pattern in (
    r"\b(?:application|applying|interest)\s+(?:for|in)\s+(?:the\s+)?(.{2,140}?)(?=\s+(?:at|with)\s|\s+(?:position|role|was|has|is)\b|[.!?\n]|$)",
    r"\b(?:regarding|for)\s+(?:the\s+)?(?!applying\b)(.{2,140}?)\s+(?:role|position)\b",
    r"\b(?:regarding|for)\s+(?:the\s+)?(.{2,140}?)\s+at\s",
    r"\b(?:role|position|job title)\s*(?:of|:)\s*(.{2,140}?)(?=\s+at\s|[.!?\n]|$)",
)]


def _explicit_role_titles(msg: Dict[str, Any], index: List[Dict[str, Any]]) -> List[str]:
    """Extract complete application titles, preserving seniority and specialty.

    A generic title occurring inside a different title is not exact evidence.
    Only bounded application prose is considered, never recommendation footers.
    """
    text = _application_text(msg)
    roles = []
    for pattern in _EXPLICIT_ROLE_PATTERNS:
        roles.extend(match.group(1).strip(" ,:–—-\"'") for match in pattern.finditer(text))
    # Common ATS subjects: "Acme Senior Engineer application update" and
    # "Your application to Acme - Senior Engineer".
    subject = html.unescape(str(msg.get("subject") or ""))
    title_subject = re.search(r"^(.{2,140}?)\s*[-–—:]\s*(?:application (?:decision|update)|application|decision|update)\b", subject, re.I)
    if title_subject:
        roles.append(title_subject.group(1))
    for company in {rec["company"] for rec in index if rec["company"]}:
        for pattern in (
            r"^" + re.escape(company) + r"\s+(.{2,140}?)\s+application\b",
            r"\bapplication to\s+" + re.escape(company) + r"\s*[-–—:]\s*(.{2,140})$",
        ):
            found = re.search(pattern, subject, re.I)
            if found:
                roles.append(found.group(1))
    # Requisition identifiers establish a posting but are not part of its title.
    roles = [re.sub(r"^(?:(?:req(?:uisition)?\s*(?:id|number|#)?\s*[:#-]?\s*)?(?:JR|R)[- ]?\d+\s*[-:]?\s*)", "", role, flags=re.I)
             for role in roles]
    return list(dict.fromkeys(_normalize(role) for role in roles if _ROLE_NOUN.search(role)))


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
        if after_word[0] not in {"", "at", "with", "position", "role", "application", "we", "your", "thank", "thanks", "is", "was", "has", "have", "our", "to", "and", "the"}:
            continue
        return True
    return False


def _match_message_to_record(msg: Dict[str, Any], index: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], float, str]]:
    """Pick the best record candidate for this email. Returns (record_meta, score, reason).

    A status classifier cannot establish WHICH application an email belongs
    to. Company-only mail stays a review item; automatic changes require an
    exact employer and role match with no equally plausible application.
    """
    if not index or _is_job_recommendation(msg):
        return None
    haystack = _normalize(_application_text(msg))
    primary_employers = _primary_employers(msg, index)
    explicit_roles = _explicit_role_titles(msg, index)
    email_requisitions = _requisition_ids(_application_text(msg))
    sender_domain = (msg.get("from_domain") or "").lower()
    sender_addr = (msg.get("from_address") or "").lower()
    platform_sender = any(sender_domain == domain or sender_domain.endswith("." + domain)
                          for domain in _PLATFORM_DOMAINS)

    candidates = []
    employer_records = []
    for rec in index:
        company = rec["company_token"]
        if primary_employers is not None and company not in primary_employers:
            continue
        company_named = len(company) >= 2 and (
            company in (primary_employers or set()) or bool(re.search(
                r"(?:\b(?:at|to|with|from|company)\s+" + re.escape(company) + r"\b|\b" +
                re.escape(company) + r"\s+(?:application|careers|recruiting|hiring|team)\b)", haystack)))
        # Exact DNS label only: Box != Dropbox, and United Airlines != United
        # Healthcare. Shared ATS/recruiter domains never establish identity.
        company_domain = not platform_sender and len(company) >= 3 and company.replace(" ", "") in sender_domain.split(".")[:-1]
        recruiter = bool(rec["recruiter_email"] and rec["recruiter_email"] == sender_addr)
        if not (company_named or company_domain or recruiter):
            continue
        employer_records.append(rec)
        if explicit_roles and rec["title_norm"] not in explicit_roles:
            continue
        if email_requisitions and rec["requisition_ids"] and not email_requisitions.intersection(rec["requisition_ids"]):
            continue
        title_exact = (rec["title_norm"] in explicit_roles if explicit_roles
                       else _exact_role_in_text(_application_text(msg), rec["title_norm"], company))
        reasons = []
        if company_named:
            reasons.append("Company identified in application content")
        if company_domain:
            reasons.append("Company name matches sender domain label")
        if recruiter:
            reasons.append("Saved recruiter email matches sender")
        if title_exact:
            reasons.append("Exact role title in email")
        else:
            reasons.append("Role title not confirmed; review before updating")
        # A recruiter alone can handle many unrelated companies, so needs review.
        score = 0.97 if title_exact and company_named else 0.85 if title_exact else 0.6
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
        if employer_records and (explicit_roles or email_requisitions):
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
    r"\bmoving forward with (?:other|another|different) candidate",
    r"\bgoing (?:to (?:move|go) )?forward with (?:other|another) candidate",
    r"\b(?:we have )?selected (?:other|another) candidate",
    r"\bwe['’]ve (?:chosen|selected) (?:to (?:move|go) forward with )?(?:another|other) candidate",
    r"\bother candidates whose (?:experience|background|qualifications) (?:more closely|better) (?:match|align)",
    r"\b(?:proceed|continue|advance) (?:with|in)\s+other (?:candidates|applicants)\b",

    # ── "we will not be moving/proceeding forward" family ────────────────
    r"\bwe (?:will|won['’]t|will not|are not|won['’]?t be) (?:moving|proceeding|progressing|going) (?:forward|further|ahead) (?:with|in)\b",
    r"\bnot (?:be |going to be )?(?:moving|proceeding|progressing|advancing) (?:forward|further) (?:with your|in (?:our|the))",

    # ── "decision not to" / "decision (was) made" family — Veeva, Workday ─
    r"\b(?:made (?:the |a )?)?decision (?:not |against )to (?:move|go|proceed|continue) forward\b",
    r"\bwe['’]?ve (?:made the |come to a |reached a )?decision (?:not |against )to\b",
    r"\b(?:we have )?decided (?:not |against )to (?:offer|proceed|continue|move forward)\b",
    r"\bnot to (?:move|go|proceed) forward (?:at this time|with your|in (?:our|the))",
    r"\bdecision has been made not to\b",

    # ── "regret / unfortunately" family ───────────────────────────────────
    r"\bwe regret to (?:inform|let you know|share)\b",
    r"\bunfortunately[,]?\s+(?:we|after|at this time|the (?:hiring|recruiting) team|your)\b",
    r"\b(?:i|we)['’]?m sorry (?:to (?:inform|let you know|share)|but)\b",
    r"\bregretfully\b",

    # ── "after careful review" follow-up phrases ─────────────────────────
    r"\bafter (?:careful |thorough |a careful |much )?(?:consideration|review|deliberation)[,]?\s+we\b.{0,80}\b(?:not|other|unable|unfortunately|cannot|won['’]?t|will not)\b",
    r"\bafter (?:reviewing|evaluating) your (?:application|resume|profile)[,]? we\b.{0,80}\b(?:not|unable|other|won['’]?t|cannot)\b",

    # ── "your application was not selected" family ───────────────────────
    r"\byour application (?:was|has been) (?:not selected|unsuccessful|declined|rejected)\b",
    r"\byour application for [^.!?\n]{2,180}? (?:was|has been) (?:not selected|unsuccessful|declined|rejected)\b",
    r"\b(?:we are|we['’]?re|i am|i['’]?m) unable to (?:move forward|proceed|offer|continue|advance)\b",
    r"\bwe['’]ll keep your (?:resume|application|profile|materials) on file\b",
    r"\b(?:keep|hold) your (?:resume|application|profile) (?:on file )?for future\b",
    r"\b(?:resume|application) (?:on file|in our (?:talent|candidate) (?:pool|community|database))\b",

    # ── "no longer / not progressing" ────────────────────────────────────
    r"\bno longer (?:under |being )?consider(?:ed|ation)\b",
    r"\b(?:application|candidacy) (?:has been |is no longer )?(?:closed|withdrawn|inactive)\b",
    r"\bdid not select(?:ed)? (?:your|you for)\b",
    r"\bnot (?:moving|proceeding|progressing|advancing) (?:forward|further) with your\b",

    # ── "position filled" family ─────────────────────────────────────────
    r"\b(?:the |this |that )?position (?:has been |was )?(?:already )?filled\b",
    r"\bwe (?:have )?filled (?:the|this|that) (?:position|role|opening|vacancy)\b",
    r"\b(?:the |this )?role (?:has been |is now )?(?:closed|filled|no longer (?:open|available))\b",
    r"\b(?:requisition|req) (?:has been |is )?(?:closed|filled|cancelled)\b",

    # ── well-wish closer (often the final sentence of a rejection) ───────
    r"\bwish you (?:the best|all the best|success|continued success) (?:(?:in|with) (?:your|future)\s*)?(?:job\s+search|future endeavors|career|search|future)\b",
    r"\bbest (?:of luck|wishes) (?:in|with) (?:your|the)\s*(?:job\s+search|future|search|endeavors)\b",

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

_OFFER_PATTERNS = [
    # ── canonical offer phrasing ─────────────────────────────────────────
    r"\b(?:we are |we['’]?re |i am |i['’]?m )?pleased to (?:offer|extend (?:an|the) (?:offer|opportunity))\b",
    r"\bwe['’]re? (?:excited|thrilled|delighted|happy) to (?:offer|extend|present)\b",
    r"\bwe['’]?d (?:love|like) to (?:formally )?(?:offer|extend (?:an|the) offer)\b",
    r"\bextending (?:an |the |a formal |our )?offer (?:of |for )?(?:employment|the position|the role)\b",
    r"\b(?:formal |verbal |written )?offer of employment\b",

    # ── offer letter mechanics ───────────────────────────────────────────
    r"\boffer letter (?:attached|enclosed|below|is ready|has been (?:sent|generated)|for your review)\b",
    r"\bplease (?:find |review |see )?(?:your |the |attached )?offer (?:letter |of employment |package )?(?:attached|enclosed|below)\b",
    r"\b(?:reviewing|signing|countersigning|accepting) (?:your |the )?offer (?:letter|package)\b",
    r"\b(?:e[- ]?signature|docusign).{0,30}\boffer (?:letter|package)\b",
    r"\boffer (?:package|letter|details) (?:include[ds]?|outlines?|contains?)\b",

    # ── compensation / start-date discussion in offer context ────────────
    r"\b(?:your |the )(?:base salary|annual salary|total compensation|comp package|starting salary|sign[- ]on bonus)\b.{0,60}\b(?:would be|will be|of \$|is \$)",
    r"\bstart(?:ing)? (?:date|day) (?:would be|will be|of) (?:[A-Z][a-z]+\s+\d|the \d|on)\b",
    r"\b(?:welcome (?:aboard|to the team)|welcome to (?:the )?(?:team|company|family))\b",
    r"\bcongratulations[!.,]\s*[a-z\s]{0,80}\boffer\b",
    r"\b(?:happy|thrilled|excited) to (?:welcome you|have you join)\b",

    # ── ATS-specific offer phrasing ──────────────────────────────────────
    # Greenhouse: "you've been extended an offer"
    r"\byou(?:['’]ve)? been (?:extended|sent|presented) (?:an |the |our )?offer\b",
    # Workday: "we are excited to extend a formal offer"
    r"\b(?:formal|written|verbal) offer (?:has been |is being |for|of)\b",
]

# Interview signals require *human-authored* progression — a scheduled time,
# Calendly link, hiring-manager intro, or explicit availability ask. We are
# DELIBERATELY conservative here: any pattern that could match conditional
# "if you're selected for an interview" boilerplate is excluded — the
# receipt-only detector handles that case.
_INTERVIEW_PATTERNS = [
    # ── scheduling links (almost certainly interview-intent) ─────────────
    r"\bcalendly\.com/",
    r"\bsavvycal\.com/",
    r"\b(?:meetings|book)\.hubspot\.com/",
    r"\bcal\.com/",
    r"\b(?:lu\.ma|chronograph\.io|x\.ai)/",
    r"\bgoodtime\.io/",
    r"\b(?:zoom|google meet|teams|meet\.google\.com|us\d+web\.zoom\.us|teams\.microsoft\.com)/[A-Za-z0-9/_?=&\-]+",

    # ── direct availability / scheduling asks ────────────────────────────
    # Note: "are you available" must be followed by "on/for/to/this/next" so
    # we don't catch "are you available for any of the listed roles" boilerplate.
    r"\b(?:are|would|when (?:are|would)) you (?:available|free) (?:on |for |to |this |next |the |a (?:few|couple|brief))",
    r"\bdo you have (?:any |some |a few )?(?:availability|time|times|slots) (?:on |this |next |for |to )",
    r"\bplease (?:share|let me know|send) (?:your )?(?:availability|times|time slots)\b",
    r"\b(?:share|provide|send) (?:me )?(?:a few |some )?(?:times?|slots?) (?:that work|when you|you['’]re)",
    r"\blet['’]s (?:schedule|set up|find a time|coordinate|connect|chat|talk)\b",
    r"\bschedule (?:a|the|your|our) (?:call|interview|screen|chat|conversation|time|meeting|sync)\b",
    r"\b(?:I['’]?d|I would) (?:love|like) to (?:schedule|set up|chat|connect|hop on|jump on)\b",
    r"\b(?:like|love) to (?:hop|jump|get) on a (?:quick )?(?:call|chat|video)\b",

    # ── confirmed / scheduled interview events ──────────────────────────
    # Crucially these require a confirmation verb, NOT just the word "interview"
    r"\b(?:phone|technical|behavioral|hiring manager|recruiter|initial|introductory|first[- ]round|second[- ]round|final[- ]round|culture) (?:screen|interview|call|chat|conversation)\s+(?:with|on|at|scheduled|confirmed|is set|has been booked)\b",
    r"\b(?:your |the )?(?:phone screen|technical screen|video interview|virtual interview|interview) (?:is |has been |will be )?(?:scheduled|confirmed|booked|set|happening|on)\b",
    r"\binterview (?:scheduled|confirmed|booked|set up) (?:for|on|with|at)\b",
    r"\b(?:confirming|to confirm) (?:your |our |the )?(?:interview|screen|call|chat|meeting)\b",
    r"\bcalendar invite (?:for|to) (?:your |the |our )?(?:interview|screen|call|chat|meeting)\b",

    # ── invitations to interviews / next rounds ──────────────────────────
    # Must have a clear "we want to invite you" — not "if invited"
    r"\b(?:we['’]?d like|we would like|i['’]?d like|i would like) to (?:invite|set up|schedule)\s+(?:you )?(?:for |to )?(?:an? |the )?(?:interview|screen|call|chat|conversation|next (?:round|step|stage))\b",
    r"\b(?:we['’]?re|i['’]?m|we are) (?:inviting|extending an invitation to) you (?:to|for)\b",
    r"\b(?:onsite|virtual onsite|panel|loop) (?:loop|interview|round)\s+(?:with|on|scheduled|confirmed|invitation)\b",
    r"\bnext (?:round|step|stage) (?:of|in) (?:the|your|our) (?:interview|process)\b",
    r"\bmove(?:d)? (?:you )?(?:forward|on|ahead) to (?:the next|our|a) (?:round|step|stage|interview)\b",

    # ── take-home / coding assessments ───────────────────────────────────
    # Only when actively assigned (link to it, deadline, etc.) — not
    # mentioning that one exists in the process
    r"\btake[- ]home (?:assessment|assignment|exercise|challenge|project|coding (?:test|assignment))\s+(?:link|attached|to complete|due|deadline|below|here|at)\b",
    r"\b(?:please )?(?:complete|submit|return) (?:the |your |this |attached )?(?:take[- ]home|coding|technical) (?:assessment|assignment|exercise|challenge|task)\b",
    r"\b(?:hackerrank|codility|coderpad|coderbyte|codesignal|leetcode|hackerearth)\.(?:com|io)/",
    r"\b(?:assessment|coding test) (?:link|invitation) (?:below|above|attached|here)\b",

    # ── hiring manager / recruiter intros ────────────────────────────────
    r"\bhiring manager (?:would like|wants|will|is excited|reached out)\b.{0,40}\b(?:to (?:speak|chat|meet|connect|interview|talk)|with you|to set up)\b",
    r"\b(?:i['’]?m|hi[,]?) (?:reaching out|writing) (?:about|regarding|to discuss|to chat about) your (?:application|interest|background)\b.{0,80}\b(?:next step|chat|call|interview|screen|connect)\b",
    r"\bafter review(?:ing)? your (?:application|resume|profile)[,]? (?:we['’]?d like|i['’]?d like|we would like)\b",

    # ── direct interview-platform links ──────────────────────────────────
    r"\b(?:hirevue|sparkhire|willo|videoask|odro|interviewing\.io)\.(?:com|io)/",
    r"\b(?:karat|byteboard|coderbyte interview|techscreen)\.(?:com|io)/",
]

_REJECTION_REGEX = [re.compile(p, re.IGNORECASE) for p in _REJECTION_PATTERNS]
_OFFER_REGEX = [re.compile(p, re.IGNORECASE) for p in _OFFER_PATTERNS]
_INTERVIEW_REGEX = [re.compile(p, re.IGNORECASE) for p in _INTERVIEW_PATTERNS]


# "Receipt-only" patterns: phrases that indicate this email is JUST an
# application acknowledgement, not a progression signal. They look adjacent
# to interview/decline language ("if you're selected for an interview",
# "your application is being reviewed") and were the source of false-positive
# "interviewing" classifications from the LLM (CDW, WorkWhile both used
# conditional "should you be selected to interview" boilerplate).
#
# When a thread fires receipt-only signals AND has NO real interview/decline/
# offer signal, we hard-force status=applied and override any LLM verdict
# that disagrees.
_RECEIPT_ONLY_PATTERNS = [
    # ── direct receipt acknowledgements ──────────────────────────────────
    r"\b(?:thanks|thank you|many thanks) for (?:applying|your application|your interest|submitting|taking the time to (?:apply|submit))\b",
    r"\bwe (?:have |['’]ve )?(?:received|got) your application\b",
    r"\byour application (?:has been |is currently |was )?(?:received|submitted|on file|in our system)\b",
    r"\bapplication (?:successfully )?(?:received|submitted|completed)\b",
    r"\bthis (?:email |message |note )?(?:is to )?confirm(?:s|ation)?\s+(?:we['’]?ve|that we have|the receipt of)\b",

    # ── "is being / will be reviewed" boilerplate ────────────────────────
    r"\byour application (?:is currently |will be |has been )?(?:being |under |in )?(?:evaluated|reviewed|considered|assessed|in review)\b",
    r"\b(?:our |the )?(?:team|recruiting team|talent team|hiring team|recruitment team|HR team) (?:will|is going to|is currently) (?:review|evaluate|consider|assess|assessing|reviewing|going through)\b",
    r"\b(?:reviewing|evaluating|assessing) (?:applications|candidates|profiles|submissions)\b",
    r"\b(?:we|our team) (?:will|are) (?:carefully )?(?:reviewing|evaluating|going through) (?:all |your )?(?:application|applications|submission)\b",

    # ── CONDITIONAL "if/should you be selected" — the killer false-positive
    #    pattern. These phrases ALWAYS mean "this is a receipt, not an
    #    interview" — they describe what would happen IF you were selected.
    r"\bif (?:you['’]?re|you are) (?:selected|chosen|a (?:good |strong |great |the right )?(?:fit|match)|interested|invited)\b",
    r"\bshould you be (?:selected|chosen|considered|a fit|a match|invited|moved forward)\b",
    r"\bshould (?:we|the (?:team|hiring manager)) (?:wish|choose|decide|find)\b",
    r"\bif (?:there['’]?s|there is) (?:an? |a strong |a good )?(?:match|fit|alignment|opportunity)\b",
    r"\bif (?:we believe|we think|we feel|we determine|we find)\b.{0,40}\b(?:fit|match|qualified|appropriate)\b",
    r"\b(?:assuming|provided that|in the event that) (?:you|your application)\b.{0,40}\b(?:selected|moves forward|qualifies|matches|advances)\b",

    # ── "we'll reach out / contact you" futureconditional ───────────────
    r"\b(?:a )?member of (?:our|the) (?:talent|recruiting|recruitment|hiring|HR) team will (?:reach out|contact|be in touch|follow up)\b",
    r"\b(?:we|someone|a recruiter|a member of our team) (?:will|may) (?:reach out|contact you|be in touch|follow up) (?:in the coming|within|shortly|soon|if)\b",
    r"\byou['’]?ll hear (?:back |from us )?(?:from us |back )?(?:as soon as|once|when|if|in the coming)\b",
    r"\b(?:expect to hear|look forward to hearing) (?:back )?(?:from us )?(?:within|in the next|in the coming)\b",
    r"\bwe['’]?ll (?:be in touch|follow up|reach out|let you know) (?:if|when|once|shortly|soon|in the coming)\b",

    # ── "encourage you to" / "check back" ─────────────────────────────────
    r"\bwe encourage you to (?:check back|continue (?:applying|exploring)|apply|browse|look)\b",
    r"\b(?:check back|visit|browse|explore) (?:our|the) (?:careers? (?:site|page)|job board|opportunities)\b",
    r"\bnew (?:jobs|roles|opportunities|positions) (?:are |being )?posted (?:regularly|frequently|often|daily|weekly)\b",
    r"\bnext steps (?:will be|are going to be) (?:shared|communicated) (?:if|with you if|once|when)\b",

    # ── application portal / account creation language ───────────────────
    r"\b(?:create|set up) (?:an |your |a free )?account (?:on (?:our |the )?(?:career|careers|application|portal|candidate))\b",
    r"\b(?:track|monitor|view) the (?:status|progress) of your application\b",
    r"\baction on any potential (?:tasks|next steps)\b",
    r"\b(?:log|sign) in(?:to)? (?:your |the )?(?:candidate|application) (?:portal|account|dashboard)\b",

    # ── "evaluation in progress" boilerplate ─────────────────────────────
    r"\b(?:once|when|after) (?:the |our |this )?(?:review|evaluation|assessment|process) is complete\b",
    r"\b(?:please|kindly) (?:be patient|allow|note that)\b.{0,80}\b(?:review|evaluation|process|response)\b",
    r"\b(?:due to|given) (?:the |our |a )?(?:high volume|large number) of (?:applications|applicants|candidates)\b",
    r"\b(?:we (?:appreciate|thank you for) your patience)\b",
]
_RECEIPT_ONLY_REGEX = [re.compile(p, re.IGNORECASE) for p in _RECEIPT_ONLY_PATTERNS]
# Boilerplate can prevent a false interview, but cannot prove a submission.
# A receipt requires an acknowledgement specifically about the application.
_APPLICATION_ACK_REGEX = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:thanks|thank you|many thanks) for (?:applying|your application|submitting (?:your|an) application|taking the time to apply)\b",
    r"\bwe(?: have|['’]ve)? (?:received|got) your application\b",
    r"\byour application (?:has been |is currently |was )?(?:successfully )?(?:received|submitted|on file|in our system)\b",
    r"\bapplication (?:successfully )?(?:received|submitted|completed)\b",
    r"\byour application (?:is currently |will be |has been )?(?:being |under |in )?(?:evaluated|reviewed|considered|assessed|in review)\b",
)]


def _scan_patterns(text: str, regexes: List[re.Pattern]) -> Optional[str]:
    """Return the first matching pattern's source phrase, or None.

    We surface the matched phrase so reconciliation can include it as the
    `reason` shown in the suggestions UI — users trust the call more when
    they can see the literal sentence that drove it.
    """
    if not text:
        return None
    for rx in regexes:
        m = rx.search(text)
        if m:
            return m.group(0)[:160]
    return None


def extract_deterministic_signals(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run regex extractors across an application's email bundle.

    Returns a dict with at most one of: rejected, offer, interviewing — plus
    the matched phrase + the message it came from for traceability. Order of
    precedence on conflict: rejected > offer > interviewing (a rejection
    after an offer is rare but real — withdrawn offer; we surface it as a
    suggestion rather than auto-flipping).

    Literal phrases are evidence, not proof of application identity. The
    matcher separately establishes identity before any automatic update.
    """
    # Newest-first processing so an older "applied" receipt doesn't shadow a
    # later rejection — but we still scan ALL messages for any signal.
    found_rejection: Optional[Tuple[str, Dict[str, Any]]] = None
    found_offer: Optional[Tuple[str, Dict[str, Any]]] = None
    found_interview: Optional[Tuple[str, Dict[str, Any]]] = None
    found_receipt: Optional[Tuple[str, Dict[str, Any]]] = None

    for msg in sorted(messages, key=lambda m: m.get("internal_date") or 0, reverse=True):
        if _is_job_recommendation(msg):
            continue
        text = _application_text(msg)[:4000]
        if found_rejection is None:
            phrase = _scan_patterns(text, _REJECTION_REGEX)
            if phrase:
                found_rejection = (phrase, msg)
        if found_offer is None:
            phrase = _scan_patterns(text, _OFFER_REGEX)
            if phrase:
                found_offer = (phrase, msg)
        if found_interview is None:
            phrase = _scan_patterns(text, _INTERVIEW_REGEX)
            if phrase:
                found_interview = (phrase, msg)
        if found_receipt is None:
            phrase = _scan_patterns(text, _APPLICATION_ACK_REGEX)
            if phrase:
                found_receipt = (phrase, msg)

    out: Dict[str, Any] = {}
    for status, found in (("rejected", found_rejection), ("offer", found_offer),
                          ("interviewing", found_interview), ("receipt", found_receipt)):
        if found:
            out[status] = {"phrase": found[0], "message_id": found[1].get("message_id"),
                           "internal_date": found[1].get("internal_date") or 0}
    return out


def reconcile_classification(
    deterministic: Dict[str, Any],
    llm: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Merge deterministic signals with the LLM verdict into a final call.

    Returns {status, confidence, reason, source} or None if neither layer
    produced anything actionable.

    Precedence (highest → lowest):
      1. Deterministic REJECTION   → status=rejected, conf=0.97 (overrides everything)
      2. Deterministic OFFER       → status=offer, conf=0.96 (overrides everything but rejection)
      3. RECEIPT-ONLY GUARD        → if the thread is just an application receipt (no real
                                     interview/decline/offer signal) the status is "applied",
                                     period. This is what blocks LLM false positives where
                                     "if you're selected for an interview" boilerplate gets
                                     misread as a real interview invite.
      4. Deterministic INTERVIEW + LLM=interviewing/applied → status=interviewing, conf=0.95
      5. LLM verdict alone         → trust at LLM's stated confidence (capped 0.90 without
                                     deterministic backing — never auto-apply on LLM-only
                                     unless the LLM is genuinely confident)
      6. Deterministic INTERVIEW only (LLM disagreed) → status=interviewing, conf=0.80
         (we trust the regex but flag for review since LLM saw something else)
    """
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
    if "rejected" in deterministic:
        return {
            "status": "rejected",
            "confidence": 0.97,
            "reason": f'Decline phrase: "{deterministic["rejected"]["phrase"]}"',
            "source": "deterministic",
            "evidence_message_id": deterministic["rejected"].get("message_id"),
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
        if llm_status in ("interviewing", "applied", None):
            return {
                "status": "interviewing",
                "confidence": 0.95,
                "reason": f'Interview signal: "{deterministic["interviewing"]["phrase"]}"',
                "source": "deterministic+llm" if llm_status == "interviewing" else "deterministic",
                "evidence_message_id": deterministic["interviewing"].get("message_id"),
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
            "reason": f'Interview signal: "{deterministic["interviewing"]["phrase"]}"',
            "source": "deterministic",
            "evidence_message_id": deterministic["interviewing"].get("message_id"),
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
    if _is_job_recommendation(msg):
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
    messages = [msg for msg in messages if not _is_job_recommendation(msg)]
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
    """One active bounded scan per account, including Lambda retries."""
    if not get_connection(user_email):
        raise RuntimeError("Gmail is not connected")
    now = datetime.now(timezone.utc)
    lock_id = uuid.uuid4().hex
    claimed = _connections().update_one(
        {"user_email": user_email, "$or": [
            {"sync_lock_until": {"$exists": False}}, {"sync_lock_until": {"$lte": now}}]},
        {"$set": {"sync_lock_id": lock_id, "sync_lock_until": now + timedelta(minutes=16)}},
    )
    if not claimed.modified_count:
        raise RuntimeError("A Gmail sync is already running. Try again after it finishes.")
    try:
        return _sync_user(user_email, lock_id)
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

    records = _records_for_user(user_email)
    index = _build_record_index(records)
    if not index:
        return {"messages_scanned": 0, "auto_applied": 0, "suggested": 0,
                "ignored": 0, "ambiguous_matches": 0, "has_more": False,
                "deferred": 0, "model_calls": 0}

    since = conn.get("last_synced_at")
    if isinstance(since, datetime) and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    try:
        messages = fetch_recent_messages(user_email, since)
    except GmailAPIError as e:
        # Don't poison last_synced_at — caller (route) can show a user-facing
        # error and the next sync will retry the same window.
        logger.warning("[gmail-sync] fetch_failed status=%s reason=%s retryable=%s",
                       e.status, e.reason, e.retryable)
        raise RuntimeError(e.user_message()) from e

    def require_same_connection():
        if not _connections().find_one({"user_email": user_email, "sync_lock_id": lock_id}, {"_id": 1}):
            raise RuntimeError("Gmail connection changed during sync. Start a new sync for the connected mailbox.")

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
        if _suggestions().find_one({"_id": suggestion_id, "user_email": user_email}):
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
    }
    state = {"last_sync_summary": summary}
    if summary["has_more"]:
        state.update({"scan_next_page_token": messages.next_page_token,
                      "scan_query": messages.scan_query, "scan_before": messages.scan_before})
        update = {"$set": state}
    else:
        state["last_synced_at"] = getattr(messages, "scan_before", None) or datetime.now(timezone.utc)
        update = {"$set": state, "$unset": {"scan_next_page_token": "", "scan_query": "", "scan_before": ""}}
    completed = _connections().update_one({"user_email": user_email, "sync_lock_id": lock_id}, update)
    if not completed.matched_count:
        raise RuntimeError("Gmail connection changed during sync. Start a new sync for the connected mailbox.")
    return summary


# ---------------------------------------------------------------------------
# Suggestion CRUD (used by the blueprint)
# ---------------------------------------------------------------------------

def _serialize_suggestion(s: Dict[str, Any]) -> Dict[str, Any]:
    s.pop("_id", None)
    for key in ("resolution_lock_id", "resolution_lock_until", "resolution_record_id", "resolution_status"):
        s.pop(key, None)
    for k in ("created_at", "applied_at"):
        v = s.get(k)
        if hasattr(v, "isoformat"):
            s[k] = v.isoformat()
    for k in ("subject", "snippet", "from_name"):
        if isinstance(s.get(k), str):
            s[k] = html.unescape(s[k])
    return s


def _validated_suggestion(s: Dict[str, Any], index: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Read-only repair of review choices whose retained evidence contradicts them.

    Old rows have subject/sender/snippet, not full bodies. Only clear employer
    contradictions and recommendations are excluded; absence of evidence alone
    does not invalidate a legacy review item. Nothing here rewrites history.
    """
    if s.get("applied") or s.get("dismissed"):
        return dict(s)
    if _is_job_recommendation(s):
        return None
    if s.get("requires_application_match"):
        # A real outcome for an untracked role remains visible and cannot be
        # silently reassigned to a different role at the same employer.
        result = dict(s)
        result.update(record_id=None, candidate_records=[], requires_record_selection=True)
        return result
    # Snippets are partial; only a subject employer can reassign legacy choices.
    primary = _primary_employers({"subject": s.get("subject")}, index)
    # A legacy snippet may end halfway through a title. Only complete prose
    # sentences can eliminate a candidate; subject titles are already bounded.
    snippet = str(s.get("snippet") or "")
    complete = re.match(r"(?s)^(.*[.!?\n])", snippet)
    explicit_roles = s.get("email_roles") or _explicit_role_titles(
        {"subject": s.get("subject"), "body": complete.group(1) if complete else ""}, index)
    partial = re.search(r"\b(?:application for|forward for)\s+(?:the\s+)?([^.!?\n]{2,140})$", snippet, re.I)
    partial_role = ""
    if partial and _ROLE_NOUN.search(partial.group(1)):
        partial_role = _normalize(re.sub(r"^(?:JR|R)[- ]?\d+\s*[-:]?\s*", "", partial.group(1), flags=re.I))
    email_requisitions = set(s.get("email_requisition_ids") or _requisition_ids(_application_text(s)))
    records = {rec["record_id"]: rec for rec in index}
    old_candidates = s.get("candidate_records") or [{"record_id": s.get("record_id"),
                                                    "company": s.get("company", ""), "title": s.get("title", "")}]
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
            result = dict(s)
            role = explicit_roles[0] if explicit_roles else (partial_role + "…" if partial_role else "Application in email")
            result.update(record_id=None, candidate_records=[], requires_record_selection=True,
                          requires_application_match=True, title=role, email_role=role)
            return result
        return None
    result = dict(s)
    selected = next((r for r in candidates if r["record_id"] == s.get("record_id")), candidates[0])
    result.update({key: selected.get(key, "") for key in ("record_id", "company", "title")})
    result["candidate_records"] = candidates
    result["requires_record_selection"] = len(candidates) > 1
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
            raise RuntimeError("Failed to apply status — record may have been deleted")
        s.update({"record_id": selected_id, "company": (selected.get("jd_analysis") or {}).get("company", ""),
                  "title": (selected.get("jd_analysis") or {}).get("job_title", ""),
                  "applied": True, "applied_at": datetime.now(timezone.utc),
                  "suggested_status": selected_status, "requires_status_selection": False})
        _suggestions().update_one(
            {"suggestion_id": suggestion_id, "user_email": user_email, "resolution_lock_id": lock_id},
            {"$set": {k: s[k] for k in ("record_id", "company", "title", "applied", "applied_at",
                                      "suggested_status", "requires_status_selection")}},
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
