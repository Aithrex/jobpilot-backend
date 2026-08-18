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
* **An EMPTY `searchText` returns the tenant's whole board in posted-date
  order.** That is what makes Workday termless under ADR-003 and lets the crawl
  early-stop at the freshness boundary. The old live path crossed every tenant
  with up to six search terms, which is where 6,078 of ~7,400 requests came
  from, and a term query is relevance-ordered, so it cannot early-stop at all.
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

_HOST_SUFFIX = ".myworkdayjobs.com"

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

    # One suffix covers every pod; the CXS API and the public apply pages live
    # on the same host, which is why the crawler's two allowlists agree here and
    # disagree for Greenhouse.
    api_hosts = (_HOST_SUFFIX,)
    portal_hosts = (_HOST_SUFFIX,)

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
        # NOT tunable: CXS ignores a larger `limit`. The crawl budget is
        # therefore spent in pages, and the cutoff normally ends it far sooner.
        page_size=20,
        max_pages=5,
    )

    fingerprints = (
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
        return Endpoint(
            vendor=self.vendor, tenant=tenant, site=site,
            host=f"{tenant}.{wd_host}{_HOST_SUFFIX}",
            # The pod is part of the URL but NOT part of `member_key`: a tenant
            # does not move between pods, and keeping it out means a corrected
            # pod does not orphan the inventory rows already written.
            extra={"wd_host": wd_host})

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

    def _base(self, endpoint: Endpoint) -> str:
        return (f"https://{endpoint.tenant}.{self._wd_host_of(endpoint)}"
                f"{_HOST_SUFFIX}")

    def _jobs_url(self, endpoint: Endpoint) -> str:
        return (f"{self._base(endpoint)}/wday/cxs/{endpoint.tenant}"
                f"/{self._site_of(endpoint)}/jobs")

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

        for page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                return
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
                return

            postings = [p for p in (payload.get("jobPostings") or [])
                        if isinstance(p, dict)]
            if not postings:
                return

            crossed_cutoff = False
            for posting in postings:
                job = self._to_raw(endpoint, base, site, posting)
                if job is None:
                    continue
                if (ctx.cutoff_iso and job.posted_at
                        and job.posted_at < ctx.cutoff_iso):
                    if keyword:
                        # A term query is RELEVANCE-ordered, so an old row says
                        # nothing about the rows after it. Drop this one and
                        # keep reading.
                        continue
                    # Termless queries are newest-first, so everything below
                    # this row is older too: stop the page and the paging.
                    crossed_cutoff = True
                    break
                yield job
            if crossed_cutoff:
                return
            if len(postings) < page_size:
                return

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
            apply_url=f"{base}/{site}/{path}" if path else base,
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

    @staticmethod
    def _total_of(payload: Dict[str, Any]) -> int:
        try:
            return int(payload.get("total") or 0)
        except (TypeError, ValueError):
            return 0


register(WorkdayProvider())
