"""SSRF containment for the auto-apply browser-execution plane (audit P0-1).

The apply URL that the cloud browser worker navigates originates from a job
posting — attacker-influenceable data. Without a guard, driving a headless
Chromium to that URL is a classic SSRF primitive against the runtime's metadata
endpoint / VPC / localhost. This module is the DEFENSE-IN-DEPTH layer that sits
in front of every navigation (atop the separate network-egress firewall the
devops task owns).

Two gates, deliberately different in strictness:

  * ``apply_url_allowed(url)`` — the ENVELOPE gate. The top-level apply target
    must be ``https`` and its host must match a PUBLIC-SUFFIX-boundary allow-list
    of the supported ATS domains (greenhouse.io / grnh.se / lever.co /
    ashbyhq.com / myworkday*.com), config-extensible via
    ``AUTO_APPLY_ATS_ALLOWLIST``. Anything else never dispatches. Pure/offline
    (no DNS) so it is cheap to call while building the execution envelope.

  * ``validate_request_url(url, resolver=...)`` — the INTERCEPTOR gate, applied
    by a Playwright ``context.route("**")`` handler to EVERY navigation /
    redirect / subresource / popup / download / websocket. It denies
    ``file:``/``data:``/``blob:``/custom schemes and blocks any request whose
    host is (or RESOLVES to) a private / loopback / link-local / reserved /
    cloud-metadata address for BOTH IPv4 and IPv6 — resolving the host and
    pinning the resolved IP so a DNS-rebinding public name pointing at an
    internal A record is rejected even though the name looks public.

The IPv4/IPv6 range logic mirrors ``services.jd_fetcher`` (stdlib ``ipaddress`` +
``socket.getaddrinfo``; no new deps), so the two SSRF guards agree byte-for-byte
on what "private" means (169.254.169.254, fd00:ec2::254, ::1, fc00::/7, fe80::/10,
RFC1918, 0.0.0.0/::, v4-mapped variants).
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Schemes the interceptor permits at all (everything else — file/data/blob/
# javascript/about/ws-less custom — is denied). The envelope gate is stricter
# still (https only).
_REQUEST_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_ENVELOPE_SCHEMES = frozenset({"https"})

# Supported ATS families' apex domains. A host matches iff it EQUALS one of these
# or is a sub-domain of it (public-suffix-boundary — "evilgreenhouse.io" and
# "greenhouse.io.evil.com" both fail).
_DEFAULT_ATS_SUFFIXES: Tuple[str, ...] = (
    "greenhouse.io", "grnh.se",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com", "myworkdaysite.com", "myworkday.com",
)


def _extra_allowlist() -> Tuple[str, ...]:
    """Operator-extensible allow-list from ``AUTO_APPLY_ATS_ALLOWLIST`` (csv).
    Never raises — a config/import failure just yields the built-in set."""
    try:
        from utils.config import _get_config_value
        raw = _get_config_value("AUTO_APPLY_ATS_ALLOWLIST", "") or ""
    except Exception:  # pragma: no cover - defensive
        return ()
    return tuple(s.strip().lower().strip(".") for s in raw.split(",") if s.strip())


def allowed_suffixes() -> frozenset:
    return frozenset(_DEFAULT_ATS_SUFFIXES) | frozenset(_extra_allowlist())


def host_on_allowlist(host: Optional[str]) -> bool:
    """Public-suffix-boundary membership: host == suffix or host endswith
    ``.`` + suffix. Case-insensitive; trailing dots ignored."""
    if not host or not isinstance(host, str):
        return False
    h = host.strip().rstrip(".").lower()
    if not h:
        return False
    for suf in allowed_suffixes():
        if h == suf or h.endswith("." + suf):
            return True
    return False


def _ip_is_public(ip) -> bool:
    """False for loopback / private (10/8, 172.16/12, 192.168/16, fc00::/7) /
    link-local (169.254/16 incl. 169.254.169.254, fe80::/10) / reserved (incl.
    fd00:ec2::254 metadata) / multicast / unspecified (0.0.0.0, ::). v4-mapped
    v6 is unwrapped so ``::ffff:127.0.0.1`` fails like ``127.0.0.1``."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _parse(url: str):
    try:
        return urlparse((url or "").strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Gate 1 — the envelope allow-list (https + suffix-boundary; NO DNS).
# ---------------------------------------------------------------------------
def apply_url_allowed(url: str) -> Tuple[bool, str]:
    """(allowed, reason) for a top-level apply URL. ``https`` only + the host on
    the supported-ATS suffix-boundary allow-list. Literal IPs are never on the
    allow-list (ATS targets are named hosts), so they are rejected here."""
    if not url or not isinstance(url, str):
        return False, "empty_url"
    parsed = _parse(url)
    if parsed is None:
        return False, "unparseable_url"
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ENVELOPE_SCHEMES:
        return False, f"scheme_not_https:{scheme or 'none'}"
    host = parsed.hostname
    if not host:
        return False, "no_host"
    if not host_on_allowlist(host):
        return False, "host_not_allowlisted"
    return True, ""


# ---------------------------------------------------------------------------
# Gate 2 — the per-request interceptor validation (scheme + IP + DNS pin).
# ---------------------------------------------------------------------------
def validate_request_url(
        url: str,
        *,
        resolver: Optional[Callable] = None) -> Tuple[bool, str]:
    """(allowed, reason) for ANY request the browser makes — navigation,
    redirect, subresource, popup, download, websocket. Denies non-http(s)/ws(s)
    schemes (file/data/blob/custom) and any host that is, or RESOLVES to, a
    non-public address (rebinding defense). DNS failure fails CLOSED.

    ``resolver`` is a ``socket.getaddrinfo``-shaped callable (injected in tests);
    defaults to the real resolver."""
    if not url or not isinstance(url, str):
        return False, "empty_url"
    parsed = _parse(url)
    if parsed is None:
        return False, "unparseable_url"
    scheme = (parsed.scheme or "").lower()
    if scheme not in _REQUEST_SCHEMES:
        # file: / data: / blob: / javascript: / about: / chrome: / custom.
        return False, f"scheme_denied:{scheme or 'none'}"
    host = parsed.hostname
    if not host:
        return False, "no_host"

    # Literal IP (incl. bracketed IPv6) — validate directly, no DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return (True, "") if _ip_is_public(literal) else (False, "private_ip_literal")

    getaddrinfo = resolver or socket.getaddrinfo
    try:
        port = parsed.port or (443 if scheme in ("https", "wss") else 80)
    except ValueError:
        return False, "bad_port"
    try:
        infos = getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, UnicodeError, ValueError):
        return False, "dns_failure"
    if not infos:
        return False, "dns_empty"
    for info in infos:
        try:
            addr = str(info[4][0]).split("%")[0]
            resolved = ipaddress.ip_address(addr)
        except (ValueError, IndexError, TypeError):
            return False, "unparseable_resolved_addr"
        if not _ip_is_public(resolved):
            return False, "resolves_to_private_ip"
    return True, ""


