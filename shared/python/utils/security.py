"""Security utilities for input validation and sanitization."""
import hashlib
import ipaddress
import os
import re
import html
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)


def _peer_ip(request) -> str:
    """Transport-level peer IP — set by the infrastructure, never spoofable.

    Under Lambda, apig-wsgi exposes the raw API Gateway request context in the
    WSGI environ (``apig_wsgi.request_context``); its ``sourceIp`` comes from
    the TCP connection as observed by AWS. (apig-wsgi also mirrors it into
    REMOTE_ADDR for HTTP API v2 events, but reading the context is explicit
    and covers v1 payloads too.) Locally this falls back to remote_addr.
    """
    try:
        ctx = request.environ.get("apig_wsgi.request_context") or {}
        source = (
            (ctx.get("http") or {}).get("sourceIp")        # HTTP API payload v2
            or (ctx.get("identity") or {}).get("sourceIp")  # REST API / v1
        )
        if source:
            return str(source).strip()
    except Exception:  # pragma: no cover - defensive (odd environ shapes)
        pass
    return (request.remote_addr or "").strip()


def _normalize_ip_token(token: str) -> str:
    """Validate one X-Forwarded-For token as an IP; '' if it isn't one.

    Handles the common proxy decorations: ``1.2.3.4:5678`` and ``[::1]:5678``.
    """
    token = (token or "").strip()
    if not token:
        return ""
    if token.startswith("["):  # [ipv6]:port
        token = token[1:].split("]", 1)[0]
    elif token.count(":") == 1 and "." in token:  # ipv4:port
        token = token.split(":", 1)[0]
    try:
        ipaddress.ip_address(token.split("%")[0])
    except ValueError:
        return ""
    return token


def _trusted_proxy_hops() -> int:
    """Number of trusted forwarders in FRONT of API Gateway that each append
    the true peer IP to X-Forwarded-For (CloudFront = 1, the prod default).
    Set TRUSTED_PROXY_HOPS=0 when API Gateway is hit directly (no CloudFront)
    so the connection source IP is used and XFF is ignored entirely."""
    try:
        return max(0, int(os.getenv("TRUSTED_PROXY_HOPS", "1")))
    except (TypeError, ValueError):
        return 1


def get_client_ip(request) -> str:
    """Real client IP, spoof-resistant behind CloudFront + API Gateway.

    Trust model: each trusted hop APPENDS the IP of its own peer to
    X-Forwarded-For, so everything a client sent itself sits on the LEFT and
    is attacker-controlled; only the RIGHTMOST entries are trustworthy.
    Prod chain (client -> CloudFront -> API Gateway -> Lambda) arrives as
    ``[client-junk..., real_client, cloudfront_edge]`` with the request
    context sourceIp == cloudfront_edge; we drop the entry duplicating the
    connection peer, then take the rightmost remaining entry (per
    TRUSTED_PROXY_HOPS, default 1 = CloudFront appended it).

    Unchanged for existing traffic: requests without X-Forwarded-For (local
    dev, tests, direct calls) still resolve to remote_addr, and a normal
    non-spoofing user keys to the same IP as the old leftmost parse — only
    attacker-prepended junk stops being honored. The return shape (a bare IP
    string used as a RateLimiter key) is identical.
    """
    peer = _peer_ip(request)

    entries: List[str] = []
    try:
        header_values = request.headers.getlist("X-Forwarded-For")
    except Exception:  # pragma: no cover - defensive
        header_values = []
    for header_value in header_values:
        for token in (header_value or "").split(","):
            token = token.strip()
            if token:
                entries.append(token)

    hops = _trusted_proxy_hops()
    if not entries or hops == 0:
        return peer

    # API Gateway appends its own peer (the CloudFront edge, or the client
    # when hit directly) as the last entry — drop the duplicate so the
    # rightmost remaining entry is what the outermost trusted hop recorded.
    if peer and entries[-1] == peer:
        entries = entries[:-1]
    if not entries:
        return peer  # direct-to-API-Gateway: the peer IS the client

    # Rightmost entry appended by the `hops` trusted forwarders = the client.
    # Never walk further LEFT than that position — everything left of it is
    # attacker-controlled; an invalid token there falls back to the peer.
    idx = max(0, len(entries) - hops)
    return _normalize_ip_token(entries[idx]) or peer


