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

import json
import re
from dataclasses import replace as _dc_replace
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
    # 4,000 rows covers the measured 2,000+ Phenom boards. The caller's
    # deadline still bounds work, and an unreached total is always truncated.
    max_pages=200,
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
    #: Since 2026-11-17. The listing carries only `descriptionTeaser` (a few
    #: hundred characters), which scored every Phenom posting as having no
    #: readable requirements. The job page (`/<region>/<lang>/job/<jobId>/...`)
    #: embeds a schema.org JobPosting with the full body (TJX: 5,457 chars).
    has_detail = True
    date_confidence = DATE_EXACT
    slug_probe = False
    #: 200 rows a page, not 20. The termless search is in relevance order (no
    #: newest-first sort the API honours), so the whole board is read to find
    #: the fresh rows, and at 20 a page TJX's 9,777 rows took 489 requests:
    #: its shard hit the 300 s budget on 2026-11-17 and stored nothing. Phenom
    #: answers size=200 in under half a second (TJX, measured). A tenant that
    #: caps the page lower is still read to its reported total, because paging
    #: advances by the rows each page actually returned.
    policy = _dc_replace(_ENTERPRISE_POLICY, page_size=200, max_pages=60)

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
        # Official Phenom front ends, 2026-10-21 onboarding (see
        # docs/audit/direct-employer-onboarding.md). Named hosts only.
        "careers.acrisure.com",
        "jobs.baesystems.com",
        "careers.snowflake.com",
        "careers.abb",
        "jobs.bechtel.com",
        # Official BCG careers page and public /widgets, checked 2026-11-05.
        "careers.bcg.com",
        # Workday employers' OWN Phenom sites (2026-11-17). Each is linked from
        # the employer's homepage and answers POST /widgets; they front the
        # Workday boards Cloudflare now challenges from our box, and are read
        # with `requisition_ids` so their cards merge with the Workday copies.
        "jobs.tjx.com",
        "careers.rtx.com",
        "jobs.cvshealth.com",
        "careers.davita.com",
        "jobs.thermofisher.com",
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
        ctx.listing_truncated = True
        if ctx.post_json is None:
            ctx.log("warning", "phenom: context has no post_json; %s skipped",
                    endpoint.tenant)
            return
        host = _host_of(endpoint)
        if not host:
            return
        size = int(self.policy.page_size)
        url = f"https://{host}/widgets"
        # A Phenom site in front of the employer's ATS can report that ATS's
        # requisition number (`reqId`) as the posting id instead of Phenom's
        # `jobSeqNo`. With the endpoint's tenant set to the ATS tenant, the
        # feed's (tenant, requisition) key then merges this copy with the ATS
        # copy instead of showing the job twice. Off for existing boards: their
        # stored ids are jobSeqNo and must not change under them.
        req_ids = bool((endpoint.extra or {}).get("requisition_ids"))
        seen: set = set()
        overlap = False
        total: Optional[int] = None
        read_count = 0
        for page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            body = {
                "lang": "en_us", "deviceType": "desktop", "country": "us",
                "pageName": "search-results", "ddoKey": "refineSearch",
                "sortBy": "", "from": read_count, "jobs": True, "counts": True,
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
            refine = payload.get("refineSearch") or {}
            data = (refine.get("data") or {}) if isinstance(refine, dict) else {}
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                return
            if any(not isinstance(row, dict) for row in data["jobs"]):
                return
            try:
                reported = int(refine["totalHits"])
                if reported >= 0:
                    total = max(total or 0, reported)
            except (KeyError, TypeError, ValueError):
                pass
            jobs = [j for j in data["jobs"] if isinstance(j, dict)]
            if not jobs:
                ctx.listing_truncated = len(seen) < total if total is not None else overlap or not seen
                return
            previous_count = len(seen)
            for job in jobs:
                if ctx.should_stop():
                    ctx.listing_truncated = True
                    return
                raw = self._to_raw(host, job, req_ids=req_ids)
                if raw is None or raw.source_job_id in seen:
                    overlap = True
                    # A row repeated across pages (the result set shifted
                    # while we paged) is yielded once.
                    continue
                seen.add(raw.source_job_id)
                if _too_old(ctx, raw.posted_at):
                    continue
                yield raw
            read_count += len(jobs)
            # A short page ends the board only when no total says otherwise:
            # a tenant that caps `size` below ours returns short pages all the
            # way down, and `from` advances by what each page returned.
            if len(jobs) < size and (total is None or read_count >= total):
                ctx.listing_truncated = len(seen) < total if total is not None else overlap
                return
            if total is not None and read_count >= total:
                ctx.listing_truncated = len(seen) < total
                return
            if len(seen) == previous_count:
                return
        # Fell out of the loop on the page cap with the board not exhausted.
        # Saying so matters: `_close_stale_in_shard` treats an untruncated
        # listing as a complete census and closes everything it did not see,
        # so a capped Phenom read (100 rows) used to close live postings on
        # any board larger than that. Measured 2026-10-21: BAE Systems and
        # Acrisure each returned exactly 100 rows, "complete".
        ctx.listing_truncated = True

    @staticmethod
    def _to_raw(host: str, job: Dict[str, Any], *,
                req_ids: bool = False) -> Optional[RawJob]:
        title = str(job.get("title") or "").strip()
        if req_ids:
            rid = str(job.get("reqId") or job.get("jobId")
                      or job.get("jobSeqNo") or "").strip()
        else:
            rid = str(job.get("jobSeqNo") or job.get("jobId")
                      or job.get("reqId") or "").strip()
        if not rid or not title or looks_like_test_posting(title):
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
            detail_url=_phenom_job_page(host, job, title),
            location=loc,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            description=html_to_text(job.get("descriptionTeaser") or "")[:2000],
            country=str(job.get("country") or ""),
            employment_type=str(job.get("type") or job.get("jobType") or ""),
            remote=looks_remote(title, loc),
        )


    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        """The full body from the job page's schema.org JobPosting."""
        url = job.detail_url
        if not url or not url.lower().startswith("https://"):
            return None
        posting = None
        # Phenom sites disagree on the job-page path: TJX and CVS serve
        # `/<region>/<lang>/job/<id>/<slug>`, DaVita only `/job/<id>`
        # (2026-11-17: the first form is a 404 there). A 404 on the first form
        # is therefore not "this job is gone"; the short form is asked next.
        for candidate in _phenom_page_candidates(url):
            status, body = ctx.get_text(candidate, params={},
                                        timeout=self.policy.detail_timeout,
                                        vendor=self.vendor)
            if self.policy.is_challenge(status, body or ""):
                raise ChallengeError(self.vendor, status)
            if status == 200 and body:
                posting = job_posting_ld(body)
                if posting:
                    break
        text = html_to_text(str((posting or {}).get("description") or ""))[:15000]
        if not text:
            return None
        posted = to_iso((posting or {}).get("datePosted") or "")
        return RawJob(source_job_id=job.source_job_id, title=job.title,
                      apply_url=job.apply_url, description=text,
                      posted_at=posted or job.posted_at,
                      date_confidence=DATE_EXACT if (posted or job.posted_at) else DATE_UNKNOWN)


