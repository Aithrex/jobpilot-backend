"""Security utilities for input validation and sanitization."""
import ipaddress
import os
import re
import html
import logging
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
    Simple in-memory rate limiter.

    Note: On AWS Lambda, each invocation may run in a different container, so state
    does not persist across requests. This provides best-effort per-container limiting
    only. API Gateway throttling (configured in Terraform) is the primary rate limit.
    """

    def __init__(self):
        self._requests = {}
    
    def is_rate_limited(self, key: str, max_requests: int = 100, window_seconds: int = 60) -> bool:
        """
        Check if a key is rate limited.
        Returns True if rate limit exceeded.
        """
        import time
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
        import time
        current_time = time.time()
        
        keys_to_remove = []
        for key, requests in self._requests.items():
            if not requests or current_time - max(requests) > 3600:
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del self._requests[key]


# Global rate limiter instance
_rate_limiter = RateLimiter()

def get_rate_limiter() -> RateLimiter:
    return _rate_limiter
