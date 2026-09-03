"""Workday CXS — the largest single ATS surface in the catalog (1,013 tenants).

The protocol
------------
Every Workday customer exposes their public board through the Candidate
Experience Service, which is a POST API rather than a REST collection:

    POST https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    {"searchText": "", "locations": [], "appliedFacets": {}, "limit": 20, "offset": 0}

Three facts about that request are load-bearing and none of them are guessable:

* **`limit` is capped at 20.** Larger values are not honoured, so a board is
  walked 20 rows at a time and page count — not page size — is the budget.
* **An EMPTY `searchText` returns the tenant's whole board.** That is what
  makes Workday termless under ADR-003. The old live path crossed every tenant
  with up to six search terms, which is where 6,078 of ~7,400 requests came
  from, and a term query is relevance-ordered.
* **That termless board is NOT globally sorted newest-first.** This file used
  to claim it was, and the crawl's early-stop rule was built on the claim.
  Measured 2026-08-27 on `cisco.wd5/Cisco_Careers` (1,227 postings): offset 0
  is "Posted Today", offset 20 opens with "Posted 10 Days Ago" - and offsets
  100 through 459 hold a contiguous block of 336 postings no more than 7 days
  old. Workday orders newest-first WITHIN blocks and does not order the blocks
  against each other, so "the first old row means the rest are older" is false
  and cost this employer ~94% of its fresh inventory. `CrawlPolicy.stale_page_
  tolerance` replaces that rule with a run of consecutive all-stale pages. No
  server-side sort parameter fixes this: `sortBy=POSTING_DATE_DESC`, `sort`,
  `sortBy=postedOn` and a `jobPostingDate` facet were each probed live and all
  four returned byte-identical pages to the unsorted request.
* **The URL has THREE identity components**, not one: tenant, the `wdN` pod the
  customer is hosted on, and the site path. Guessing costs
  `103 host variants x N site names` per company, which is why discovery
  fingerprints the employer's own "View openings" link instead (ADR-004 §2.1).

Dates are RELATIVE, and that is the point
-----------------------------------------
The list payload carries `postedOn: "Posted 2 Days Ago"` — text, not a
timestamp. Under ADR-001 these rows are CONFIDENCE_RELATIVE: derived, never
published. "Posted 30+ Days Ago" parses to exactly 30, which is a LOWER bound on
the age, so such a row is at best 30 days old and is dropped by any 30-day
cutoff — that is the right outcome, but the reason it is right is worth knowing
before someone "fixes" the parse. A posting with no `postedOn` gets
`posted_at=""` and `DATE_UNKNOWN`; it is never promoted to today.

The US facet
------------
`appliedFacets.locationCountry` narrows the board server-side. Two failure modes
were measured across the full 532-tenant run and both are handled below: roughly
half the tenants 400 the facet because their site does not expose it (remembered
per tenant so only the first request pays for the discovery), and some others
silently IGNORE it and return worldwide rows with a 200. Facet acceptance
therefore proves nothing, and the client-side geo filter downstream always runs.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Dict, Iterator, Optional, Set, Tuple

from services.ats_providers.base import (DATE_RELATIVE, DATE_UNKNOWN,
                                         STATUS_BLOCKED, STATUS_DEAD,
                                         STATUS_EMPTY, STATUS_VALIDATED,
                                         AtsProvider, ChallengeError,
                                         CrawlPolicy, Endpoint, FetchContext,
                                         RawJob, ValidationResult, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import from_relative, html_to_text, looks_remote


def _env_int(name: str, default: int) -> int:
    """Crawl-budget knob, readable without a deploy."""
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


#: Workday serves the SAME CXS contract from TWO public domains, in two URL
#: shapes. `myworkdayjobs.com` puts the tenant in the subdomain;
#: `myworkdaysite.com` does not — `https://wd1.myworkdaysite.com/wday/cxs/
#: snapchat/snap/jobs` is a live board and was unreachable while the domain
#: was hardcoded.
#:
#: `workday_cxs_fetcher` learned this (see `WD_DOMAIN_SITE` there) and THIS
#: MODULE DID NOT, so the two Workday paths disagreed: the legacy fetcher
#: could crawl a `myworkdaysite.com` board typed into a catalog, while the
#: provider could not FINGERPRINT one at all. Fingerprinting is how
#: `career_source_ownership` walks from an employer's own careers page to
#: their board, so an employer on this domain could never be owner-verified —
#: not because verification failed, but because the link on their careers page
#: matched no pattern and the endpoint was never seen.
_HOST_SUFFIX = ".myworkdayjobs.com"
_SITE_SUFFIX = ".myworkdaysite.com"

_DEFAULT_WD_HOST = "wd1"
_DEFAULT_SITE = "External"

# Both are path segments in a server-built URL, so they are constrained rather
# than escaped: anything outside this shape is refused, not sanitised.
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,60}$")
_SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,60}$")
_WD_HOST_RE = re.compile(r"^wd\d{1,3}$", re.IGNORECASE)

# Workday's global country reference WID for the United States. Confirmed
# identical across independent tenants (CVS wd1, Adobe wd5, Salesforce wd12) —
# the CXS locationCountry facet uses GLOBAL reference ids, not tenant-local
# ones, which is the only reason a single hard-coded value can work at all.
US_COUNTRY_FACET_ID = "bc33aa3152ec42d4995f4791a106ed09"

# Tenants observed to 400 the locationCountry facet, remembered for the process
# lifetime so only the FIRST request per tenant pays the 400 + retry round trip.
# The live 532-tenant run had 264 such tenants; without this cache each one paid
# the extra request on every page of every crawl.
_FACET_UNSUPPORTED: Set[str] = set()
_FACET_LOCK = threading.Lock()

# Site-path candidates, ordered by empirical hit rate against well-known
# tenants. Used ONLY by `validate` — a crawl always knows its site path (it came
# from the fingerprinted URL or the catalog), and probing during a crawl would
# multiply every tenant's request count by the length of this list.
_COMMON_SITE_FALLBACKS: Tuple[str, ...] = (
    "External", "Careers", "ExternalCareerSite", "External_Career_Site",
    "External_Careers", "ExternalCareers", "1", "2",
    "CareerSite", "Careers_External", "external_experienced",
    "External_Career", "CorporateCareers", "Corporate_Careers",
)


class WorkdayProvider(AtsProvider):
    vendor = "workday"
    label = "Workday"

    termless = True
    has_detail = True
    # See the module docstring: the list payload publishes relative text only.
    date_confidence = DATE_RELATIVE

    # Two suffixes, one per public domain; within each, one suffix covers
    # every pod. The CXS API and the public apply pages live on the same host,
    # which is why the crawler's two allowlists agree here and disagree for
    # Greenhouse.
    api_hosts = (_HOST_SUFFIX, _SITE_SUFFIX)
    portal_hosts = (_HOST_SUFFIX, _SITE_SUFFIX)

    policy = CrawlPolicy(
        # Per-host, i.e. per tenant: every customer is a separate origin on a
        # separate pod, so one slow tenant must not throttle the rest. Unlike
        # iCIMS there is no shared, IP-scoped wall to coordinate around.
        pacing_key=None,
        min_interval=0.15,
        jitter=0.10,
        max_concurrency=8,
        timeout=8.0,
        detail_timeout=6.0,
        challenge_statuses=frozenset({429}),
        # NOT tunable: CXS ignores a larger `limit` (a `limit` of 100 is a
        # hard 400). The crawl budget is therefore spent in pages.
        page_size=20,
        # 5 pages = 100 rows, and the old first-stale-row stop usually ended a
        # tenant at 20. Measured against live boards (see `stale_page_tolerance`
        # on `CrawlPolicy`), a large tenant's fresh postings run to offset ~450
        # and beyond, so 100 rows could not reach most of them no matter what
        # the stop rule said. 30 pages = 600 rows, which covered every fresh row
        # on Cisco (336 fresh, last at offset 459) and NVIDIA (434 fresh in the
        # first 600) in one pass. At ~0.35s/page that is ~10s per tenant; a
        # 48-shard split gives ~21 tenants per shard against a 90s budget with
        # 6 listing workers, and `MEMBER_BUDGET_SECONDS` still caps any single
        # slow tenant.
        max_pages=_env_int("WORKDAY_MAX_LISTING_PAGES", 30),
        # Workday is ordered newest-first only WITHIN blocks, so the crawl has
        # to be able to step OVER a run of old ones. Sized from the measured
        # gaps, not guessed: Cisco's fresh block starts at offset 100 behind
        # four consecutive all-stale pages (offsets 20-80), so a tolerance of 3
        # stops one page short of 318 live postings. Verified end to end
        # against the real boards on 2026-08-27, fresh rows recovered per
        # tenant (7-day cutoff, was ~20 each):
        #
        #     tolerance=3   tolerance=8   independent full-board count
        #   Cisco      18          336          336
        #   Intel     124          124          124
        #   NVIDIA    433          433          434 (in the first 600)
        #   Citi      600          600          600+ (board deeper than 30 pages)
        #
        # Eight pages is 160 consecutive stale rows, which still ends a board
        # that has genuinely run out of fresh postings well inside `max_pages`.
        stale_page_tolerance=_env_int("WORKDAY_STALE_PAGE_TOLERANCE", 8),
    )

    fingerprints = (
        # --- myworkdaysite.com: tenant in the PATH ---------------------------
        # Listed first and at the highest confidence because its URLs are
        # strictly more specific: `wd1.myworkdaysite.com/wday/cxs/snap/...`
        # would otherwise be a near-miss for nothing at all.
        fp(r"https?://(?P<wd_host>wd\d+)\.myworkdaysite\.com/wday/cxs/"
           r"(?P<tenant>[A-Za-z0-9\-]+)/(?P<site>[A-Za-z0-9_\-]+)/jobs",
           confidence=99),
        fp(r"https?://(?P<wd_host>wd\d+)\.myworkdaysite\.com/"
           r"(?:[a-z]{2}-[A-Za-z]{2}/)?recruiting/(?P<tenant>[A-Za-z0-9\-]+)/"
           r"(?P<site>[A-Za-z0-9_\-]+)", confidence=96),
        # --- myworkdayjobs.com: tenant in the SUBDOMAIN ----------------------
        # The CXS endpoint itself — unambiguous, and it names all three
        # identity components.
        fp(r"https?://(?P<tenant>[A-Za-z0-9\-]+)\.(?P<wd_host>wd\d+)"
           r"\.myworkdayjobs\.com/wday/cxs/[A-Za-z0-9\-]+/"
           r"(?P<site>[A-Za-z0-9_\-]+)/jobs", confidence=98),
        # The candidate-facing board, with or without a locale segment
        # (`/en-US/External`). Captures the site path, so it outranks the
        # host-only pattern below.
        fp(r"https?://(?P<tenant>[A-Za-z0-9\-]+)\.(?P<wd_host>wd\d+)"
           r"\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?"
           r"(?P<site>[A-Za-z0-9_\-]+)", confidence=95),
        # Host only: proves Workday and yields the pod, but the site path has to
        # be resolved by validation before the endpoint can be crawled.
        fp(r"https?://(?P<tenant>[A-Za-z0-9\-]+)\.(?P<wd_host>wd\d+)"
           r"\.myworkdayjobs\.com", confidence=70),
    )

    # -- identity ------------------------------------------------------------

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        """Workday needs the pod as well as the tenant, so the default
        implementation (tenant/site/host) is not enough."""
        tenant = (fields.get("tenant") or "").strip().lower()
        wd_host = (fields.get("wd_host") or "").strip().lower()
        if not _TENANT_RE.match(tenant) or not _WD_HOST_RE.match(wd_host):
            return None
        site = (fields.get("site") or "").strip() or _DEFAULT_SITE
        if not _SITE_RE.match(site):
            # A path segment we cannot use as a site path (locale codes,
            # `wday`, encoded junk). Fall back to the commonest site name and
            # let validation confirm or discard it.
            site = _DEFAULT_SITE
        if site.lower() == "wday":
            site = _DEFAULT_SITE
        # Which public domain the URL came from. Stored in `extra` beside the
        # pod — NOT in `member_key` — for the same reason the pod is not:
        # `member_key` is the prefix of every `source_job_id` this board
        # writes, so a board that turns out to be reachable on the other
        # domain must not orphan its own inventory.
        on_site_domain = _SITE_SUFFIX[1:] in (url or "").lower()
        host = (f"{wd_host}{_SITE_SUFFIX}" if on_site_domain
                else f"{tenant}.{wd_host}{_HOST_SUFFIX}")
        return Endpoint(
            vendor=self.vendor, tenant=tenant, site=site, host=host,
            # The pod is part of the URL but NOT part of `member_key`: a tenant
            # does not move between pods, and keeping it out means a corrected
            # pod does not orphan the inventory rows already written.
            extra={"wd_host": wd_host,
                   "wd_domain": (_SITE_SUFFIX[1:] if on_site_domain
                                 else _HOST_SUFFIX[1:])})

    @classmethod
    def _wd_host_of(cls, endpoint: Endpoint) -> str:
        raw = str((endpoint.extra or {}).get("wd_host") or "").strip().lower()
        if not raw:
            # Legacy catalog rows carry it under `wd_host`; discovered ones may
            # only have the full hostname.
            host = (endpoint.host or "").strip().lower()
            parts = host.split(".")
            raw = parts[1] if len(parts) > 2 else ""
        return raw if _WD_HOST_RE.match(raw) else _DEFAULT_WD_HOST

    @classmethod
    def _site_of(cls, endpoint: Endpoint) -> str:
        site = (endpoint.site or "").strip() or _DEFAULT_SITE
        return site if _SITE_RE.match(site) else _DEFAULT_SITE

    @classmethod
    def _domain_of(cls, endpoint: Endpoint) -> str:
        """Which public Workday domain this board lives on.

        Read from `extra.wd_domain` (set by the fingerprint or a catalog row)
        and, failing that, sniffed off `host` — a legacy row that carries only
        the hostname still resolves correctly rather than silently defaulting
        to the wrong domain and 404ing forever.
        """
        raw = str((endpoint.extra or {}).get("wd_domain") or "").strip().lower()
        if raw in (_HOST_SUFFIX[1:], _SITE_SUFFIX[1:]):
            return raw
        if _SITE_SUFFIX[1:] in str(endpoint.host or "").lower():
            return _SITE_SUFFIX[1:]
        return _HOST_SUFFIX[1:]

    def _base(self, endpoint: Endpoint) -> str:
        """The CXS origin. The two domains put the tenant in DIFFERENT places.

        `myworkdayjobs.com` -> `https://{tenant}.{pod}.myworkdayjobs.com`
        `myworkdaysite.com` -> `https://{pod}.myworkdaysite.com`

        Getting this wrong does not error; it 404s, and a 404 on a board we
        never reached reads as "this employer has no board".
        """
        pod = self._wd_host_of(endpoint)
        if self._domain_of(endpoint) == _SITE_SUFFIX[1:]:
            return f"https://{pod}{_SITE_SUFFIX}"
        return f"https://{endpoint.tenant}.{pod}{_HOST_SUFFIX}"

    def board_url(self, endpoint: Endpoint) -> str:
        """The candidate-facing board. Needs the POD, which a bare tenant does
        not carry — so an endpoint with neither `host` nor `extra.wd_host`
        gets "" rather than a URL built on the default pod, which would be a
        confident wrong answer about somebody else's board."""
        pod = str((endpoint.extra or {}).get("wd_host") or "").strip().lower()
        if not pod and not str(endpoint.host or "").strip():
            return ""
        return f"{self._apply_base(endpoint)}/{self._site_of(endpoint)}"

    def _jobs_url(self, endpoint: Endpoint) -> str:
        return (f"{self._base(endpoint)}/wday/cxs/{endpoint.tenant}"
                f"/{self._site_of(endpoint)}/jobs")

    def _apply_base(self, endpoint: Endpoint) -> str:
        """The HUMAN-facing prefix an `externalPath` hangs off.

        NOT the same shape as the CXS origin. `myworkdaysite.com` puts the
        tenant in the PATH (`/recruiting/{tenant}/{site}`) where
        `myworkdayjobs.com` puts it in the subdomain and needs only `/{site}`.
        Mirrors `workday_cxs_fetcher.apply_base`, which learned this first:
        getting it wrong yields an apply link that 404s AND a detail URL the
        enricher cannot parse back, so the posting arrives with no description.
        """
        base = self._base(endpoint)
        if self._domain_of(endpoint) == _SITE_SUFFIX[1:]:
            return f"{base}/recruiting/{endpoint.tenant}"
        return base

    # -- listing -------------------------------------------------------------

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        if ctx.post_json is None:
            ctx.log("warning", "workday: context has no post_json; %s skipped",
                    endpoint.tenant)
            return
        if not _TENANT_RE.match(endpoint.tenant or ""):
            ctx.log("warning", "workday: refusing unusable tenant %r",
                    endpoint.tenant)
            return

        base = self._base(endpoint)
        site = self._site_of(endpoint)
        url = self._jobs_url(endpoint)
        page_size = int(self.policy.page_size)

        # Empty on the crawl path, which is what makes the feed newest-first.
        # The interactive path may pass a term; see the early-stop guard below.
        keyword = str((ctx.options or {}).get("keyword") or "").strip()
        us_only = bool((ctx.options or {}).get("us_only", True))
        facets = self._initial_facets(endpoint.tenant, us_only)

        max_pages = max(1, int(self.policy.max_pages))
        tolerance = max(0, int(getattr(self.policy, "stale_page_tolerance", 0)))
        stale_streak = 0
        pages_read = 0
        for page in range(max_pages):
            if ctx.should_stop():
                # Out of budget with the board not exhausted. Say so: the
                # crawler must not read a cut-short listing as "these are all
                # the fresh openings this employer has" and close the rest.
                ctx.listing_truncated = True
                return
            pages_read += 1
            body: Dict[str, Any] = {
                "searchText": keyword,
                "locations": [],
                "appliedFacets": facets,
                "limit": page_size,
                "offset": page * page_size,
            }
            status, payload = ctx.post_json(url, json_body=body,
                                            timeout=self.policy.timeout,
                                            vendor=self.vendor)
            if status == 400 and facets:
                # This tenant does not expose the locationCountry facet. Drop
                # it, remember that for the rest of the process, and retry
                # unfaceted rather than losing the whole board.
                facets = {}
                self._remember_facet_unsupported(endpoint.tenant)
                body["appliedFacets"] = {}
                status, payload = ctx.post_json(url, json_body=body,
                                                timeout=self.policy.timeout,
                                                vendor=self.vendor)
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                # An error PART WAY through a board leaves the rest unread.
                # Only a clean first-page failure is "this member has nothing".
                if pages_read > 1:
                    ctx.listing_truncated = True
                return

            postings = [p for p in (payload.get("jobPostings") or [])
                        if isinstance(p, dict)]
            if not postings:
                return

            # A page is "stale" only when it produced NO row inside the
            # cutoff. Unlike the old rule, a single old row no longer ends the
            # crawl: Workday interleaves blocks that are each newest-first but
            # are not ordered against one another, so the row after an old one
            # is routinely fresh. See `CrawlPolicy.stale_page_tolerance`.
            page_fresh = 0
            for posting in postings:
                job = self._to_raw(endpoint, base, site, posting)
                if job is None:
                    continue
                if (ctx.cutoff_iso and job.posted_at
                        and job.posted_at < ctx.cutoff_iso):
                    # Old row: skipped, whether the query carried a term or
                    # not. A term query is relevance-ordered and a termless one
                    # is only block-ordered, so in neither case does this row
                    # say anything about the rows after it.
                    continue
                page_fresh += 1
                yield job

            if page_fresh:
                stale_streak = 0
            else:
                stale_streak += 1
                if stale_streak > tolerance:
                    # `tolerance` consecutive pages with nothing fresh. With
                    # tolerance=0 this is exactly the old "stop at the first
                    # stale page" rule, which is what a globally-ordered vendor
                    # still wants.
                    return

            if len(postings) < page_size:
                # A short page is the end of the board, not a stale run.
                return

        # Fell out of the loop: the page cap stopped us, not the board.
        ctx.listing_truncated = True

    @staticmethod
    def _initial_facets(tenant: str, us_only: bool) -> Dict[str, Any]:
        if not us_only:
            return {}
        with _FACET_LOCK:
            known_bad = tenant in _FACET_UNSUPPORTED
        return {} if known_bad else {"locationCountry": [US_COUNTRY_FACET_ID]}

    @staticmethod
    def _remember_facet_unsupported(tenant: str) -> None:
        with _FACET_LOCK:
            _FACET_UNSUPPORTED.add(tenant)

    def _to_raw(self, endpoint: Endpoint, base: str, site: str,
                posting: Dict[str, Any]) -> Optional[RawJob]:
        title = str(posting.get("title") or "").strip()
        if not title:
            return None
        path = str(posting.get("externalPath") or "").lstrip("/")
        # `bulletFields[0]` is the customer's requisition number, which is
        # stable across title edits; the path is the fallback identity.
        req_id = str((posting.get("bulletFields") or [None])[0] or path or "")
        if not req_id:
            return None

        posted_text = str(posting.get("postedOn") or "").strip()
        posted = from_relative(posted_text)
        location = str(posting.get("locationsText") or "").strip()

        return RawJob(
            source_job_id=req_id,
            title=title,
            # The canonical form Workday itself emits in the detail payload's
            # `externalUrl` — no locale segment. Minting a different shape here
            # would make the same posting look like two different jobs to the
            # apply flow.
            apply_url=(f"{self._apply_base(endpoint)}/{site}/{path}" if path
                       else self._apply_base(endpoint)),
            location=location,
            posted_at=posted,
            date_confidence=DATE_RELATIVE if posted else DATE_UNKNOWN,
            raw_posted_text=posted_text,
            employment_type=str(posting.get("timeType") or "").strip(),
            remote=looks_remote(location),
            detail_url=(f"{base}/wday/cxs/{endpoint.tenant}/{site}/{path}"
                        if path else ""),
            # The path is what `fetch_detail` needs and it is not derivable from
            # the requisition id, so it is carried rather than re-parsed out of
            # the apply URL.
            extra={"external_path": path} if path else {},
        )

    # -- detail --------------------------------------------------------------

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        """The JD body. The list endpoint returns NO description at all, so
        without this every Workday row is scored on its title alone."""
        url = job.detail_url
        if not url:
            path = str((job.extra or {}).get("external_path") or "")
            if not path:
                return None
            url = (f"{self._base(endpoint)}/wday/cxs/{endpoint.tenant}"
                   f"/{self._site_of(endpoint)}/{path}")
        status, payload = ctx.get_json(url,
                                       timeout=self.policy.detail_timeout,
                                       vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not isinstance(payload, dict):
            return None
        info = payload.get("jobPostingInfo")
        if not isinstance(info, dict):
            return None
        body = html_to_text(info.get("jobDescription") or "")
        if not body:
            return None
        return RawJob(
            source_job_id=job.source_job_id,
            title=job.title,
            apply_url=job.apply_url,
            description=body[:40000],
            location=str(info.get("location") or "").strip(),
            employment_type=str(info.get("timeType") or "").strip(),
            # Deliberately no date: the detail payload repeats the same relative
            # `postedOn` text, so re-reading it here would add a request without
            # improving ADR-001 confidence.
        )

    # -- validation ----------------------------------------------------------

    def validate(self, endpoint: Endpoint,
                 ctx: FetchContext) -> ValidationResult:
        """Confirm the board exists, resolving the site path if it has to.

        The site path is the one identity component a company's careers link may
        omit, and a wrong path answers 404 — indistinguishable from a dead
        tenant unless the alternatives are tried. So on 404 this walks a short
        ordered candidate list and, on success, WRITES the working path back
        onto the endpoint so the caller can persist it and never probe again.

        Probing lives here rather than in `list_jobs` on purpose: validation
        runs once per endpoint, a crawl runs every tick, and multiplying every
        tenant's request count by fourteen candidates to re-derive a value we
        already know would be the single most expensive line in this package.
        """
        if ctx.post_json is None:
            return ValidationResult(STATUS_DEAD, detail="no post_json in context")
        if not _TENANT_RE.match(endpoint.tenant or ""):
            return ValidationResult(STATUS_DEAD, detail="unusable tenant")

        declared = self._site_of(endpoint)
        candidates = [declared] + [s for s in _COMMON_SITE_FALLBACKS
                                   if s != declared]
        base = self._base(endpoint)
        last_status: Optional[int] = None

        for index, site in enumerate(candidates):
            if ctx.should_stop():
                return ValidationResult(STATUS_BLOCKED, detail="deadline reached")
            status, payload = ctx.post_json(
                f"{base}/wday/cxs/{endpoint.tenant}/{site}/jobs",
                json_body={"searchText": "", "locations": [],
                           "appliedFacets": {}, "limit": 1, "offset": 0},
                timeout=self.policy.timeout,
                vendor=self.vendor)
            last_status = status
            if self.policy.is_challenge(status):
                return ValidationResult(STATUS_BLOCKED, detail=f"status={status}")
            if status is not None and 500 <= int(status) < 600:
                # A pod that is briefly down must never be grounds to delete a
                # tenant; retry later instead.
                return ValidationResult(STATUS_BLOCKED,
                                        detail=f"pod unavailable ({status})")
            if status != 200 or not isinstance(payload, dict):
                # 404 = wrong path, 422 = path exists but CXS is gated on it.
                # Either way, keep trying candidates.
                continue

            if index:
                endpoint.site = site
            total = self._total_of(payload)
            postings = [p for p in (payload.get("jobPostings") or [])
                        if isinstance(p, dict)]
            detail = f"site={site}" if index else ""
            if not postings and total <= 0:
                # Reachable with nothing open is a NORMAL, recurring state
                # (hiring freezes). Retiring it would only mean rediscovering
                # the same board later.
                return ValidationResult(STATUS_EMPTY, 0,
                                        detail=detail or "zero open reqs")
            return ValidationResult(
                STATUS_VALIDATED, total or len(postings),
                str(postings[0].get("title") or "") if postings else "",
                detail=detail)

        return ValidationResult(STATUS_DEAD,
                                detail=f"no working site path (last={last_status})")

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        """Workday rarely publishes structured owner metadata; the public
        board HTML often does (`Careers at F5`, og:title). Returning that
        string lets ownership corroborate without trusting the tenant slug.
        """
        url = self.board_url(endpoint)
        if not url:
            return None
        try:
            status, text = ctx.get_text(url, timeout=self.policy.timeout,
                                        vendor=self.vendor)
        except Exception:
            return None
        if status != 200 or not text:
            return None
        import re
        m = re.search(
            r"""(?:og:title|og:site_name)[^>]+content\s*=\s*["']([^"']{1,120})["']""",
            text, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            title = re.sub(r"^(careers?\s+(at|@)\s+)", "", title, flags=re.I).strip()
            title = re.sub(r"\s+careers?\s*$", "", title, flags=re.I).strip()
            if title:
                return title[:200]
        m = re.search(r"Careers?\s+(?:at|@)\s+([A-Za-z0-9][A-Za-z0-9 .,&'\-]{1,80})",
                      text, re.IGNORECASE)
        if m:
            return m.group(1).strip()[:200]
        return None

    @staticmethod
    def _total_of(payload: Dict[str, Any]) -> int:
        try:
            return int(payload.get("total") or 0)
        except (TypeError, ValueError):
            return 0


register(WorkdayProvider())