def _phenom_page_candidates(url: str) -> List[str]:
    """The stored job-page URL, then the same job at `/job/<id>` on its host."""
    out = [url]
    m = re.match(r"(https://[^/]+)/(?:[a-z]{2,8}/[a-z]{2}/)?job/([A-Za-z0-9_.-]+)", url)
    if m:
        short = f"{m.group(1)}/job/{m.group(2)}"
        if short != url:
            out.append(short)
    return out


def _phenom_job_page(host: str, job: Dict[str, Any], title: str) -> str:
    """`https://<host>/<region>/<lang>/job/<jobId>/<slug>` - the page Phenom
    itself links each search result to. `locale` is "en_GLOBAL" / "en_US"."""
    job_id = str(job.get("jobId") or job.get("reqId") or "").strip()
    if not host or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", job_id):
        return ""
    lang, _, region = str(job.get("locale") or "en_US").partition("_")
    lang = lang.lower() if re.fullmatch(r"[A-Za-z]{2}", lang) else "en"
    region = region.lower() if re.fullmatch(r"[A-Za-z]{2,8}", region) else "us"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:120] or "job"
    return f"https://{host}/{region}/{lang}/job/{job_id}/{slug}"


#: Placeholder rows some boards publish to test their own pipeline
#: (jobs.thermofisher.com, 2026-11-17: "SA TEST OCT 15 1", "sourcing agent test
#: oct 15"). Narrow on purpose: "Test Engineer" and "Test Technician" are jobs.
_TEST_POSTING = re.compile(
    r"^\s*(?:sa\s+|sourcing\s+agent\s+)?test(?:ing)?\s+(?:job|posting|req(?:uisition)?|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b"
    r"|^\s*(?:do\s+not\s+apply|dummy\s+(?:job|posting))\b",
    re.I)


