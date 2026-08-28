"""Provider registry — the single place a vendor is added, and everything the
rest of the system derives from that one registration.

`job_ingest_worker` keeps four hand-maintained tables that must all agree
(`FETCHERS`, `_HOST_ALLOW`, `_PROBE_HOST_ALLOW`, `_TERMLESS_ATS`) and its own
comment records what happens when they do not: "Adding a fetcher without adding
it here ships a source that returns nothing." Those tables are now DERIVED here,
so the failure mode is structurally impossible rather than merely documented.

Import side effects are deliberate: importing this module registers the built-in
providers, so `get(vendor)` works without callers knowing which module defines
which vendor.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from services.ats_providers.base import (AtsProvider, Endpoint, Fingerprint,
                                         STATUS_CANDIDATE)

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
#: Held across the built-in IMPORTS, and separate from `_LOCK` on purpose: the
#: modules being imported call `register`, which takes `_LOCK`, so loading
#: under that lock would rely on reentrancy from the loading thread while still
#: letting other threads through. See `_load_builtins`.
_LOAD_LOCK = threading.Lock()
_PROVIDERS: Dict[str, AtsProvider] = {}
_LOADED = False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(provider: AtsProvider, *, replace: bool = False) -> AtsProvider:
    """Add a provider. Raises on a duplicate vendor unless `replace`.

    Duplicate detection matters: two providers claiming the same vendor key
    would make `member_key` ambiguous, and `member_key` is the prefix of every
    `source_job_id` written under ADR-003.
    """
    vendor = (getattr(provider, "vendor", "") or "").strip()
    if not vendor:
        raise ValueError("provider must declare a non-empty `vendor`")
    with _LOCK:
        if vendor in _PROVIDERS and not replace:
            raise ValueError(f"provider already registered: {vendor}")
        _PROVIDERS[vendor] = provider
    return provider


def _load_builtins() -> None:
    """Import the built-in providers exactly once, for the whole process.

    The flag is set AFTER the imports and the whole thing runs under
    `_LOAD_LOCK`. Setting it first - which is what this did - is a race with
    real consequences rather than a theoretical one: a second thread calling
    `get()` while the first was still importing saw `_LOADED = True`, returned
    immediately, and read an EMPTY `_PROVIDERS`. So `get("greenhouse")`
    returned None for a vendor that is registered.

    Measured: an endpoint audit running six worker threads got a provider back
    for 1 of 342 rows and silently skipped the other 341. Every concurrent
    caller in this repo is exposed to it - `job_ingest_worker` fans out over a
    shard's members, `discover_batch` over a batch of companies - and the
    symptom there is not an error, it is a member that quietly lists nothing.
    """
    global _LOADED
    with _LOAD_LOCK:
        if _LOADED:
            return
        # Imported for side effect (each module calls `register`). Failures are
        # logged, never raised: one broken vendor module must not take down job
        # search for every other vendor.
        for mod in ("boards", "workday", "oracle_orc", "icims", "taleo",
                    "payroll", "enterprise", "amazon", "radancy"):
            try:
                __import__(f"services.ats_providers.{mod}")
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("ats_providers: failed to load %s: %s (%s)",
                               mod, exc, type(exc).__name__)
        _LOADED = True


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def get(vendor: str) -> Optional[AtsProvider]:
    _load_builtins()
    return _PROVIDERS.get((vendor or "").strip().lower())


def require(vendor: str) -> AtsProvider:
    p = get(vendor)
    if p is None:
        raise KeyError(f"unknown ATS vendor: {vendor}")
    return p


def all_providers(*, enabled_only: bool = True) -> List[AtsProvider]:
    _load_builtins()
    with _LOCK:
        vals = list(_PROVIDERS.values())
    if enabled_only:
        vals = [p for p in vals if getattr(p, "enabled", True)]
    return sorted(vals, key=lambda p: p.vendor)


def vendors(*, enabled_only: bool = True) -> List[str]:
    return [p.vendor for p in all_providers(enabled_only=enabled_only)]


def termless_vendors() -> Tuple[str, ...]:
    """Vendors the BACKGROUND crawler may sweep.

    Replaces `job_ingest_worker._TERMLESS_ATS`. A term-driven vendor cannot be
    paged newest-first, so it cannot honour ADR-003's early-stop rule and must
    not join the sweep.
    """
    return tuple(p.vendor for p in all_providers() if p.termless)


def detail_vendors() -> Tuple[str, ...]:
    return tuple(p.vendor for p in all_providers() if p.has_detail)


# ---------------------------------------------------------------------------
# Derived tables
# ---------------------------------------------------------------------------

def api_hosts(vendor: Optional[str] = None) -> Tuple[str, ...]:
    """Hosts the crawler may call for listing/detail data."""
    provs = [require(vendor)] if vendor else all_providers()
    out: List[str] = []
    for p in provs:
        out.extend(p.api_hosts)
    return tuple(sorted(set(out)))


def portal_hosts(vendor: Optional[str] = None) -> Tuple[str, ...]:
    """Hosts the liveness probe may call (public apply pages).

    Kept separate from `api_hosts` for the reason `job_ingest_worker` already
    documents: they are genuinely different hosts (`boards-api.greenhouse.io` vs
    `boards.greenhouse.io`) and widening the crawl allowlist to cover probing
    would grow the fetch surface for no reason.
    """
    provs = [require(vendor)] if vendor else all_providers()
    out: List[str] = []
    for p in provs:
        out.extend(p.portal_hosts or ())
    return tuple(sorted(set(out)))


def pacing_key_for(vendor: str, host: str) -> str:
    """Throttle scope for a request.

    Falls back to the hostname, which is today's `HostPolicy` behaviour, so
    providers that do not care are unaffected. iCIMS overrides it to
    "icims.com" because its wall is enforced per source IP across every tenant
    hostname (ADR-004 §3.2).
    """
    p = get(vendor)
    if p is not None and p.policy.pacing_key:
        return p.policy.pacing_key
    return (host or "").lower()


# ---------------------------------------------------------------------------
# Fingerprinting — used by career_portal_discovery
# ---------------------------------------------------------------------------

def match_url(url: str) -> Optional[Tuple[AtsProvider, Dict[str, str], int]]:
    """Best (provider, captured fields, confidence) for one URL, or None.

    "Best" is the highest-confidence fingerprint that captured a tenant. A
    pattern capturing tenant+site should declare higher confidence than one
    capturing tenant alone, so a richer match wins.
    """
    best: Optional[Tuple[AtsProvider, Dict[str, str], int]] = None
    for p in all_providers():
        for f in p.fingerprints:
            fields = f.match(url)
            if fields is None:
                continue
            if f.identity_only or not fields:
                continue
            if best is None or f.confidence > best[2]:
                best = (p, fields, f.confidence)
    return best


def endpoints_from_urls(urls: Iterable[str]) -> List[Endpoint]:
    """Every distinct ATS endpoint implied by a page's links.

    Deduped on `member_key` because a careers page routinely links the same board
    many times (nav, hero button, footer, per-job cards).
    """
    seen: Dict[str, Endpoint] = {}
    for url in urls:
        hit = match_url(url)
        if hit is None:
            continue
        provider, fields, _conf = hit
        try:
            ep = provider.endpoint_from_match(fields, url)
        except Exception:
            ep = None
        if ep is None or not ep.tenant:
            continue
        ep.status = STATUS_CANDIDATE
        seen.setdefault(ep.member_key, ep)
    return list(seen.values())


def vendor_of_url(url: str) -> Optional[str]:
    hit = match_url(url)
    return hit[0].vendor if hit else None
