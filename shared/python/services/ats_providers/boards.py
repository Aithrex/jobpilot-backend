"""Board ATSes — the seven vendors whose whole open-roles list arrives in one
or two requests.

Why they share a module
-----------------------
Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, NeoGov,
Jobvite and BambooHR are the same shape: a public JSON endpoint keyed by a
tenant slug that returns the employer's ENTIRE board, unpaged (SmartRecruiters
excepted), with no search term. That shape is what makes them termless under ADR-003, and it is why the
crawler spends one request per company on them rather than tenants x terms.
They differ only in field names, so one module with seven small classes reads
better than seven near-identical files.

This is a PORT, not a rewrite
-----------------------------
Every endpoint, parameter and date field below is copied from code already
verified against the live services (`career_pages_service._fetch_*`, and the
crawler's `job_ingest_worker._list_career`). The comments that record *why* a
field was chosen are copied with them, because each one is a bug someone
already paid for:

* Greenhouse's `first_published` (never `updated_at`) — §Greenhouse below.
* NeoGov's POST + XHR header, and its title living in `Classification` rather
  than in the field literally named `JobTitle`.
* SmartRecruiters' `country=us`, without which a 5,800-posting multinational
  buries its US slice past our page budget.
* Lever's four-field body assembly, without which ~60% of its postings store
  no description at all.

Where `career_pages_service` (the live interactive fan-out) and
`job_ingest_worker` (the background crawl) disagree about a field, the comment
says so and says which one this module follows. Those disagreements are the
drift ADR-004 exists to end, and they should be resolved deliberately by
whoever retires the old paths, not silently here.

Dates
-----
All seven publish a real timestamp, so their rows are ADR-001 CONFIDENCE_EXACT.
A posting whose date field is missing or unparseable yields `posted_at=""` and
`DATE_UNKNOWN`; it is never back-filled with now().
"""
from __future__ import annotations

import re
from dataclasses import replace as _replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import (from_epoch, html_to_text, looks_remote,
                                         to_iso)

# Board APIs are fast, cheap and untroubled by our traffic; these are the
# crawler's current numbers (`job_ingest_worker.HOST_MIN_INTERVAL` / `_JITTER`)
# so switching a source onto this framework does not change its politeness.
# `pacing_key` stays None throughout: Greenhouse, Lever, Ashby, SmartRecruiters,
# Workable and NeoGov each serve every tenant from ONE hostname, so hostname
# keying already gives all their tenants a single shared bucket. Recruitee is
# the exception (`{tenant}.recruitee.com`) and per-host keying is correct there
# too — each tenant is a separate origin, and unlike iCIMS there is no
# IP-scoped wall to share a budget with.
_BOARD_POLICY = CrawlPolicy(
    pacing_key=None,
    min_interval=0.15,
    jitter=0.10,
    max_concurrency=8,
    timeout=8.0,
    detail_timeout=6.0,
    page_size=100,
    max_pages=1,          # whole board in one call; overridden where it pages
)

# Pay fields, by the names the boards actually use. Deliberately a TOP-LEVEL
# lookup on a known list rather than handing the whole job dict to the parser
# and letting it hunt: that parser accepts generic range keys (`min`/`max`,
# `from`/`to`, `start`/`end`), and a job payload has unrelated fields by those
# names - `start` is a start date, not a floor - so a blind search would invent
# salaries. Absent here simply means the description fallback decides.
_PAY_KEYS: Tuple[str, ...] = (
    "pay_input_ranges", "salaryRange", "salary_range", "compensation",
    "compensationTierSummary", "salary", "payRange", "pay")


def _pay_of(job: Dict[str, Any]) -> Any:
    """The vendor's raw pay object, unformatted. `salary_normalizer` reads
    SHAPES rather than one vendor's field names, so passing the object through
    untouched is what lets a board rename its pay field without breaking us."""
    if not isinstance(job, dict):
        return None
    for key in _PAY_KEYS:
        value = job.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _join(*parts: Any, sep: str = ", ") -> str:
    return sep.join(str(p).strip() for p in parts if p and str(p).strip())


