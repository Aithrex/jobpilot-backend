"""User-submitted company requests for the tech feed.

The HTTP path NEVER fetches the submitted URL. Sanitisation is static
(scheme, credentials, private IP literals). An async job or operator later
runs the guarded fetcher.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from utils.security import InputSanitizer

logger = logging.getLogger(__name__)

REQUESTS = "tech_feed_company_requests"
AUDIT = "tech_feed_company_request_audit"
REQUEST_VERSION = "tech-feed-company-request-1.0.0"

STATUS_SUBMITTED = "submitted"
STATUS_ALREADY = "already_requested"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"

MAX_NAME = 80
MAX_URL = 500
MAX_PER_USER_HOUR = 8
MAX_PER_USER = 40


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db():
    from utils.db_connect import DBConnect
    return DBConnect().get_db()


def ensure_indexes(*, db=None) -> List[str]:
    db = db if db is not None else _db()
    created: List[str] = []
    try:
        col = db[REQUESTS]
        created.append(col.create_index(
            [("request_id", 1)], unique=True, name="company_request_id_unique"))
        created.append(col.create_index(
            [("user_email", 1), ("created_at", -1)],
            name="company_request_user_recent"))
        created.append(col.create_index(
            [("dedupe_key", 1), ("user_email", 1)],
            name="company_request_user_dedupe"))
        created.append(col.create_index(
            [("dedupe_key", 1), ("status", 1)],
            name="company_request_dedupe_status"))
        created.append(db[AUDIT].create_index(
            [("at", -1)], name="company_request_audit_recent"))
    except Exception as exc:  # pragma: no cover
        logger.warning("tech_feed_company_requests.ensure_indexes: %s", exc)
    return [c for c in created if c]


def normalize_company_name(value: Any) -> str:
    name = InputSanitizer.sanitize_string(value or "", max_length=MAX_NAME)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def sanitize_career_url(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Static SSRF-oriented checks. Does not resolve DNS and does not fetch.

    Returns `(normalized_url, None)` or `(None, reason)`.
    """
    if value in (None, ""):
        return None, None
    raw = str(value).strip()
    if len(raw) > MAX_URL:
        return None, "url_too_long"
    if any(ch in raw for ch in ("\x00", "\r", "\n", "\t")):
        return None, "url_control_chars"
    try:
        parsed = urlparse(raw)
    except Exception:
        return None, "unparseable_url"
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None, f"scheme_not_allowed:{scheme or 'none'}"
    if parsed.username or parsed.password:
        return None, "credentials_in_url"
    if parsed.params or (parsed.query and "token=" in parsed.query.lower()):
        # Query strings are allowed except obvious credential bearers.
        q = (parsed.query or "").lower()
        for needle in ("access_token=", "password=", "secret=", "api_key=",
                       "apikey=", "authorization="):
            if needle in q:
                return None, "credential_bearing_query"
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return None, "no_host"
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return None, "localhost"
    if host.endswith(".internal") or host.endswith(".corp"):
        return None, "internal_host"
    # Literal IPs: refuse private/loopback/link-local/unspecified. No DNS.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return None, "private_ip_literal"
    # Unescape once so `%2e%2e` games cannot hide a second host.
    decoded_host = unquote(host)
    if decoded_host != host and decoded_host in {"localhost"}:
        return None, "localhost"
    path = parsed.path or "/"
    normalized = f"{scheme}://{host}{path}"
    if parsed.query:
        normalized += "?" + parsed.query
    return normalized, None


def _name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def _dedupe_key(name: str, url: Optional[str]) -> str:
    host = ""
    if url:
        try:
            host = (urlparse(url).hostname or "").lower().lstrip("www.")
        except Exception:
            host = ""
    material = f"host:{host}|name:{_name_key(name)}" if host else f"name:{_name_key(name)}"
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _public_id(email: str, dedupe: str) -> str:
    blob = f"{email}|{dedupe}".encode("utf-8")
    return "creq_" + hashlib.sha1(blob).hexdigest()[:12]


def _audit(event: str, *, user_email: str, extra: Optional[Dict[str, Any]] = None,
           db=None) -> None:
    db = db if db is not None else _db()
    try:
        db[AUDIT].insert_one({
            "event": event,
            "user_hash": hashlib.sha256(user_email.encode("utf-8")).hexdigest()[:16],
            "at": _now(),
            **(extra or {}),
        })
    except Exception:  # pragma: no cover
        logger.warning("company_request audit write failed")


