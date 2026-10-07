"""Radancy (formerly Jibe / TMP Worldwide) Talent Acquisition Cloud.

The employer's careers site is the API. Radancy serves the same JSON contract
from every tenant's own hostname:

    GET https://{host}/api/jobs?page=1&limit=100&sortBy=posted_date&descending=true
    -> {"jobs": [{"data": {...}}], "totalCount": 1205, "count": 1205, ...}

Verified live against `careers.amd.com` on 2026-08-27. Four properties decided
how this provider is written and none of them are guessable:

* **`sortBy=posted_date&descending=true` is a REAL global sort.** Page 1 came
  back 2026-08-27, page 2 2026-08-24, page 13 2025-10. So unlike Workday - whose
  termless board is ordered only within blocks - the cheap "stop at the first
  page with nothing fresh" rule is correct here, and `stale_page_tolerance`
  stays 0.
* **`limit` caps at 100** (`limit=200` is a hard 422), and `page` is 1-based,
  not an offset.
* **The listing carries the whole posting** - `description`, `qualifications`
  and `responsibilities` are all in the search payload - so `has_detail` is
  False and a second request per posting would buy nothing.
* **`posted_date` is an exact ISO timestamp**, so these rows are DATE_EXACT.

Why AMD is here rather than on iCIMS
------------------------------------
AMD's careers page still links `careers-amd.icims.com`, and validating that
endpoint through `IcimsProvider` returns a redirect stub reading "migrated to
https://careers.amd.com/jobs" - a live example of catalog decay, and exactly the
failure `REVALIDATE_AFTER_DAYS` exists to catch. The iCIMS host answers 200 and
serves zero jobs; the Radancy host serves 1,205. Note the migration is only
partial: Radancy rows still carry `apply_url` pointing back into iCIMS, because
Radancy is the search layer over an ATS rather than the ATS itself. That is why
`apply_url` is preferred over a constructed URL below - it is where a candidate
actually applies.

SSRF
----
Every tenant is a first-party careers domain, so there is no shared vendor
suffix to allowlist. `api_hosts` is therefore the list of VERIFIED tenants, the
same posture `enterprise.py` takes for Phenom and Eightfold: a new tenant is
added here after it is validated, and an unlisted host stays unreachable. An
open suffix would punch a hole in the crawler's fail-closed guard.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List, Optional

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote, to_iso


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _host_of(endpoint: Endpoint) -> str:
    return str(endpoint.host or "").strip().lower()


class RadancyProvider(AtsProvider):
    vendor = "radancy"
    label = "Radancy"

    termless = True
    has_detail = False
    date_confidence = DATE_EXACT
    # Every tenant is its own first-party domain, so a slug guess is a DNS
    # lookup against a name we invented rather than a cheap 404 on a shared
    # host. Discovery reaches these by fingerprinting the careers page.
    slug_probe = False

    #: Verified tenants only - see the SSRF note in the module docstring.
    # careers.rivian.com: probed live 2026-10-07, the same /api/jobs contract,
    # totalCount 819. Rivian left iCIMS (see the seed note in the worker).
    api_hosts = ("careers.amd.com", "careers.rivian.com")
    portal_hosts = api_hosts

    policy = CrawlPolicy(
        pacing_key=None,
        min_interval=0.35,
        jitter=0.10,
        max_concurrency=4,
        timeout=10.0,
        detail_timeout=6.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=100,
        max_pages=_env_int("RADANCY_MAX_LISTING_PAGES", 12),
        # The sort is genuinely global (see the docstring), so one page with
        # nothing fresh really does mean the rest are older.
        stale_page_tolerance=0,
    )

    fingerprints = (
        # The API path is the only unambiguous marker: the careers pages
        # themselves are ordinary first-party domains with no vendor branding.
        fp(r"https?://(?P<host>[A-Za-z0-9.\-]+)/api/jobs\?[^\"']*sortBy=posted_date",
           confidence=90),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        host = (fields.get("host") or "").strip().lower()
        if not host:
            return None
        tenant = host.split(".")[1] if host.count(".") >= 2 else host.split(".")[0]
        return Endpoint(vendor=self.vendor, tenant=tenant, site="", host=host)

    # -- listing -------------------------------------------------------------

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        host = _host_of(endpoint)
        if not host:
            return
        size = int(self.policy.page_size)
        tolerance = max(0, int(getattr(self.policy, "stale_page_tolerance", 0)))
        keyword = str((ctx.options or {}).get("keyword") or "").strip()
        stale_streak = 0

        # `page` is 1-based here, not an offset. Getting that wrong silently
        # re-reads page 1 forever rather than erroring.
        for page in range(1, max(2, int(self.policy.max_pages) + 1)):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            params: Dict[str, Any] = {
                "page": page,
                "limit": size,
                "sortBy": "posted_date",
                "descending": "true",
            }
            if keyword:
                params["keywords"] = keyword
            status, payload = ctx.get_json(f"https://{host}/api/jobs",
                                           params=params,
                                           timeout=self.policy.timeout,
                                           vendor=self.vendor)
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                if page > 1:
                    ctx.listing_truncated = True
                return
            rows = [r for r in (payload.get("jobs") or []) if isinstance(r, dict)]
            if not rows:
                return

            page_fresh = 0
            for row in rows:
                job = self._to_raw(host, row)
                if job is None:
                    continue
                if (ctx.cutoff_iso and job.posted_at
                        and job.posted_at < ctx.cutoff_iso):
                    continue
                page_fresh += 1
                yield job

            if page_fresh:
                stale_streak = 0
            else:
                stale_streak += 1
                if stale_streak > tolerance:
                    return
            if len(rows) < size:
                return

        ctx.listing_truncated = True

    # -- mapping -------------------------------------------------------------

    @staticmethod
    def _to_raw(host: str, row: Dict[str, Any]) -> Optional[RawJob]:
        data = row.get("data")
        if not isinstance(data, dict):
            return None
        rid = str(data.get("req_id") or data.get("slug") or "").strip()
        title = str(data.get("title") or "").strip()
        if not rid or not title:
            return None
        posted = to_iso(data.get("posted_date"))
        location = str(data.get("full_location")
                       or data.get("location_name") or "").strip()
        body = " ".join(str(data.get(k) or "") for k in (
            "description", "responsibilities", "qualifications"))
        # `apply_url` is where the candidate actually applies and often points
        # at the underlying ATS (AMD's go to iCIMS). Prefer it over a URL built
        # from the search host, which would send them to a listing page.
        apply_url = str(data.get("apply_url") or "").strip() or \
            f"https://{host}/jobs/{data.get('slug') or rid}"
        return RawJob(
            source_job_id=rid,
            title=title,
            apply_url=apply_url,
            location=location,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            raw_posted_text=str(data.get("posted_date") or ""),
            description=html_to_text(body)[:15000],
            country=str(data.get("country_code") or ""),
            employment_type=str(data.get("employment_type") or ""),
            department=", ".join(
                str((c or {}).get("name") or "")
                for c in (data.get("categories") or [])
                if isinstance(c, dict))[:120],
            remote=looks_remote(title, location),
        )


register(RadancyProvider())