class InputSanitizer:
    """Utility class for sanitizing user inputs to prevent injection attacks"""
    
    # Characters that could be used in NoSQL injection
    NOSQL_INJECTION_PATTERNS = [
        r'\$where', r'\$gt', r'\$lt', r'\$ne', r'\$eq', r'\$regex',
        r'\$or', r'\$and', r'\$not', r'\$exists', r'\$type',
        r'\$expr', r'\$jsonSchema', r'\$mod', r'\$text',
    ]
    
    @staticmethod
    def sanitize_string(value: str, max_length: int = 1000) -> str:
        """
        Sanitize a string input:
        - Strip whitespace
        - Remove null bytes and control characters
        - Limit length
        - Escape HTML entities
        """
        if not value:
            return ""
        
        # Convert to string and limit length
        value = str(value).strip()[:max_length]
        
        # Remove null bytes and control characters (except newlines/tabs)
        value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', value)
        
        return value
    
    @staticmethod
    def sanitize_html(value: str, max_length: int = 10000) -> str:
        """Sanitize and escape HTML entities to prevent XSS"""
        value = InputSanitizer.sanitize_string(value, max_length)
        return html.escape(value)
    
    @staticmethod
    def sanitize_email(email: str) -> Optional[str]:
        """Validate and sanitize email address"""
        if not email:
            return None
        
        email = InputSanitizer.sanitize_string(email.lower(), 320)
        
        # RFC 5322 inspired email pattern
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if re.match(email_pattern, email):
            return email
        return None
    
    @staticmethod
    def sanitize_username(username: str) -> str:
        """Sanitize username - only alphanumeric and underscores"""
        if not username:
            return ""
        
        username = InputSanitizer.sanitize_string(username, 50)
        # Only allow alphanumeric and underscores
        return re.sub(r'[^a-zA-Z0-9_]', '', username)
    
    @staticmethod
    def check_nosql_injection(value: str) -> bool:
        """
        Check if a string contains potential NoSQL injection patterns.
        Returns True if suspicious patterns are found.
        """
        if not value:
            return False
        
        value_lower = value.lower()
        for pattern in InputSanitizer.NOSQL_INJECTION_PATTERNS:
            if re.search(pattern, value_lower, re.IGNORECASE):
                logger.warning(f"Potential NoSQL injection detected: {pattern}")
                return True
        return False
    
    @staticmethod
    def sanitize_mongo_query(value: str) -> str:
        """
        Sanitize a value for use in MongoDB queries.
        Removes $ prefix and converts to safe string.
        """
        if not value:
            return ""
        
        value = InputSanitizer.sanitize_string(value)
        
        # Remove $ from start of string (prevents operator injection)
        while value.startswith('$'):
            value = value[1:]
        
        # Remove dots to prevent field traversal
        value = value.replace('.', '_')
        
        return value
    
    @staticmethod
    def sanitize_ip_address(ip: str) -> Optional[str]:
        """Validate and sanitize IP address (IPv4 or IPv6)"""
        if not ip:
            return None
        
        ip = InputSanitizer.sanitize_string(ip, 45)  # Max IPv6 length
        
        # IPv4 pattern
        ipv4_pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
        # Simplified IPv6 pattern
        ipv6_pattern = r'^([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}$'
        
        if re.match(ipv4_pattern, ip) or re.match(ipv6_pattern, ip):
            return ip
        
        return None


class RateLimiter:
    """
    In-memory (per-container) sliding-window rate limiter.

    NOT a security control on its own: on AWS Lambda each concurrently-running
    container keeps its own dict, so the effective ceiling is
    (containers x max_requests). Use it only as the CHEAP FIRST PASS in front
    of ``DistributedRateLimiter`` (see ``check_rate_limit`` below), which holds
    the authoritative, cross-container count in MongoDB.
    """

    def __init__(self):
        self._requests = {}

    def is_rate_limited(self, key: str, max_requests: int = 100, window_seconds: int = 60) -> bool:
        """
        Check if a key is rate limited.
        Returns True if rate limit exceeded.
        """
        current_time = time.time()

        # Periodic cleanup to prevent memory growth (every 100 checks)
        if not hasattr(self, '_check_count'):
            self._check_count = 0
        self._check_count += 1
        if self._check_count % 100 == 0:
            self.cleanup()

        if key not in self._requests:
            self._requests[key] = []

        # Remove old requests outside the window
        self._requests[key] = [
            req_time for req_time in self._requests[key]
            if current_time - req_time < window_seconds
        ]

        # Check if limit exceeded
        if len(self._requests[key]) >= max_requests:
            return True

        # Add current request
        self._requests[key].append(current_time)
        return False
    
    def cleanup(self):
        """Remove stale entries to prevent memory growth"""
        current_time = time.time()
        
        keys_to_remove = []
        for key, requests in self._requests.items():
            if not requests or current_time - max(requests) > 3600:
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del self._requests[key]


