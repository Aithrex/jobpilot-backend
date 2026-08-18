"""Vendor-aware paced HTTP, and the `FetchContext` factories built on it.

Two things live here that the existing crawler cannot express.

**1. Pacing keyed on the vendor, not the hostname.**
`job_ingest_worker.HostPolicy` spaces requests per hostname. That is right for
Greenhouse (every tenant shares `boards-api.greenhouse.io`, so the hostname IS
the vendor) and wrong for iCIMS, where each tenant has its own hostname
(`careers-<slug>.icims.com`) but the anti-bot wall counts per source IP across
all of them. Measured 2026-08-17: six concurrent requests spread over sixty
distinct iCIMS hostnames tripped the wall on all sixty. `PacedSession` therefore
resolves a *pacing key* through the provider registry, so a vendor can declare
that all its hosts share one bucket.

**2. Text as a first-class response.**
`http_get_json` can only return decoded JSON, so an HTML-only vendor is
unreachable through it - which is precisely why iCIMS was written off. `get_text`
is the same guarded, paced, deadline-clamped path that happens to return a body.

Everything here is transport. Host allowlisting and SSRF guarding are supplied by
the caller as a `guard` callable, so the crawler keeps its fail-closed posture
and a CLI script can opt into a looser one without this module deciding policy.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import defaultdict
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from services.ats_providers.base import ChallengeError, CrawlPolicy, FetchContext

logger = logging.getLogger(__name__)

DEFAULT_UA = "Mozilla/5.0 (compatible; JobPilotIngest/1.0; +https://jobpilot.ai/bot)"

# A vendor serving HTML must not be told we only accept JSON: several return 406
# or a stripped page when the Accept header disagrees with what they render.
_JSON_HEADERS = {"User-Agent": DEFAULT_UA, "Accept": "application/json"}
_TEXT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class PacedSession:
    """Token-bucket spacing + circuit breaking, keyed on an arbitrary scope.

    Run-scoped and in-process for the same reason `HostPolicy` is: a shard is one
    Lambda invocation, so a run-scoped breaker is the whole blast radius and a
    shared one would cost a write per request. The durable half is
    `job_source_state.next_attempt_at`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_at: Dict[str, float] = {}
        self._failures: Dict[str, int] = defaultdict(int)
        self._inflight: Dict[str, int] = defaultdict(int)
        self._semaphores: Dict[str, threading.Semaphore] = {}
        self.tripped: set = set()
        #: Keys currently serving an anti-bot challenge, with the monotonic time
        #: they may be retried. Vendor-wide, because that is how the wall counts.
        self.cooldowns: Dict[str, float] = {}

    # -- scope helpers -------------------------------------------------------

    @staticmethod
    def host_of(url: str) -> str:
        try:
            return (urlparse(str(url or "")).hostname or "").lower()
        except Exception:
            return ""

    def key_for(self, url: str, vendor: str = "") -> str:
        host = self.host_of(url)
        if vendor:
            try:
                from services.ats_providers import registry as _registry
                return _registry.pacing_key_for(vendor, host)
            except Exception:
                pass
        return host

    def semaphore(self, key: str, limit: int) -> threading.Semaphore:
        with self._lock:
            sem = self._semaphores.get(key)
            if sem is None:
                sem = threading.Semaphore(max(1, int(limit)))
                self._semaphores[key] = sem
            return sem

    # -- gating --------------------------------------------------------------

    def in_cooldown(self, key: str) -> bool:
        with self._lock:
            until = self.cooldowns.get(key)
            if until is None:
                return False
            if time.monotonic() >= until:
                self.cooldowns.pop(key, None)
                return False
            return True

    def start_cooldown(self, key: str, seconds: float) -> None:
        with self._lock:
            self.cooldowns[key] = max(self.cooldowns.get(key, 0.0),
                                      time.monotonic() + max(1.0, float(seconds)))
        logger.warning("ats http: %s challenged, cooling down %.0fs", key, seconds)

    def acquire(self, key: str, *, min_interval: float, jitter: float,
                deadline: float, stop: Optional[Any] = None) -> bool:
        if not key:
            return False
        if self.in_cooldown(key):
            return False
        now = time.monotonic()
        with self._lock:
            if key in self.tripped:
                return False
            earliest = max(now, self._next_at.get(key, 0.0))
            wait_until = earliest + max(0.0, min_interval)
            if jitter:
                wait_until += random.uniform(0.0, jitter)
            self._next_at[key] = wait_until
        delay = earliest - now
        if delay <= 0:
            return True
        if earliest >= deadline:
            return False
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if (stop is not None and getattr(stop, "is_set", bool)()) or \
                    time.monotonic() >= deadline:
                return False
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        return True

    def note_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def note_failure(self, key: str, threshold: int = 4) -> bool:
        with self._lock:
            self._failures[key] += 1
            if self._failures[key] >= threshold and key not in self.tripped:
                self.tripped.add(key)
                return True
        return False


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

