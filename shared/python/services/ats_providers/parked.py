"""Career surfaces we have OBSERVED but cannot read, registered as parked.

Why register a vendor we cannot crawl
-------------------------------------
`registry.readiness()` answers "can we crawl this vendor?" from the code. A
vendor with no module reads `not_implemented`, which says nothing about WHY -
"nobody looked" and "we looked, the public API refuses anonymous callers" are
different facts with different next steps. A parked provider (`enabled =
False`) is kept out of the crawl and out of discovery, reports `parked` with
the evidence in `readiness_note`, and its `validate` says BLOCKED - never
EMPTY, because a refusal is a fact about us, not about whether the employer
is hiring.

Nothing here makes a request. `list_jobs` marks the listing truncated and
yields nothing, so a member that reached the crawler by some other path can
never be read as a verified zero and can never close live postings.

Each entry records what was observed and when. Re-test before un-parking; if
the public behavior changes, replace the entry with a real provider module.
"""
from __future__ import annotations

from typing import Iterator

from services.ats_providers.base import (DATE_UNKNOWN, STATUS_BLOCKED, AtsProvider,
                                         CrawlPolicy, Endpoint, FetchContext, RawJob,
                                         ValidationResult, fp)
from services.ats_providers.registry import register


class ParkedProvider(AtsProvider):
    enabled = False
    termless = False
    has_detail = False
    date_confidence = DATE_UNKNOWN
    slug_probe = False
    policy = CrawlPolicy(max_concurrency=1, max_pages=1)

    def list_jobs(self, endpoint: Endpoint, ctx: FetchContext) -> Iterator[RawJob]:
        ctx.listing_truncated = True
        return iter(())

    def validate(self, endpoint: Endpoint, ctx: FetchContext) -> ValidationResult:
        return ValidationResult(STATUS_BLOCKED, 0, detail=self.readiness_note)


class DayforceProvider(ParkedProvider):
    """Dayforce (Ceridian) candidate portals, `jobs.dayforcehcm.com`.

    Observed 2026-10-22 on GM Performance Power Units'
    `/en-US/gmppu/CANDIDATEPORTAL` (linked from gmppu.com): the page is a
    Next.js shell whose server render carries only `site-info` (client
    105555, board `candidateportal`). Its own route chunk lists postings with
    `POST /api/geo/{clientNamespace}/jobposting/search` (body:
    clientNamespace, jobBoardCode, cultureCode, distanceUnit,
    paginationStart = (page-1)*25). That call answered **403 "Forbidden"**
    both cookieless and with the page's own session cookies and same-origin
    headers, behind Cloudflare bot management (`__cf_bm`). The legacy
    `candidatePortalBaseUrl` host `us252.dayforcehcm.com` does not resolve.
    We do not impersonate a browser to pass a bot wall, so: blocked.
    """

    vendor = "dayforce"
    label = "Dayforce"
    readiness_note = ("observed 2026-10-22: public listing call "
                      "POST jobs.dayforcehcm.com/api/geo/<ns>/jobposting/search "
                      "returns 403 to non-browser clients (Cloudflare bot "
                      "management); blocked, not empty")
    fingerprints = (
        fp(r"https?://jobs\.dayforcehcm\.com/(?:[a-z]{2}-[A-Z]{2}/)?"
           r"(?P<tenant>[A-Za-z0-9_-]+)/(?P<site>[A-Za-z0-9_-]+)", confidence=90),
    )


register(DayforceProvider())