class DistributedRateLimiter:
    """Authoritative rate limiter: one fixed-window counter per (key, window)
    held in MongoDB, incremented atomically.

    Why this exists: ``RateLimiter`` above keeps state in a per-container dict.
    With N concurrently-warm Lambda containers the real ceiling becomes
    N x max_requests, so "10 uploads per hour per client" was never actually
    enforced. API Gateway throttling cannot express a per-client-per-hour rule
    either (it is per-route and account-wide), so the limit has to live in the
    one piece of state every container shares: Mongo.

    Mechanics (same shape as guest_global_counters / guest_challenge_velocity):
    a single ``find_one_and_update`` with ``$inc`` + ``upsert`` is atomic on the
    server, so concurrent containers cannot interleave a lost update. The window
    index is baked into the document id, so a new window starts on a fresh doc
    and old docs are reaped by a TTL index on ``expires_at``.

    Fixed windows (not sliding) are deliberate: one round trip, no array
    growth, no read-modify-write. The known trade-off is that a client can
    burst up to 2x the limit across a window boundary; that is an acceptable
    price for a limit that is actually shared.

    No raw IP is stored: the document id is a salted-free sha256 of the
    composite key, and only a coarse non-PII ``bucket`` label is kept for ops.
    """

    COLLECTION = "rate_limit_buckets"

    def __init__(self, collection_name: str = COLLECTION):
        self._collection_name = collection_name
        self._index_ready = False

    def _collection(self):
        # Imported lazily: utils.security is imported by modules that must not
        # pull in pymongo/db config at import time (cold-start cost).
        from utils.db_connect import DBConnect

        return DBConnect().get_db()[self._collection_name]

    def _ensure_index(self, collection) -> None:
        if self._index_ready:
            return
        try:
            collection.create_index("expires_at", expireAfterSeconds=0)
        except Exception:  # pragma: no cover - index may already exist / no perms
            pass
        self._index_ready = True  # never retry per request

    def is_rate_limited(
        self,
        key: str,
        max_requests: int = 100,
        window_seconds: int = 60,
        fail_open: bool = False,
    ) -> bool:
        """True when this key has exceeded ``max_requests`` in the current
        window, counted across ALL containers.

        ``fail_open`` decides what a storage error means. See
        ``check_rate_limit`` for the policy split.
        """
        if max_requests <= 0:
            return True
        window_seconds = max(1, int(window_seconds or 1))
        now = time.time()
        window_index = int(now // window_seconds)
        composite = f"{key}|{window_seconds}|{window_index}"
        doc_id = hashlib.sha256(composite.encode("utf-8")).hexdigest()
        # Keep the leading, non-identifying part of the key (e.g. "guest:onboard")
        # for dashboards; the client-identifying tail never lands in Mongo.
        bucket = ":".join(str(key).split(":")[:2])[:64]
        expires_at = datetime.fromtimestamp(
            (window_index + 2) * window_seconds, tz=timezone.utc
        )
        try:
            from pymongo import ReturnDocument

            collection = self._collection()
            self._ensure_index(collection)
            doc = collection.find_one_and_update(
                {"_id": doc_id},
                {
                    "$inc": {"count": 1},
                    "$setOnInsert": {"bucket": bucket, "expires_at": expires_at},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return int((doc or {}).get("count") or 0) > max_requests
        except Exception as e:
            logger.error(
                "distributed rate limiter unavailable for bucket=%s (fail_%s): %s",
                bucket, "open" if fail_open else "closed", type(e).__name__,
            )
            return not fail_open


# Global rate limiter instances
_rate_limiter = RateLimiter()
_distributed_rate_limiter = DistributedRateLimiter()


def get_rate_limiter() -> RateLimiter:
    return _rate_limiter


def get_distributed_rate_limiter() -> DistributedRateLimiter:
    return _distributed_rate_limiter


def check_rate_limit(
    key: str,
    max_requests: int = 100,
    window_seconds: int = 60,
    fail_open: bool = False,
) -> bool:
    """THE rate-limit decision. True = reject the request.

    Two stages, in order:
      1. Cheap per-container first pass. If THIS container alone has already
         used the whole allowance, the shared count has certainly been exceeded
         too, so we can reject without a Mongo round trip. This can only ever
         short-circuit to "limited" - it can never grant a request.
      2. The AUTHORITATIVE distributed counter. Every request that survives
         stage 1 is counted here, so the limit holds across containers.

    Storage-failure policy (``fail_open``):
      * EXPENSIVE / abusable endpoints (guest onboard, guest matches, tailor,
        download, claim, auth) pass fail_open=False - a Mongo outage must not
        become an open door to unmetered LLM, scrape or storage spend.
      * CHEAP READ endpoints (job polling, session lookup) pass fail_open=True
        - these are what an ALREADY-ADMITTED user is doing every couple of
        seconds, and a brief Mongo blip should degrade to "unthrottled reads",
        not "every active session breaks". The blast radius of an unlimited
        read is a few extra Mongo reads; the blast radius of a blocked poll is
        a broken product for everyone mid-flow.
    """
    if _rate_limiter.is_rate_limited(key, max_requests, window_seconds):
        return True
    return _distributed_rate_limiter.is_rate_limited(
        key, max_requests, window_seconds, fail_open=fail_open
    )