def looks_like_test_posting(title: str) -> bool:
    return bool(_TEST_POSTING.search(str(title or "")))


def job_posting_ld(html: str) -> Optional[Dict[str, Any]]:
    """The first schema.org JobPosting in a page's JSON-LD, or None."""
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
                         html or "", re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else (data.get("@graph") or [data]
                                                     if isinstance(data, dict) else [])
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


# ---------------------------------------------------------------------------
# Eightfold
# ---------------------------------------------------------------------------

#: Eightfold's own policy, and the ONE number in it that matters is `page_size`.
#:
#: The shared `_ENTERPRISE_POLICY` says 20, which is what the interactive
#: fetcher asks for (`num=20`) and NOT what the API returns. Measured live
#: 2026-09-15 on three tenants: every page comes back with exactly TEN
#: positions, `num` is ignored (`num=20` and `num=50` both yield 10) and
#: `start` is a row offset that pages cleanly (start=0/10/20/30 share no ids).
#: `list_jobs` stops on `len(positions) < size`, so 10 < 20 was true on the
#: FIRST page of every Eightfold employer we have and the crawl took one page
#: and left. That is the whole explanation for the live inventory:
#:
#:     Microsoft   29 active postings stored / 1,178 US published
#:     Qualcomm     4                        /   741
#:     Netflix     10                        /   204
#:     Lockheed    19                        / (pcsx, same shape)
#:
#: Those employers were not thin and they were not blocked. They were read
#: once. `max_pages` is raised to match. 150 was the first try and Lockheed
#: Martin stopped dead on 1,500 rows — exactly 150 x 10 — which is a truncation
#: wearing a round number, so the ceiling is 260: it clears Lockheed's ~2,195
#: and Microsoft's 1,178, and the short-page stop still ends a small board on
#: its own (Netflix finishes in 21 pages).
_EIGHTFOLD_POLICY = _dc_replace(_ENTERPRISE_POLICY, page_size=10, max_pages=260)


