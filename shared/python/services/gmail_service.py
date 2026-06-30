"""
Gmail integration — OAuth, encrypted-token storage, message sync, and
LLM-classified status suggestions for application tracking.

Design notes
------------
- Uses Google's OAuth 2.0 web flow with the readonly Gmail scope.
- Refresh tokens are encrypted at rest (Fernet); the encryption key comes
  from `GMAIL_TOKEN_ENCRYPTION_KEY` (SSM/env). Access tokens are never
  persisted — they are minted on demand from the refresh token.
- Sync is incremental: we remember `last_history_id` per user so we only
  process new messages on each run. Falls back to a date-bounded list
  query on first sync.
- Classification is delegated to the LLM (FLASH tier) with a strict JSON schema.
  High-confidence results auto-apply; lower-confidence ones land in a
  per-user "suggestions" inbox the user reviews from the UI.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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

# Cap how many messages we look at per sync. Bumped along with the 60-day
# lookback so a freshly-linked inbox gets enough headroom to scan two months
# of recruiter mail in one pass.
MAX_MESSAGES_PER_SYNC = 250

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
        {"$set": doc},
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
    """Raised when a Gmail API call returns a non-200 we don't want to swallow."""
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"Gmail API returned {status}: {body[:200]}")


def _gmail_get(path: str, access_token: str, params: Optional[Dict[str, Any]] = None, raise_on_error: bool = False) -> Dict[str, Any]:
    resp = requests.get(
        f"{GMAIL_API_BASE}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params or {},
        timeout=15,
    )
    if resp.status_code == 429:
        time.sleep(1.5)
        resp = requests.get(
            f"{GMAIL_API_BASE}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params or {},
            timeout=15,
        )
    if resp.status_code != 200:
        logger.warning("Gmail API %s failed: %s %s", path, resp.status_code, resp.text[:300])
        if raise_on_error:
            raise GmailAPIError(resp.status_code, resp.text)
        return {}
    return resp.json()


def _list_message_ids(access_token: str, query: str, max_results: int) -> List[str]:
    ids: List[str] = []
    page_token: Optional[str] = None
    while len(ids) < max_results:
        params: Dict[str, Any] = {"q": query, "maxResults": min(100, max_results - len(ids))}
        if page_token:
            params["pageToken"] = page_token
        # raise_on_error=True so a transient 4xx/5xx surfaces as an exception in
        # sync_user — otherwise we'd swallow the failure and poison last_synced_at.
        body = _gmail_get("/users/me/messages", access_token, params=params, raise_on_error=True)
        for m in body.get("messages") or []:
            if m.get("id"):
                ids.append(m["id"])
        page_token = body.get("nextPageToken")
        if not page_token:
            break
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
    access, _conn = _get_access_token(user_email)
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=INITIAL_LOOKBACK_DAYS)
    # Gmail accepts unix-seconds for `after:` queries.
    after_epoch = int(since.timestamp())
    query = f"in:anywhere after:{after_epoch} -category:promotions -category:social"
    ids = _list_message_ids(access, query, MAX_MESSAGES_PER_SYNC)
    out: List[Dict[str, Any]] = []
    for mid in ids:
        full = _gmail_get(f"/users/me/messages/{mid}", access, params={"format": "full"})
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
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())


def _company_token(company: str) -> str:
    """Reduce a company string to a short matching token (drops Inc/LLC/etc.)."""
    if not company or company == "Not specified":
        return ""
    s = _normalize(company)
    for stop in (" inc", " llc", " corp", " corporation", " ltd", " limited", " co", " gmbh"):
        if s.endswith(stop):
            s = s[: -len(stop)]
    return s.strip().split()[0] if s.strip() else ""


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
            "recruiter_email": recruiter_email,
            "recruiter_domain": recruiter_domain,
            "current_status": app.get("status") or "draft",
        })
    return index


