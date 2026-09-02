"""Phenom and Eightfold — enterprise career sites whose API lives on the
employer's own hostname, not a shared vendor host.

Why they are here
-----------------
The interactive career-pages fan-out already speaks both protocols
(`career_pages_service._fetch_phenom` / `_fetch_eightfold`) for a hand-maintained
catalog of eleven MNCs. Those rows never joined the background crawl: they are
not in `CAREER_PAGES_CATALOG`, and until this module they had no `AtsProvider`,
so `job_ingest_worker._load_career_members` skipped them. Registering them as
termless providers — empty keyword, client-side cutoff — is what makes US Bank,
United, Microsoft, Netflix, … show up in `job_postings` without a user typing
a search term.

This is a PORT of those two fetchers. Empty `keywords`/`query` replaces the
interactive term loop; paging is capped at `policy.max_pages` the same way
Workday is. Sort order is NOT promised (Phenom's `sortBy` is tenant-specific
and an empty value is what the live path sends), so the freshness boundary is
a ROW filter, not a paging stop. That is the same contract as Greenhouse.

SSRF
----
These APIs sit on first-party career domains (`careers.usbank.com/widgets`,
`apply.careers.microsoft.com/api/...`), not `*.phenompeople.com`. The
allowlist is therefore the verified ENTERPRISE_CATALOG hosts plus the two
vendor suffixes discovery can actually fingerprint. Custom domains that are
not in `api_hosts` stay unreachable until they are added here — that is
deliberate; an open `*.com` suffix would punch a hole in the crawler's
fail-closed guard.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlparse

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import (from_epoch, html_to_text, looks_remote,
                                         to_iso)

# Labels that are path/branding, not the employer slug, when we have to mint
# a tenant from a custom hostname (`apply.careers.microsoft.com` -> microsoft).
_HOST_SKIP = frozenset({
    "www", "apply", "careers", "jobs", "explore", "api", "cdn", "talent",
    "job", "career", "us", "en",
})

_ENTERPRISE_POLICY = CrawlPolicy(
    pacing_key=None,
    min_interval=0.25,
    jitter=0.10,
    max_concurrency=4,
    timeout=8.0,
    detail_timeout=6.0,
    page_size=20,
    max_pages=5,
)


def _tenant_from_host(host: str) -> str:
    parts = [p for p in (host or "").split(".")
             if p and p.lower() not in _HOST_SKIP]
    return parts[0].lower() if parts else ""


def _host_of(endpoint: Endpoint) -> str:
    return str(endpoint.host or "").strip().lower()


def _too_old(ctx: FetchContext, posted_at: str) -> bool:
    return bool(ctx.cutoff_iso and posted_at and posted_at < ctx.cutoff_iso)


# ---------------------------------------------------------------------------
# Phenom
# ---------------------------------------------------------------------------

class PhenomProvider(AtsProvider):
    """Phenom People widgets search.

    POST `{host}/widgets` with `ddoKey=refineSearch`. Verified live against the
    ENTERPRISE_CATALOG hosts (US Bank, United, Southwest, Lilly, Merck, Lowe's,
    Ecolab, Land O'Lakes). Empty `keywords` is the crawl path; the interactive
    path fills that field with a title term.
    """

    vendor = "phenom"
    label = "Phenom"
    termless = True
    has_detail = False
    date_confidence = DATE_EXACT
    slug_probe = False
    policy = _ENTERPRISE_POLICY

    api_hosts = (
        "careers.usbank.com",
        "careers.united.com",
        "careers.southwestair.com",
        "careers.lilly.com",
        "jobs.merck.com",
        "talent.lowes.com",
        "jobs.ecolab.com",
        "careers.landolakesinc.com",
        # From MNC_CATALOG, which the background crawl now loads. Verified
        # 2026-08-27: POST /widgets returns real postings.
        "careers.toyota.com",
        ".phenompeople.com",
    )
    portal_hosts = api_hosts

    #: Phenom's own hostnames under the shared domain. EVERY customer's
    #: careers page loads these, so a page-wide fingerprint yields them for
    #: every employer on the platform — Adobe, Cisco and Splunk each had all
    #: four registered as their own owner-verified career source.
    #:
    #: Declared per-vendor rather than in the shared `_RESERVED_TENANTS`
    #: because the shared list has to stay short enough to be safe for every
    #: vendor: `greenhouse:css` is CloudKitchens (City Storage Systems) and
    #: `greenhouse:manifest` is Manifest Cyber. No Phenom customer's tenant is
    #: `assets`.
    reserved_tenants = ("assets", "cdn", "cdn-bot", "pp-cdn",
                        "cdn-prod-static", "content-us", "content-ca",
                        "content-ir", "content-eu", "content-apac", "static")

    fingerprints = (
        # Shared Phenom host only. Custom career domains (careers.usbank.com)
        # are seeded from ENTERPRISE_CATALOG — a generic `/widgets` or
        # `/us/en/job/` pattern would mis-tag unrelated sites.
        fp(r"https?://(?P<tenant>[A-Za-z0-9][A-Za-z0-9\-]*?)\.phenompeople\.com",
           confidence=92),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        host = (fields.get("host") or "").strip().lower()
        if not host:
            host = (urlparse(url).hostname or "").strip().lower()
        tenant = (fields.get("tenant") or "").strip().lower() or _tenant_from_host(host)
        if not tenant or not host:
            return None
        if host in {"www.phenompeople.com", "cdn.phenompeople.com"}:
            return None
        # Every Phenom customer's careers page loads the SAME four asset
        # hosts, so fingerprinting a page's urls yields `assets`, `pp-cdn`,
        # `content-us` and `cdn-prod-static` for every one of them. Adobe,
        # Cisco and Splunk each registered all four as owner-verified career
        # sources before this check existed.
        if self.is_reserved_tenant(tenant):
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant, host=host)

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        if ctx.post_json is None:
            ctx.log("warning", "phenom: context has no post_json; %s skipped",
                    endpoint.tenant)
            return
        host = _host_of(endpoint)
        if not host:
            return
        size = int(self.policy.page_size)
        url = f"https://{host}/widgets"
        for page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                return
            body = {
                "lang": "en_us", "deviceType": "desktop", "country": "us",
                "pageName": "search-results", "ddoKey": "refineSearch",
                "sortBy": "", "from": page * size, "jobs": True, "counts": True,
                "all_fields": ["category", "country", "state", "city", "type"],
                "size": size, "keywords": "", "global": True,
                # No server-side country filter: tenants disagree on the value
                # ("United States" vs "United States of America") and a miss
                # silently returns 0 hits. The row's own country field is kept
                # for the US filter downstream.
                "selected_fields": {},
                "locationData": {},
            }
            status, payload = ctx.post_json(
                url, json_body=body, timeout=self.policy.timeout,
                vendor=self.vendor)
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                return
            data = ((payload.get("refineSearch") or {}).get("data") or {})
            jobs = [j for j in (data.get("jobs") or []) if isinstance(j, dict)]
            if not jobs:
                return
            for job in jobs:
                if ctx.should_stop():
                    return
                raw = self._to_raw(host, job)
                if raw is None:
                    continue
                if _too_old(ctx, raw.posted_at):
                    continue
                yield raw
            if len(jobs) < size:
                return

    @staticmethod
    def _to_raw(host: str, job: Dict[str, Any]) -> Optional[RawJob]:
        title = str(job.get("title") or "").strip()
        rid = str(job.get("jobSeqNo") or job.get("jobId")
                  or job.get("reqId") or "").strip()
        if not rid or not title:
            return None
        loc = str(job.get("cityState") or "").strip() or ", ".join(
            x for x in [job.get("city"), job.get("state")] if x)
        posted = to_iso(job.get("postedDate") or job.get("dateCreated"))
        apply_url = str(job.get("applyUrl") or job.get("externalApply") or "")
        if not apply_url:
            apply_url = f"https://{host}/us/en/job/{rid}"
        return RawJob(
            source_job_id=rid,
            title=title,
            apply_url=apply_url,
            location=loc,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            description=html_to_text(job.get("descriptionTeaser") or "")[:2000],
            country=str(job.get("country") or ""),
            employment_type=str(job.get("type") or job.get("jobType") or ""),
            remote=looks_remote(title, loc),
        )


# ---------------------------------------------------------------------------
# Eightfold
# ---------------------------------------------------------------------------

class EightfoldProvider(AtsProvider):
    """Eightfold apply API (classic `/api/apply/v2/jobs` and PCSX search).

    Microsoft is the PCSX variant (`apply.careers.microsoft.com`); Netflix is
    classic. `extra.domain` and `extra.variant` are copied from ENTERPRISE_CATALOG
    onto the endpoint so listing can pick the right URL without guessing.
    """

    vendor = "eightfold"
    label = "Eightfold"
    termless = True
    has_detail = True
    date_confidence = DATE_EXACT
    slug_probe = False
    policy = _ENTERPRISE_POLICY

    api_hosts = (
        "apply.careers.microsoft.com",
        "explore.jobs.netflix.net",
        # From MNC_CATALOG, which the background crawl now loads. Verified
        # 2026-08-27: the pcsx search returns 710 US openings.
        "careers.qualcomm.com",
        ".eightfold.ai",
    )
    portal_hosts = api_hosts

    fingerprints = (
        fp(r"https?://(?P<tenant>[A-Za-z0-9][A-Za-z0-9\-]*?)\.eightfold\.ai",
           confidence=92),
        fp(r"https?://(?P<host>[A-Za-z0-9.-]+)/api/apply/v2/jobs",
           confidence=88),
        fp(r"https?://(?P<host>[A-Za-z0-9.-]+)/api/pcsx/search",
           confidence=88),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        host = (fields.get("host") or "").strip().lower()
        if not host:
            host = (urlparse(url).hostname or "").strip().lower()
        tenant = (fields.get("tenant") or "").strip().lower() or _tenant_from_host(host)
        if not tenant or not host:
            return None
        extra: Dict[str, Any] = {}
        if "/api/pcsx/" in (url or ""):
            extra["variant"] = "pcsx"
        return Endpoint(vendor=self.vendor, tenant=tenant, host=host,
                        extra=extra)

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        host = _host_of(endpoint)
        if not host:
            return
        extra = endpoint.extra or {}
        domain = str(extra.get("domain") or "")
        variant = str(extra.get("variant") or "classic")
        size = int(self.policy.page_size)
        for page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                return
            start = page * size
            if variant == "pcsx":
                url = f"https://{host}/api/pcsx/search"
                params = {"domain": domain, "query": "",
                          "location": "United States", "start": start}
            else:
                url = f"https://{host}/api/apply/v2/jobs"
                params = {"domain": domain, "query": "",
                          "location": "United States", "start": start,
                          "num": size}
            status, payload = ctx.get_json(
                url, params=params, timeout=self.policy.timeout,
                vendor=self.vendor)
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                return
            positions = self._positions(payload, variant)
            if not positions:
                return
            for job in positions:
                if ctx.should_stop():
                    return
                raw = self._to_raw(host, domain, job)
                if raw is None:
                    continue
                if _too_old(ctx, raw.posted_at):
                    continue
                yield raw
            if len(positions) < size:
                return

    @staticmethod
    def _positions(payload: Dict[str, Any], variant: str) -> List[Dict[str, Any]]:
        if variant == "pcsx":
            rows = ((payload.get("data") or {}).get("positions") or [])
        else:
            rows = payload.get("positions") or []
        return [r for r in rows if isinstance(r, dict)]

    @staticmethod
    def _to_raw(host: str, domain: str, job: Dict[str, Any]) -> Optional[RawJob]:
        title = str(job.get("name") or job.get("title") or "").strip()
        rid = str(job.get("id") or "").strip()
        if not rid or not title:
            return None
        locs = job.get("locations") or (
            [job.get("location")] if job.get("location") else [])
        loc = "; ".join(str(x) for x in locs[:3] if x)
        # postedTs / creationTs only — t_update/t_create are record-edit times.
        posted = from_epoch(job.get("postedTs") or job.get("creationTs"))
        apply_url = str(job.get("canonicalPositionUrl") or "")
        if not apply_url:
            suffix = f"?domain={domain}" if domain else ""
            apply_url = f"https://{host}/careers/job/{rid}{suffix}"
        return RawJob(
            source_job_id=rid,
            title=title,
            apply_url=apply_url,
            location=loc,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            remote=looks_remote(title, loc),
            detail_url=(f"https://{host}/api/apply/v2/jobs/{rid}"),
        )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        extra = endpoint.extra or {}
        if str(extra.get("variant") or "") == "pcsx":
            # PCSX tenants have no anonymous detail JSON (career_pages_service
            # documents this). The listing teaser is all we get.
            return None
        host = _host_of(endpoint)
        domain = str(extra.get("domain") or "")
        rid = job.source_job_id
        if not host or not rid:
            return None
        url = job.detail_url or f"https://{host}/api/apply/v2/jobs/{rid}"
        status, payload = ctx.get_json(
            url, params={"domain": domain} if domain else {},
            timeout=self.policy.detail_timeout, vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not isinstance(payload, dict):
            return None
        body = html_to_text(payload.get("job_description") or "")[:5000]
        if not body:
            return None
        return RawJob(source_job_id=rid, title=job.title,
                      apply_url=job.apply_url, description=body)


register(PhenomProvider())
register(EightfoldProvider())
