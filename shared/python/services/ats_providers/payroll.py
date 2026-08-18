"""Payroll-attached ATS vendors — UKG, Paylocity, Rippling.

Why these three, and why they matter more than their brand recognition suggests
-----------------------------------------------------------------------------
Every vendor JobPilot crawled before this file serves either large enterprises
(Workday, Oracle ORC, iCIMS, Phenom, Eightfold) or venture-backed technology
companies (Greenhouse, Lever, Ashby). Both populations are heavily aggregated
already: a Greenhouse posting at a funded startup is on a dozen job boards
within hours, and hundreds of applicants see it.

The employers that are genuinely under-served are small and mid-sized firms —
the clinic, the regional contractor, the 40-person manufacturer. They do not buy
a standalone ATS. They get recruiting bundled with the payroll system they
already pay for, which in the US is overwhelmingly UKG, Paylocity, Paycom,
Paychex, ADP or (newly) Rippling. Those boards are poorly aggregated precisely
because each one is small and none of them is a name a job-board scraper thinks
to target.

So this module is aimed squarely at the "few applicants, low visibility"
problem: it is where the jobs are that nobody else is showing.

Protocols, all verified live on 2026-08-18
------------------------------------------
**UKG / UltiPro** — `POST /{code}/JobBoard/{guid}/JobBoardView/LoadSearchResults`
with a JSON search envelope. Returns `{opportunities, totalCount, locations}`
with a real ISO `PostedDate`, a `BriefDescription`, and structured
`Locations[].Address`. Newest-first ordering is requested explicitly, so the
ADR-003 early-stop rule applies. Identity is TWO opaque ids (an alphanumeric
customer code and a board GUID); neither is derivable from a company name, so
this vendor is fingerprint-only, never slug-probed.

**Paylocity** — no JSON API. `GET /recruiting/jobs/All/{guid}` returns the board
as HTML with the complete job array embedded as `"Jobs": [...]`, including
`PublishedDate` (ISO with offset), a full `Description`, and a structured
`JobLocation` with city/state/zip. That is more per-row data than several of the
JSON vendors give, which is worth stating plainly: a vendor is not lower quality
because its transport is HTML.

**Rippling** — `GET /api/v1/board/{slug}/jobs` returns the whole board as a
clean JSON array, but with NO date and NO description; both live on
`/api/v1/board/{slug}/jobs/{uuid}`. Hence `date_confidence = DATE_UNKNOWN` on
the listing and a detail fetch that upgrades it.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List, Optional

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import (html_to_text, looks_minted_now,
                                         looks_remote, to_iso)

_POLICY = CrawlPolicy(
    min_interval=0.4,
    jitter=0.2,
    max_concurrency=4,
    timeout=20.0,
    detail_timeout=15.0,
    # These are single-tenant-per-customer SaaS hosts behind a shared front
    # door; 5xx under load is transient, not proof an employer is gone.
    challenge_statuses=frozenset({429, 503}),
    page_size=50,
    max_pages=20,
)


def _text(value: Any) -> str:
    """A displayable string for a field that may be a nested code/name object.

    UKG publishes `Address.State` as `{"Code": "SC", "Name": "South Carolina"}`
    and `Address.Country` likewise. `str()` of that yields a Python repr, and an
    indexed location reading `Myrtle Beach, {'Code': 'SC', ...}` matches nothing
    a job seeker will ever type. `Code` is preferred over `Name` because "SC" is
    what postings and resumes both use.
    """
    if isinstance(value, dict):
        for key in ("Code", "code", "Name", "name", "label", "Label"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return ""
    return str(value or "").strip()


def _join(*parts: Any) -> str:
    seen: List[str] = []
    for part in parts:
        text = _text(part)
        if text and text not in seen:
            seen.append(text)
    return ", ".join(seen)


def _job_posting_ld(body: str) -> Optional[Dict[str, Any]]:
    """The `JobPosting` schema.org block on a posting page, if any."""
    for match in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            body, re.IGNORECASE | re.DOTALL):
        try:
            data = json.loads(match.group(1).strip())
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and \
                    str(item.get("@type") or "").lower() == "jobposting":
                return item
    return None


# ---------------------------------------------------------------------------
# UKG / UltiPro
# ---------------------------------------------------------------------------

class UkgProvider(AtsProvider):
    """UKG Ready / UltiPro Recruiting public job boards."""

    vendor = "ukg"
    label = "UKG"

    termless = True
    has_detail = False          # the listing already carries a description
    date_confidence = DATE_EXACT
    policy = _POLICY

    api_hosts = ("recruiting.ultipro.com", "recruiting2.ultipro.com")
    portal_hosts = ("recruiting.ultipro.com", "recruiting2.ultipro.com")

    #: A UKG board is `{customer code}/JobBoard/{board guid}`. BOTH are opaque
    #: and unguessable, so there is no slug to probe — the pair is only ever
    #: learned by seeing a real link on an employer's careers page.
    slug_probe = False

    fingerprints = (
        fp(r"https?://recruiting2?\.ultipro\.com/(?P<tenant>[A-Za-z0-9_\-]+)"
           r"/JobBoard/(?P<site>[0-9a-fA-F\-]{36})", confidence=95),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        site = (fields.get("site") or "").strip()
        if not tenant or not site:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant, site=site)

    @staticmethod
    def _body(skip: int, top: int) -> Dict[str, Any]:
        return {
            "opportunitySearch": {
                "Top": int(top), "Skip": int(skip), "QueryString": "",
                # Newest-first, so paging can stop at the freshness boundary
                # instead of reading a whole board to find nothing new.
                "OrderBy": [{"Value": "postedDateDesc",
                             "PropertyName": "PostedDate",
                             "Ascending": False}],
                "Filters": [],
            },
            "matchCriteria": {
                "PreferredJobs": [], "Educations": [],
                "LicenseAndCertifications": [], "Skills": [],
                "hasNoLicenses": False, "SkippedSkills": [],
            },
        }

    def _board_url(self, endpoint: Endpoint) -> str:
        host = (endpoint.host or "recruiting.ultipro.com").strip().lower()
        return f"https://{host}/{endpoint.tenant}/JobBoard/{endpoint.site}"

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        if not endpoint.tenant or not endpoint.site:
            ctx.log("info", "ukg: %s needs both a customer code and a board id",
                    endpoint.member_key)
            return
        if ctx.post_json is None:
            return
        board = self._board_url(endpoint)
        url = f"{board}/JobBoardView/LoadSearchResults"
        page_size = int(self.policy.page_size)
        total: Optional[int] = None
        seen: set = set()

        for page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                return
            status, payload = ctx.post_json(
                url, params={}, json_body=self._body(page * page_size,
                                                     page_size),
                timeout=self.policy.timeout, vendor=self.vendor,
                extra_headers={"Accept": "application/json"})
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                return
            rows = [r for r in (payload.get("opportunities") or [])
                    if isinstance(r, dict)]
            if not rows:
                return
            if total is None:
                try:
                    total = int(payload.get("totalCount") or 0)
                except (TypeError, ValueError):
                    total = 0

            stop_early = False
            for raw in rows:
                job = self._to_raw(board, raw)
                if job is None or job.source_job_id in seen:
                    continue
                seen.add(job.source_job_id)
                # Newest-first: once we cross the boundary every later row is
                # older still, so the remaining pages are not worth fetching.
                if ctx.cutoff_iso and job.posted_at and \
                        job.posted_at < ctx.cutoff_iso:
                    stop_early = True
                    continue
                yield job
            if stop_early or len(rows) < page_size:
                return
            if total and (page + 1) * page_size >= total:
                return

    def _to_raw(self, board: str, raw: Dict[str, Any]) -> Optional[RawJob]:
        rid = str(raw.get("Id") or "").strip()
        title = str(raw.get("Title") or "").strip()
        if not rid or not title:
            return None
        posted_raw = raw.get("PostedDate")
        posted = "" if looks_minted_now(posted_raw) else to_iso(posted_raw)
        location = self._location_of(raw)
        full_time = raw.get("FullTime")
        return RawJob(
            source_job_id=rid,
            title=title[:200],
            apply_url=f"{board}/OpportunityDetail?opportunityId={rid}",
            location=location,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            description=html_to_text(raw.get("BriefDescription") or "")[:20000],
            department=str(raw.get("JobCategoryName") or ""),
            employment_type=("Full-Time" if full_time is True else
                             "Part-Time" if full_time is False else ""),
            remote=looks_remote(title, location,
                                raw.get("JobLocationType")),
            extra={"requisition_number": str(raw.get("RequisitionNumber")
                                             or "")},
        )

    @staticmethod
    def _location_of(raw: Dict[str, Any]) -> str:
        """City/state from the first structured location.

        `LocalizedDescription` is the employer's own label for the site ("Myrtle
        Beach Office") and is NOT a place a seeker searches for, so the postal
        address wins and the label is only a fallback.
        """
        places: List[str] = []
        for loc in (raw.get("Locations") or [])[:3]:
            if not isinstance(loc, dict):
                continue
            addr = loc.get("Address")
            if isinstance(addr, dict):
                joined = _join(addr.get("City"), addr.get("State"),
                               addr.get("Country"))
                if joined:
                    places.append(joined)
                    continue
            label = str(loc.get("LocalizedDescription") or "").strip()
            if label:
                places.append(label)
        return "; ".join(places)[:160]


# ---------------------------------------------------------------------------
# Paylocity
# ---------------------------------------------------------------------------

class PaylocityProvider(AtsProvider):
    """Paylocity Recruiting boards — HTML page, embedded job array."""

    vendor = "paylocity"
    label = "Paylocity"

    termless = True
    #: The embedded listing `Description` is a fixed 110-character TEASER, not
    #: the job description — measured across every row of a live board. Indexing
    #: it as the JD would leave each posting with a truncated first sentence and
    #: nothing to match a resume against, so the real text (and the salary band)
    #: is fetched per posting.
    has_detail = True
    date_confidence = DATE_EXACT
    policy = _POLICY

    api_hosts = ("recruiting.paylocity.com",)
    portal_hosts = ("recruiting.paylocity.com",)
    #: The tenant is a GUID. Nothing to guess.
    slug_probe = False

    fingerprints = (
        fp(r"https?://recruiting\.paylocity\.com/[Rr]ecruiting/[Jj]obs/All/"
           r"(?P<tenant>[0-9a-fA-F\-]{36})", confidence=95),
    )

    #: The board page is a server-rendered app shell whose state object holds
    #: the whole list. Anchored on `"Jobs"` with a non-greedy array so a later
    #: `"Jobs"` key elsewhere in the payload cannot swallow the document.
    _JOBS_RE = re.compile(r'"Jobs"\s*:\s*(\[.*?\])\s*[,}]', re.DOTALL)

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        tenant = (endpoint.tenant or "").strip()
        if not tenant:
            return
        url = f"https://recruiting.paylocity.com/recruiting/jobs/All/{tenant}"
        status, body = ctx.get_text(url, params={},
                                    timeout=self.policy.timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return
        match = self._JOBS_RE.search(body)
        if not match:
            ctx.log("info", "paylocity: %s served no embedded job array",
                    tenant)
            return
        try:
            rows = json.loads(match.group(1))
        except ValueError:
            ctx.log("info", "paylocity: %s job array did not parse", tenant)
            return

        for raw in rows if isinstance(rows, list) else []:
            if ctx.should_stop():
                return
            if not isinstance(raw, dict):
                continue
            # Internal-only postings cannot be applied to from outside, so
            # showing them wastes a seeker's time (same rule as iCIMS).
            if raw.get("IsInternal") is True:
                continue
            rid = str(raw.get("JobId") or "").strip()
            title = str(raw.get("JobTitle") or "").strip()
            if not rid or not title:
                continue
            posted_raw = raw.get("PublishedDate")
            posted = "" if looks_minted_now(posted_raw) else to_iso(posted_raw)
            if ctx.cutoff_iso and posted and posted < ctx.cutoff_iso:
                continue
            location = self._location_of(raw)
            remote = raw.get("IsRemote")
            yield RawJob(
                source_job_id=rid,
                title=title[:200],
                apply_url=("https://recruiting.paylocity.com/recruiting/jobs"
                           f"/Details/{rid}"),
                location=location,
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                # Teaser only; `fetch_detail` replaces it with the real JD.
                description=html_to_text(raw.get("Description") or "")[:400],
                department=str(raw.get("HiringDepartment") or ""),
                remote=(bool(remote) if isinstance(remote, bool)
                        else looks_remote(title, location)),
                detail_url=("https://recruiting.paylocity.com/recruiting/jobs"
                            f"/Details/{rid}"),
            )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        """Full JD and pay band from the posting page's schema.org block."""
        url = job.detail_url
        if not url:
            return None
        status, body = ctx.get_text(url, params={},
                                    timeout=self.policy.detail_timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return None
        ld = _job_posting_ld(body)
        if not ld:
            return None
        posted_raw = ld.get("datePosted")
        posted = "" if looks_minted_now(posted_raw) else to_iso(posted_raw)
        description = html_to_text(ld.get("description") or "")
        if not description and not posted:
            return None
        return RawJob(
            source_job_id=job.source_job_id,
            title=job.title,
            apply_url=job.apply_url,
            description=description[:40000],
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            compensation=ld.get("baseSalary") or None,
        )

    @staticmethod
    def _location_of(raw: Dict[str, Any]) -> str:
        """City/state/zip when published, else the employer's site label.

        `LocationName` alone is often an internal code ("BIS"), which is not a
        place; the structured `JobLocation` carries the real one.
        """
        loc = raw.get("JobLocation")
        if isinstance(loc, dict):
            joined = _join(loc.get("City"), loc.get("State"),
                           loc.get("Country"))
            if joined:
                return joined[:160]
        if raw.get("ShouldDisplayLocation") is False:
            return ""
        return str(raw.get("LocationName") or "").strip()[:160]


# ---------------------------------------------------------------------------
# Rippling
# ---------------------------------------------------------------------------

class RipplingProvider(AtsProvider):
    """Rippling ATS public boards."""

    vendor = "rippling"
    label = "Rippling"

    termless = True
    #: The listing has neither a date nor a description; both are detail-only.
    has_detail = True
    date_confidence = DATE_UNKNOWN
    policy = _POLICY

    api_hosts = ("ats.rippling.com",)
    portal_hosts = ("ats.rippling.com",)
    #: One shared host and a readable slug, so a guess is one definitive call.
    slug_probe = True

    fingerprints = (
        fp(r"https?://ats\.rippling\.com/(?:api/v\d+/board/)?"
           r"(?P<tenant>[A-Za-z0-9_\-]+)/jobs", confidence=92),
        fp(r"https?://ats\.rippling\.com/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=70),
    )
    _RESERVED = frozenset({"api", "jobs", "board", "www"})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not tenant or tenant.lower() in self._RESERVED:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant)

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        tenant = (endpoint.tenant or "").strip()
        if not tenant or tenant.lower() in self._RESERVED:
            return
        status, payload = ctx.get_json(
            f"https://ats.rippling.com/api/v1/board/{tenant}/jobs",
            params={}, timeout=self.policy.timeout, vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not isinstance(payload, list):
            return
        for raw in payload:
            if ctx.should_stop():
                return
            if not isinstance(raw, dict):
                continue
            rid = str(raw.get("uuid") or "").strip()
            title = str(raw.get("name") or "").strip()
            if not rid or not title:
                continue
            location = self._label_of(raw.get("workLocation"))
            yield RawJob(
                source_job_id=rid,
                title=title[:200],
                apply_url=str(raw.get("url")
                              or f"https://ats.rippling.com/{tenant}/jobs/{rid}"),
                location=location,
                posted_at="",
                date_confidence=DATE_UNKNOWN,
                department=self._label_of(raw.get("department")),
                remote=looks_remote(title, location),
                detail_url=(f"https://ats.rippling.com/api/v1/board/{tenant}"
                            f"/jobs/{rid}"),
            )

    @staticmethod
    def _label_of(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("label") or value.get("id") or "").strip()[:160]
        return str(value or "").strip()[:160]

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        url = job.detail_url
        if not url:
            return None
        status, payload = ctx.get_json(url, params={},
                                       timeout=self.policy.detail_timeout,
                                       vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not isinstance(payload, dict):
            return None
        created = payload.get("createdOn")
        posted = "" if looks_minted_now(created) else to_iso(created)
        description = html_to_text(payload.get("description") or "")
        if not description and not posted:
            return None
        return RawJob(
            source_job_id=job.source_job_id,
            title=job.title,
            apply_url=job.apply_url,
            description=description[:40000],
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            employment_type=self._label_of(payload.get("employmentType")),
            compensation=payload.get("payRangeDetails") or None,
        )


register(UkgProvider())
register(PaylocityProvider())
register(RipplingProvider())


# ---------------------------------------------------------------------------
# Cornerstone OnDemand
# ---------------------------------------------------------------------------

class CornerstoneProvider(AtsProvider):
    """Cornerstone OnDemand career sites.

    The board is a JS shell whose data API answers **401** to an anonymous
    request, which reads like "authentication required" and is where an earlier
    look at this vendor stopped. It is not: the shell page itself publishes the
    credential. `csod.context` is inlined into the anonymous HTML and carries a
    2 KB JWT, which the page's own bundle sends as `Authorization: Bearer`.

    That makes Cornerstone the third vendor here with the same shape — iCIMS
    hands out a `hashed` token, Taleo prints its `portal` id, Cornerstone
    inlines a JWT. **The page gives you the credential; the job is to read it,
    not to guess around it.**

    Verified live 2026-08-18:

        GET  /ux/ats/careersite/{site}/home        -> csod.context.token
        POST /services/x/career-site/v1/search     -> {data:{totalCount,
                                                             requisitions[]}}
             {"careerSiteId": N, "cultureId": 1, "CultureName": "en-US",
              "pageNumber": 1, "pageSize": 50}

    `CultureName` is required and its absence is reported as a 400 naming the
    field — a well-behaved API, once it is asked properly.

    **Site ids are not derivable and are not always 1.** University of Arizona
    serves 324 requisitions on site 4 and zero on site 1; Alamo Colleges serves
    212 on site 1. An endpoint without a known site is therefore probed across a
    small range rather than assumed, and the discovered id is cached back onto
    the endpoint.

    **No job description is available.** Every documented detail route 404s and
    the posting page carries no JSON-LD, so rows are title/location/date only.
    That is a real quality limit for resume matching and is recorded in
    `docs/coverage/ats-vendor-coverage.md` rather than papered over.
    """

    vendor = "cornerstone"
    label = "Cornerstone"

    termless = True
    has_detail = False
    date_confidence = DATE_EXACT
    policy = _POLICY

    api_hosts = (".csod.com",)
    portal_hosts = (".csod.com",)
    #: Per-tenant subdomain plus an opaque site id: nothing useful to guess.
    slug_probe = False

    fingerprints = (
        fp(r"https?://(?P<tenant>[A-Za-z0-9][A-Za-z0-9\-]*)\.csod\.com"
           r"/ux/ats/careersite/(?P<site>\d+)", confidence=95),
        fp(r"https?://(?P<tenant>[A-Za-z0-9][A-Za-z0-9\-]*)\.csod\.com",
           confidence=65),
    )
    _RESERVED = frozenset({"www", "api", "help", "clientweb", "content"})

    #: Sites to try when none is known. Deliberately short: this costs one
    #: request each and the overwhelming majority of tenants use a low id.
    _SITE_PROBES = (1, 2, 3, 4, 5)

    _CONTEXT_RE = re.compile(r"csod\.context\s*=\s*(\{)", re.IGNORECASE)

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip().lower()
        if not tenant or tenant in self._RESERVED:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant,
                        site=(fields.get("site") or "").strip(),
                        host=f"{tenant}.csod.com")

    @staticmethod
    def _host_of(endpoint: Endpoint) -> str:
        host = (endpoint.host or "").strip().lower()
        if host.endswith(".csod.com"):
            return host
        return f"{(endpoint.tenant or '').strip().lower()}.csod.com"

    def _token(self, host: str, site: int, ctx: FetchContext) -> str:
        """The bearer token the anonymous shell page publishes."""
        status, body = ctx.get_text(
            f"https://{host}/ux/ats/careersite/{site}/home", params={},
            timeout=self.policy.timeout, vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return ""
        match = self._CONTEXT_RE.search(body)
        if not match:
            return ""
        # Brace-match rather than regex the object: the context contains nested
        # objects, and a non-greedy `\{.*?\}` truncates it before `token`.
        start = match.start(1)
        depth = 0
        for index in range(start, min(len(body), start + 200_000)):
            char = body[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return str(json.loads(body[start:index + 1])
                                   .get("token") or "")
                    except ValueError:
                        return ""
        return ""

    def _search(self, host: str, site: int, token: str, page: int,
                ctx: FetchContext) -> Optional[Dict[str, Any]]:
        if ctx.post_json is None:
            return None
        status, payload = ctx.post_json(
            f"https://{host}/services/x/career-site/v1/search", params={},
            json_body={"careerSiteId": int(site), "cultureId": 1,
                       # Required; omitting it is a 400 that names the field.
                       "CultureName": "en-US",
                       "pageNumber": int(page),
                       "pageSize": int(self.policy.page_size)},
            timeout=self.policy.timeout, vendor=self.vendor,
            extra_headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/json"})
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not isinstance(payload, dict):
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        host = self._host_of(endpoint)
        tenant = (endpoint.tenant or "").strip().lower()
        if not tenant or tenant in self._RESERVED:
            return

        sites: List[int] = []
        if str(endpoint.site or "").strip().isdigit():
            sites = [int(endpoint.site)]
        else:
            sites = list(self._SITE_PROBES)

        for site in sites:
            if ctx.should_stop():
                return
            token = self._token(host, site, ctx)
            if not token:
                continue
            first = self._search(host, site, token, 1, ctx)
            if first is None:
                continue
            total = int(first.get("totalCount") or 0)
            if not total:
                # A real site with genuinely nothing open, or the wrong site
                # id. Both look identical here, so try the next candidate.
                continue

            # Remember which site answered, so the stored endpoint (and the
            # next run) skips the probing entirely.
            endpoint.site = str(site)
            yield from self._rows(host, tenant, site, first, ctx)

            page = 2
            emitted = len(first.get("requisitions") or [])
            while emitted < total and page <= int(self.policy.max_pages):
                if ctx.should_stop():
                    return
                data = self._search(host, site, token, page, ctx)
                rows = (data or {}).get("requisitions") or []
                if not rows:
                    return
                yield from self._rows(host, tenant, site, data, ctx)
                emitted += len(rows)
                page += 1
            return

    def _rows(self, host: str, tenant: str, site: int, data: Dict[str, Any],
              ctx: FetchContext) -> Iterator[RawJob]:
        for raw in (data.get("requisitions") or []):
            if not isinstance(raw, dict):
                continue
            rid = str(raw.get("requisitionId") or "").strip()
            title = str(raw.get("displayJobTitle") or "").strip()
            if not rid or not title:
                continue
            posted_raw = raw.get("postingEffectiveDate")
            posted = "" if looks_minted_now(posted_raw) else to_iso(posted_raw)
            if ctx.cutoff_iso and posted and posted < ctx.cutoff_iso:
                continue
            location = self._location_of(raw)
            yield RawJob(
                source_job_id=rid,
                title=title[:200],
                apply_url=(f"https://{host}/ux/ats/careersite/{site}/home"
                           f"/requisition/{rid}?c={tenant}"),
                location=location,
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                # Cornerstone publishes no description on any public route.
                description="",
                remote=looks_remote(title, location),
            )

    @staticmethod
    def _location_of(raw: Dict[str, Any]) -> str:
        places: List[str] = []
        for loc in (raw.get("locations") or [])[:3]:
            if not isinstance(loc, dict):
                continue
            joined = _join(loc.get("city"), loc.get("state"),
                           loc.get("country"))
            if joined:
                places.append(joined)
        return "; ".join(places)[:160]


register(CornerstoneProvider())