Guard = Callable[[str, str], str]   # (vendor, url) -> url, raises to refuse


def _noop_guard(vendor: str, url: str) -> str:
    return url


def make_fetchers(
    session: PacedSession,
    *,
    guard: Guard = _noop_guard,
    deadline: float = float("inf"),
    stop: Optional[Any] = None,
    retries: int = 2,
    retry_base: float = 0.4,
    requests_module: Any = None,
    on_challenge: Optional[Callable[[str, str, Optional[int]], None]] = None,
) -> Tuple[Any, Any, Any]:
    """Build `(get_json, get_text, post_json)` bound to one run.

    A fetcher returns `(status, payload)` rather than raising, so a provider can
    distinguish "gone" from "challenged" without owning retry policy. The one
    exception is a challenge, which raises `ChallengeError` through
    `_maybe_challenge` so it can never be mistaken for an empty board.

    Requests share a COOKIE JAR for the life of these fetchers
    ---------------------------------------------------------
    This used to call the bare `requests` module, so every request was
    cookieless. That is invisible for a board API and fatal for a vendor whose
    protocol is a handshake: Cornerstone publishes a bearer token in its
    anonymous shell page but answers **401** to a search that presents the token
    without the `ASP.NET_SessionId` cookie issued alongside it. The token alone
    is not the credential — the token *plus the session* is. Same class of
    failure as iCIMS's `hashed` token and Taleo's `tz` header: a two-step
    protocol looks like a broken endpoint when only step two is sent.

    The jar is scoped to one `make_fetchers` call — i.e. to one member's crawl
    within one run — so nothing leaks between employers, and it disappears with
    the run. It is thread-LOCAL because a provider may fetch with up to
    `max_concurrency` workers and `requests.Session` is not documented as
    thread-safe; per-thread sessions keep the connection pooling and the jar
    without sharing mutable state across threads.
    """
    import requests as _requests

    if requests_module is not None:
        req = requests_module
    else:
        _local = threading.local()

        class _SessionProxy:
            """`requests`-module-shaped facade over a per-thread Session."""

            @staticmethod
            def _session():
                sess = getattr(_local, "session", None)
                if sess is None:
                    sess = _requests.Session()
                    _local.session = sess
                return sess

            def get(self, *args, **kwargs):
                return self._session().get(*args, **kwargs)

            def post(self, *args, **kwargs):
                return self._session().post(*args, **kwargs)

        req = _SessionProxy()

    def _policy_for(vendor: str) -> CrawlPolicy:
        try:
            from services.ats_providers import registry as _registry
            provider = _registry.get(vendor)
            if provider is not None:
                return provider.policy
        except Exception:
            pass
        return CrawlPolicy()

    def _request(method: str, url: str, *, params=None, json_body=None,
                 form_body=None, timeout: float = 8.0, vendor: str = "",
                 extra_headers: Optional[Dict[str, str]] = None,
                 want_text: bool = False):
        policy = _policy_for(vendor)
        key = session.key_for(url, vendor)

        try:
            url = guard(vendor, url)
        except Exception as exc:
            logger.debug("ats http: refused %s (%s)", url, exc)
            return None, None

        base = dict(_TEXT_HEADERS if want_text else _JSON_HEADERS)
        if method == "POST" and json_body is not None:
            base["Content-Type"] = "application/json"
        headers = {**base, **(extra_headers or {})}

        attempts = max(1, int(retries))
        for attempt in range(attempts):
            if (stop is not None and getattr(stop, "is_set", bool)()) or \
                    time.monotonic() >= deadline:
                return None, None
            if not session.acquire(key, min_interval=policy.min_interval,
                                   jitter=policy.jitter, deadline=deadline,
                                   stop=stop):
                return None, None
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                return None, None
            eff_timeout = max(0.5, min(float(timeout), remaining))

            sem = session.semaphore(key, policy.max_concurrency)
            acquired = sem.acquire(timeout=max(0.1, min(5.0, remaining)))
            if not acquired:
                return None, None
            try:
                if method == "POST":
                    resp = req.post(url, params=params, json=json_body,
                                    data=form_body, headers=headers,
                                    timeout=eff_timeout)
                else:
                    resp = req.get(url, params=params, headers=headers,
                                   timeout=eff_timeout)
            except Exception as exc:
                session.note_failure(key)
                logger.debug("ats http: %s %s network %s", method, key,
                             type(exc).__name__)
                resp = None
            finally:
                sem.release()

            if resp is not None:
                status = resp.status_code
                # Challenge detection reads the BODY as well as the status: iCIMS
                # serves its wall with a 405, and a bare 405 is otherwise a
                # legitimate "wrong method" that must not trigger a cooldown.
                body_head = ""
                if policy.challenge_markers:
                    try:
                        body_head = resp.text[:4000]
                    except Exception:
                        body_head = ""
                if policy.is_challenge(status, body_head):
                    session.start_cooldown(key, policy.cooldown_seconds)
                    if on_challenge is not None:
                        try:
                            on_challenge(vendor, key, status)
                        except Exception:
                            pass
                    raise ChallengeError(vendor or key, status)

                if status == 200:
                    session.note_success(key)
                    if want_text:
                        try:
                            return 200, resp.text
                        except Exception:
                            return 200, None
                    try:
                        return 200, resp.json()
                    except ValueError:
                        return 200, None

                # A non-429 4xx is a permanent answer for THIS url: the board is
                # gone or gated. Do not burn retries or trip the breaker on one
                # dead employer.
                if 400 <= status < 500 and status != 429:
                    return status, None
                session.note_failure(key)

            if attempt + 1 < attempts:
                backoff = retry_base * (2 ** attempt) + random.uniform(0, retry_base)
                if time.monotonic() + backoff >= deadline:
                    return None, None
                time.sleep(backoff)
        return None, None

    def get_json(url: str, *, params=None, timeout: float = 8.0,
                 vendor: str = "", extra_headers=None):
        return _request("GET", url, params=params, timeout=timeout,
                        vendor=vendor, extra_headers=extra_headers)

    def get_text(url: str, *, params=None, timeout: float = 8.0,
                 vendor: str = "", extra_headers=None):
        return _request("GET", url, params=params, timeout=timeout,
                        vendor=vendor, extra_headers=extra_headers,
                        want_text=True)

    def post_json(url: str, *, params=None, json_body=None, form_body=None,
                  timeout: float = 8.0, vendor: str = "", extra_headers=None,
                  want_text: bool = False):
        return _request("POST", url, params=params, json_body=json_body,
                        form_body=form_body, timeout=timeout, vendor=vendor,
                        extra_headers=extra_headers, want_text=want_text)

    return get_json, get_text, post_json


def simple_context(*, budget_seconds: float = 120.0,
                   cutoff_iso: Optional[str] = None,
                   options: Optional[Dict[str, Any]] = None,
                   guard: Guard = _noop_guard,
                   session: Optional[PacedSession] = None,
                   logger_obj: Any = None) -> FetchContext:
    """A `FetchContext` for scripts, tests and one-off validation.

    The crawler builds its own with a fail-closed allowlist guard; this exists so
    `scripts/` and the discovery validator can drive the exact same provider code
    without duplicating it. Same providers, same protocol handling, no drift -
    the two divergent Workday tenant lists in this repo are what drift costs.
    """
    sess = session or PacedSession()
    deadline = time.monotonic() + max(1.0, float(budget_seconds))
    get_json, get_text, post_json = make_fetchers(
        sess, guard=guard, deadline=deadline)
    return FetchContext(get_json=get_json, get_text=get_text,
                        post_json=post_json, deadline=deadline,
                        cutoff_iso=cutoff_iso,
                        logger=logger_obj or logger,
                        options=dict(options or {}))