def _match_message_to_record(msg: Dict[str, Any], index: List[Dict[str, Any]]) -> Optional[Tuple[Dict[str, Any], float, str]]:
    """Pick the best record candidate for this email. Returns (record_meta, score, reason).

    Scoring is intentionally simple — the LLM does the nuanced part. Here we just
    pick a viable candidate so we don't waste classifier calls on noise.
    """
    if not index:
        return None
    haystack = _normalize(f"{msg.get('subject','')} {msg.get('snippet','')} {msg.get('from_name','')} {msg.get('from_address','')}")
    sender_domain = (msg.get("from_domain") or "").lower()
    sender_addr = (msg.get("from_address") or "").lower()

    best: Optional[Tuple[Dict[str, Any], float, str]] = None
    for rec in index:
        score = 0.0
        reasons: List[str] = []

        if rec["recruiter_email"] and rec["recruiter_email"] == sender_addr:
            score += 0.6
            reasons.append("recruiter email matches sender")
        elif rec["recruiter_domain"] and sender_domain and rec["recruiter_domain"] == sender_domain:
            score += 0.4
            reasons.append("recruiter domain matches sender")

        if rec["company_token"] and len(rec["company_token"]) >= 3:
            if rec["company_token"] in haystack:
                score += 0.35
                reasons.append(f"company name '{rec['company_token']}' present")
            if sender_domain and rec["company_token"] in sender_domain:
                score += 0.25
                reasons.append("company present in sender domain")

        if rec["title_norm"]:
            title_words = [w for w in rec["title_norm"].split() if len(w) >= 4]
            hits = sum(1 for w in title_words if w in haystack)
            if title_words:
                ratio = hits / len(title_words)
                if ratio >= 0.5:
                    score += 0.2 * ratio
                    reasons.append(f"job title overlap {hits}/{len(title_words)}")

        if score > 0 and (best is None or score > best[1]):
            best = (rec, score, "; ".join(reasons))
    # 0.3 catches borderline matches (e.g. company name in subject only, no
    # recruiter signal). The LLM gets a chance to filter these via "ignore"
    # if the email is actually unrelated noise.
    if best and best[1] >= 0.3:
        return best
    return None


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
    r"\b(?:made (?:the |a )?)?decision (?:not |against )?to (?:move|go|proceed|continue) forward\b",
    r"\bwe['’]?ve (?:made the |come to a |reached a )?decision (?:not |against )?to\b",
    r"\b(?:we have )?decided (?:not |against )?to (?:offer|proceed|continue|move forward)\b",
    r"\bnot to (?:move|go|proceed) forward (?:at this time|with your|in (?:our|the))",
    r"\bdecision has been made (?:not )?to\b",

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
    r"\bthank you for your (?:interest|application|time)[\.\,]?\s+(?:we (?:have )?(?:decided|concluded)|after|unfortunately)\b",
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

    100%-precision claim: a hit on these patterns means the literal phrase
    appears in the email; the only failure mode is the upstream Gmail API
    handing us truncated bodies, which is bounded by `_extract_body_text`.
    """
    # Newest-first processing so an older "applied" receipt doesn't shadow a
    # later rejection — but we still scan ALL messages for any signal.
    found_rejection: Optional[Tuple[str, Dict[str, Any]]] = None
    found_offer: Optional[Tuple[str, Dict[str, Any]]] = None
    found_interview: Optional[Tuple[str, Dict[str, Any]]] = None
    found_receipt: Optional[Tuple[str, Dict[str, Any]]] = None

    for msg in messages:
        text = " ".join([
            msg.get("subject") or "",
            msg.get("snippet") or "",
            (msg.get("body") or "")[:3500],  # bound CPU; bodies > this are usually footer/quoted-thread
        ])
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
            phrase = _scan_patterns(text, _RECEIPT_ONLY_REGEX)
            if phrase:
                found_receipt = (phrase, msg)

    out: Dict[str, Any] = {}
    if found_rejection:
        out["rejected"] = {"phrase": found_rejection[0], "message_id": found_rejection[1].get("message_id")}
    if found_offer:
        out["offer"] = {"phrase": found_offer[0], "message_id": found_offer[1].get("message_id")}
    if found_interview:
        out["interviewing"] = {"phrase": found_interview[0], "message_id": found_interview[1].get("message_id")}
    if found_receipt:
        out["receipt"] = {"phrase": found_receipt[0], "message_id": found_receipt[1].get("message_id")}
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
    # 1. Hard rejection — never overridden by anything else.
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
    text = (msg.get("subject", "") + " " + msg.get("snippet", "") + " " + msg.get("body", "")).lower()
    for status, phrases in _STATUS_KEYWORDS.items():
        for phrase in phrases:
            if phrase in text:
                return status, 0.7
    return None, 0.0


def classify_message(msg: Dict[str, Any], record_meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Classify an email → {status, confidence, reason}.

    Uses keyword heuristics + the LLM (FLASH tier). Returns None on failure.
    """
    heur_status, heur_conf = _heuristic_status(msg)

    prompt = f"""You are an experienced recruiter's assistant classifying an email so a job
application tracker can update itself automatically. Be decisive — the user has explicitly
asked you to make the call rather than punting back to them. Hedge only when the email is
genuinely ambiguous.

The user is tracking this application:
- Company: {record_meta.get('company') or 'unknown'}
- Role: {record_meta.get('title') or 'unknown'}
- Current tracker status: {record_meta.get('current_status')}

The email:
From: {msg.get('from_name', '')} <{msg.get('from_address', '')}>
Subject: {msg.get('subject', '')}
Snippet: {msg.get('snippet', '')}
Body (truncated):
{(msg.get('body') or '')[:1500]}

Pick the best status:
- applied        → confirmation that the user's application was received / is under review
- interviewing   → any signal of progression: recruiter wants to schedule, confirmed a time,
                   sent prep material, asked for availability, moved to next round, sent a
                   take-home, scheduled an onsite, hiring manager intro, etc.
- offer          → an offer of employment is being extended (or compensation is being discussed)
- rejected       → the user is no longer being considered, even if framed politely
                   ("we've decided to move forward with other candidates", "not at this time",
                   "we'll keep your resume on file")
- ignore         → marketing, automated job alert, recruiter outreach for a *different* role,
                   newsletter, calendar invite from an unrelated meeting

Confidence guidance:
- 0.90+  unambiguous: explicit phrasing for that status
- 0.70-0.89  clear from context even without an explicit phrase
- 0.50-0.69  leaning that way but could be misread
- below 0.50  truly uncertain — only use this when you genuinely can't tell

Default to a definite status when the email is *about* this application and you can read its
intent. Only fall back to "ignore" if the email is unrelated or noise.

Output strict JSON: {{"status": "...", "confidence": 0.0-1.0, "reason": "one short sentence"}}.
"""

    try:
        from services.gemini_client import gemini_json, GEMINI_FLASH
        result = gemini_json(
            prompt=prompt,
            max_tokens=200,
            temperature=0.0,
            model=GEMINI_FLASH,
            max_retries=1,
        )
    except Exception as e:
        logger.warning("Gmail classifier LLM call failed: %s", e)
        if heur_status:
            return {"status": heur_status, "confidence": heur_conf, "reason": "keyword heuristic"}
        return None

    status = (result.get("status") or "").lower()
    confidence = float(result.get("confidence") or 0.0)
    reason = (result.get("reason") or "")[:240]

    if status == "ignore":
        return {"status": "ignore", "confidence": confidence, "reason": reason}
    if status not in VALID_STATUSES:
        return None

    # If both heuristic and LLM agree, nudge confidence up slightly.
    if heur_status == status:
        confidence = min(1.0, confidence + 0.1)

    return {"status": status, "confidence": confidence, "reason": reason}


