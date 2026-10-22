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
import re
import socket
from typing import Any, Callable, Optional, Tuple
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


# ---------------------------------------------------------------------------
# Host parsing + boundary-aware domain matching (shared with
# `job_legitimacy_service`, which asks "is this apply link really on vendor X").
# ---------------------------------------------------------------------------
_WEB_SCHEMES = frozenset({"http", "https"})
# Characters whose presence in the AUTHORITY means two parsers can disagree on
# the host. `urlparse` takes the host after the LAST '@'; a browser treats '\'
# as '/' for http(s), so "https://attacker.example\@boards.greenhouse.io/" is
# greenhouse to urllib and attacker.example to Chromium. '%' is decoded by a
# browser and not by urllib. Whitespace/control characters are stripped by
# browsers. Refusing them is cheaper than emulating WHATWG.
_AMBIGUOUS_AUTHORITY = re.compile(r"[\\%\s\x00-\x1f\x7f]")
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def canonical_host(host: Any) -> str:
    """Lowercase, trailing-dot-free, IDNA (punycode) form of a hostname, or "".

    "" means "not a hostname we can reason about" and must be treated as no
    match — never as a wildcard. IP literals are returned unchanged (they can
    never equal a vendor domain)."""
    if not host or not isinstance(host, str):
        return ""
    h = host.strip().rstrip(".")
    if not h or _AMBIGUOUS_AUTHORITY.search(h):
        return ""
    try:
        ipaddress.ip_address(h.strip("[]"))
        return h.strip("[]").lower()
    except ValueError:
        pass
    try:
        # IDNA2003 via the stdlib codec: maps fullwidth dots and case exactly
        # as a browser would, and turns any non-ASCII label into xn--.
        h = h.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return ""
    labels = h.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.match(lb) for lb in labels):
        return ""
    return h


def url_host(url: Any) -> str:
    """The canonical host of an http(s) URL, or "" when there is none we trust.

    Scheme must be http/https; scheme-relative and bare hosts are refused
    (a vendor name in free text is not a URL). The host is what a BROWSER
    would connect to: authorities a browser and urllib would read differently
    are refused outright."""
    if not url or not isinstance(url, str):
        return ""
    raw = url.strip()
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if (parsed.scheme or "").lower() not in _WEB_SCHEMES:
        return ""
    netloc = parsed.netloc or ""
    if not netloc or _AMBIGUOUS_AUTHORITY.search(netloc):
        return ""
    try:
        host = parsed.hostname
    except ValueError:
        return ""
    return canonical_host(host or "")


def url_userinfo(url: Any) -> str:
    """The userinfo part of a URL ("" when absent). Never used as a host."""
    try:
        netloc = urlparse(str(url or "").strip()).netloc or ""
    except Exception:
        return ""
    return netloc.rpartition("@")[0] if "@" in netloc else ""


def host_matches_domain(host: Any, domain: Any) -> bool:
    """Boundary-aware: ``host == domain`` or ``host`` ends with ``"." + domain``.

    Both sides are canonicalised first, so "BOARDS.Greenhouse.IO." matches
    "greenhouse.io" and "greenhouse.io.attacker.example" / "evilgreenhouse.io"
    do not."""
    h = canonical_host(host) if isinstance(host, str) else ""
    d = canonical_host(domain) if isinstance(domain, str) else ""
    if not h or not d:
        return False
    return h == d or h.endswith("." + d)


def host_on_allowlist(host: Optional[str]) -> bool:
    """Public-suffix-boundary membership: host == suffix or host endswith
    ``.`` + suffix. Case-insensitive; trailing dots ignored."""
    if not host or not isinstance(host, str):
        return False
    h = host.strip().rstrip(".").lower()
    if not h:
        return False
    for suf in allowed_suffixes():
        if host_matches_domain(h, suf):
            return True
    return False


_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


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
        # RFC 6598 shared address space (100.64/10): CGNAT and some clouds'
        # internal service endpoints. `ipaddress` calls it neither private
        # nor reserved, so it has to be named.
        or (ip.version == 4 and ip in _SHARED_ADDRESS_SPACE)
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
    # An apply target never carries credentials, and "user@host" is the shape
    # of every host-confusion trick ("https://greenhouse.io@attacker.example").
    if "@" in (parsed.netloc or ""):
        return False, "userinfo_not_allowed"
    # `parsed.hostname` is what urllib THINKS the host is; the browser that
    # navigates may disagree ('\', '%', whitespace). `url_host` refuses those.
    host = url_host(url)
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