class EightfoldProvider(AtsProvider):
    """Eightfold apply API (classic `/api/apply/v2/jobs` and PCSX search).

    Microsoft is the PCSX variant (`apply.careers.microsoft.com`); Netflix is
    classic. `extra.domain` and `extra.variant` are copied from ENTERPRISE_CATALOG
    onto the endpoint so listing can pick the right URL without guessing.
    """

    vendor = "eightfold"
    detail_revision = 2          # route fixed 2026-11-03
    label = "Eightfold"
    termless = True
    has_detail = True
    # Classic tenants can publish no posting timestamp. Individual rows with
    # postedTs/creationTs still upgrade to exact in _to_raw.
    date_confidence = DATE_UNKNOWN
    slug_probe = False
    policy = _EIGHTFOLD_POLICY

    api_hosts = (
        "apply.careers.microsoft.com",
        "explore.jobs.netflix.net",
        # From MNC_CATALOG, which the background crawl now loads. Verified
        # 2026-08-27: the pcsx search returns 710 US openings.
        "careers.qualcomm.com",
        # Northrop Grumman's public search, linked from northropgrumman.com/
        # careers (2026-11-17); PCSX with domain ngc.com, 3,709 postings.
        "jobs.northropgrumman.com",
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
        ctx.listing_truncated = True
        host = _host_of(endpoint)
        if not host:
            return
        extra = endpoint.extra or {}
        domain = str(extra.get("domain") or "")
        variant = str(extra.get("variant") or "classic")
        size = int(self.policy.page_size)
        total: Optional[int] = None
        seen: set = set()
        overlap = False
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
            block = payload.get("data") if variant == "pcsx" else payload
            if not isinstance(block, dict) or not isinstance(block.get("positions"), list):
                return
            if any(not isinstance(row, dict) for row in block["positions"]):
                return
            try:
                reported = int(block["count"])
                if reported >= 0:
                    total = max(total or 0, reported)
            except (KeyError, TypeError, ValueError):
                pass
            positions = self._positions(payload, variant)
            if not positions:
                ctx.listing_truncated = len(seen) < total if total is not None else overlap or not seen
                return
            previous_count = len(seen)
            for job in positions:
                if ctx.should_stop():
                    return
                raw = self._to_raw(host, domain, job,
                                   req_ids=bool(extra.get("requisition_ids")))
                if raw is None or raw.source_job_id in seen:
                    overlap = True
                    continue
                seen.add(raw.source_job_id)
                if _too_old(ctx, raw.posted_at):
                    continue
                yield raw
            if len(positions) < size:
                ctx.listing_truncated = len(seen) < total if total is not None else overlap
                return
            if total is not None and len(seen) >= total:
                ctx.listing_truncated = False
                return
            if len(seen) == previous_count:
                return

    @staticmethod
    def _positions(payload: Dict[str, Any], variant: str) -> List[Dict[str, Any]]:
        if variant == "pcsx":
            rows = ((payload.get("data") or {}).get("positions") or [])
        else:
            rows = payload.get("positions") or []
        return [r for r in rows if isinstance(r, dict)]

    @staticmethod
    def _to_raw(host: str, domain: str, job: Dict[str, Any], *,
                req_ids: bool = False) -> Optional[RawJob]:
        title = str(job.get("name") or job.get("title") or "").strip()
        # Eightfold's own position id addresses the page and the detail API.
        position_id = str(job.get("id") or "").strip()
        # With `requisition_ids`, the posting id is the employer ATS's number
        # (`atsJobId`, e.g. Northrop's Workday "R10261033"), so the card merges
        # with that ATS's copy (see Phenom `requisition_ids`).
        rid = (str(job.get("atsJobId") or job.get("displayJobId") or "").strip()
               if req_ids else "") or position_id
        if not rid or not position_id or not title:
            return None
        locs = job.get("locations") or (
            [job.get("location")] if job.get("location") else [])
        loc = "; ".join(str(x) for x in locs[:3] if x)
        # postedTs / creationTs only — t_update/t_create are record-edit times.
        posted = from_epoch(job.get("postedTs") or job.get("creationTs"))
        apply_url = str(job.get("canonicalPositionUrl") or "")
        if not apply_url:
            suffix = f"?domain={domain}" if domain else ""
            apply_url = f"https://{host}/careers/job/{position_id}{suffix}"
        return RawJob(
            source_job_id=rid,
            title=title,
            apply_url=apply_url,
            location=loc,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            remote=looks_remote(title, loc),
            detail_url=(f"https://{host}/api/apply/v2/jobs/{position_id}"),
        )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        extra = endpoint.extra or {}
        # PCSX tenants (Microsoft, Lockheed Martin, Qualcomm) were skipped here
        # as having "no anonymous detail JSON". Re-probed 2026-11-03: they serve
        # the classic `/api/apply/v2/jobs/{id}` too (7,103 / 6,889 / 7,412
        # chars on one live posting each), and `/api/pcsx/position_details`
        # carries the same body as `jobDescription`. 539 active postings had
        # only their listing teaser because of the skip.
        pcsx = str(extra.get("variant") or "") == "pcsx"
        host = _host_of(endpoint)
        domain = str(extra.get("domain") or "")
        rid = job.source_job_id
        # The detail API is addressed by Eightfold's position id, which is the
        # last segment of `detail_url` - not always the posting id (see
        # `requisition_ids` in `_to_raw`).
        m = re.search(r"/api/apply/v2/jobs/([A-Za-z0-9_-]+)", job.detail_url or "")
        position_id = m.group(1) if m else rid
        if not host or not position_id:
            return None
        params = {"domain": domain} if domain else {}
        url = job.detail_url or f"https://{host}/api/apply/v2/jobs/{position_id}"
        status, payload = ctx.get_json(
            url, params=params, timeout=self.policy.detail_timeout, vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        body = ""
        if status == 200 and isinstance(payload, dict):
            body = html_to_text(payload.get("job_description") or "")
        if not body and pcsx:
            status, payload = ctx.get_json(
                f"https://{host}/api/pcsx/position_details",
                params=dict(params, position_id=position_id, hl="en"),
                timeout=self.policy.detail_timeout, vendor=self.vendor)
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            data = (payload or {}).get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                body = html_to_text(data.get("jobDescription") or "")
        body = body[:15000]
        if not body:
            return None
        return RawJob(source_job_id=rid, title=job.title,
                      apply_url=job.apply_url, description=body)


register(PhenomProvider())
register(EightfoldProvider())