def _rate_limited(email: str, *, db=None) -> bool:
    db = db if db is not None else _db()
    since = _now() - timedelta(hours=1)
    n = db[REQUESTS].count_documents(
        {"user_email": email, "created_at": {"$gte": since}})
    return n >= MAX_PER_USER_HOUR


def submit(user_email: str, *, company_name: Any, career_url: Any = None,
           db=None) -> Dict[str, Any]:
    db = db if db is not None else _db()
    ensure_indexes(db=db)
    email = str(user_email or "").strip().lower()
    if not email:
        raise ValueError("authentication required")
    name = normalize_company_name(company_name)
    if len(name) < 2:
        raise ValueError("company_name is required")
    url, url_err = sanitize_career_url(career_url)
    if url_err:
        raise ValueError(url_err)
    if _rate_limited(email, db=db):
        raise OverflowError("rate_limited")
    if db[REQUESTS].count_documents({"user_email": email}) >= MAX_PER_USER:
        raise OverflowError("account_cap")

    dedupe = _dedupe_key(name, url)
    request_id = _public_id(email, dedupe)
    existing = db[REQUESTS].find_one(
        {"user_email": email, "request_id": request_id}, {"_id": 0})
    if existing:
        _audit("duplicate_self", user_email=email,
               extra={"status": existing.get("status")}, db=db)
        return _public(existing, status_override=STATUS_ALREADY)

    # Another user's pending/accepted request for the same key: flag, don't
    # merge identities, don't expose their row.
    other = db[REQUESTS].find_one(
        {"dedupe_key": dedupe, "user_email": {"$ne": email},
         "status": {"$in": [STATUS_SUBMITTED, STATUS_ALREADY, STATUS_ACCEPTED]}},
        {"_id": 1})
    status = STATUS_ALREADY if other else STATUS_SUBMITTED
    now = _now()
    doc = {
        "request_id": request_id,
        "user_email": email,
        "company_name": name,
        "name_key": _name_key(name),
        "career_url": url,
        "dedupe_key": dedupe,
        "status": STATUS_SUBMITTED if not other else STATUS_SUBMITTED,
        "disposition": status,
        "request_version": REQUEST_VERSION,
        "created_at": now,
        "updated_at": now,
    }
    db[REQUESTS].insert_one(doc)
    try:
        from services import company_discovery_intake as intake
        domain = ""
        if url:
            domain = (urlparse(url).hostname or "").lower().lstrip("www.")
        if domain:
            intake.upsert_candidate({
                "company_name": name,
                "owner_domain": domain,
                "official_career_url": url or "",
                "discovery_method": "user_request",
                "evidence_url": url or "",
                "verification_state": intake.STATE_CANDIDATE,
                "verification_reason": "User-requested; queued, not fetched on submit.",
            }, db=db)
    except Exception as exc:
        logger.warning("company_request intake mirror failed (%s)", type(exc).__name__)
    _audit("submitted", user_email=email,
           extra={"disposition": status, "has_url": bool(url)}, db=db)
    return _public(doc, status_override=status)


def _public(doc: Dict[str, Any], *, status_override: Optional[str] = None
            ) -> Dict[str, Any]:
    return {
        "request_id": doc.get("request_id"),
        "status": status_override or doc.get("disposition") or doc.get("status"),
        "company_name": doc.get("company_name"),
        "submitted": True,
    }


def get_for_user(user_email: str, request_id: str, *, db=None
                 ) -> Optional[Dict[str, Any]]:
    db = db if db is not None else _db()
    doc = db[REQUESTS].find_one(
        {"user_email": str(user_email or ""), "request_id": str(request_id or "")},
        {"_id": 0, "user_email": 0})
    return _public(doc) if doc else None


def counts(*, db=None) -> Dict[str, int]:
    db = db if db is not None else _db()
    out: Dict[str, int] = {}
    for row in db[REQUESTS].aggregate([
            {"$group": {"_id": "$disposition", "n": {"$sum": 1}}}]):
        out[str(row["_id"] or "unknown")] = int(row["n"])
    out["total"] = sum(out.values())
    return out
