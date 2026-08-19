"""ATS provider contract — what every vendor plugin must declare and implement.

Why a class instead of another entry in `career_pages_service.FETCHERS`
-----------------------------------------------------------------------
A fetch function answers exactly one question: "give me this board's jobs". The
crawler needs four more, and today it answers them from four SEPARATE tables that
a new vendor has to be added to by hand:

    FETCHERS                      -> how do I fetch it
    _HOST_ALLOW / _PROBE_HOST_ALLOW -> may I fetch it            (job_ingest_worker)
    _TERMLESS_ATS                 -> may the CRAWLER fetch it    (job_ingest_worker)
    _ATS_WITH_DETAIL              -> is a second request needed  (job_ingest_worker)

`job_ingest_worker` already documents the trap in a comment on `_TERMLESS_ATS`:
"Adding a fetcher without adding it here ships a source that returns nothing" -
the rows exist, the fetcher exists, the tests pass, and the crawler never visits.
That is a registration problem, not a fetching problem, so the fix is to make
registration atomic: a provider declares all of it in one place (`registry.py`
derives the tables), and a vendor cannot be half-added.

Providers never import the crawler
----------------------------------
Everything a provider needs to touch the network arrives in a `FetchContext`
supplied by the caller. `job_ingest_worker` injects its guarded, paced,
deadline-clamped fetchers; a CLI script can inject plain `requests`. So the same
provider code serves the background crawl, the live interactive fan-out and the
discovery validator without a circular import between this package and the
worker, and without a provider being able to bypass the SSRF guard by calling
`requests` itself.

ADR-001 note
------------
`date_confidence` is declared per provider, not guessed per row. A provider that
cannot produce a real posting date says so (`DATE_UNKNOWN`) and the inventory
records the row as such. Substituting `now()` for a missing date is forbidden -
it re-dates stale reqs as fresh, which is the single most damaging thing this
pipeline can do to a job seeker.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import (Any, Callable, Dict, Iterable, Iterator, List, Optional,
                    Sequence, Tuple)

# --- ADR-001 posting-date confidence ---------------------------------------
DATE_EXACT = "exact"        # the vendor published a real date
DATE_RELATIVE = "relative"  # "Posted 3 Days Ago" -> derived, approximate
DATE_UNKNOWN = "unknown"    # no date available; never substitute now()

# --- endpoint lifecycle -----------------------------------------------------
STATUS_CANDIDATE = "candidate"   # fingerprinted, not yet proven to serve jobs
STATUS_VALIDATED = "validated"   # returned >=1 posting at least once
STATUS_EMPTY = "empty"           # reachable, genuinely zero open roles
STATUS_DEAD = "dead"             # gone; repeated permanent failures
STATUS_BLOCKED = "blocked"       # anti-bot wall; retry later, do NOT delete


# ---------------------------------------------------------------------------
# Crawl policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CrawlPolicy:
    """How this vendor may be crawled.

    `pacing_key` is the important field and exists because of a measured
    failure (ADR-004 §3.2). `HostPolicy` spaces requests per HOSTNAME, which is
    correct for Greenhouse (one shared API host) and WRONG for iCIMS, where every
    tenant is its own hostname (`careers-<slug>.icims.com`) but the anti-bot wall
    is enforced per source IP across all of them. Six concurrent requests to six
    DIFFERENT iCIMS hosts tripped the wall on all sixty tenants probed.

    A provider that sets `pacing_key="icims.com"` makes every tenant share one
    token bucket, which is the only scope that matches how the vendor actually
    counts.
    """

    # Throttle scope. None => key on the request hostname (today's behaviour).
    pacing_key: Optional[str] = None
    min_interval: float = 0.15
    jitter: float = 0.10
    # Upper bound on simultaneous in-flight requests for this vendor within one
    # shard. 1 == strictly serial.
    max_concurrency: int = 8
    timeout: float = 8.0
    detail_timeout: float = 6.0

    # --- anti-bot / soft-block detection ---
    # Statuses that mean "you are being challenged", NOT "this resource is gone".
    # `http_get_json` treats any non-429 4xx as permanent, so without this an
    # iCIMS 405 challenge would retire a live employer as dead.
    challenge_statuses: frozenset = frozenset({429})
    # Case-insensitive substrings that identify a challenge/interstitial body.
    challenge_markers: Tuple[str, ...] = ()
    # When True, a challenge STATUS alone is not enough - the body must also
    # match `challenge_markers`.
    #
    # This exists because the opposite default cost a whole discovery run. A
    # vendor-wide cooldown is a big hammer: it stops every tenant of the vendor
    # for `cooldown_seconds`. Tripping it on a bare status means one dead
    # subdomain can silence thousands of live employers - and dead subdomains
    # are exactly what tenant discovery feeds in, because Certificate
    # Transparency lists a vendor's own infrastructure alongside its customers.
    #
    # Measured 2026-08-19: an iCIMS discovery pass over 90 candidates tripped
    # the wall on the FIRST request and reported all 90 as "blocked", while
    # `staff-emory` and `careers-fremontbank` were serving jobs normally the
    # whole time. Non-employer hosts answer 404/401/403 with ordinary error
    # pages; the REAL wall is a 2,115-byte "Human Verification" body, which is
    # what `challenge_markers` matches. Requiring the marker keeps the
    # protection and removes the false positive.
    challenge_requires_marker: bool = False
    # How long the whole vendor sits out after a challenge (durable, written to
    # job_source_state.next_attempt_at).
    cooldown_seconds: float = 900.0

    # --- paging ---
    page_size: int = 50
    max_pages: int = 20

    def is_challenge(self, status: Optional[int], body: str = "") -> bool:
        """True when a response is a soft block rather than a real answer."""
        marker_hit = False
        if body and self.challenge_markers:
            low = body[:4000].lower()
            marker_hit = any(m in low for m in self.challenge_markers)
        if marker_hit:
            return True
        if self.challenge_requires_marker:
            # The status is not sufficient on its own for this vendor.
            return False
        return status is not None and int(status) in self.challenge_statuses


# ---------------------------------------------------------------------------
# Fingerprints — how discovery recognises this vendor on a company's own site
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fingerprint:
    """A regex that recognises a vendor from a URL and captures its identity.

    Discovery fetches a company's site plus its careers page and matches every
    href/script-src against these. The employer's own "View openings" link names
    their ATS and tenant, which is why this beats guessing slugs: it is two
    requests per company for ALL vendors at once, and it survives the very common
    case of tenant != company name (Bread Financial's Workday tenant is
    `alliancedata`).

    Named capture groups become endpoint fields, so a pattern must name what it
    captures: (?P<tenant>...), (?P<site>...), (?P<wd_host>...).
    """

    pattern: re.Pattern
    # Higher wins when several patterns match the same URL. Use a higher score
    # for patterns that capture MORE identity (tenant+site beats tenant alone).
    confidence: int = 50
    # Set when a match proves the vendor but yields no usable tenant (e.g. a
    # tracking pixel). Useful as a signal, not as an endpoint.
    identity_only: bool = False

    def match(self, url: str) -> Optional[Dict[str, str]]:
        m = self.pattern.search(url or "")
        if not m:
            return None
        out = {k: v for k, v in (m.groupdict() or {}).items() if v}
        return out


def fp(pattern: str, confidence: int = 50, *, identity_only: bool = False,
       flags: int = re.IGNORECASE) -> Fingerprint:
    """Terser Fingerprint constructor for provider modules."""
    return Fingerprint(re.compile(pattern, flags), confidence,
                       identity_only=identity_only)


# ---------------------------------------------------------------------------
# Endpoint — one crawlable board belonging to one employer
# ---------------------------------------------------------------------------

@dataclass
class Endpoint:
    """A single crawlable board.

    This is the runtime twin of a `company_ats_endpoints` document and of a
    legacy catalog row, so the crawler can treat hand-written catalog entries and
    auto-discovered endpoints identically.
    """

    vendor: str
    tenant: str
    display_name: str = ""
    site: str = ""                 # Workday site / ORC siteNumber / Taleo portal
    host: str = ""                 # explicit host when not derivable from tenant
    industry: str = "other"
    company_id: str = ""
    status: str = STATUS_CANDIDATE
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def member_key(self) -> str:
        """Stable, colon-free crawl identity.

        ADR-003 makes this the PREFIX of every `source_job_id` this endpoint
        writes, which is how a shard decides which inventory rows it owns. It
        must therefore never contain ':' and never change for a given board.
        """
        base = f"{self.vendor}-{self.tenant}"
        if self.site:
            base = f"{base}-{self.site}"
        return _slugify(base)

    def to_doc(self) -> Dict[str, Any]:
        return {
            "vendor": self.vendor, "tenant": self.tenant, "site": self.site,
            "host": self.host, "display_name": self.display_name,
            "industry": self.industry, "company_id": self.company_id,
            "status": self.status, "extra": dict(self.extra or {}),
            "member_key": self.member_key,
        }

    @classmethod
    def from_doc(cls, doc: Dict[str, Any]) -> "Endpoint":
        return cls(
            vendor=str(doc.get("vendor") or ""),
            tenant=str(doc.get("tenant") or ""),
            display_name=str(doc.get("display_name") or ""),
            site=str(doc.get("site") or ""),
            host=str(doc.get("host") or ""),
            industry=str(doc.get("industry") or "other"),
            company_id=str(doc.get("company_id") or ""),
            status=str(doc.get("status") or STATUS_CANDIDATE),
            extra=dict(doc.get("extra") or {}),
        )


def _slugify(value: Any, limit: int = 140) -> str:
    raw = str(value or "").strip().lower()
    out = [ch if (ch.isalnum() or ch in "-_") else "-" for ch in raw]
    return "-".join(p for p in "".join(out).split("-") if p)[:limit]


# ---------------------------------------------------------------------------
# RawJob — the provider's output, before inventory normalization
# ---------------------------------------------------------------------------

@dataclass
class RawJob:
    """One posting as the vendor described it.

    Deliberately NOT the `job_postings` schema: normalization, scoring, salary
    parsing and content hashing stay in `job_inventory_service.normalize_posting`
    so all sources agree. A provider's job is to map vendor fields onto these and
    to be honest about `date_confidence`.
    """

    source_job_id: str
    title: str
    apply_url: str
    location: str = ""
    posted_at: str = ""                 # ISO-8601, or "" when unknown
    date_confidence: str = DATE_UNKNOWN
    raw_posted_text: str = ""
    description: str = ""
    country: str = ""
    employment_type: str = ""
    compensation: Any = None
    department: str = ""
    remote: Optional[bool] = None
    detail_url: str = ""                # set when a second fetch can enrich
    extra: Dict[str, Any] = field(default_factory=dict)

    def merged_with_detail(self, other: "RawJob") -> "RawJob":
        """Fold a detail fetch into the listing row, preferring richer values."""
        return replace(
            self,
            description=other.description or self.description,
            location=other.location or self.location,
            posted_at=other.posted_at or self.posted_at,
            date_confidence=(other.date_confidence
                             if other.posted_at else self.date_confidence),
            employment_type=other.employment_type or self.employment_type,
            compensation=(other.compensation
                          if other.compensation is not None else self.compensation),
            department=other.department or self.department,
            country=other.country or self.country,
            extra={**(self.extra or {}), **(other.extra or {})},
        )


@dataclass
class ValidationResult:
    """Outcome of asking "is this endpoint real and does it serve jobs?"."""

    status: str                      # one of STATUS_*
    job_count: int = 0
    sample_title: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_VALIDATED


# ---------------------------------------------------------------------------
# FetchContext — the only way a provider reaches the network
# ---------------------------------------------------------------------------

# A fetcher returns (status_code, body_text_or_None). Returning the status lets a
# provider distinguish "gone" from "challenged" without owning retry policy.
TextFetcher = Callable[..., Tuple[Optional[int], Optional[str]]]
JsonFetcher = Callable[..., Tuple[Optional[int], Optional[Any]]]


@dataclass
class FetchContext:
    """Injected capability bundle.

    Providers must not import `requests` directly. The crawler supplies guarded
    implementations (host allowlist + SSRF guard + pacing + deadline clamping);
    scripts may supply simpler ones. Either way the provider code is identical,
    which is what keeps the background crawl and the live path from drifting -
    the two Workday tenant lists in this repo are what drift looks like.
    """

    get_json: JsonFetcher
    get_text: TextFetcher
    post_json: Optional[JsonFetcher] = None
    # Monotonic deadline; providers should stop paging as it approaches.
    deadline: float = float("inf")
    # Cooperative cancellation (threading.Event-like: has .is_set()).
    stop: Any = None
    # Only postings at least this fresh are wanted; providers paging
    # newest-first should stop once they cross it. None == no boundary.
    cutoff_iso: Optional[str] = None
    logger: Any = None
    # Free-form knobs (search terms for term-driven vendors, etc.)
    options: Dict[str, Any] = field(default_factory=dict)

    def should_stop(self) -> bool:
        import time as _time
        if self.stop is not None:
            try:
                if self.stop.is_set():
                    return True
            except Exception:
                pass
        return _time.monotonic() >= self.deadline

    def log(self, level: str, msg: str, *args: Any) -> None:
        if self.logger is None:
            return
        try:
            getattr(self.logger, level, self.logger.info)(msg, *args)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# The provider interface
# ---------------------------------------------------------------------------

class AtsProvider:
    """Base class for every ATS vendor plugin.

    Subclasses set the class attributes and implement `list_jobs`. Everything
    else has a working default.
    """

    # --- identity ---
    vendor: str = ""                 # stable key, e.g. "oracle_orc"
    label: str = ""                  # UI badge, e.g. "Oracle Recruiting"

    # --- capability declarations (registry derives the crawler tables) ---
    #: Can the whole board be paged newest-first with NO search term? Only
    #: termless providers may join the background crawl, because ADR-003's
    #: early-stop rule needs newest-first ordering.
    termless: bool = True
    #: Does a second per-posting request add anything?
    has_detail: bool = False
    #: ADR-001 confidence of `posted_at` produced by `list_jobs`.
    date_confidence: str = DATE_UNKNOWN
    #: Hosts the crawler may call for LIST/DETAIL data. "." prefix == suffix match.
    api_hosts: Tuple[str, ...] = ()
    #: Hosts the liveness probe may call (public apply pages; often different).
    portal_hosts: Tuple[str, ...] = ()
    #: Patterns that recognise this vendor on a company's own website.
    fingerprints: Tuple[Fingerprint, ...] = ()
    #: Crawl budget / politeness / anti-bot posture.
    policy: CrawlPolicy = CrawlPolicy()
    #: False parks a provider without deleting it (kept out of crawl + discovery).
    enabled: bool = True

    #: May discovery FALL BACK to guessing a tenant slug for this vendor?
    #:
    #: Only true for vendors exposing one shared API host where a slug guess is
    #: a single definitive request (`boards-api.greenhouse.io/v1/boards/<slug>`
    #: either returns a board or 404s). It is deliberately FALSE for:
    #:   * Workday  - tenants shard across wd1..wd103, so one guess costs ~103
    #:                requests and mostly proves nothing;
    #:   * iCIMS    - `*.icims.com` is wildcard DNS, so a wrong guess returns a
    #:                perfectly valid page, and guessing burns the shared
    #:                anti-bot budget for every other tenant;
    #:   * Oracle   - pod hostnames (`eeho.fa.us2...`) are not derivable from a
    #:                company name at all.
    #: Those three are reachable ONLY by fingerprinting, which is the whole
    #: reason fingerprinting is the primary strategy rather than this.
    slug_probe: bool = False

    def slug_candidates(self, name: str, domain: str = "") -> Sequence[str]:
        """Tenant slugs worth trying for this vendor. Override for vendor
        conventions (SmartRecruiters, for instance, uses PascalCase ids)."""
        from services.ats_providers.util import slug_variants
        return slug_variants(name, domain)

    # -- required ------------------------------------------------------------
    def list_jobs(self, endpoint: Endpoint, ctx: FetchContext) -> Iterator[RawJob]:
        raise NotImplementedError

    # -- optional ------------------------------------------------------------
    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        return None

    def validate(self, endpoint: Endpoint, ctx: FetchContext) -> ValidationResult:
        """Default: a board is real if listing yields anything.

        Note the deliberate distinction between EMPTY and DEAD. A reachable board
        with zero open roles is a normal, recurring state (seasonal hiring
        freezes); retiring it would mean rediscovering it later. Only repeated
        permanent failures make an endpoint DEAD, and a challenge makes it
        BLOCKED, never DEAD.
        """
        try:
            first: Optional[RawJob] = None
            count = 0
            for job in self.list_jobs(endpoint, ctx):
                count += 1
                if first is None:
                    first = job
                if count >= 5:
                    break
            if count:
                return ValidationResult(STATUS_VALIDATED, count,
                                        (first.title if first else ""))
            return ValidationResult(STATUS_EMPTY, 0, detail="no postings returned")
        except ChallengeError as exc:
            return ValidationResult(STATUS_BLOCKED, 0, detail=str(exc))
        except Exception as exc:  # never let one vendor break a discovery run
            return ValidationResult(STATUS_DEAD, 0,
                                    detail=f"{type(exc).__name__}: {exc}")

    def canonical_url(self, endpoint: Endpoint, job: RawJob) -> str:
        return job.apply_url

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        """Build an Endpoint from a fingerprint match. Override when a vendor
        needs derivation beyond the captured groups (see Workday's wd_host)."""
        tenant = (fields.get("tenant") or "").strip()
        if not tenant:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant,
                        site=(fields.get("site") or "").strip(),
                        host=(fields.get("host") or "").strip(),
                        extra={k: v for k, v in fields.items()
                               if k not in ("tenant", "site", "host")})

    # -- helpers available to subclasses -------------------------------------
    def _paced_pages(self, total: Optional[int], page_size: int) -> Iterable[int]:
        """Offsets to request, respecting the provider's max_pages cap."""
        cap = max(1, int(self.policy.max_pages))
        if not total or total <= 0:
            return range(0, cap * page_size, page_size)
        pages = min(cap, (int(total) + page_size - 1) // page_size)
        return range(0, pages * page_size, page_size)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AtsProvider {self.vendor}>"


class ChallengeError(RuntimeError):
    """Raised when a vendor served an anti-bot interstitial.

    Distinct from a transport error on purpose: the caller must record a
    vendor-wide durable cooldown rather than marking one employer dead.
    """

    def __init__(self, vendor: str, status: Optional[int] = None,
                 detail: str = ""):
        self.vendor = vendor
        self.status = status
        super().__init__(f"{vendor} challenge"
                         + (f" (status={status})" if status else "")
                         + (f": {detail}" if detail else ""))


class PortalMovedError(RuntimeError):
    """Raised when a tenant's board has migrated off this vendor entirely.

    Distinct from `ChallengeError` (retry later) and from an ordinary empty
    board (keep, it will have openings again). A migrated portal will NEVER
    serve jobs again, but the employer has not stopped hiring — their postings
    moved to another vendor. So the destination travels with the error, and
    discovery re-fingerprints it instead of the employer silently vanishing
    from the index.
    """

    def __init__(self, vendor: str, destination: str = ""):
        self.vendor = vendor
        self.destination = destination
        super().__init__(f"{vendor} portal moved"
                         + (f" to {destination}" if destination else ""))
