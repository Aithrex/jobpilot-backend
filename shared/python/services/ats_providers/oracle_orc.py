"""Oracle Recruiting Cloud (ORC) — Oracle Fusion HCM's Candidate Experience API.

Where Oracle's volume actually lives. Oracle has been migrating Taleo customers
here for years (ADR-004 §3.3: most `{zone}.taleo.net` hosts no longer resolve),
so ORC is the Oracle surface worth building, and it is large: two hosts probed on
2026-08-17 reported 7,381 (JPMorgan Chase) and 2,106 (Oracle) open requisitions.

What this fixes versus `career_pages_service._fetch_orc`
--------------------------------------------------------
That fetcher issues ONE request with `limit=25` and no `offset`, so it reads 25
of JPMorgan's 7,381 requisitions - 0.3% - and it is registered as a term-driven
"enterprise" fetcher, which excludes it from `_TERMLESS_ATS` and therefore from
the background crawl entirely. Both are wrong, and the endpoint itself proves it:

* every response carries `TotalJobsCount`, and `offset` pages cleanly, so the
  whole board is reachable;
* `sortBy=POSTING_DATES_DESC` makes it newest-first, which is exactly the
  ordering ADR-003's early-stop rule needs, so this is a TERMLESS board crawl
  rather than a keyword-driven one.

Endpoint (verified live 2026-08-17)
-----------------------------------
    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true
        &expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},limit={n},sortBy=POSTING_DATES_DESC,offset={k}

Response: `items[0].requisitionList[]`, with `items[0].TotalJobsCount`.

Dates
-----
`PostedDate` is a true ISO date ("2026-08-18"), not relative text, so ORC rows are
ADR-001 CONFIDENCE_EXACT - strictly better than Workday's "Posted 2 Days Ago".

Liveness
--------
Retired or parked pods answer 503/504, NOT 404 (observed on
`ejjh.fa.us2.oraclecloud.com` and `fa-etmm-saasfaprod1...`). Validation must
therefore treat 5xx as "unknown, retry later" and never as grounds to delete an
endpoint, or a transient Oracle outage would wipe the catalog.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional

from services.ats_providers.base import (DATE_EXACT, STATUS_BLOCKED,
                                         STATUS_DEAD, STATUS_EMPTY,
                                         STATUS_VALIDATED, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob,
                                         ValidationResult, fp)
from services.ats_providers.registry import register

# ORC pods are all under oraclecloud.com, but customers routinely CNAME a vanity
# host (`jpmc.fa.oraclecloud.com`, `careers.<company>.com`). The allowlist covers
# the Oracle-owned space; vanity hosts are admitted per-endpoint via `host`
# only after validation, so an attacker cannot point us anywhere by planting a
# link (see registry/discovery validation).
_API_HOSTS = (".oraclecloud.com", ".oracle.com")

_DEFAULT_SITE = "CX_1"

# `finder` is a small DSL, not a query string; a stray ',' or ';' in a value
# would change its meaning. Site numbers are `CX_1`-shaped, so anything else is
# refused rather than escaped.
_SITE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")


def _clean_html(text: str) -> str:
    from services.ats_providers.util import html_to_text
    return html_to_text(text)


class OracleOrcProvider(AtsProvider):
    vendor = "oracle_orc"
    label = "Oracle Recruiting"

    termless = True
    has_detail = True
    date_confidence = DATE_EXACT

    api_hosts = _API_HOSTS
    portal_hosts = _API_HOSTS

    policy = CrawlPolicy(
        # Keyed per host: each customer runs their own pod, so one slow tenant
        # must not throttle the rest. Unlike iCIMS there is no shared wall.
        pacing_key=None,
        min_interval=0.25,
        jitter=0.15,
        max_concurrency=6,
        # Big boards (7k reqs) are noticeably slower than board ATSes; the
        # existing code already uses a 15s HEAVY_REQUEST_TIMEOUT for ORC.
        timeout=15.0,
        detail_timeout=10.0,
        challenge_statuses=frozenset({429}),
        page_size=200,      # server honours large limits; fewer round trips
        max_pages=60,       # 60 * 200 = 12,000 reqs, above the largest seen
    )

    fingerprints = (
        # Canonical candidate-experience URL. Captures the site number, which is
        # required to call the API, so it outranks the bare-host pattern.
        fp(r"https?://(?P<host>[A-Za-z0-9.\-]*oraclecloud\.com)"
           r"/hcmUI/CandidateExperience/[A-Za-z\-]+/sites/(?P<site>[A-Za-z0-9_\-]+)",
           confidence=95),
        # REST endpoint linked directly (rare, but unambiguous).
        fp(r"https?://(?P<host>[A-Za-z0-9.\-]*oraclecloud\.com)"
           r"/hcmRestApi/resources/[^?]*recruitingCEJobRequisitions"
           r"[^\"']*siteNumber=(?P<site>[A-Za-z0-9_\-]+)",
           confidence=90),
        # Host only: proves ORC, site defaults to CX_1 (correct for the large
        # majority of deployments) and validation confirms or discards it.
        fp(r"https?://(?P<host>[A-Za-z0-9.\-]*\.fa\.[A-Za-z0-9\-]+\.oraclecloud\.com)",
           confidence=60),
    )

    # -- identity ------------------------------------------------------------

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        host = (fields.get("host") or "").strip().lower()
        if not host:
            return None
        site = (fields.get("site") or "").strip() or _DEFAULT_SITE
        if not _SITE_RE.match(site):
            return None
        # The pod hostname is the stable tenant identity: a company may expose
        # several sites (CX_1 external, CX_2 campus) on one pod, and each is its
        # own board.
        tenant = host.split(".")[0] or host
        return Endpoint(vendor=self.vendor, tenant=tenant, site=site, host=host)

    def board_url(self, endpoint: Endpoint) -> str:
        """Oracle's pod hostname is not derivable from a company name at all
        (`eeho.fa.us2.oraclecloud.com`), so without a stored host there is no
        board URL to give — and inventing one would fetch a stranger's page."""
        host = self._host_of(endpoint)
        if not host or host.startswith("."):
            return ""
        site = str(endpoint.site or "").strip() or "CX_1"
        return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}"

    @staticmethod
    def _host_of(endpoint: Endpoint) -> str:
        host = (endpoint.host or "").strip().lower()
        if host:
            return host
        # Legacy catalog rows carry the pod in `extra.host`.
        return str((endpoint.extra or {}).get("host") or "").strip().lower()

    @staticmethod
    def _site_of(endpoint: Endpoint) -> str:
        site = (endpoint.site or "").strip() or \
            str((endpoint.extra or {}).get("site_number") or "").strip() or _DEFAULT_SITE
        return site if _SITE_RE.match(site) else _DEFAULT_SITE

    def _list_url(self, host: str) -> str:
        return (f"https://{host}/hcmRestApi/resources/latest"
                f"/recruitingCEJobRequisitions")

    # -- listing -------------------------------------------------------------

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        host = self._host_of(endpoint)
        if not host:
            ctx.log("warning", "orc: endpoint %s has no host", endpoint.tenant)
            return
        site = self._site_of(endpoint)
        url = self._list_url(host)
        page_size = int(self.policy.page_size)

        keyword = str((ctx.options or {}).get("keyword") or "").strip()
        # A keyword narrows the board; only used by the interactive path. The
        # crawl leaves it empty so it sweeps newest-first.
        if "," in keyword or ";" in keyword:
            keyword = keyword.replace(",", " ").replace(";", " ")

        offset = 0
        total: Optional[int] = None
        pages = 0
        while pages < int(self.policy.max_pages):
            if ctx.should_stop():
                return
            finder = f"findReqs;siteNumber={site}"
            if keyword:
                finder += f",keyword={keyword}"
            finder += (f",limit={page_size},sortBy=POSTING_DATES_DESC"
                       f",offset={offset}")
            status, payload = ctx.get_json(
                url,
                params={"onlyData": "true",
                        "expand": "requisitionList.secondaryLocations",
                        "finder": finder},
                timeout=self.policy.timeout,
                vendor=self.vendor,
            )
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                return
            items = payload.get("items") or []
            if not items or not isinstance(items[0], dict):
                return
            block = items[0]
            if total is None:
                try:
                    total = int(block.get("TotalJobsCount") or 0)
                except (TypeError, ValueError):
                    total = 0
                ctx.log("info", "orc: %s site=%s total=%s", host, site, total)

            rows = [r for r in (block.get("requisitionList") or [])
                    if isinstance(r, dict)]
            if not rows:
                return

            crossed_cutoff = False
            for raw in rows:
                job = self._to_raw(endpoint, host, site, raw)
                if job is None:
                    continue
                # Newest-first: once we are past the freshness boundary every
                # later row is older too, so stop rather than page the archive.
                if (ctx.cutoff_iso and job.posted_at
                        and job.posted_at < ctx.cutoff_iso):
                    crossed_cutoff = True
                    break
                yield job
            if crossed_cutoff:
                return

            pages += 1
            offset += len(rows)
            if len(rows) < page_size:
                return
            if total is not None and offset >= total:
                return

    def _to_raw(self, endpoint: Endpoint, host: str, site: str,
                raw: Dict[str, Any]) -> Optional[RawJob]:
        rid = str(raw.get("Id") or "").strip()
        title = str(raw.get("Title") or "").strip()
        if not rid or not title:
            return None

        posted = str(raw.get("PostedDate") or "").strip()
        # ORC returns a bare date; normalise to an ISO instant so string
        # comparison against `cutoff_iso` is well-defined.
        posted_iso = f"{posted}T00:00:00+00:00" if re.match(r"^\d{4}-\d{2}-\d{2}$",
                                                            posted) else posted

        locations = [str(raw.get("PrimaryLocation") or "").strip()]
        for sec in (raw.get("secondaryLocations") or []):
            if isinstance(sec, dict):
                name = str(sec.get("Name") or sec.get("LocationName") or "").strip()
                if name:
                    locations.append(name)
        location = "; ".join([x for x in locations if x][:4])

        desc = " ".join(x for x in [
            str(raw.get("ExternalDescriptionStr") or ""),
            str(raw.get("ShortDescriptionStr") or ""),
            str(raw.get("ExternalResponsibilitiesStr") or ""),
            str(raw.get("ExternalQualificationsStr") or ""),
            str(raw.get("JobDescription") or ""),
        ] if x)

        workplace = str(raw.get("WorkplaceType") or "").strip().lower()
        remote: Optional[bool] = None
        if workplace:
            remote = "remote" in workplace or "virtual" in workplace

        return RawJob(
            source_job_id=rid,
            title=title,
            apply_url=(f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                       f"{site}/job/{rid}"),
            location=location,
            posted_at=posted_iso,
            date_confidence=DATE_EXACT if posted_iso else "unknown",
            raw_posted_text=posted,
            description=_clean_html(desc)[:20000],
            country=str(raw.get("PrimaryLocationCountry") or "").strip(),
            employment_type=str(raw.get("JobType")
                                or raw.get("JobSchedule") or "").strip(),
            department=str(raw.get("Department")
                           or raw.get("JobFamily") or "").strip(),
            remote=remote,
            # The listing already carries a usable body for most tenants; detail
            # is only fetched when it does not (see fetch_detail).
            detail_url=(f"https://{host}/hcmRestApi/resources/latest"
                        f"/recruitingCEJobRequisitionDetails"),
            extra={"site": site, "host": host,
                   "org": str(raw.get("Organization") or "")},
        )

    # -- detail --------------------------------------------------------------

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        """Full JD body. Only worth calling when the listing body was thin -
        `ShortDescriptionStr` is a teaser on many tenants."""
        host = self._host_of(endpoint)
        site = self._site_of(endpoint)
        if not host or not job.source_job_id:
            return None
        status, payload = ctx.get_json(
            f"https://{host}/hcmRestApi/resources/latest"
            f"/recruitingCEJobRequisitionDetails",
            params={"onlyData": "true", "expand": "all",
                    "finder": f'ById;Id="{job.source_job_id}",'
                              f"siteNumber={site}"},
            timeout=self.policy.detail_timeout,
            vendor=self.vendor,
        )
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not isinstance(payload, dict):
            return None
        items = payload.get("items") or []
        if not items or not isinstance(items[0], dict):
            return None
        d = items[0]
        body = " ".join(x for x in [
            str(d.get("ExternalDescriptionStr") or ""),
            str(d.get("ExternalResponsibilitiesStr") or ""),
            str(d.get("ExternalQualificationsStr") or ""),
            str(d.get("ShortDescriptionStr") or ""),
        ] if x)
        if not body:
            return None
        return RawJob(
            source_job_id=job.source_job_id,
            title=job.title,
            apply_url=job.apply_url,
            description=_clean_html(body)[:40000],
            location=str(d.get("PrimaryLocation") or "").strip(),
            employment_type=str(d.get("JobType") or "").strip(),
            compensation=d.get("Salary") or d.get("SalaryRange"),
        )

    # -- validation ----------------------------------------------------------

    def validate(self, endpoint: Endpoint,
                 ctx: FetchContext) -> ValidationResult:
        """Ask for one row and read `TotalJobsCount`.

        Cheaper than the base implementation (one request, `limit=1`) and it
        distinguishes the three outcomes that matter: a real board, a reachable
        board with nothing open, and an unreachable pod. 5xx is explicitly NOT
        fatal - Oracle pods return 503/504 when parked or briefly down, and
        deleting an endpoint on that basis would churn the catalog.
        """
        host = self._host_of(endpoint)
        if not host:
            return ValidationResult(STATUS_DEAD, detail="no host")
        site = self._site_of(endpoint)
        status, payload = ctx.get_json(
            self._list_url(host),
            params={"onlyData": "true",
                    # Without `expand`, `requisitionList` comes back empty and
                    # only the count survives — which validates fine but leaves
                    # no sample title to eyeball in a discovery report.
                    "expand": "requisitionList",
                    "finder": f"findReqs;siteNumber={site},limit=1,"
                              f"sortBy=POSTING_DATES_DESC,offset=0"},
            timeout=self.policy.timeout,
            vendor=self.vendor,
        )
        if self.policy.is_challenge(status):
            return ValidationResult(STATUS_BLOCKED, detail=f"status={status}")
        if status is not None and 500 <= int(status) < 600:
            return ValidationResult(STATUS_BLOCKED,
                                    detail=f"pod unavailable ({status})")
        if status != 200 or not isinstance(payload, dict):
            return ValidationResult(STATUS_DEAD, detail=f"status={status}")
        items = payload.get("items") or []
        if not items or not isinstance(items[0], dict):
            return ValidationResult(STATUS_EMPTY, detail="no items block")
        block = items[0]
        try:
            total = int(block.get("TotalJobsCount") or 0)
        except (TypeError, ValueError):
            total = 0
        rows = [r for r in (block.get("requisitionList") or [])
                if isinstance(r, dict)]
        if total <= 0 and not rows:
            return ValidationResult(STATUS_EMPTY, 0, detail="zero open reqs")
        return ValidationResult(STATUS_VALIDATED, total,
                                str(rows[0].get("Title") or "") if rows else "")


register(OracleOrcProvider())