class _BoardProvider(AtsProvider):
    """Shared plumbing for the seven board vendors.

    Only network access and cutoff handling live here. Every vendor still
    declares its own hosts, fingerprints and field mapping, because those are
    exactly the things that must not be guessed by a shared default.
    """

    termless = True
    date_confidence = DATE_EXACT
    policy = _BOARD_POLICY

    # Every vendor here answers on ONE shared API host, so a slug guess costs a
    # single definitive request (the board exists or the API 404s). That is what
    # makes these safe as discovery's fallback when a careers page turns out to
    # be a JS shell with no ATS link in its HTML - and it is exactly what is NOT
    # true of Workday (wd1..wd103), iCIMS (wildcard DNS) or Oracle (opaque pod
    # hostnames), which is why those three opt out.
    slug_probe = True

    # -- network -------------------------------------------------------------

    def _get_json(self, ctx: FetchContext, url: str,
                  params: Optional[Dict[str, Any]] = None,
                  *, timeout: Optional[float] = None) -> Optional[Any]:
        """One guarded GET. None means "no usable answer for this URL".

        A challenge is raised rather than returned so a soft block is recorded
        as a retryable vendor-wide condition instead of retiring the employer.
        """
        status, payload = ctx.get_json(
            url, params=params or {},
            timeout=timeout if timeout is not None else self.policy.timeout,
            vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200:
            return None
        return payload

    # -- cutoff --------------------------------------------------------------

    def _too_old(self, ctx: FetchContext, posted_at: str) -> bool:
        """True when a row predates the caller's freshness boundary.

        These boards return the whole list in one unpaged call and promise no
        ordering, so the cutoff is a ROW filter here, not a paging stop (which
        is what it is for Workday and ORC). A row with no date is never
        dropped by the cutoff: we cannot tell where it sits, and guessing is
        the ADR-001 failure this pipeline exists to avoid.
        """
        return bool(ctx.cutoff_iso and posted_at and posted_at < ctx.cutoff_iso)

    # -- rows ----------------------------------------------------------------

    #: Strip the boilerplate an ATS appends to the employer's name in a page
    #: title: "Ramp Jobs", "Match Group Careers", "Acme - Job Board".
    _TITLE_TRIM_RE = re.compile(
        r"\s*[|\-\u2013\u2014:]?\s*"
        r"(jobs?|careers?|job board|open (?:roles|positions)|openings|"
        r"we(?:'|\u2019)?re hiring|hiring|current openings)\s*$",
        re.IGNORECASE)

    def _owner_from_portal_title(self, url: str, ctx: FetchContext) -> Optional[str]:
        """The employer name out of a board portal's `<title>`.

        Lever and Ashby publish no owner field on their JSON APIs, which is
        exactly why they were the two vendors where a guessed slug could
        become `validated` on string similarity alone. Both render the
        employer's own name into the portal title - `jobs.lever.co/matchgroup`
        is "Match Group", `jobs.ashbyhq.com/ramp` is "Ramp Jobs" - so one
        cheap GET turns a guess into a checkable claim.

        Returns None rather than guessing when the title is missing, empty, an
        error page, or so generic that it names no employer.
        """
        try:
            status, body = ctx.get_text(url, timeout=self.policy.timeout,
                                        vendor=self.vendor)
        except Exception:
            return None
        if status != 200 or not body:
            return None
        m = re.search(r"<title[^>]*>(.{0,200}?)</title>", body,
                      re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip()
        prev = None
        while prev != name:
            prev = name
            name = self._TITLE_TRIM_RE.sub("", name).strip(" -|:\u2013\u2014")
        low = name.lower()
        if (len(name) < 2 or len(name) > 120
                or "not found" in low or "404" in low or "error" in low
                or low in {"jobs", "careers", "job board", "lever", "ashby"}):
            return None
        return name

    @staticmethod
    def _rows(payload: Any, key: str) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        return [r for r in (payload.get(key) or []) if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

class GreenhouseProvider(_BoardProvider):
    """Greenhouse job boards.

    The largest single vendor in the existing catalog (686 of 1,309 companies),
    which is why its date handling below is load-bearing rather than a detail.
    """

    vendor = "greenhouse"
    label = "Greenhouse"

    has_detail = True

    # The LIST api and the PUBLIC board are different hosts, and the crawler
    # keeps their allowlists separate for that reason (`_HOST_ALLOW` vs
    # `_PROBE_HOST_ALLOW`). `job-boards.greenhouse.io` is the newer public host;
    # both still serve live employers.
    api_hosts = ("boards-api.greenhouse.io", "job-boards.eu.greenhouse.io")
    portal_hosts = ("boards.greenhouse.io", "job-boards.greenhouse.io",
                    "job-boards.eu.greenhouse.io", "boards.eu.greenhouse.io")

    #: Greenhouse runs a SEPARATE EU region, and a board lives in exactly one
    #: of them. `endpoint.site` carries the region ("" = US, "eu" = EU),
    #: exactly as Lever does, so `greenhouse-cognite-eu` is a different member
    #: from a (non-existent) US `greenhouse-cognite`.
    #:
    #: What the EU region actually exposes, observed 2026-10-21 on Cognite
    #: (the board its official careers page links): `boards-api.eu.greenhouse.io`
    #: does NOT resolve (NXDOMAIN), and `api.eu.greenhouse.io` is the
    #: recruiter app (302 to /users/sign_in) - there is no public EU JSON board
    #: API to call. The public board `job-boards.eu.greenhouse.io/<token>` is a
    #: server-rendered Remix page whose `window.__remixContext` carries the
    #: listing as `jobPosts` {count, page, total, total_pages, data[]} and pages
    #: with `?page=N` (50 per page); a job page carries `jobPost` with the full
    #: `content`. That is what an EU board is read from.
    #:
    #: The US API also answered for `cognite` that day (51 jobs whose
    #: absolute_url is on the EU host). That is an observation about one board,
    #: not a contract, and the region rule here is "never query the US region
    #: for an EU board": an EU endpoint never touches `boards-api.greenhouse.io`.
    REGION_BOARD_HOSTS = {"": "job-boards.greenhouse.io",
                          "eu": "job-boards.eu.greenhouse.io"}
    #: Page cap for the EU board walk. 50 rows a page, so 40 pages is 2,000
    #: postings - far beyond any EU board we read - and hitting it is reported
    #: as truncation, never as a complete census.
    EU_MAX_PAGES = 40

    fingerprints = (
        # EU region FIRST and at higher confidence, so `boards.eu.greenhouse.io`
        # is never read as a US board. (The US patterns cannot match an EU URL
        # anyway - `boards.eu.` is not `boards.greenhouse.` - but ordering makes
        # the intent explicit.)
        fp(r"https?://(?:job-)?boards\.(?P<site>eu)\.greenhouse\.io/embed/job_board"
           r"[^\"'\s]*[?&]for=(?P<tenant>[A-Za-z0-9_\-]+)", confidence=93),
        fp(r"https?://(?:job-)?boards\.(?P<site>eu)\.greenhouse\.io/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=86),
        # The API URL itself, occasionally linked by careers pages that render
        # their board client-side.
        fp(r"https?://boards-api\.greenhouse\.io/v\d+/boards/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=95),
        # The older public API host. JS shells (DigitalOcean and peers) XHR
        # `api.greenhouse.io/v1/boards/<tenant>/embed/...` and never mention
        # `boards-api` or `boards.greenhouse.io` in the HTML a plain fetcher
        # reads — so without this fingerprint a rendered pass that captured
        # the request still could not name the board.
        fp(r"https?://api\.greenhouse\.io/v\d+/boards/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=94),
        # The embed form names the tenant in a query param, so it must be
        # matched BEFORE the generic path pattern (which would capture the
        # literal "embed" as the tenant).
        fp(r"https?://(?:job-)?boards\.greenhouse\.io/embed/job_board"
           r"[^\"'\s]*[?&]for=(?P<tenant>[A-Za-z0-9_\-]+)", confidence=92),
        fp(r"https?://(?:job-)?boards\.greenhouse\.io/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=85),
    )

    # Path segments that are Greenhouse's own, not an employer slug.
    _RESERVED = frozenset({"embed", "job_board", "jobs", "assets", "static"})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not tenant or tenant.lower() in self._RESERVED:
            return None
        region = (fields.get("site") or "").strip().lower()
        if region and region not in self.REGION_BOARD_HOSTS:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant, site=region)

    # -- region --------------------------------------------------------------

    def region_of(self, endpoint: Endpoint) -> Optional[str]:
        """"" (US), "eu", or None for a region we do not know.

        None is deliberately NOT the US: an unknown region is a board we
        cannot reach, and quietly asking the US API about it would read
        someone else's board - or none - as this employer's.
        """
        region = str(endpoint.site or "").strip().lower()
        return region if region in self.REGION_BOARD_HOSTS else None

    def board_url(self, endpoint: Endpoint) -> str:
        region = self.region_of(endpoint)
        tenant = str(endpoint.tenant or "").strip()
        if region is None or not tenant:
            return ""
        if region == "":
            # US: unchanged default (`boards.greenhouse.io/<tenant>`).
            return super().board_url(endpoint)
        return f"https://{self.REGION_BOARD_HOSTS[region]}/{tenant}"

    @staticmethod
    def _remix_route(html: str, route_prefix: str) -> Optional[Dict[str, Any]]:
        """The loader data of one Remix route out of a job-boards page.

        Returns None when the page carries no parsable context - a schema
        change, a challenge page or an error page. The caller then yields
        FEWER rows (none), never raises.
        """
        if not html:
            return None
        marker = "window.__remixContext"
        at = html.find(marker)
        if at < 0:
            return None
        eq = html.find("=", at + len(marker))
        if eq < 0:
            return None
        try:
            import json as _json
            ctx_obj, _end = _json.JSONDecoder().raw_decode(html[eq + 1:].lstrip())
        except (ValueError, TypeError):
            return None
        loader = (((ctx_obj or {}).get("state") or {}).get("loaderData") or {}) \
            if isinstance(ctx_obj, dict) else {}
        if not isinstance(loader, dict):
            return None
        for key, value in loader.items():
            if str(key).startswith(route_prefix) and isinstance(value, dict):
                return value
        return None

    def _eu_board_page(self, endpoint: Endpoint, ctx: FetchContext,
                       page: int) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
        host = self.REGION_BOARD_HOSTS["eu"]
        url = f"https://{host}/{endpoint.tenant}"
        status, body = ctx.get_text(url, params={"page": page} if page > 1 else None,
                                    timeout=self.policy.timeout, vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200:
            return status, None
        route = self._remix_route(body or "", "routes/$url_token")
        return status, route

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        """Greenhouse names the board's owner on the board itself, which is
        what makes a guessed slug checkable here rather than merely plausible.
        `/v1/boards/super` returns `{"name": "Super Technologies"}` - not Super
        Micro Computer, whose name produced that guess."""
        region = self.region_of(endpoint)
        if region is None:
            return None
        if region == "eu":
            _status, route = self._eu_board_page(endpoint, ctx, 1)
            board = (route or {}).get("board") if isinstance(route, dict) else None
            name = str((board or {}).get("name") or "").strip() \
                if isinstance(board, dict) else ""
            return name or None
        status, payload = ctx.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{endpoint.tenant}",
            params=None, timeout=self.policy.timeout, vendor=self.vendor)
        if status != 200 or not isinstance(payload, dict):
            return None
        return str(payload.get("name") or "").strip() or None

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        region = self.region_of(endpoint)
        if region is None:
            ctx.log("warning", "greenhouse: unknown region %r for %s; skipped",
                    endpoint.site, endpoint.tenant)
            return
        if region == "eu":
            yield from self._list_eu(endpoint, ctx)
            return
        payload = self._get_json(
            ctx, f"https://boards-api.greenhouse.io/v1/boards/"
                 f"{endpoint.tenant}/jobs",
            # `content=false` keeps the list response small; bodies come from
            # `fetch_detail` for the rows that survive the freshness filter,
            # which is far fewer requests than shipping every body every crawl.
            {"content": "false"})
        for job in self._rows(payload, "jobs"):
            if ctx.should_stop():
                return
            title = str(job.get("title") or "").strip()
            rid = str(job.get("id") or "").strip()
            if not rid or not title:
                continue
            # first_published ONLY. `updated_at` is an EDIT timestamp, not a
            # posting date: falling back to it re-dated months-old reqs as
            # "new", and because Greenhouse is over half the catalog it was the
            # single biggest source of fake freshness in the whole product. No
            # first_published => the date is UNKNOWN and stays unknown.
            posted = to_iso(job.get("first_published"))
            if self._too_old(ctx, posted):
                continue
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=str(job.get("absolute_url") or ""),
                location=str((job.get("location") or {}).get("name") or ""),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                raw_posted_text=str(job.get("first_published") or ""),
                compensation=_pay_of(job),
                remote=looks_remote((job.get("location") or {}).get("name")),
                detail_url=(f"https://boards-api.greenhouse.io/v1/boards/"
                            f"{endpoint.tenant}/jobs/{rid}"),
            )

    def _list_eu(self, endpoint: Endpoint,
                 ctx: FetchContext) -> Iterator[RawJob]:
        """Walk an EU board's public pages. See `REGION_BOARD_HOSTS`.

        Stops on the board's own `total_pages`, on an empty page, or on the
        cap. Anything short of the board's own end - a failed later page, the
        cap, the deadline - sets `ctx.listing_truncated`, because a partial
        census read as complete would close live postings. A row id seen on
        an earlier page is not yielded twice (a board that shifts while we
        page can repeat a row across the boundary).
        """
        host = self.REGION_BOARD_HOSTS["eu"]
        seen: set = set()
        total_pages: Optional[int] = None
        page = 1
        while True:
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            if page > self.EU_MAX_PAGES:
                ctx.listing_truncated = True
                return
            status, route = self._eu_board_page(endpoint, ctx, page)
            posts = (route or {}).get("jobPosts") if isinstance(route, dict) else None
            if not isinstance(posts, dict):
                # Page 1 with no listing: unreadable board (404, schema
                # change). Later page: the walk was cut short.
                if page > 1:
                    ctx.listing_truncated = True
                elif status == 200:
                    ctx.log("warning", "greenhouse-eu: %s page has no jobPosts; "
                            "schema change?", endpoint.tenant)
                return
            if total_pages is None:
                try:
                    total_pages = max(1, int(posts.get("total_pages") or 1))
                except (TypeError, ValueError):
                    total_pages = 1
            data = [r for r in (posts.get("data") or []) if isinstance(r, dict)]
            if not data:
                return
            for job in data:
                if ctx.should_stop():
                    ctx.listing_truncated = True
                    return
                title = str(job.get("title") or "").strip()
                rid = str(job.get("id") or "").strip()
                if not rid or not title or rid in seen:
                    continue
                seen.add(rid)
                # `published_at` is the public board's first-publication
                # stamp (it equals the US API's `first_published` for the same
                # post; `updated_at` is an edit time and is never used).
                posted = to_iso(job.get("published_at"))
                if self._too_old(ctx, posted):
                    continue
                location = job.get("location")
                if isinstance(location, dict):
                    location = location.get("name")
                location = str(location or "")
                dept = job.get("department")
                yield RawJob(
                    source_job_id=rid,
                    title=title,
                    apply_url=str(job.get("absolute_url")
                                  or f"https://{host}/{endpoint.tenant}/jobs/{rid}"),
                    location=location,
                    posted_at=posted,
                    date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                    raw_posted_text=str(job.get("published_at") or ""),
                    department=str((dept or {}).get("name") or "")
                    if isinstance(dept, dict) else "",
                    remote=looks_remote(location),
                    detail_url=f"https://{host}/{endpoint.tenant}/jobs/{rid}",
                    extra={"region": "eu",
                           "requisition_id": str(job.get("requisition_id") or "")},
                )
            if page >= total_pages:
                return
            page += 1

    def _fetch_detail_eu(self, endpoint: Endpoint, job: RawJob,
                         ctx: FetchContext) -> Optional[RawJob]:
        host = self.REGION_BOARD_HOSTS["eu"]
        url = job.detail_url or f"https://{host}/{endpoint.tenant}/jobs/{job.source_job_id}"
        if not url.startswith(f"https://{host}/"):
            # A detail ref from anywhere else is not this board's job page.
            return None
        status, body = ctx.get_text(url, timeout=self.policy.detail_timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200:
            return None
        route = self._remix_route(body or "", "routes/$url_token_.jobs_")
        post = (route or {}).get("jobPost") if isinstance(route, dict) else None
        if not isinstance(post, dict):
            return None
        parts = [html_to_text(post.get(k) or "")
                 for k in ("introduction", "content", "conclusion")]
        text = "\n\n".join(p for p in parts if p.strip())
        if not text.strip():
            return None
        return RawJob(source_job_id=job.source_job_id, title=job.title,
                      apply_url=job.apply_url, description=text[:40000],
                      compensation=post.get("pay_ranges") or None)

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        region = self.region_of(endpoint)
        if region is None:
            return None
        if region == "eu":
            return self._fetch_detail_eu(endpoint, job, ctx)
        url = job.detail_url or (
            f"https://boards-api.greenhouse.io/v1/boards/"
            f"{endpoint.tenant}/jobs/{job.source_job_id}")
        payload = self._get_json(ctx, url, timeout=self.policy.detail_timeout)
        if not isinstance(payload, dict):
            return None
        body = html_to_text(payload.get("content") or "")
        if not body:
            return None
        return RawJob(source_job_id=job.source_job_id, title=job.title,
                      apply_url=job.apply_url, description=body[:40000],
                      compensation=_pay_of(payload))


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------

class LeverProvider(_BoardProvider):
    """Lever postings API. Ships the full body inline, so it needs no detail
    request — but the body is spread across four fields (see `_body`)."""

    vendor = "lever"
    label = "Lever"

    has_detail = False

    #: Lever runs a SECOND region on `eu.lever.co`, and a tenant lives in one
    #: region or the other — `api.lever.co/v0/postings/quantinuum` 404s while
    #: `api.eu.lever.co/v0/postings/quantinuum` returns the board. Both hosts
    #: were absent from `api_hosts`, `portal_hosts` and every fingerprint, so
    #: an EU-hosted employer could not be discovered, could not be matched off
    #: their own careers page, and could not be crawled if somebody had typed
    #: the tenant in by hand. The request was never attempted.
    #:
    #: This is the same defect as the Workday one already recorded in
    #: CLAUDE.md ("both Workday fetchers hardcoded `myworkdayjobs.com`, so
    #: every board on `myworkdaysite.com` was unreachable"), on a different
    #: vendor, and it is invisible in exactly the same way: nothing fails,
    #: there is simply no row.
    REGION_HOSTS = {"": ("api.lever.co", "jobs.lever.co"),
                    "eu": ("api.eu.lever.co", "jobs.eu.lever.co")}

    api_hosts = ("api.lever.co", "api.eu.lever.co")
    portal_hosts = ("jobs.lever.co", "jobs.eu.lever.co")

    fingerprints = (
        # Region-bearing patterns FIRST and at higher confidence, so a URL
        # that names a region is not captured by the region-less pattern with
        # `eu` swallowed into the tenant.
        fp(r"https?://api\.(?P<site>eu)\.lever\.co/v\d+/postings/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=97),
        fp(r"https?://jobs\.(?P<site>eu)\.lever\.co/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=96),
        fp(r"https?://api\.lever\.co/v\d+/postings/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=95),
        fp(r"https?://jobs\.lever\.co/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=90),
    )

    def _hosts(self, endpoint: Endpoint) -> Tuple[str, str]:
        """`(api host, portal host)` for this endpoint's region.

        An unknown `site` falls back to the US pair rather than raising: a
        region we have not heard of is a board we cannot reach, and the
        fallback makes that a 404 we record instead of a crash that stops a
        shard.
        """
        return self.REGION_HOSTS.get(
            str(endpoint.site or "").strip().lower(), self.REGION_HOSTS[""])

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        """Lever's postings API carries no company field, but the public
        portal titles itself with the employer's name. Without this, `lever`
        was one of the two vendors where `slug_is_trustworthy` was the only
        thing standing between a guess and `validated` - and it cannot tell
        Archer Aviation from Archer Daniels Midland."""
        _api, portal = self._hosts(endpoint)
        return self._owner_from_portal_title(
            f"https://{portal}/{endpoint.tenant}", ctx)

    def canonical_url(self, endpoint: Endpoint, job: Optional[RawJob]) -> str:
        if job is not None and getattr(job, "apply_url", ""):
            return str(job.apply_url)
        _api, portal = self._hosts(endpoint)
        return f"https://{portal}/{endpoint.tenant}"

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        api, _portal = self._hosts(endpoint)
        payload = self._get_json(
            ctx, f"https://{api}/v0/postings/{endpoint.tenant}",
            {"mode": "json"})
        # Lever answers with a bare LIST, not an object.
        if not isinstance(payload, list):
            return
        for job in payload:
            if not isinstance(job, dict):
                continue
            if ctx.should_stop():
                return
            title = str(job.get("text") or "").strip()
            rid = str(job.get("id") or "").strip()
            if not rid or not title:
                continue
            posted = from_epoch(job.get("createdAt"))
            if self._too_old(ctx, posted):
                continue
            cats = job.get("categories") or {}
            workplace = str(job.get("workplaceType") or "").lower()
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=str(job.get("hostedUrl") or ""),
                location=str(cats.get("location") or ""),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                description=self._body(job)[:20000],
                country=str(job.get("country") or ""),
                employment_type=str(cats.get("commitment") or ""),
                compensation=_pay_of(job),
                department=str(cats.get("team")
                               or cats.get("department") or ""),
                remote=True if workplace == "remote" else looks_remote(
                    cats.get("location")),
            )

    @staticmethod
    def _body(job: Dict[str, Any]) -> str:
        """Assemble a Lever posting's full text from the four fields it spans.

        `descriptionPlain` alone is wrong twice over. It is EMPTY on a large
        share of real postings (7 of 17 on the first board checked), and even
        when present it holds only the intro blurb: the actual requirements live
        in `lists`, the structured "What You'll Do" / "What You'll Bring"
        sections, with `additionalPlain` carrying benefits and EEO text. Reading
        only `descriptionPlain` stored ~60% of Lever postings with no body at
        all, and a body-less row cannot be requirement-parsed, cannot be scored
        beyond its title, and is excluded by the job-evidence gate — it occupies
        the inventory while being unservable.

        (`career_pages_service._fetch_lever` still reads `descriptionPlain`
        alone; this follows `job_ingest_worker._lever_body`, which is the
        measured fix.)

        Section headings are kept deliberately: "What You'll Bring" is a strong
        signal for the required-vs-preferred split in `fit_score`.
        """
        parts: List[str] = []
        for key in ("openingPlain", "descriptionPlain"):
            value = str(job.get(key) or "").strip()
            if value:
                parts.append(value)
        if not parts:
            body = str(job.get("descriptionBodyPlain") or "").strip()
            if body:
                parts.append(body)
        for section in (job.get("lists") or []):
            if not isinstance(section, dict):
                continue
            heading = str(section.get("text") or "").strip()
            content = html_to_text(section.get("content") or "").strip()
            if heading and content:
                parts.append(f"{heading}\n{content}")
            elif content:
                parts.append(content)
        extra = str(job.get("additionalPlain") or "").strip()
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------

class AshbyProvider(_BoardProvider):
    vendor = "ashby"
    label = "Ashby"

    has_detail = False

    api_hosts = ("api.ashbyhq.com",)
    portal_hosts = ("jobs.ashbyhq.com",)

    #: `jobs.ashbyhq.com/api` is Ashby's own path, not an employer named "api".
    #: A rendered careers page that links the host root (or an /api docs link)
    #: otherwise registers `ashby:api` as an owner-verified board.
    reserved_tenants = ("api", "app", "www", "docs", "help", "static", "assets")

    fingerprints = (
        fp(r"https?://api\.ashbyhq\.com/posting-api/job-board/"
           r"(?P<tenant>[A-Za-z0-9_\-\.]+)", confidence=95),
        fp(r"https?://jobs\.ashbyhq\.com/(?P<tenant>[A-Za-z0-9_\-\.]+)",
           confidence=90),
    )

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        """Ashby's posting API returns jobs and nothing about the org, so the
        owner comes from the portal title (`jobs.ashbyhq.com/ramp` -> "Ramp
        Jobs" -> "Ramp"). Ashby was the other vendor with no owner check."""
        return self._owner_from_portal_title(
            f"https://jobs.ashbyhq.com/{endpoint.tenant}", ctx)

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        # GET only. POST to this path returns 401, which reads as "credentials
        # required" and previously got Ashby written off as a private API.
        payload = self._get_json(
            ctx, f"https://api.ashbyhq.com/posting-api/job-board/"
                 f"{endpoint.tenant}")
        for job in self._rows(payload, "jobs"):
            if ctx.should_stop():
                return
            # `isListed: false` is a posting the employer has unpublished but
            # not deleted; absent means listed.
            if job.get("isListed") is False:
                continue
            title = str(job.get("title") or "").strip()
            rid = str(job.get("id") or "").strip()
            if not rid or not title:
                continue
            posted = to_iso(job.get("publishedAt"))
            if self._too_old(ctx, posted):
                continue
            secondary = ", ".join(
                str((s or {}).get("location") or "")
                for s in (job.get("secondaryLocations") or [])
                if isinstance(s, dict))
            location = str(job.get("location") or "")
            if secondary.strip(", "):
                location = f"{location} · {secondary}"
            country = str((((job.get("address") or {}).get("postalAddress")
                            or {}).get("addressCountry")) or "")
            description = str(job.get("descriptionPlain") or "").strip() or \
                html_to_text(job.get("descriptionHtml") or "")
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=str(job.get("jobUrl") or job.get("applyUrl") or ""),
                location=location,
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                description=description[:20000],
                country=country,
                employment_type=str(job.get("employmentType") or ""),
                compensation=_pay_of(job),
                department=str(job.get("department") or job.get("team") or ""),
                remote=job.get("isRemote") if isinstance(
                    job.get("isRemote"), bool) else looks_remote(location),
            )


# ---------------------------------------------------------------------------
# SmartRecruiters
# ---------------------------------------------------------------------------

class SmartRecruitersProvider(_BoardProvider):
    """SmartRecruiters postings API — the one board vendor here that pages."""

    vendor = "smartrecruiters"
    label = "SmartRecruiters"

    has_detail = True
    #: Set below `list_jobs`; see `board_owner`.

    api_hosts = ("api.smartrecruiters.com",)
    portal_hosts = ("jobs.smartrecruiters.com", "careers.smartrecruiters.com")

    # 100 per page, two pages. 200 newest is plenty for a 30-day window and
    # bounds what a giant multinational costs us.
    policy = _replace(_BOARD_POLICY, page_size=100, max_pages=2)

    fingerprints = (
        fp(r"https?://api\.smartrecruiters\.com/v\d+/companies/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=95),
        fp(r"https?://(?:jobs|careers)\.smartrecruiters\.com/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=90),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        # SmartRecruiters company identifiers are CASE-SENSITIVE in the API
        # path ("Visa", not "visa"), so the captured slug is stored verbatim.
        # `member_key` slugifies separately, so crawl identity is unaffected.
        tenant = (fields.get("tenant") or "").strip()
        if not tenant:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant)

    def slug_candidates(self, name: str, domain: str = "") -> Sequence[str]:
        """Case matters here, so the guesses carry casing variants.

        The base helper lowercases everything, which would miss every
        SmartRecruiters tenant whose id is PascalCase — and most are.
        """
        base = list(super().slug_candidates(name, domain))
        out: List[str] = []
        for slug in base:
            for variant in (slug, slug.capitalize(),
                            "".join(p.capitalize() for p in slug.split("-"))):
                if variant not in out:
                    out.append(variant)
        return out[:6]

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        """Every SmartRecruiters posting carries `company.name`, so one
        one-row request says who the board belongs to. See
        `AtsProvider.board_owner` for why a guessed slug needs that."""
        status, payload = ctx.get_json(
            f"https://api.smartrecruiters.com/v1/companies/"
            f"{endpoint.tenant}/postings",
            params={"limit": 1}, timeout=self.policy.timeout,
            vendor=self.vendor)
        if status != 200 or not isinstance(payload, dict):
            return None
        rows = payload.get("content") or []
        if not rows or not isinstance(rows[0], dict):
            return None
        return str((rows[0].get("company") or {}).get("name") or "").strip() or None

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        page_size = int(self.policy.page_size)
        offset = 0
        for _page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                return
            payload = self._get_json(
                ctx, f"https://api.smartrecruiters.com/v1/companies/"
                     f"{endpoint.tenant}/postings",
                # country=us is applied SERVER-side: giant multinationals
                # (AccorHotels: 5,800+ postings) would otherwise bury their US
                # slice beyond the two pages we are willing to read.
                {"limit": page_size, "offset": offset, "country": "us"})
            content = self._rows(payload, "content")
            if not content:
                return
            for job in content:
                title = str(job.get("name") or "").strip()
                rid = str(job.get("id") or "").strip()
                if not rid or not title:
                    continue
                posted = to_iso(job.get("releasedDate"))
                if self._too_old(ctx, posted):
                    continue
                loc = job.get("location") or {}
                # SmartRecruiters ships function / department / experience /
                # industry labels inline and NO description, so these labels are
                # the only scoring signal a row carries before a detail fetch:
                # an on-topic but oddly-titled posting ("Clinical Partner",
                # function "Nursing") is recalled on them alone.
                exp = str((job.get("experienceLevel") or {}).get("label") or "")
                yield RawJob(
                    source_job_id=rid,
                    title=title,
                    apply_url=(f"https://jobs.smartrecruiters.com/"
                               f"{endpoint.tenant}/{rid}"),
                    location=_join(loc.get("city"), loc.get("region")),
                    posted_at=posted,
                    date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                    country=str(loc.get("country") or ""),
                    compensation=_pay_of(job),
                    department=_join(
                        (job.get("function") or {}).get("label"),
                        (job.get("department") or {}).get("label"),
                        (job.get("industry") or {}).get("label"), sep=" · "),
                    remote=True if loc.get("remote") else looks_remote(
                        loc.get("city"), loc.get("region")),
                    detail_url=(f"https://api.smartrecruiters.com/v1/companies/"
                                f"{endpoint.tenant}/postings/{rid}"),
                    extra={"experience_level": exp} if exp else {},
                )
            offset += len(content)
            if len(content) < page_size:
                return

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        url = job.detail_url or (
            f"https://api.smartrecruiters.com/v1/companies/"
            f"{endpoint.tenant}/postings/{job.source_job_id}")
        payload = self._get_json(ctx, url, timeout=self.policy.detail_timeout)
        if not isinstance(payload, dict):
            return None
        sections = ((payload.get("jobAd") or {}).get("sections") or {})
        body = " ".join(
            html_to_text((sections.get(k) or {}).get("text") or "")
            for k in ("jobDescription", "qualifications",
                      "additionalInformation")).strip()
        if not body:
            return None
        return RawJob(source_job_id=job.source_job_id, title=job.title,
                      apply_url=job.apply_url, description=body[:40000],
                      compensation=_pay_of(payload))


# ---------------------------------------------------------------------------
# Workable
# ---------------------------------------------------------------------------

class WorkableProvider(_BoardProvider):
    vendor = "workable"
    label = "Workable"

    has_detail = True

    # Workable serves the widget API and the public apply pages from the same
    # host, which is why it appears in both crawler allowlists unchanged.
    #: NOT slug-probeable. Workable answers a burst of slug guesses with 429
    #: and a vendor-wide cooldown — observed repeatedly during live discovery
    #: ("apply.workable.com challenged, cooling down 900s"), which takes the
    #: board offline for every OTHER company in the run too, including ones we
    #: would have found by fingerprinting. A guess that reliably earns a rate
    #: limit is not "a single definitive request"; Workable tenants come from
    #: fingerprinting the employer's own careers page instead.
    slug_probe = False

    api_hosts = ("apply.workable.com",)
    portal_hosts = ("apply.workable.com",)

    fingerprints = (
        fp(r"https?://apply\.workable\.com/api/v\d+/(?:widget/)?accounts/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=95),
        fp(r"https?://apply\.workable\.com/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=90),
        # Legacy per-tenant host, still linked from older careers pages.
        fp(r"https?://(?P<tenant>[A-Za-z0-9_\-]+)\.workable\.com",
           confidence=70),
    )

    # `apply.workable.com/j/<shortcode>` is a single posting, `/api/...` is the
    # API itself; neither names an employer.
    _RESERVED = frozenset({"api", "j", "www", "apply", "accounts", "assets",
                           "static", "backend"})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not tenant or tenant.lower() in self._RESERVED:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant)

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        # The v1 widget endpoint, exactly as `_fetch_workable` calls it. The
        # crawler's `_list_career` appends `?details=true`, which inlines the
        # body and would let this vendor skip `fetch_detail` — a real
        # divergence between the two live paths, left as-is here because it is
        # a behaviour change, not a port, and it belongs to whoever retires the
        # old fetchers.
        payload = self._get_json(
            ctx, f"https://apply.workable.com/api/v1/widget/accounts/"
                 f"{endpoint.tenant}")
        for job in self._rows(payload, "jobs"):
            if ctx.should_stop():
                return
            title = str(job.get("title") or "").strip()
            rid = str(job.get("shortcode") or job.get("code") or "").strip()
            if not rid or not title:
                continue
            posted = to_iso(job.get("published_on"))
            if self._too_old(ctx, posted):
                continue
            # Function / department / experience / industry, inline and free.
            # Same scoring role as the SmartRecruiters labels above.
            exp = str(job.get("experience") or "")
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=str(job.get("url") or ""),
                location=_join(job.get("city"), job.get("state")),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                raw_posted_text=str(job.get("published_on") or ""),
                country=str(job.get("country") or ""),
                employment_type=str(job.get("employment_type") or ""),
                compensation=_pay_of(job),
                department=_join(job.get("function"), job.get("department"),
                                 job.get("industry"), sep=" · "),
                remote=True if job.get("telecommuting") else looks_remote(
                    job.get("city"), job.get("state")),
                detail_url=(f"https://apply.workable.com/api/v2/accounts/"
                            f"{endpoint.tenant}/jobs/{rid}"),
                extra={"experience_level": exp} if exp else {},
            )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        url = job.detail_url or (
            f"https://apply.workable.com/api/v2/accounts/"
            f"{endpoint.tenant}/jobs/{job.source_job_id}")
        payload = self._get_json(ctx, url, timeout=self.policy.detail_timeout)
        if not isinstance(payload, dict):
            return None
        # Requirements live in their own field; a body without them scores
        # badly because the required-vs-preferred split has nothing to read.
        body = html_to_text(
            f"{payload.get('description') or ''} "
            f"{payload.get('requirements') or ''}")
        if not body.strip():
            return None
        return RawJob(source_job_id=job.source_job_id, title=job.title,
                      apply_url=job.apply_url, description=body[:40000],
                      compensation=_pay_of(payload))


# ---------------------------------------------------------------------------
# Recruitee
# ---------------------------------------------------------------------------

class RecruiteeProvider(_BoardProvider):
    """Recruitee. The only board vendor here with a per-tenant hostname, so its
    pacing keys per host — correct, because each tenant is a separate origin."""

    vendor = "recruitee"
    label = "Recruitee"

    has_detail = False

    api_hosts = (".recruitee.com",)
    portal_hosts = (".recruitee.com",)

    fingerprints = (
        fp(r"https?://(?P<tenant>[A-Za-z0-9_\-]+)\.recruitee\.com/api/offers",
           confidence=95),
        fp(r"https?://(?P<tenant>[A-Za-z0-9_\-]+)\.recruitee\.com",
           confidence=90),
    )

    _RESERVED = frozenset({"www", "api", "app", "cdn", "static", "assets",
                           "help", "blog", "status", "support"})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip().lower()
        if not tenant or tenant in self._RESERVED:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant,
                        host=f"{tenant}.recruitee.com")

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        payload = self._get_json(
            ctx, f"https://{endpoint.tenant}.recruitee.com/api/offers/")
        for job in self._rows(payload, "offers"):
            if ctx.should_stop():
                return
            title = str(job.get("title") or "").strip()
            rid = str(job.get("id") or "").strip()
            if not rid or not title:
                continue
            # `created_at`, per `_fetch_recruitee`. (`_list_career` prefers
            # `published_at` and falls back to this; both are publication
            # timestamps rather than edit timestamps, so neither carries the
            # Greenhouse `updated_at` hazard — but the two live paths do
            # disagree, and this module follows the named fetcher.)
            posted = to_iso(job.get("created_at"))
            if self._too_old(ctx, posted):
                continue
            remote_raw = str(job.get("remote") or "").strip().lower()
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=str(job.get("careers_url")
                              or job.get("careers_apply_url") or ""),
                location=str(job.get("location") or ""),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                description=html_to_text(job.get("description") or "")[:20000],
                country=str(job.get("country_code") or ""),
                employment_type=str(job.get("employment_type_code")
                                    or job.get("employment_type") or ""),
                compensation=_pay_of(job),
                department=str(job.get("department") or ""),
                remote=True if remote_raw == "true" else looks_remote(
                    job.get("location")),
            )


# ---------------------------------------------------------------------------
# NeoGov / GovernmentJobs
# ---------------------------------------------------------------------------

def _neogov_iso(raw: Any) -> str:
    """NeoGov posts dates as MM/DD/YY.

    Its own format, so `util.to_iso` cannot read it (that helper requires a
    four-digit year, and guessing a century from two digits is exactly the kind
    of silent inference it refuses to make). Unparseable -> "" so the date is
    UNKNOWN rather than invented (ADR-001). Measured across 781 live postings
    from two agencies: zero unparseable, so this is a guard, not a common path.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%m/%d/%y").replace(
            tzinfo=timezone.utc).isoformat()
    except ValueError:
        return ""


class NeoGovProvider(_BoardProvider):
    """Government employers (states, counties, cities) on NeoGov/GovernmentJobs.

    POST, not GET, and it needs the XHR header — this is the agency page's own
    job-list call. Every GET variant 404s, which is why this vendor was
    initially written off as unfetchable; `scripts/probe_new_ats.py` carries the
    discovery record. A 404 here reads as "no such agency" when it actually
    means "you asked wrong", so the failure is quiet and expensive.

    One request returns the agency's whole board (706 for State of Washington,
    75 for City of Seattle) — the same whole-board shape as Greenhouse, with no
    paging to walk and no per-job detail endpoint.
    """

    vendor = "neogov"
    label = "GovernmentJobs"

    has_detail = False

    # Opted out of the slug-probe fallback. It would technically work (one
    # shared host), but the fallback runs for every company whose careers page
    # is a JS shell, and virtually none of those are municipal governments — so
    # it would be a wasted request on essentially every probe. Agencies reach
    # the catalog by fingerprint or by explicit seeding.
    slug_probe = False

    api_hosts = ("www.governmentjobs.com",)
    portal_hosts = ("www.governmentjobs.com",)

    # Agency boards are the slowest responders in this set (a 706-posting
    # payload with full descriptions inline), so they get the crawler's
    # HEAVY_REQUEST_TIMEOUT rather than the board default.
    policy = _replace(_BOARD_POLICY, timeout=15.0)

    fingerprints = (
        fp(r"https?://(?:www\.)?governmentjobs\.com/careers/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=95),
        fp(r"https?://agency\.governmentjobs\.com/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=85),
    )

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        if ctx.post_json is None:
            ctx.log("warning", "neogov: context has no post_json; %s skipped",
                    endpoint.tenant)
            return
        slug = endpoint.tenant
        status, payload = ctx.post_json(
            f"https://www.governmentjobs.com/careers/{slug}/home/loadJobsOnMaps",
            # An EMPTY FORM body. The endpoint is the page's own XHR and
            # answers to `application/x-www-form-urlencoded`; the header below
            # is what it actually gates on — without it the response is a 404.
            form_body={},
            extra_headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=self.policy.timeout,
            vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200:
            return
        for job in self._rows(payload, "jobList"):
            if ctx.should_stop():
                return
            job_id = job.get("ID")
            if not job_id:
                continue
            # `JobTitle` is a URL SLUG ("utility-rate-analyst-senior-economist")
            # despite its name; the human title is `Classification`. Using
            # JobTitle puts hyphenated slugs on every card and wrecks
            # title-based role classification downstream.
            title = str(job.get("Classification") or "").strip()
            if not title:
                continue
            posted = _neogov_iso(job.get("PostingDate"))
            if self._too_old(ctx, posted):
                continue
            remote = str(job.get("RemoteWorkOptionText") or "").strip().lower()
            yield RawJob(
                source_job_id=str(job_id),
                title=title,
                # No URL in the payload; the public permalink is built from the
                # ID and the agency slug.
                apply_url=(f"https://www.governmentjobs.com/careers/"
                           f"{slug}/jobs/{job_id}"),
                location=str(job.get("Location") or "").strip(),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                raw_posted_text=str(job.get("PostingDate") or ""),
                description=str(job.get("FullDescription") or "")[:20000],
                # Every agency on this platform is a US public employer.
                country="us",
                employment_type=str(job.get("JobType") or ""),
                compensation=job.get("SalaryInfo"),
                # Department is real signal on an agency board, where titles
                # are terse and civil-service coded.
                department=str(job.get("DepartmentName") or ""),
                remote=bool(remote and remote not in ("no", "none", "onsite")),
            )


# ---------------------------------------------------------------------------
# Jobvite
# ---------------------------------------------------------------------------

class JobviteProvider(_BoardProvider):
    """Jobvite public boards — HTML, because Jobvite does not serve JSON.

    Correcting the record (verified live 2026-08-18)
    ------------------------------------------------
    This provider was written against `jobs.jobvite.com/{slug}/jobs.json`, on
    the strength of a pattern recorded in `scripts/probe_new_ats.py`. That URL
    does respond 200 — with `Content-Type: text/html`. It is not a JSON API and
    was probably never one; a 200 was mistaken for a working endpoint because
    nothing checked what came back. `/api/v1/company/{slug}/jobs`, the
    documented-looking fallback, redirects every slug to Jobvite's marketing
    site at `/support/job-seeker-support/?invalid=1` — also 200.

    So the old code could only ever yield zero rows for every tenant, which is
    exactly what production shows: Jobvite has 0 postings in `job_postings`.
    This is the same lesson ADR-004 §7 draws for iCIMS, in the other direction:
    there, "returns HTML" was wrongly read as "not fetchable"; here, "returns
    200" was wrongly read as "returns jobs". **A status code is not a payload.**

    What Jobvite actually serves
    ----------------------------
    `GET https://jobs.jobvite.com/{slug}/search` renders the COMPLETE board in
    one page — there is no pagination to follow — as a table:

        <td class="jv-job-list-name"><a href="/{slug}/job/{id}">Title</a></td>
        <td class="jv-job-list-location">Honolulu, Hawaii</td>

    The id (`ouFyAfw1`) is an opaque stable token. Multi-location reqs render
    `<div class="jv-meta">2 Locations</div>` instead of a place, which is a
    count and not a location, so it is dropped rather than stored.

    Dates: Jobvite publishes NONE. Not on the listing, not on the detail page,
    and there is no `JobPosting` JSON-LD. Hence `date_confidence = DATE_UNKNOWN`
    — declaring `DATE_EXACT` and shipping empty strings is the mismatch
    `scripts/live_provider_check.py` now fails a provider for.

    Some tenants serve the same board from their own domain
    (`careers.victaulic.com`), which is why a redirect off `jobs.jobvite.com` is
    followed rather than treated as a dead board.
    """

    vendor = "jobvite"
    label = "Jobvite"
    has_detail = True
    #: Jobvite's own asset path segment. It is not a company.
    reserved_tenants = ("__assets__", "assets", "static", "cdn")
    #: NOT slug-probeable, unlike the JSON boards this class sits with.
    #:
    #: `slug_probe` is documented as safe where "a slug guess is a single
    #: definitive request" — Greenhouse either returns a board or 404s. Jobvite
    #: answers EVERY wrong slug with HTTP 200 and ~200 KB of marketing HTML, so
    #: a guess costs a full page fetch and parse to learn nothing. Observed in a
    #: live discovery pass burning requests on `sysco`, `shopify`,
    #: `hcahealthcare`, `norfolksouthern`, `teledynetechnologies` and their
    #: hyphenated variants, none of which are Jobvite tenants.
    #:
    #: Jobvite tenants come from fingerprinting an employer's own careers page,
    #: or from the archives via `tenant_discovery`.
    slug_probe = False
    #: Jobvite publishes no posting date anywhere on the public board.
    date_confidence = DATE_UNKNOWN
    api_hosts = ("jobs.jobvite.com",)
    portal_hosts = ("jobs.jobvite.com",)
    fingerprints = (
        fp(r"https?://jobs\.jobvite\.com/(?P<tenant>[A-Za-z0-9_\-]+)/",
           confidence=90),
        fp(r"https?://jobs\.jobvite\.com/(?P<tenant>[A-Za-z0-9_\-]+)$",
           confidence=85),
    )
    _RESERVED = frozenset({"api", "careers", "jobs", "search", "company",
                           "support", "job"})
    #: Employee-only boards, same rule as iCIMS: they serve well-formed rows an
    #: outsider can never apply to, so showing them is worse than omitting them.
    _INTERNAL_RE = re.compile(r"(?:^|-)(?:internal|employee|redeploy)(?:-|$)",
                              re.IGNORECASE)

    _ROW_RE = re.compile(
        r'<td[^>]*class="[^"]*jv-job-list-name[^"]*"[^>]*>(.*?)</td>\s*'
        r'<td[^>]*class="[^"]*jv-job-list-location[^"]*"[^>]*>(.*?)</td>',
        re.IGNORECASE | re.DOTALL)
    _HREF_RE = re.compile(r'href="([^"]*/job/([A-Za-z0-9_\-]+))[^"]*"',
                          re.IGNORECASE)
    #: "2 Locations" is a count, not a place. Storing it would make every
    #: multi-site req match nowhere and look like it is hiring in a town called
    #: "2 Locations".
    _MULTI_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.IGNORECASE)

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not tenant or tenant.lower() in self._RESERVED:
            return None
        if self._INTERNAL_RE.search(tenant):
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant)

    # -- listing -------------------------------------------------------------

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        slug = endpoint.tenant
        if not slug:
            return
        base = f"https://jobs.jobvite.com/{slug}"
        status, body = ctx.get_text(f"{base}/search", params={},
                                    timeout=self.policy.timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return
        # Jobvite bounces unknown slugs to its own marketing site with a 200.
        # Without this an invalid tenant reads as "a board with no jobs" and is
        # retained and re-crawled forever.
        if "job-seeker-support" in body and "jv-job-list-name" not in body:
            ctx.log("info", "jobvite: %s is not a live board", slug)
            return

        seen: set = set()
        for match in self._ROW_RE.finditer(body):
            if ctx.should_stop():
                return
            name_cell, loc_cell = match.group(1), match.group(2)
            href_match = self._HREF_RE.search(name_cell)
            if not href_match:
                continue
            href, rid = href_match.group(1), href_match.group(2)
            if rid in seen:
                continue
            title = " ".join(html_to_text(name_cell).split())
            if not title:
                continue
            seen.add(rid)

            location = " ".join(html_to_text(loc_cell).split())
            if self._MULTI_RE.match(location):
                location = ""
            url = urljoin(base + "/", href.lstrip("/")) if href.startswith("/") \
                else href
            if href.startswith("/"):
                url = f"https://jobs.jobvite.com{href}"
            yield RawJob(
                source_job_id=rid,
                title=title[:200],
                apply_url=url,
                location=location[:160],
                posted_at="",
                date_confidence=DATE_UNKNOWN,
                remote=looks_remote(title, location),
                detail_url=url,
            )

    # -- detail --------------------------------------------------------------

    _DESC_RE = re.compile(
        r'<div[^>]*class="[^"]*jv-job-detail-description[^"]*"[^>]*>(.*?)'
        r'(?:<div[^>]*class="[^"]*jv-job-detail-bottom-actions|</body>)',
        re.IGNORECASE | re.DOTALL)

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        """The JD body. Deliberately does NOT invent a posting date.

        Jobvite detail pages carry no date and no `JobPosting` JSON-LD, so this
        returns a description and leaves `posted_at` empty — ADR-001: a missing
        date stays missing.
        """
        url = job.detail_url or job.apply_url
        if not url:
            return None
        status, body = ctx.get_text(url, params={},
                                    timeout=self.policy.detail_timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return None
        m = self._DESC_RE.search(body)
        description = html_to_text(m.group(1)) if m else ""
        if not description:
            return None
        return RawJob(
            source_job_id=job.source_job_id,
            title=job.title,
            apply_url=job.apply_url,
            description=description[:40000],
            posted_at="",
            date_confidence=DATE_UNKNOWN,
        )


# ---------------------------------------------------------------------------
# BambooHR
# ---------------------------------------------------------------------------

class BambooHrProvider(_BoardProvider):
    """BambooHR public boards — listing for identity, detail for everything else.

    Corrected against the live API on 2026-08-18. The previous version mapped
    fields BambooHR does not send, so every row it produced was wrong in ways
    that a fixture test could not catch because the fixture invented the same
    fields:

      * `datePosted` — **not present on the listing at all**. The class
        nonetheless declared `date_confidence = DATE_EXACT`, promising dates it
        could never deliver.
      * `locationLabel` — also absent. The real field is `location`, a DICT
        (`{"city": "Wattana", "state": "Bangkok"}`), and `str()` of it was being
        stored verbatim, so the index would have carried locations reading
        `{'city': 'Wattana', 'state': 'Bangkok'}`.

    What the vendor actually serves:

        GET /careers/list            -> {"meta": {"totalCount": 8},
                                         "result": [{id, jobOpeningName,
                                                     departmentLabel,
                                                     employmentStatusLabel,
                                                     location{city,state},
                                                     isRemote, locationType}]}
        GET /careers/{id}/detail     -> {"result": {"jobOpening": {
                                            datePosted, description,
                                            compensation, minimumExperience,
                                            jobOpeningShareUrl, ...}}}

    So the date, the JD and the salary all live on the DETAIL call, which is why
    this provider now declares `has_detail = True`. That matters more here than
    for most vendors: BambooHR's customers are overwhelmingly small employers,
    which is exactly the population whose postings are invisible on aggregators
    — a row with no description and no salary is not much use to a job seeker
    even once it is indexed.
    """

    vendor = "bamboohr"
    label = "BambooHR"
    #: The listing carries no date, no description and no pay; all three come
    #: from `/careers/{id}/detail`.
    has_detail = True
    #: Honest about the LISTING. `fetch_detail` upgrades a row to `exact` when
    #: it reads a real `datePosted`.
    date_confidence = DATE_UNKNOWN
    api_hosts = (".bamboohr.com",)
    portal_hosts = (".bamboohr.com",)
    fingerprints = (
        fp(r"https?://(?P<tenant>[A-Za-z0-9][A-Za-z0-9\-]*?)\.bamboohr\.com"
           r"/(?:careers|jobs)", confidence=92),
        fp(r"https?://(?P<tenant>[A-Za-z0-9][A-Za-z0-9\-]*?)\.bamboohr\.com",
           confidence=70),
    )
    #: `www` and friends resolve like any tenant and answer `/careers/list` with
    #: BambooHR's own marketing HTML, so an unfiltered label yields a board
    #: that is permanently empty.
    _RESERVED = frozenset({"www", "app", "api", "static", "cdn", "help",
                           "marketplace", "resources", "blog", "smetrics"})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not tenant or tenant.lower() in self._RESERVED:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant,
                        host=f"{tenant.lower()}.bamboohr.com")

    @staticmethod
    def _location_of(job: Dict[str, Any]) -> str:
        """City/state/country from whichever shape this tenant uses.

        `location` is a dict on every tenant observed; `atsLocation` is the
        newer sibling and is usually all-nulls. `locationLabel` is accepted only
        because a string costs nothing to support if some tenant does send one.
        """
        label = job.get("locationLabel")
        if isinstance(label, str) and label.strip():
            return label.strip()[:160]
        for key in ("location", "atsLocation"):
            loc = job.get(key)
            if not isinstance(loc, dict):
                continue
            joined = _join(loc.get("city"),
                           loc.get("state") or loc.get("province"),
                           loc.get("country"))
            if joined:
                return joined[:160]
        return ""

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        slug = (endpoint.tenant or "").strip().lower()
        if not slug or slug in self._RESERVED:
            return
        host = (endpoint.host or f"{slug}.bamboohr.com").strip().lower()
        payload = self._get_json(ctx, f"https://{host}/careers/list")
        for job in self._rows(payload, "result"):
            if ctx.should_stop():
                return
            title = str(job.get("jobOpeningName") or job.get("title")
                        or "").strip()
            rid = str(job.get("id") or "").strip()
            if not rid or not title:
                continue
            # No cutoff filter here on purpose: the listing has no date, so
            # there is nothing to compare. Dropping undated rows would empty
            # the vendor; ADR-001 keeps them and records them as undated.
            location = self._location_of(job)
            remote = job.get("isRemote")
            yield RawJob(
                source_job_id=rid,
                title=title[:200],
                apply_url=f"https://{host}/careers/{rid}",
                location=location,
                posted_at="",
                date_confidence=DATE_UNKNOWN,
                department=str(job.get("departmentLabel") or ""),
                employment_type=str(job.get("employmentStatusLabel")
                                    or job.get("employmentType") or ""),
                remote=(bool(remote) if isinstance(remote, bool)
                        else looks_remote(title, location)),
                detail_url=f"https://{host}/careers/{rid}/detail",
            )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        """The date, the JD and the pay band — none of which the listing has."""
        url = job.detail_url
        if not url:
            slug = (endpoint.host or f"{endpoint.tenant}.bamboohr.com").lower()
            url = f"https://{slug}/careers/{job.source_job_id}/detail"
        payload = self._get_json(ctx, url,
                                 timeout=self.policy.detail_timeout)
        if not isinstance(payload, dict):
            return None
        result = payload.get("result")
        opening = result.get("jobOpening") if isinstance(result, dict) else None
        if not isinstance(opening, dict):
            return None
        posted = to_iso(opening.get("datePosted"))
        description = html_to_text(opening.get("description") or "")
        if not posted and not description:
            return None
        return RawJob(
            source_job_id=job.source_job_id,
            title=job.title,
            apply_url=str(opening.get("jobOpeningShareUrl") or job.apply_url),
            description=description[:40000],
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            employment_type=str(opening.get("employmentStatusLabel") or ""),
            department=str(opening.get("departmentLabel") or ""),
            # A free-text band ("17k-19k"); `normalize_posting` parses it.
            compensation=opening.get("compensation") or None,
        )


register(GreenhouseProvider())
register(LeverProvider())
register(AshbyProvider())
register(SmartRecruitersProvider())
register(WorkableProvider())
register(RecruiteeProvider())
register(NeoGovProvider())
register(JobviteProvider())
register(BambooHrProvider())
