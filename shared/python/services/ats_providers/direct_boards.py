"""Two employers that run their own careers stack and publish no ATS board:
Google and Goldman Sachs.

Both were the top of the H-1B gap list (3,183 and 1,050 approvals in FY2026)
with no source on file, and both were read end to end from this environment on
2026-10-07 before a line of this module was kept.

Google
------
`GET https://www.google.com/about/careers/applications/jobs/results/
?location=United States&sort_by=date&page=N` is a server-rendered page whose
rows are embedded as `AF_initDataCallback({key: 'ds:1', ... data: [...]})`.
`data` is `[rows, _, total, page_size]`; 1,812 US postings, 20 a page, page 92
empty. One row is a positional list - the positions used are named below and
every access is guarded, because a positional payload is exactly the kind that
reshapes without notice: a row that does not fit yields nothing, never an
exception.

Three timestamps come with each row. `[14]` is the one the page is SORTED on
(monotone across twenty pages) and is refreshed when a requisition is
republished; `[12]` is when the requisition was created. `posted_at` is `[12]`:
a role opened last year and republished yesterday was not posted yesterday
(ADR-001). The walk stops on `[14]`, which is what makes "this page is older
than the cutoff" true of every later page.

The description is in the listing (responsibilities, qualifications and the
"about the job" text), so there is no detail request.

Goldman Sachs
-------------
`POST https://api-higher.gs.com/gateway/api/v1/graphql`, the endpoint behind
higher.gs.com. `roleSearch` lists 667 roles, 100 a page, with no description
and no date; `role(externalSourceId)` returns `descriptionHtml` (5,188
characters on the role probed). So rows are DATE_UNKNOWN and the body is a
detail fetch.

Its `corporateTitle` is the firm's own statement of the level and is passed
through as such: an Analyst is an entry-level hire, a Vice President a senior
individual contributor. Read off the words alone a VP files as an executive.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote

# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------

_GOOGLE_HOST = "www.google.com"
_GOOGLE_RESULTS = f"https://{_GOOGLE_HOST}/about/careers/applications/jobs/results/"
_GOOGLE_DATA_RE = re.compile(
    r"AF_initDataCallback\(\{key: 'ds:1', hash: '[^']*', data:(.*?), "
    r"sideChannel: \{\}\}\);", re.DOTALL)

# Positions in one row of `ds:1`.
_G_ID, _G_TITLE = 0, 1
_G_RESPONSIBILITIES, _G_QUALIFICATIONS = 3, 4
_G_COMPANY, _G_LOCATIONS, _G_ABOUT = 7, 9, 10
_G_CREATED, _G_SORTED_ON = 12, 14


def _at(row: Any, index: int) -> Any:
    return row[index] if isinstance(row, list) and len(row) > index else None


def _g_text(cell: Any) -> str:
    """A rich-text cell is `[null, "<html>"]`."""
    value = _at(cell, 1) if isinstance(cell, list) else cell
    return html_to_text(value).strip() if isinstance(value, str) else ""


def _g_epoch(cell: Any) -> Optional[datetime]:
    seconds = _at(cell, 0)
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(seconds), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_google_results(body: str) -> List[List[Any]]:
    """The rows embedded in one results page, or [] for anything unexpected."""
    match = _GOOGLE_DATA_RE.search(body or "")
    if not match:
        return []
    try:
        data = json.loads(match.group(1))
    except (TypeError, ValueError):
        return []
    rows = _at(data, 0)
    return [r for r in rows if isinstance(r, list)] if isinstance(rows, list) else []


def google_row_to_raw(row: List[Any]) -> Optional[RawJob]:
    rid = str(_at(row, _G_ID) or "").strip()
    title = str(_at(row, _G_TITLE) or "").strip()
    if not rid.isdigit() or not title:
        return None
    places = [str(_at(loc, 0) or "").strip()
              for loc in (_at(row, _G_LOCATIONS) or []) if isinstance(loc, list)]
    location = "; ".join(p for p in places[:4] if p)
    parts = []
    for label, index in (("", _G_ABOUT),
                         ("Responsibilities", _G_RESPONSIBILITIES),
                         ("Qualifications", _G_QUALIFICATIONS)):
        text = _g_text(_at(row, index))
        if text:
            parts.append(f"{label}\n{text}" if label else text)
    created = _g_epoch(_at(row, _G_CREATED))
    posted = created.isoformat() if created else ""
    return RawJob(
        source_job_id=rid,
        title=title,
        apply_url=f"{_GOOGLE_RESULTS}{rid}",
        location=location,
        posted_at=posted,
        date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
        description="\n\n".join(parts)[:15000],
        remote=looks_remote(title, location),
    )


class GoogleCareersProvider(AtsProvider):
    vendor = "google_careers"
    label = "Google Careers"

    termless = True
    has_detail = False
    date_confidence = DATE_EXACT
    slug_probe = False
    fingerprints: tuple = ()

    api_hosts = (_GOOGLE_HOST,)
    portal_hosts = api_hosts

    policy = CrawlPolicy(
        pacing_key=None,
        # One page is 1.3 MB of HTML; this is deliberately the slowest pacing
        # of any provider here.
        min_interval=1.0,
        jitter=0.25,
        max_concurrency=1,
        timeout=20.0,
        detail_timeout=10.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=20,
        # A week of US postings was 30 pages on 2026-10-07.
        max_pages=60,
        stale_page_tolerance=0,
    )

    def default_endpoint(self) -> Endpoint:
        return Endpoint(vendor=self.vendor, tenant="google",
                        host=_GOOGLE_HOST, display_name="Google")

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        cutoff = _parse_iso(ctx.cutoff_iso)
        location = str((endpoint.extra or {}).get("location") or "United States")
        for page in range(1, int(self.policy.max_pages) + 1):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            status, body = ctx.get_text(
                _GOOGLE_RESULTS,
                params={"location": location, "sort_by": "date", "page": page},
                timeout=self.policy.timeout, vendor=self.vendor)
            if self.policy.is_challenge(status, ""):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not body:
                if page > 1:
                    ctx.listing_truncated = True
                return
            rows = parse_google_results(body)
            if not rows:
                return
            newest_sort_key: Optional[datetime] = None
            for row in rows:
                sorted_on = _g_epoch(_at(row, _G_SORTED_ON))
                if sorted_on and (newest_sort_key is None or sorted_on > newest_sort_key):
                    newest_sort_key = sorted_on
                job = google_row_to_raw(row)
                if job is None:
                    continue
                if ctx.cutoff_iso and job.posted_at and job.posted_at < ctx.cutoff_iso:
                    continue
                yield job
            # The page is ordered on `[14]`; once its newest row is older than
            # the cutoff, so is everything after it.
            if cutoff and newest_sort_key and newest_sort_key < cutoff:
                return
            if len(rows) < int(self.policy.page_size):
                return
        ctx.listing_truncated = True

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        return None


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Goldman Sachs
# ---------------------------------------------------------------------------

_GS_HOST = "api-higher.gs.com"
_GS_GRAPHQL = f"https://{_GS_HOST}/gateway/api/v1/graphql"
_GS_LIST_QUERY = (
    "query GetRoles($searchQueryInput: RoleSearchQueryInput!) { "
    "roleSearch(searchQueryInput: $searchQueryInput) { totalCount items { "
    "roleId corporateTitle jobTitle jobFunction "
    "locations { primary state country city } status division "
    "externalSource { sourceId } } } }")
_GS_ROLE_QUERY = (
    "query GetRoleById($externalSourceId: String!, $externalSourceFetch: Boolean) { "
    "role(externalSourceId: $externalSourceId, "
    "externalSourceFetch: $externalSourceFetch) { roleId jobTitle descriptionHtml } }")

#: The firm's corporate titles, as bands. Managing Director is left to the
#: title rules (it reads as an executive, which is right).
_GS_LEVELS: Dict[str, str] = {
    "analyst": "entry", "associate": "mid", "senior associate": "mid",
    "vice president": "senior", "executive director": "staff",
}


def goldman_item_to_raw(item: Dict[str, Any]) -> Optional[RawJob]:
    source = item.get("externalSource") if isinstance(item.get("externalSource"), dict) else {}
    rid = str(source.get("sourceId") or "").strip()
    title = str(item.get("jobTitle") or "").strip()
    if not rid or not title or str(item.get("status") or "POSTED") != "POSTED":
        return None
    places = []
    country = ""
    for loc in (item.get("locations") or []):
        if not isinstance(loc, dict):
            continue
        place = ", ".join(str(loc.get(k) or "").strip()
                          for k in ("city", "state", "country") if loc.get(k))
        if place:
            places.append(place)
        country = country or str(loc.get("country") or "").strip()
    location = "; ".join(places[:4])
    corporate = str(item.get("corporateTitle") or "").strip()
    return RawJob(
        source_job_id=rid,
        title=title,
        apply_url=f"https://higher.gs.com/roles/{rid}",
        location=location,
        country=country,
        posted_at="",
        date_confidence=DATE_UNKNOWN,
        department=str(item.get("division") or "")[:120],
        remote=looks_remote(title, location),
        extra={"seniority": _GS_LEVELS.get(corporate.lower(), ""),
               "corporate_title": corporate},
    )


class GoldmanSachsProvider(AtsProvider):
    vendor = "goldman_sachs"
    label = "Goldman Sachs"

    termless = True
    has_detail = True
    date_confidence = DATE_UNKNOWN
    slug_probe = False
    fingerprints: tuple = ()

    api_hosts = (_GS_HOST,)
    portal_hosts = ("higher.gs.com", _GS_HOST)

    policy = CrawlPolicy(
        pacing_key=None,
        min_interval=0.5,
        jitter=0.15,
        max_concurrency=2,
        timeout=15.0,
        detail_timeout=10.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=100,
        max_pages=15,
        stale_page_tolerance=0,
    )

    def default_endpoint(self) -> Endpoint:
        return Endpoint(vendor=self.vendor, tenant="goldmansachs",
                        host=_GS_HOST, display_name="Goldman Sachs")

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        if ctx.post_json is None:
            ctx.log("warning", "goldman_sachs: needs post_json")
            return
        size = int(self.policy.page_size)
        seen = 0
        for page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            status, payload = ctx.post_json(
                _GS_GRAPHQL,
                json_body={
                    "operationName": "GetRoles",
                    "variables": {"searchQueryInput": {
                        "page": {"pageSize": size, "pageNumber": page},
                        "sort": {"sortStrategy": "POSTED_DATE", "sortOrder": "DESC"},
                        "filters": [],
                        "experiences": ["PROFESSIONAL", "EARLY_CAREER"],
                        "searchTerm": ""}},
                    "query": _GS_LIST_QUERY},
                timeout=self.policy.timeout, vendor=self.vendor)
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            search = _dig(payload, "data", "roleSearch")
            if status != 200 or not isinstance(search, dict):
                if page:
                    ctx.listing_truncated = True
                return
            items = [i for i in (search.get("items") or []) if isinstance(i, dict)]
            if not items:
                return
            for item in items:
                job = goldman_item_to_raw(item)
                if job is not None:
                    yield job
            seen += len(items)
            try:
                total = int(search.get("totalCount") or 0)
            except (TypeError, ValueError):
                total = 0
            if len(items) < size or (total and seen >= total):
                return
        ctx.listing_truncated = True

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        rid = str(job.source_job_id or "").strip()
        if not rid or ctx.post_json is None:
            return None
        status, payload = ctx.post_json(
            _GS_GRAPHQL,
            json_body={"operationName": "GetRoleById",
                       "variables": {"externalSourceId": rid,
                                     "externalSourceFetch": False},
                       "query": _GS_ROLE_QUERY},
            timeout=self.policy.detail_timeout, vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        role = _dig(payload, "data", "role")
        if status != 200 or not isinstance(role, dict):
            return None
        text = html_to_text(role.get("descriptionHtml") or "").strip()
        if not text:
            return None
        return RawJob(source_job_id=rid, title=job.title,
                      apply_url=job.apply_url, description=text[:15000])

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        return None


def _dig(payload: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


register(GoogleCareersProvider())
register(GoldmanSachsProvider())