# ---------------------------------------------------------------------------
# Playwright route handler factory — context.route("**", make_route_handler()).
# ---------------------------------------------------------------------------
def make_route_handler(resolver: Optional[Callable] = None) -> Callable:
    """Return a Playwright route handler that aborts any request failing
    ``validate_request_url`` and continues the rest. Bound to ``context.route``
    so it fires for every navigation/redirect/subresource/popup/download/ws.

    The handler NEVER raises: a Playwright fault while aborting/continuing is
    swallowed (the egress firewall is the backstop), and an un-abortable request
    defaults to abort (fail-closed)."""
    def _handler(route, request=None):
        req = request if request is not None else getattr(route, "request", None)
        url = getattr(req, "url", "") if req is not None else ""
        allowed, reason = validate_request_url(url, resolver=resolver)
        try:
            if allowed:
                route.continue_()
            else:
                logger.warning("ssrf_guard: aborted browser request (%s)", reason)
                route.abort()
        except Exception:  # pragma: no cover - defensive; egress fw is the backstop
            try:
                route.abort()
            except Exception:
                pass
    return _handler


def install_route_guard(context, resolver: Optional[Callable] = None) -> bool:
    """Install the interceptor on a Playwright BrowserContext. Best-effort:
    returns True on success, False if the context has no ``route`` (older API /
    a page-only object) — the caller then falls back to page-level routing."""
    try:
        context.route("**", make_route_handler(resolver))
        return True
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("ssrf_guard: could not install context route guard: %s", e)
        return False
