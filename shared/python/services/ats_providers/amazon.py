"""Amazon's own job search API — one employer, and one of the largest.

Why a whole module for one company
----------------------------------
`career_pages_service.ENTERPRISE_CATALOG` has carried a row for Amazon since it
was written:

    {"display_name": "Amazon", "ats": "amazon", "slug": "amazon",
     "host": "www.amazon.jobs", ...}

but no provider was ever registered for the vendor key `amazon`, and
`job_ingest_worker._load_career_members` skips any catalog row whose `ats` is
not in the registry. The row was therefore dropped in silence on every crawl,
which is why a live `job_postings` query for Amazon returned **zero** rows while
the catalog claimed to cover it. The same silence hid it from
`inventory_feed._ats_labels`, which even carries the "Amazon Jobs" badge for a
source that never produced a posting. This module closes that gap, and
`_load_career_members` now logs the skip rather than swallowing it.

The protocol
------------
    GET https://www.amazon.jobs/en/search.json
        ?offset=0&result_limit=100&sort=recent&country[]=USA

Verified live 2026-08-27. Facts worth knowing before changing anything here:

* **`sort=recent` really is newest-first**, unlike Workday's termless board, so
  the freshness cutoff is a genuine paging stop rather than a row filter.
* **`result_limit` accepts 100** (Workday caps at 20), so one page is five
  Workday pages.
* **`hits` saturates at 10000** and `offset` stops paying out past 9990. That
  is a ceiling on one pass, not on coverage: the crawl runs on a 15-minute
  tick against a 7-day window, and the whole point of `sort=recent` is that the
  rows we have not seen are at the front.
* **The listing already carries the full description** (`description` plus
  `basic_qualifications` / `preferred_qualifications`), so `has_detail` is
  False and a second request per posting would buy nothing.
* **`posted_date` is an exact date** ("August 27, 2026"), not relative text, so
  these rows are DATE_EXACT.
* `is_intern` and `university_job` are present as FIELDS on a search row but
  measured null on every row of the first 1,800 - Amazon only populates them
  on the detail document. They are still read below, because when a row does
  carry one it is the employer's own statement of the band and beats any
  title guess; they are just not something to rely on.

Measured on 2026-08-27: 1,802 postings inside a 7-day window in 53 seconds,
against 0 rows for Amazon in `job_postings` before this module existed.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote

_HOST = "www.amazon.jobs"
_SEARCH = f"https://{_HOST}/en/search.json"

# `offset` stops returning rows past this; asking anyway just wastes a request.
_MAX_OFFSET = 9990

# "August 27, 2026" is the only format this API emits.
_POSTED_RE = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})$")
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _posted_iso(value: Any) -> str:
    m = _POSTED_RE.match(str(value or "").strip())
    if not m:
        return ""
    month = _MONTHS.get(m.group(1).lower())
    if not month:
        return ""
    try:
        return datetime(int(m.group(3)), month, int(m.group(2))).strftime(
            "%Y-%m-%dT00:00:00+00:00")
    except ValueError:
        return ""


class AmazonProvider(AtsProvider):
    vendor = "amazon"
    label = "Amazon Jobs"

    termless = True
    # The listing ships the whole description; a detail fetch adds nothing.
    has_detail = False
    date_confidence = DATE_EXACT
    # Single-tenant by construction: there is exactly one amazon.jobs.
    slug_probe = False

    api_hosts = (_HOST,)
    portal_hosts = (_HOST, "amazon.jobs")

    policy = CrawlPolicy(
        # One origin serves every request, so pace on it explicitly rather than
        # letting the default hostname key give the same answer by accident.
        pacing_key="amazon.jobs",
        min_interval=0.6,
        jitter=0.20,
        max_concurrency=2,
        timeout=12.0,
        detail_timeout=8.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=100,
        # 40 x 100 = 4,000 rows, inside the API's own 9,990 offset ceiling.
        max_pages=_env_int("AMAZON_MAX_LISTING_PAGES", 40),
        # `sort=recent` IS globally newest-first (measured), so the cheap
        # first-stale-page stop is correct here - unlike Workday.
        stale_page_tolerance=0,
    )

    fingerprints = (
        fp(r"https?://(?:www\.)?amazon\.jobs", confidence=95),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        return Endpoint(vendor=self.vendor, tenant="amazon", site="",
                        host=_HOST)

    # -- listing -------------------------------------------------------------

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        size = int(self.policy.page_size)
        tolerance = max(0, int(getattr(self.policy, "stale_page_tolerance", 0)))
        keyword = str((ctx.options or {}).get("keyword") or "").strip()
        us_only = bool((ctx.options or {}).get("us_only", True))
        stale_streak = 0

        for page in range(max(1, int(self.policy.max_pages))):
            offset = page * size
            if offset > _MAX_OFFSET:
                return
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            params: Dict[str, Any] = {
                "offset": offset,
                "result_limit": size,
                "sort": "recent",
            }
            if keyword:
                params["base_query"] = keyword
            if us_only:
                # The API takes this as a repeated `country[]` parameter.
                params["country[]"] = "USA"
            status, payload = ctx.get_json(_SEARCH, params=params,
                                           timeout=self.policy.timeout,
                                           vendor=self.vendor)
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                if page > 0:
                    ctx.listing_truncated = True
                return
            jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
            if not jobs:
                return

            page_fresh = 0
            for job in jobs:
                raw = self._to_raw(job)
                if raw is None:
                    continue
                if (ctx.cutoff_iso and raw.posted_at
                        and raw.posted_at < ctx.cutoff_iso):
                    continue
                page_fresh += 1
                yield raw

            if page_fresh:
                stale_streak = 0
            else:
                stale_streak += 1
                if stale_streak > tolerance:
                    return
            if len(jobs) < size:
                return

        ctx.listing_truncated = True

    # -- mapping -------------------------------------------------------------

    @staticmethod
    def _to_raw(job: Dict[str, Any]) -> Optional[RawJob]:
        rid = str(job.get("id_icims") or job.get("id") or "").strip()
        title = str(job.get("title") or "").strip()
        if not rid or not title:
            return None
        posted = _posted_iso(job.get("posted_date"))
        path = str(job.get("job_path") or "").strip()
        apply_url = (f"https://{_HOST}{path}" if path.startswith("/")
                     else f"https://{_HOST}/en/jobs/{rid}")
        location = str(job.get("normalized_location")
                       or job.get("location") or "").strip()
        body = " ".join(str(job.get(k) or "") for k in (
            "description", "basic_qualifications", "preferred_qualifications"))
        # `is_intern` / `university_job` are null on search rows today (see the
        # module docstring) but are read when present: an employer's own flag
        # beats a title guess, and Amazon titles many graduate requisitions
        # plainly ("Software Development Engineer"). `normalize_posting` uses
        # `seniority` from `raw` in preference to classifying the title.
        extra: Dict[str, Any] = {}
        if job.get("is_intern"):
            extra["seniority"] = "intern"
        elif job.get("university_job"):
            extra["seniority"] = "entry"
        return RawJob(
            source_job_id=rid,
            title=title,
            apply_url=apply_url,
            location=location,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            raw_posted_text=str(job.get("posted_date") or ""),
            description=html_to_text(body)[:15000],
            country=str(job.get("country_code") or ""),
            employment_type=str(job.get("job_schedule_type") or ""),
            department=str(job.get("job_category") or ""),
            remote=looks_remote(title, location),
            extra=extra,
        )


register(AmazonProvider())
