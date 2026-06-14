"""Security utilities for input validation and sanitization."""
import re
import html
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_client_ip(request) -> str:
    """
    Get client IP from request, respecting X-Forwarded-For when behind a proxy.
    Uses the leftmost (original client) IP when the header contains multiple IPs.
    """
    raw = ""
    if request.headers.getlist("X-Forwarded-For"):
        raw = request.headers.getlist("X-Forwarded-For")[0].strip()
    else:
        raw = (request.remote_addr or "").strip()
    return raw.split(",")[0].strip() if raw else ""


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