def classify_record_thread(messages: List[Dict[str, Any]], record_meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Classify an application's CURRENT status by reading the full set of
    related emails together (newest first).

    This is dramatically more accurate than per-message classification because
    the LLM can see the timeline as one story. A thread that goes
    "applied → interview invite → rejection" is unambiguous when read in
    order — but classifying each email in isolation can leave a record stuck
    on whichever message happened to be processed last.
    """
    if not messages:
        return None

    blocks: List[str] = []
    for i, msg in enumerate(messages):
        blocks.append(
            f"--- Email {i+1} (most recent first) ---\n"
            f"Date: {msg.get('date', '')}\n"
            f"From: {msg.get('from_name','')} <{msg.get('from_address','')}>\n"
            f"Subject: {msg.get('subject', '')}\n"
            f"Snippet: {msg.get('snippet', '')}\n"
            f"Body: {(msg.get('body') or '')[:900]}"
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

    # Two-tier model approach: Pro first for accuracy on nuanced cases
    # (politely-phrased rejections, soft interview signals, multi-message
    # threads where status flips mid-conversation). Flash as fallback when
    # Pro rate-limits / times out — still better than no classification.
    # Temperature stays at 0.0 for deterministic output.
    result: Optional[Dict[str, Any]] = None
    try:
        from services.gemini_client import gemini_json, GEMINI_PRO, GEMINI_FLASH
        try:
            result = gemini_json(
                prompt=prompt,
                max_tokens=400,
                temperature=0.0,
                model=GEMINI_PRO,
                max_retries=1,
            )
        except Exception as pro_err:
            logger.info("classify_record_thread Pro failed (%s); retrying with Flash", pro_err)
            result = gemini_json(
                prompt=prompt,
                max_tokens=400,
                temperature=0.0,
                model=GEMINI_FLASH,
                max_retries=1,
            )
    except Exception as e:
        logger.warning("classify_record_thread LLM call failed (both tiers): %s", e)
        return None
    if not isinstance(result, dict):
        return None

    status = (result.get("status") or "").lower()
    confidence = float(result.get("confidence") or 0.0)
    reason = (result.get("reason") or "")[:240]

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
_FUNNEL_ORDER = {"draft": 0, "applied": 1, "interviewing": 2, "offer": 3}


def _is_funnel_backwards(current: str, proposed: str) -> bool:
    """True if `proposed` would move the record backwards in the funnel.

    Used as a soft safety net — a backwards move still goes through, but only
    as a suggestion the user has to confirm, never an auto-apply.
    """
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
            "application.status": 1,
            "application.recruiter_email": 1,
            "_id": 0,
        },
    )
    return list(cursor)


def _apply_status(user_email: str, record_id: str, status: str) -> bool:
    """Persist a status change directly. Mirrors the PATCH /application logic minimally."""
    if status not in VALID_STATUSES:
        return False
    db = DBConnect().get_db()
    updates: Dict[str, Any] = {
        "application.status": status,
        "application.updated_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    if status == "applied":
        updates.setdefault("application.applied_at", datetime.utcnow())
    res = db.tailoring_records.update_one(
        {"record_id": record_id, "user_email": user_email},
        {"$set": updates},
    )
    return bool(res.matched_count)


def sync_user(user_email: str) -> Dict[str, Any]:
    """Pull recent Gmail, match to records, classify, and write applies/suggestions.

    Returns a summary dict for the API response.
    """
    conn = get_connection(user_email)
    if not conn:
        raise RuntimeError("Gmail is not connected")

    records = _records_for_user(user_email)
    index = _build_record_index(records)
    if not index:
        _connections().update_one(
            {"user_email": user_email},
            {"$set": {"last_synced_at": datetime.now(timezone.utc)}},
        )
        return {"messages_scanned": 0, "auto_applied": 0, "suggested": 0, "ignored": 0}

    since = conn.get("last_synced_at")
    if isinstance(since, datetime) and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    try:
        messages = fetch_recent_messages(user_email, since)
    except GmailAPIError as e:
        # Don't poison last_synced_at — caller (route) can show a user-facing
        # error and the next sync will retry the same window.
        logger.warning("[gmail-sync] fetch_failed user=%s status=%s", user_email, e.status)
        raise RuntimeError(f"Gmail API error ({e.status}). Check that the Gmail API is enabled and the OAuth scope is granted.") from e

    auto_applied = 0
    suggested = 0
    ignored = 0
    skipped_no_match = 0
    skipped_already_in_status = 0
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
    record_meta_by_id: Dict[str, Dict[str, Any]] = {r["record_id"]: r for r in index}

    for msg in messages:
        mid = msg["message_id"]
        if mid in seen_message_ids:
            continue
        seen_message_ids.add(mid)

        match = _match_message_to_record(msg, index)
        if not match:
            skipped_no_match += 1
            ignored += 1
            logger.info(
                "[gmail-sync] no_match subject=%r from=%s",
                (msg.get("subject") or "")[:80], msg.get("from_address"),
            )
            continue
        record_meta, match_score, match_reason = match
        record_buckets[record_meta["record_id"]].append({
            "msg": msg, "score": match_score, "reason": match_reason,
        })

    # ── Phase 2: classify each record's thread as a whole ─────────────────
    # We deliberately re-classify every record that has matched mail on every
    # sync — the per-record thread call is cheap and self-correcting, so a
    # record that was wrongly stuck on "applied" by an older per-message
    # classifier will fix itself the next time sync runs. Skipping when "all
    # messages were seen before" is what trapped users on stale statuses.
    for record_id, items in record_buckets.items():
        rec_meta = record_meta_by_id.get(record_id)
        if not rec_meta:
            continue

        # Newest first — the LLM weighs the most recent email most heavily.
        items.sort(key=lambda x: x["msg"].get("internal_date", 0), reverse=True)
        msg_ids = [it["msg"]["message_id"] for it in items]

        msgs_for_llm = [it["msg"] for it in items[:10]]  # cap at 10 emails for token budget

        # Tier 1: deterministic regex extraction. Runs even if the LLM call
        # later fails, so a clear "we have decided to move forward with other
        # candidates" still resolves to rejected.
        deterministic = extract_deterministic_signals(msgs_for_llm)

        # Tier 2: LLM thread classification. May return None on transient
        # failure — that's fine, deterministic alone can carry the decision.
        llm_cls = classify_record_thread(msgs_for_llm, rec_meta)

        # Reconcile both tiers into a single verdict.
        cls = reconcile_classification(deterministic, llm_cls)
        if not cls:
            ignored += len(items)
            logger.info(
                "[gmail-sync] no_verdict record=%s emails=%d (LLM and regex both inconclusive)",
                record_id, len(items),
            )
            continue
        if cls["status"] == "ignore":
            ignored += len(items)
            logger.info(
                "[gmail-sync] classified=ignore record=%s emails=%d reason=%s",
                record_id, len(items), cls.get("reason"),
            )
            continue

        new_status = cls["status"]
        confidence = cls["confidence"]

        if rec_meta["current_status"] == new_status:
            skipped_already_in_status += 1
            logger.info(
                "[gmail-sync] already_in_status record=%s status=%s confidence=%.2f",
                record_id, new_status, confidence,
            )
            # Even if the status already matches, we may have a stale
            # *pending* suggestion proposing a different state — drop it so
            # the suggestions panel reflects ground truth.
            _suggestions().update_many(
                {
                    "user_email": user_email,
                    "record_id": record_id,
                    "applied": False,
                    "dismissed": False,
                    "suggested_status": {"$ne": new_status},
                },
                {"$set": {"dismissed": True, "dismissed_at": datetime.now(timezone.utc),
                          "dismissed_reason": "superseded by re-classification"}},
            )
            continue

        # Funnel-backwards moves are usually wrong (rejected→interviewing,
        # offer→applied). We still SUGGEST them but never auto-apply, so the
        # user has a chance to confirm.
        backwards = _is_funnel_backwards(rec_meta["current_status"], new_status)
        if backwards:
            logger.info(
                "[gmail-sync] backwards_move record=%s %s→%s — suggesting only",
                record_id, rec_meta["current_status"], new_status,
            )

        # Before writing a fresh suggestion, supersede any prior unresolved
        # suggestions on the same record. This prevents the suggestions panel
        # from accumulating duplicates as we re-classify on every sync, and
        # ensures the user always sees the *current* verdict.
        _suggestions().update_many(
            {
                "user_email": user_email,
                "record_id": record_id,
                "applied": False,
                "dismissed": False,
            },
            {"$set": {"dismissed": True, "dismissed_at": datetime.now(timezone.utc),
                      "dismissed_reason": "superseded by re-classification"}},
        )

        # Use the newest message as the canonical reference; the suggestion
        # row carries supporting_message_ids so we can show the thread later.
        primary = items[0]["msg"]
        suggestion = {
            "suggestion_id": str(uuid.uuid4()),
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
            "confidence": confidence,
            "reason": cls.get("reason", ""),
            "source": cls.get("source", "llm"),
            "evidence_message_id": cls.get("evidence_message_id"),
            "supporting_message_ids": msg_ids,
            "applied": False,
            "dismissed": False,
            "auto_applied": False,
            "created_at": datetime.now(timezone.utc),
        }

        if confidence >= AUTO_APPLY_CONFIDENCE and not backwards \
                and _apply_status(user_email, record_id, new_status):
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

        _suggestions().insert_one(suggestion)

    _connections().update_one(
        {"user_email": user_email},
        {"$set": {"last_synced_at": datetime.now(timezone.utc)}},
    )

    logger.info(
        "[gmail-sync] done user=%s scanned=%d records_with_mail=%d auto=%d suggest=%d "
        "ignored=%d skipped_nomatch=%d skipped_same_status=%d",
        user_email, len(messages), len(record_buckets), auto_applied, suggested, ignored,
        skipped_no_match, skipped_already_in_status,
    )

    return {
        "messages_scanned": len(messages),
        "records_with_mail": len(record_buckets),
        "auto_applied": auto_applied,
        "suggested": suggested,
        "ignored": ignored,
        "skipped_no_match": skipped_no_match,
        "skipped_already_in_status": skipped_already_in_status,
    }


# ---------------------------------------------------------------------------
# Suggestion CRUD (used by the blueprint)
# ---------------------------------------------------------------------------

def _serialize_suggestion(s: Dict[str, Any]) -> Dict[str, Any]:
    s.pop("_id", None)
    for k in ("created_at", "applied_at"):
        v = s.get(k)
        if hasattr(v, "isoformat"):
            s[k] = v.isoformat()
    return s


def list_suggestions(user_email: str, include_resolved: bool = False) -> List[Dict[str, Any]]:
    q: Dict[str, Any] = {"user_email": user_email}
    if not include_resolved:
        q["applied"] = False
        q["dismissed"] = False
    docs = list(_suggestions().find(q).sort("created_at", -1).limit(100))
    return [_serialize_suggestion(d) for d in docs]


def apply_suggestion(user_email: str, suggestion_id: str) -> Dict[str, Any]:
    s = _suggestions().find_one({"suggestion_id": suggestion_id, "user_email": user_email})
    if not s:
        raise LookupError("Suggestion not found")
    if s.get("applied") or s.get("dismissed"):
        return _serialize_suggestion(s)
    if not _apply_status(user_email, s["record_id"], s["suggested_status"]):
        raise RuntimeError("Failed to apply status — record may have been deleted")
    _suggestions().update_one(
        {"suggestion_id": suggestion_id},
        {"$set": {"applied": True, "applied_at": datetime.now(timezone.utc)}},
    )
    s["applied"] = True
    s["applied_at"] = datetime.now(timezone.utc)
    return _serialize_suggestion(s)


def dismiss_suggestion(user_email: str, suggestion_id: str) -> bool:
    res = _suggestions().update_one(
        {"suggestion_id": suggestion_id, "user_email": user_email, "applied": False},
        {"$set": {"dismissed": True, "dismissed_at": datetime.now(timezone.utc)}},
    )
    return bool(res.matched_count)
