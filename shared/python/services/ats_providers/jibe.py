"""Jibe (iCIMS Talent Cloud) public career search - `<client>.jibeapply.com`.

Observed live 2026-10-22 on EchoStar: echostar.com/careers links
`jobs.echostar.com/jobs`, whose HTML declares canonical
`https://dish.jibeapply.com/jobs` and loads the Jibe search app
(`app.jibecdn.com/prod/search/...`, client assets under `/prod/dish/`). That
app lists postings with

    GET /api/jobs?page=<n>&internal=false        (same origin)
    -> {"jobs": [{"data": {...}}], "totalCount": 656, "count": 656, ...}

10 rows a page, newest first by `posted_date`. Each row carries `req_id`,
`slug`, `client_code` ("dish"), title, full description, qualifications,
responsibilities, city/state/country, `posted_date` (exact),
`employment_type`, `hiring_organization` ("EchoStar") and the downstream
iCIMS `apply_url` (`attract-careers1-echostar.icims.com/jobs/<req>/login`).
The canonical host answers the same call. `internal=false` is what the
public app sends; internal postings are never requested.

The tenant is the Jibe client code; requests go to `<tenant>.jibeapply.com`.
`api/jobs` is the only call made - no detail request is needed.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, Optional

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import (html_to_text, looks_minted_now,
                                         looks_remote, to_iso)

VENDOR = "jibe"
_SUFFIX = ".jibeapply.com"
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_PAGE = 10


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


class JibeProvider(AtsProvider):
    vendor = VENDOR
    label = "iCIMS Jibe"

    termless = True
    has_detail = False
    date_confidence = DATE_EXACT
    slug_probe = False

    api_hosts = (_SUFFIX,)
    portal_hosts = (_SUFFIX,)
    reserved_tenants = ("app", "assets", "www", "api")

    policy = CrawlPolicy(
        pacing_key="jibeapply.com", min_interval=0.5, jitter=0.2, max_concurrency=1,
        timeout=20.0, detail_timeout=15.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=_PAGE, max_pages=120, stale_page_tolerance=1,
    )

    fingerprints = (
        fp(r"https?://(?P<tenant>[a-z0-9][a-z0-9-]{0,40})\.jibeapply\.com(?:[/?#\"'\s]|$)",
           confidence=92),
        # The client asset path the branded frontend loads names the tenant.
        fp(r"https?://assets\.jibecdn\.com/prod/(?P<tenant>[a-z0-9][a-z0-9-]{0,40})/",
           confidence=60),
    )

    def endpoint_from_match(self, fields: Dict[str, str], url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip().lower()
        if not _TENANT_RE.match(tenant) or self.is_reserved_tenant(tenant):
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant, host=f"{tenant}{_SUFFIX}")

    def board_url(self, endpoint: Endpoint) -> str:
        tenant = str(endpoint.tenant or "").strip().lower()
        return f"https://{tenant}{_SUFFIX}/jobs" if _TENANT_RE.match(tenant) else ""

    def list_jobs(self, endpoint: Endpoint, ctx: FetchContext) -> Iterator[RawJob]:
        tenant = str(endpoint.tenant or "").strip().lower()
        if not _TENANT_RE.match(tenant) or self.is_reserved_tenant(tenant):
            return
        api = f"https://{tenant}{_SUFFIX}/api/jobs"
        seen = set()
        total: Optional[int] = None
        stale_pages = 0
        for page in range(1, int(self.policy.max_pages) + 1):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            status, payload = ctx.get_json(api, params={"page": page, "internal": "false"},
                                           timeout=self.policy.timeout, vendor=self.vendor)
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict) or \
                    not isinstance(payload.get("jobs"), list):
                ctx.listing_truncated = True
                return
            try:
                total = int(payload.get("totalCount"))
            except (TypeError, ValueError):
                pass
            rows = payload["jobs"]
            fresh = 0
            for item in rows:
                job = self._to_raw(tenant, item)
                if job is None or job.source_job_id in seen:
                    continue
                seen.add(job.source_job_id)
                if ctx.cutoff_iso and job.posted_at and job.posted_at < ctx.cutoff_iso:
                    continue
                fresh += 1
                yield job
            if not rows or len(rows) < _PAGE or (total is not None and len(seen) >= total):
                if total is not None and len(seen) < total and not ctx.cutoff_iso:
                    ctx.listing_truncated = True
                return
            if ctx.cutoff_iso and not fresh:
                stale_pages += 1
                if stale_pages > int(self.policy.stale_page_tolerance):
                    return                       # newest first: the rest are older
            else:
                stale_pages = 0
        ctx.listing_truncated = True

    @staticmethod
    def _to_raw(tenant: str, item: Any) -> Optional[RawJob]:
        data = item.get("data") if isinstance(item, dict) else None
        if not isinstance(data, dict):
            return None
        if data.get("internal") is True and data.get("external") is False:
            return None
        rid = _text(data.get("req_id") or data.get("slug"))
        title = _text(data.get("title"))
        if not re.match(r"^[A-Za-z0-9_-]{1,40}$", rid) or not title:
            return None
        raw_posted = data.get("posted_date") or data.get("create_date")
        posted = "" if (not raw_posted or looks_minted_now(raw_posted)) else to_iso(raw_posted)
        location = _text(data.get("full_location") or ", ".join(
            x for x in (_text(data.get("city")), _text(data.get("state"))) if x))
        parts = []
        for key, heading in (("description", ""), ("responsibilities", "Responsibilities"),
                             ("qualifications", "Qualifications")):
            body = html_to_text(data.get(key) or "").strip()
            if body:
                parts.append(f"{heading}\n{body}" if heading else body)
        apply_url = _text(data.get("apply_url"))
        slug = _text(data.get("slug")) or rid
        extra = {"application_method": "ats_url", "jibe_client": _text(data.get("client_code")),
                 "posting_url": f"https://{tenant}{_SUFFIX}/jobs/{slug}"}
        org = _text(data.get("hiring_organization"))
        if org:
            extra["hiring_organization"] = org
        comp = None
        if data.get("salary_min_value") or data.get("salary_max_value"):
            comp = {"min": data.get("salary_min_value"), "max": data.get("salary_max_value")}
        return RawJob(
            source_job_id=rid, title=title[:200],
            apply_url=apply_url if apply_url.startswith("https://") else extra["posting_url"],
            location=location[:200], country=_text(data.get("country")),
            posted_at=posted, date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            raw_posted_text=_text(raw_posted),
            description="\n\n".join(parts)[:15000],
            employment_type=_text(data.get("employment_type")),
            department=_text(data.get("department"))[:120],
            compensation=comp,
            remote=looks_remote(title, location, _text(data.get("location_type"))),
            extra=extra,
        )

    def board_owner(self, endpoint: Endpoint, ctx: FetchContext) -> Optional[str]:
        return None


register(JibeProvider())
