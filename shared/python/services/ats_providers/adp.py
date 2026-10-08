"""ADP Workforce Now recruitment ("career center") boards.

Why this vendor
---------------
Of 420 H-1B sponsors whose website discovery reached and found no board on
(2026-10-07), ADP was the most common system it could not read: the careers
page links `workforcenow.adp.com/mascsr/default/mdf/recruitment/
recruitment.html?cid=<guid>&ccId=<id>` and the listing behind it is a public
JSON endpoint.

What was verified live (2026-10-07, four sponsors' boards)
----------------------------------------------------------
    GET https://workforcenow.adp.com/mascsr/default/careercenter/public/
        events/staffing/v1/job-requisitions?cid=<guid>&ccId=<id>&lang=en_US
        &$top=20&$skip=<n>
    -> {"jobRequisitions": [...], "meta": {"totalNumber": 27}}

* Twenty rows a page whatever `$top` asks for.
* `$skip` is a ONE-based start index, not a count of rows to skip: 1 and 21
  are the first two pages (`$skip=20` returns rows 20-27 of 27).
* Newest first, and `postDate` is a real ISO timestamp, so rows are
  DATE_EXACT and the walk can stop at the cutoff.
* The listing has no description. `GET .../job-requisitions/<itemID>` returns
  `requisitionDescription` (4,806 characters on the row probed).
* `ccId` is optional on the wire; boards print `19000101_000001`.

The tenant is the `cid` GUID. It cannot be guessed and nothing on the board
names its owner, so there is no slug probe: an endpoint exists only because an
employer's own careers page linked it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote, to_iso

_HOST = "workforcenow.adp.com"
_API = (f"https://{_HOST}/mascsr/default/careercenter/public/events/staffing"
        "/v1/job-requisitions")
_PORTAL = f"https://{_HOST}/mascsr/default/mdf/recruitment/recruitment.html"
_DEFAULT_CC = "19000101_000001"
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_CC_RE = re.compile(r"^[0-9]{6,10}_[0-9]{4,8}$")


def _string_field(row: Dict[str, Any], code: str) -> str:
    group = row.get("customFieldGroup") if isinstance(row.get("customFieldGroup"), dict) else {}
    for item in (group.get("stringFields") or []):
        if isinstance(item, dict) and (item.get("nameCode") or {}).get("codeValue") == code:
            return str(item.get("stringValue") or "").strip()
    return ""


def _location(row: Dict[str, Any]) -> str:
    places: List[str] = []
    for loc in (row.get("requisitionLocations") or []):
        if not isinstance(loc, dict):
            continue
        short = str((loc.get("nameCode") or {}).get("shortName") or "").strip()
        if not short:
            address = loc.get("address") if isinstance(loc.get("address"), dict) else {}
            short = ", ".join(x for x in (
                str(address.get("cityName") or "").strip(),
                str((address.get("countrySubdivisionLevel1") or {}).get("codeValue") or "").strip(),
            ) if x)
        if short and short not in places:
            places.append(short)
    return "; ".join(places[:4])


class AdpWorkforceNowProvider(AtsProvider):
    vendor = "adp_wfn"
    label = "ADP Workforce Now"

    termless = True
    has_detail = True
    date_confidence = DATE_EXACT
    slug_probe = False

    api_hosts = (_HOST,)
    portal_hosts = api_hosts

    policy = CrawlPolicy(
        pacing_key=None,
        min_interval=0.4,
        jitter=0.10,
        max_concurrency=2,
        timeout=12.0,
        detail_timeout=8.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=20,
        max_pages=25,
        stale_page_tolerance=0,
    )

    fingerprints = (
        # The careers link, however the page escaped its `=` and `&`.
        fp(r"workforcenow\.adp\.com/mascsr/default/mdf/recruitment/"
           r"recruitment\.html\?cid(?:=|&#61;|%3D)"
           r"(?P<tenant>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})"
           r"(?:(?:&amp;|&#0?38;|&|amp;)+ccId(?:=|&#61;|%3D)(?P<site>[0-9]+_[0-9]+))?",
           confidence=95),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip().lower()
        if not _GUID_RE.match(tenant):
            return None
        site = (fields.get("site") or "").strip()
        return Endpoint(vendor=self.vendor, tenant=tenant,
                        site=site if _CC_RE.match(site) else _DEFAULT_CC,
                        host=_HOST)

    @staticmethod
    def _ids(endpoint: Endpoint) -> Optional[Dict[str, str]]:
        cid = str(endpoint.tenant or "").strip().lower()
        if not _GUID_RE.match(cid):
            return None
        site = str(endpoint.site or "").strip()
        return {"cid": cid, "ccId": site if _CC_RE.match(site) else _DEFAULT_CC,
                "lang": "en_US", "locale": "en_US"}

    # -- listing -------------------------------------------------------------

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        ids = self._ids(endpoint)
        if ids is None:
            return
        size = int(self.policy.page_size)
        seen = 0
        for page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            # `$skip` is the one-based index of the first row wanted.
            status, payload = ctx.get_json(
                _API, params=dict(ids, **{"$top": size, "$skip": page * size + 1}),
                timeout=self.policy.timeout, vendor=self.vendor)
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                if page:
                    ctx.listing_truncated = True
                return
            rows = [r for r in (payload.get("jobRequisitions") or [])
                    if isinstance(r, dict)]
            if not rows:
                return
            fresh = 0
            for row in rows:
                job = self._to_raw(ids, row)
                if job is None:
                    continue
                if ctx.cutoff_iso and job.posted_at and job.posted_at < ctx.cutoff_iso:
                    continue
                fresh += 1
                yield job
            seen += len(rows)
            if not fresh and ctx.cutoff_iso:
                return                      # newest first: the rest are older
            try:
                total = int((payload.get("meta") or {}).get("totalNumber") or 0)
            except (TypeError, ValueError):
                total = 0
            if len(rows) < size or (total and seen >= total):
                return
        ctx.listing_truncated = True

    def _to_raw(self, ids: Dict[str, str], row: Dict[str, Any]) -> Optional[RawJob]:
        item = str(row.get("itemID") or "").strip()
        title = str(row.get("requisitionTitle") or "").strip()
        if not item or not title:
            return None
        posted = to_iso(row.get("postDate"))
        location = _location(row)
        job_id = _string_field(row, "ExternalJobID")
        apply_url = (f"{_PORTAL}?cid={ids['cid']}&ccId={ids['ccId']}"
                     + (f"&jobId={job_id}" if job_id else "") + "&lang=en_US")
        return RawJob(
            source_job_id=item,
            title=title,
            apply_url=apply_url,
            location=location,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            raw_posted_text=str(row.get("postDate") or ""),
            employment_type=str((row.get("workLevelCode") or {}).get("shortName") or "")
            if isinstance(row.get("workLevelCode"), dict) else "",
            remote=looks_remote(title, location),
        )

    # -- detail --------------------------------------------------------------

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        ids = self._ids(endpoint)
        item = str(job.source_job_id or "").strip()
        if ids is None or not re.match(r"^[0-9A-Za-z_]{4,40}$", item):
            return None
        status, payload = ctx.get_json(f"{_API}/{item}", params=dict(ids),
                                       timeout=self.policy.detail_timeout,
                                       vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not isinstance(payload, dict):
            return None
        text = html_to_text(payload.get("requisitionDescription") or "").strip()
        if not text:
            return None
        return RawJob(source_job_id=item, title=job.title,
                      apply_url=job.apply_url, description=text[:15000])

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        return None


register(AdpWorkforceNowProvider())
