"""Taleo (Oracle Taleo Enterprise) — careersection job board REST.

Read this before extending it
-----------------------------
Taleo is a SHRINKING surface and is deliberately not on the critical path.
Oracle has spent years migrating Taleo customers to Oracle Recruiting Cloud, and
the evidence is blunt (probed 2026-08-17): of six well-known former Taleo
customers, five no longer resolve at all -

    chevron.taleo.net   NXDOMAIN
    csx.taleo.net       NXDOMAIN
    flowserve.taleo.net NXDOMAIN
    parsons.taleo.net   NXDOMAIN
    oracle.taleo.net    NXDOMAIN
    tetratech.taleo.net LIVE

So `oracle_orc.py` is where Oracle's volume went, and this module exists to cover
the remainder rather than to be a major source.

The protocol (verified live 2026-08-17 against tetratech)
---------------------------------------------------------
    POST https://{zone}.taleo.net/careersection/rest/jobboard/searchjobs
         ?lang=en&portal={portal_id}
    body: {"multilineEnabled": false,
           "sortingSelection": {...}, "fieldData": {...},
           "filterSelectionParam": {...}, "pageNo": N}

Returns `{requisitionList[], pagingData:{currentPageNo,pageSize,totalCount}}`.
tetratech with the right portal id: **totalCount 947**, 25 rows per page.

Two things about the payload that are easy to get wrong:

**1. Rows are POSITIONAL, and the positions are per-portal.** A requisition is
`{"jobId","contestNo","column":[...],"linkedColumn":1,"locationsColumns":[2]}`.
`column` is a bare array whose meaning is configured per career section, and the
row itself tells you how to read it: `linkedColumn` is the index of the title,
`locationsColumns` lists the indices holding locations. Hardcoding
`column[1] == title` happens to work for tetratech and WILL silently mis-map
other tenants, so this module reads the indices from the row.

**2. There is no posting date in the list payload at all.** Under ADR-001 that
means list rows are `DATE_UNKNOWN` - the date is NOT invented, and a caller that
needs one must fetch the detail page.

Why `portal` must be configured
-------------------------------
`portal` is mandatory: without it the endpoint returns 200 with an empty
`requisitionList`, which reads exactly like an empty board. Portal ids are opaque
numerics (e.g. 101430233) that are NOT derivable from the zone name, and probing
found no reliable way to enumerate them - `jobsearch.ftl` did not expose one, and
wrong ids return an indistinguishable empty 200.

That is why this provider does NOT set `slug_probe` and why an endpoint without a
portal id reports `STATUS_DEAD` with an explicit reason instead of quietly
returning zero jobs. An unconfigured Taleo tenant is a KNOWN-UNKNOWN, and it says
so, rather than masquerading as an employer with no openings.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence
from urllib.parse import urlparse

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN,
                                         STATUS_BLOCKED, STATUS_DEAD,
                                         STATUS_EMPTY, STATUS_VALIDATED,
                                         AtsProvider, ChallengeError,
                                         CrawlPolicy, Endpoint, FetchContext,
                                         RawJob, ValidationResult, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote, to_iso

_PAGE_SIZE = 25          # server-fixed; `pageSize` in pagingData confirms it

#: The numeric portal id as the career-section page prints it. Matched loosely
#: (`portal=`, `portal:`, `"portal" : "…"`) because the surrounding markup
#: differs between Taleo template versions, while the id itself is always a run
#: of at least six digits.
_PORTAL_ID_RE = re.compile(r"portal[\"']?\s*[:=]\s*[\"']?(\d{6,})",
                           re.IGNORECASE)


def _search_body(page: int, keyword: str = "") -> Dict[str, Any]:
    return {
        "multilineEnabled": False,
        "sortingSelection": {"sortBySelectionParam": "3",
                             "ascendingSortingOrder": "false"},
        "fieldData": {"fields": {"KEYWORD": keyword, "LOCATION": ""},
                      "valid": True},
        "filterSelectionParam": {"searchFilterSelections": []},
        "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
        "pageNo": int(page),
    }


class TaleoProvider(AtsProvider):
    vendor = "taleo"
    label = "Taleo"

    termless = True
    has_detail = True
    # The LIST carries no date at all; detail pages sometimes do. Declaring
    # UNKNOWN here is the honest default and stops a caller assuming otherwise.
    date_confidence = DATE_UNKNOWN

    api_hosts = (".taleo.net",)
    portal_hosts = (".taleo.net",)

    # Explicitly NOT slug-probeable: a wrong `portal` returns an empty 200 that
    # is indistinguishable from a real empty board, so guessing would
    # manufacture confident nonsense.
    slug_probe = False

    policy = CrawlPolicy(
        pacing_key=None,          # one zone per customer; per-host is right
        min_interval=0.5,
        jitter=0.25,
        max_concurrency=2,
        timeout=20.0,
        detail_timeout=15.0,
        # Taleo zones return 500 under load (observed on a portal that had
        # answered 200 moments earlier), so 5xx is treated as retryable rather
        # than as evidence the tenant is gone.
        challenge_statuses=frozenset({429, 503}),
        page_size=_PAGE_SIZE,
        max_pages=40,
    )

    fingerprints = (
        fp(r"https?://(?P<tenant>[A-Za-z0-9\-]+)\.taleo\.net/careersection"
           r"/(?P<site>[A-Za-z0-9_\-]+)/", confidence=90),
        fp(r"https?://(?P<tenant>[A-Za-z0-9\-]+)\.taleo\.net", confidence=70),
        # Taleo Business Edition is a different product on a shared host; the
        # org code is the tenant there.
        fp(r"https?://[A-Za-z0-9\-]*\.?tbe\.taleo\.net/[A-Za-z0-9]+/ats/"
           r"careerportal[^\"']*org=(?P<tenant>[A-Za-z0-9_\-]+)", confidence=85),
    )

    # -- identity ------------------------------------------------------------

    @staticmethod
    def _zone(endpoint: Endpoint) -> str:
        host = (endpoint.host or "").strip().lower()
        if host.endswith(".taleo.net"):
            return host.split(".")[0]
        return (endpoint.tenant or "").strip().lower()

    @staticmethod
    def _portal(endpoint: Endpoint) -> str:
        extra = endpoint.extra or {}
        portal = str(extra.get("portal") or extra.get("portal_id") or "").strip()
        return portal if portal.isdigit() else ""

    def _discover_portal(self, zone: str, ctx: FetchContext) -> str:
        """Read the numeric portal id off the career section's own page.

        ADR-004 §3.3 concluded that Taleo could not be seeded because "every
        zone needs an opaque portal id that is not derivable and that we do not
        have". The id is not derivable — but it is not hidden either. It is
        printed in the career-section page that the zone's bare search URL
        redirects to (verified on `aarcorp.taleo.net`, 2026-08-18):

            GET https://{zone}.taleo.net/careersection/jobsearch.ftl
              -> 302 /careersection/2/jobsearch.ftl
              -> body contains  portal=101430233

        One extra GET per zone converts Taleo from "cannot be configured" into
        an ordinary source. The searchjobs POST for that portal then returned
        200 requisitions for AAR Corp, with titles, locations and real posted
        dates — so this is a live surface, not the vestige the ADR assumed.

        Returns "" when the zone does not resolve or publishes no portal, which
        is a configuration answer, not a claim that the employer has no jobs.
        """
        status, body = ctx.get_text(
            f"https://{zone}.taleo.net/careersection/jobsearch.ftl",
            params={"lang": "en"}, timeout=self.policy.timeout,
            vendor=self.vendor)
        if status != 200 or not body:
            return ""
        m = _PORTAL_ID_RE.search(body)
        return m.group(1) if m else ""

    def _url(self, zone: str) -> str:
        return f"https://{zone}.taleo.net/careersection/rest/jobboard/searchjobs"

    # -- listing -------------------------------------------------------------

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        zone = self._zone(endpoint)
        if not zone:
            ctx.log("info", "taleo: %s has no zone, skipping",
                    endpoint.member_key)
            return
        portal = self._portal(endpoint)
        if not portal:
            portal = self._discover_portal(zone, ctx)
            if portal:
                # Cache on the endpoint so a re-list in the same run, and the
                # `company_ats_endpoints` row written from it, skip the extra
                # request next time.
                endpoint.extra = dict(endpoint.extra or {})
                endpoint.extra["portal"] = portal
        if not portal:
            ctx.log("info", "taleo: %s publishes no portal id, skipping", zone)
            return
        if ctx.post_json is None:
            return

        url = self._url(zone)
        keyword = str((ctx.options or {}).get("keyword") or "").strip()
        total: Optional[int] = None
        seen: set = set()

        for page in range(1, int(self.policy.max_pages) + 1):
            if ctx.should_stop():
                return
            status, payload = ctx.post_json(
                url, params={"lang": "en", "portal": portal},
                json_body=_search_body(page, keyword),
                timeout=self.policy.timeout, vendor=self.vendor,
                # `tz` is REQUIRED. Without it this endpoint answers 500 —
                # not 400, so it reads as a broken zone rather than a malformed
                # request, which is how Taleo stayed "unbuildable". Isolated on
                # `aarcorp` (2026-08-18): tz alone -> 200 with 25 rows; Referer
                # alone -> 500. Cookies are irrelevant.
                extra_headers={"X-Requested-With": "XMLHttpRequest",
                               "tz": "GMT+00:00"})
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                return
            if payload.get("careerSectionUnAvailable"):
                ctx.log("info", "taleo: %s career section unavailable", zone)
                return

            rows = [r for r in (payload.get("requisitionList") or [])
                    if isinstance(r, dict)]
            if not rows:
                return
            if total is None:
                paging = payload.get("pagingData") or {}
                try:
                    total = int(paging.get("totalCount") or 0)
                except (TypeError, ValueError):
                    total = 0
                ctx.log("info", "taleo: %s portal=%s total=%s", zone, portal,
                        total)

            for raw in rows:
                job = self._to_raw(endpoint, zone, portal, raw)
                if job is None or job.source_job_id in seen:
                    continue
                seen.add(job.source_job_id)
                yield job

            if len(rows) < _PAGE_SIZE:
                return
            if total and page * _PAGE_SIZE >= total:
                return

    def _to_raw(self, endpoint: Endpoint, zone: str, portal: str,
                raw: Dict[str, Any]) -> Optional[RawJob]:
        job_id = str(raw.get("jobId") or raw.get("contestNo") or "").strip()
        if not job_id:
            return None

        columns = raw.get("column")
        columns = columns if isinstance(columns, list) else []

        def col(idx: Any) -> str:
            try:
                i = int(idx)
            except (TypeError, ValueError):
                return ""
            return str(columns[i]) if 0 <= i < len(columns) else ""

        # Read the layout from the row rather than assuming it: `column` is
        # configured per career section, so fixed indices mis-map other tenants.
        title = col(raw.get("linkedColumn", 1)).strip()
        if not title:
            # Fall back to the first column that is not the requisition number.
            contest = str(raw.get("contestNo") or "")
            title = next((str(c).strip() for c in columns
                          if str(c).strip() and str(c).strip() != contest
                          and not str(c).strip().startswith("[")), "")
        if not title:
            return None

        locations: List[str] = []
        for idx in (raw.get("locationsColumns") or []):
            value = col(idx)
            # Locations arrive JSON-encoded inside the positional array, e.g.
            # '["TH-TH-TH-Bangkok"]', and the dash-joined form is
            # country-region-state-city.
            if value.startswith("["):
                try:
                    for entry in json.loads(value):
                        parts = [p for p in str(entry).split("-") if p]
                        locations.append(", ".join(dict.fromkeys(parts[::-1]))
                                         if parts else str(entry))
                except ValueError:
                    locations.append(value)
            elif value:
                locations.append(value)
        location = "; ".join(locations[:3])[:160]

        # Most career sections DO publish a posting date, as one more entry in
        # the same positional array ("Aug 17, 2026" on `aarcorp`). The response
        # names the title and location columns but not this one, so it is found
        # by parsing: a column that is not the title, not a location and that
        # `to_iso` accepts as a calendar date IS the date. Nothing is inferred
        # when no column parses — ADR-001, the row stays undated.
        used = {int(i) for i in (raw.get("locationsColumns") or [])
                if str(i).lstrip("-").isdigit()}
        try:
            used.add(int(raw.get("linkedColumn", 0)))
        except (TypeError, ValueError):
            pass
        posted = ""
        for idx, value in enumerate(columns):
            if idx in used:
                continue
            text = str(value).strip()
            if not text or text.startswith("["):
                continue
            iso = to_iso(text)
            if iso:
                posted = iso
                break

        return RawJob(
            source_job_id=job_id,
            title=title[:200],
            apply_url=(f"https://{zone}.taleo.net/careersection/jobdetail.ftl"
                       f"?job={job_id}&lang=en"),
            location=location,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            remote=looks_remote(title, location),
            detail_url=(f"https://{zone}.taleo.net/careersection/rest/jobboard"
                        f"/jobDetail"),
            extra={"contest_no": str(raw.get("contestNo") or ""),
                   "portal": portal, "hot": bool(raw.get("hotJob"))},
        )

    # -- detail --------------------------------------------------------------

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        zone = self._zone(endpoint)
        portal = self._portal(endpoint)
        if not zone or not job.source_job_id:
            return None
        status, payload = ctx.get_json(
            f"https://{zone}.taleo.net/careersection/rest/jobboard/jobDetail",
            params={"lang": "en", "portal": portal, "jobId": job.source_job_id},
            timeout=self.policy.detail_timeout, vendor=self.vendor,
            extra_headers={"X-Requested-With": "XMLHttpRequest"})
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not isinstance(payload, dict):
            return None

        detail = payload.get("job") if isinstance(payload.get("job"), dict) \
            else payload
        description = html_to_text(" ".join(
            str(detail.get(k) or "") for k in
            ("description", "jobDescription", "briefDescription",
             "qualification", "jobQualification")))
        posted = to_iso(detail.get("postedDate") or detail.get("startDate") or "")
        if not description and not posted:
            return None
        return RawJob(
            source_job_id=job.source_job_id, title=job.title,
            apply_url=job.apply_url, description=description[:40000],
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
        )

    # -- validation ----------------------------------------------------------

    def validate(self, endpoint: Endpoint,
                 ctx: FetchContext) -> ValidationResult:
        zone = self._zone(endpoint)
        if not zone:
            return ValidationResult(STATUS_DEAD, detail="no zone")
        if not self._portal(endpoint):
            # The distinction that matters: we do not know whether this employer
            # has jobs, because we lack the portal id. Reporting EMPTY would be a
            # false claim about the employer; DEAD with a reason is a true claim
            # about our configuration.
            return ValidationResult(
                STATUS_DEAD,
                detail="no portal id configured (see module docstring)")
        try:
            jobs = list(self._first_page(endpoint, ctx))
        except ChallengeError as exc:
            return ValidationResult(STATUS_BLOCKED, detail=str(exc))
        if jobs:
            return ValidationResult(STATUS_VALIDATED, len(jobs), jobs[0].title)
        return ValidationResult(STATUS_EMPTY, 0, detail="empty requisitionList")

    def _first_page(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Iterator[RawJob]:
        count = 0
        for job in self.list_jobs(endpoint, ctx):
            yield job
            count += 1
            if count >= 5:
                return


register(TaleoProvider())
