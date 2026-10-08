"""Jobs an employer lists on its OWN careers page, with no job-board system.

Why this exists, and how narrow it is on purpose
------------------------------------------------
Of the H-1B sponsors discovery reached a website for and found no board on,
most publish their openings as ordinary pages on that site. Measured on 200 of
them (2026-10-08): 156 careers pages read, and on 11 the openings were plain
same-site links under a jobs path whose text is a job title - 56 postings,
10 of the 11 genuine. That is this provider's whole target. It is small, and
it is exactly the IT-services employers the sponsor list is full of.

A general "read any careers page" scraper is a precision problem before it is
a coverage one: the same sample, read loosely, called "Executive Team", a
product page and a press release job postings on 50 other sites. So a link is
a posting only when ALL of these hold:

  * it is on the employer's own host;
  * its path has a jobs segment followed by a slug
    (`/jobs/java-developer/`, `/careers/staff-accountant`);
  * its text reads as a job title - it names a role, and is not a section of
    the site ("Executive Management", "Sales Team", "Careers at Acme");
  * the page offers at least `MIN_LINKS` such links. One match on a page is
    more often a menu item than a vacancy.

and the description is only kept when the linked page itself reads as a
posting (`fetch_detail`). An employer with a single opening is missed by the
last listing rule; that is the price of not inventing jobs.

There is no posting date on such pages, so rows are DATE_UNKNOWN and age from
when we first saw them.

SSRF
----
Every tenant is a different employer's domain, so there is no vendor host to
allowlist. `api_hosts` is therefore EMPTY and the crawler pins each member to
its own host instead (`job_ingest_worker._provider_fetch_context`); a link
that leaves the host is never followed.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from services.ats_providers.base import (DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote, slugify, to_iso

VENDOR = "careers_page"
MIN_LINKS = 2
MAX_LINKS = 60

_JOB_PATH_RE = re.compile(
    r"/(jobs?|careers?|positions?|openings?|vacanc(?:y|ies)|opportunit(?:y|ies)|"
    r"job-openings|current-openings|open-positions|join-us|employment)"
    r"/(?P<slug>[^/?#]{6,120})/?$", re.IGNORECASE)

#: A job title names a role.
_ROLE_RE = re.compile(
    r"\b(engineer|developer|analyst|manager|specialist|scientist|architect|"
    r"consultant|designer|technician|administrator|coordinator|associate|"
    r"director|accountant|recruiter|intern|representative|nurse|physician|"
    r"assistant|lead|officer|programmer|tester|supervisor|professor|lecturer|"
    r"therapist|pharmacist|attorney|paralegal|operator|mechanic|planner|"
    r"strategist|writer|editor|researcher|instructor|teacher|buyer|estimator|"
    r"superintendent|drafter|chemist|biologist|executive|clerk|controller|"
    r"auditor|bookkeeper|dba|sdet)\b", re.IGNORECASE)

#: ...and a section of the site does not, even when it borrows the word.
_SECTION_RE = re.compile(
    r"\b(team|management|leadership|benefits|programs?|services|solutions|"
    r"board|committee|relations|coverage|protection|news|stories|blog|"
    r"training|resources|portal|login|policy|overview|directory|testimonials?)\s*$"
    r"|^(careers?|jobs?|about|our|meet|why|life at|working at|join)\b"
    r"|@",
    re.IGNORECASE)

#: A card's link often wraps its button too: "Software Engineer More Details".
_BUTTON_TEXT_RE = re.compile(
    r"\s*[-|:]?\s*(more details|view details|read more|learn more|apply now|"
    r"apply|view job|view|details)\s*$", re.IGNORECASE)
_POSTING_TEXT_RE = re.compile(
    r"\b(responsibilit|qualification|requirement|experience|apply|"
    r"job description|what you|skills)", re.IGNORECASE)
_STRIP_RE = re.compile(
    r"<(script|style|nav|header|footer|form|noscript|svg)\b.*?</\1>",
    re.IGNORECASE | re.DOTALL)
_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)


def _own_host(host: str) -> str:
    return re.sub(r"^www\.", "", str(host or "").strip().lower())


def _same_site(url: str, host: str) -> bool:
    theirs = _own_host(urlparse(url).hostname or "")
    ours = _own_host(host)
    return bool(ours) and (theirs == ours or theirs.endswith(f".{ours}"))


def looks_like_a_job_title(text: str) -> bool:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not (6 <= len(text) <= 90) or len(text.split()) > 12:
        return False
    return bool(_ROLE_RE.search(text)) and not _SECTION_RE.search(text)


def job_links(body: str, page_url: str, host: str) -> List[Tuple[str, str]]:
    """`(url, title)` for every link on a page that is a posting by the rules
    in the module docstring. [] unless the page offers at least MIN_LINKS."""
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(body or "")
    except Exception:
        return []
    page = page_url.split("#")[0].rstrip("/")
    found: Dict[str, str] = {}
    for anchor in doc.xpath("//a[@href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(page_url, href).split("#")[0]
        if not url.startswith("https://") or not _same_site(url, host):
            continue
        if url.rstrip("/") == page or not _JOB_PATH_RE.search(urlparse(url).path):
            continue
        title = _BUTTON_TEXT_RE.sub("", re.sub(
            r"\s+", " ", anchor.text_content() or "").strip()).strip(" -|:")
        if not looks_like_a_job_title(title):
            continue
        found.setdefault(url, title)
        if len(found) >= MAX_LINKS:
            break
    return list(found.items()) if len(found) >= MIN_LINKS else []


def endpoint_for_pages(host: str, pages: List[Tuple[str, str]]) -> Optional[Endpoint]:
    """The endpoint for the careers page that lists the most postings, if any
    page a discovery walk fetched lists them at all."""
    best_url, best = "", 0
    for url, body in pages or []:
        count = len(job_links(body, url, host))
        if count > best:
            best_url, best = url, count
    if not best_url:
        return None
    return Endpoint(vendor=VENDOR, tenant=_own_host(host), host=_own_host(host),
                    extra={"listing_url": best_url})


def _ld_posting(body: str) -> Dict[str, Any]:
    for raw in _LD_RE.findall(body or ""):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


class OwnSiteCareersProvider(AtsProvider):
    vendor = VENDOR
    label = "Company careers page"

    termless = True
    has_detail = True
    date_confidence = DATE_UNKNOWN
    slug_probe = False
    fingerprints: tuple = ()

    #: Empty on purpose - see the SSRF note above.
    api_hosts: tuple = ()
    portal_hosts: tuple = ()

    policy = CrawlPolicy(
        pacing_key=None,
        min_interval=1.0,
        jitter=0.25,
        max_concurrency=1,
        timeout=10.0,
        detail_timeout=10.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=MAX_LINKS,
        max_pages=1,
        stale_page_tolerance=0,
    )

    @staticmethod
    def _listing(endpoint: Endpoint) -> Tuple[str, str]:
        host = _own_host(endpoint.host or endpoint.tenant)
        url = str((endpoint.extra or {}).get("listing_url") or "").strip()
        if not host or not url.startswith("https://") or not _same_site(url, host):
            return "", ""
        return host, url

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        host, url = self._listing(endpoint)
        if not url:
            return
        status, body = ctx.get_text(url, params={}, timeout=self.policy.timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            # Not "no jobs": we did not read the page.
            ctx.listing_truncated = True
            return
        for link, title in job_links(body, url, host):
            match = _JOB_PATH_RE.search(urlparse(link).path)
            rid = slugify(match.group("slug") if match else link, 80)
            if not rid:
                continue
            yield RawJob(
                source_job_id=rid,
                title=title[:200],
                apply_url=link,
                detail_url=link,
                posted_at="",
                date_confidence=DATE_UNKNOWN,
                remote=looks_remote(title, ""),
            )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        host, _listing = self._listing(endpoint)
        url = job.detail_url if str(job.detail_url).startswith("https://") \
            else job.apply_url
        if not host or not _same_site(str(url), host):
            return None
        status, body = ctx.get_text(url, params={}, timeout=self.policy.detail_timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return None
        posting = _ld_posting(body)
        text = html_to_text(posting.get("description") or "").strip()
        if not text:
            text = html_to_text(_STRIP_RE.sub(" ", body)).strip()
            # A page that does not read as a posting is not described as one.
            if len(text) < 300 or not _POSTING_TEXT_RE.search(text):
                return None
        out = RawJob(source_job_id=job.source_job_id, title=job.title,
                     apply_url=job.apply_url, description=text[:15000])
        posted = to_iso(posting.get("datePosted")) if posting else ""
        if posted:
            out.posted_at = posted
        return out

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        return None


register(OwnSiteCareersProvider())
