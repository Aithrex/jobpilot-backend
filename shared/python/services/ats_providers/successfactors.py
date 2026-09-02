"""SAP SuccessFactors (Recruiting Marketing / "RMK") career sites.

What this vendor is
-------------------
SuccessFactors is two products wearing one name, and conflating them is the
first way a provider for it goes wrong:

  * the **RMK career site** — a server-RENDERED search page, the thing
    `jobs.sap.com` is, and the only half that is crawlable without a session;
  * the **RCM candidate portal** on `career<N>.successfactors.<tld>` /
    `performancemanager<N>.successfactors.<tld>` — a JavaScript application
    keyed by `?company=<tenant>`, where the listing arrives over an authenticated
    XHR that an anonymous client cannot reproduce.

Both are handled below, and they are handled DIFFERENTLY on purpose: the first
is parsed, the second is attempted and allowed to come back empty. Pretending
the second is fetchable would ship a source that quietly returns nothing, which
is the exact failure `job_ingest_worker` documents on `_TERMLESS_ATS`.

Endpoint shapes, and how confident this module is in each
--------------------------------------------------------
**NOTHING here was verified against the live vendor from this environment —
there is no outbound network in it.** Recording that plainly matters more than
sounding certain: Jobvite shipped a provider that could only ever yield zero
rows because a 200 was mistaken for a working endpoint, and the test that was
supposed to catch it was written against an invented payload. So:

  1. `GET https://{host}/search?q=&sortColumn=referencedate&sortDirection=desc
     &startrow={n}` — **transcribed from the RMK template**, which renders the
     board as a table of `<tr class="data-row">` with `a.jobTitle-link`,
     `span.jobLocation`, `span.jobDepartment`, `span.jobDate`. This is the
     primary path. `startrow` is a ROW offset stepping by the page size (25 in
     the stock template), not a page number.
  2. `GET https://{host}/services/xml/{feed}` — the RSS feed some RMK
     deployments expose. **Best-effort and OPT-IN**: it is requested only when
     an endpoint carries `extra["rss_path"]`, because firing a speculative
     request at every tenant to discover that most 404 is a cost paid on every
     crawl for a benefit that arrives on a minority of them. When it is
     configured and answers, its `<pubDate>` is a real date.
  3. `GET https://{host}/career?company={tenant}` — the RCM portal. Best-effort:
     anchors carrying `career_job_req_id=<id>` are parsed when the server
     rendered any, and an empty result is the expected outcome, not an error.

Whichever shape answers, a parse failure must degrade to "no rows", never to an
exception: one employer's redesigned page must not end a shard's crawl. The
only thing allowed out of `list_jobs` is `ChallengeError`, and only because
swallowing it would report an anti-bot wall as an empty board — the mistake
ADR-004 §7 was written about.

Dates (ADR-001)
---------------
The template's date column is the `referencedate` the listing is sorted on, and
it renders as an absolute date ("Aug 27, 2026"), so a row that carries one is
`DATE_EXACT`. A row whose column is missing, empty or unparseable gets
`posted_at=""` and `DATE_UNKNOWN` — never `now()`.

Because the sort is REQUESTED but not verified, the cutoff is applied as a row
filter, and paging stops early only when a whole page is *conclusively* older:
every row on it carried a real date and every one of those dates was stale. A
page that is merely undated proves nothing and does not stop the walk.

SSRF
----
`api_hosts` is the vendor's own two suffixes plus the first-party hosts named
in the registry. It is deliberately not an open suffix: most RMK deployments
sit on the employer's own domain (`careers.<company>.com`), and each of those
is added here once it has been verified, the same posture `enterprise.py` and
`radancy.py` take.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urljoin

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import (html_to_text, looks_remote, slugify,
                                         to_iso)

#: Host labels that name SuccessFactors' own infrastructure or a generic
#: careers subdomain, never the employer.
_INFRA_LABELS = frozenset({
    "career", "careers", "performancemanager", "jobs", "job", "www", "hcm",
    "recruiting", "talent", "apply", "sfcareers",
})

#: `?company=<tenant>` is how the RCM portal names its tenant.
_COMPANY_RE = re.compile(r"[?&]company=(?P<company>[A-Za-z0-9_\-]+)",
                         re.IGNORECASE)
#: `/job/<City-Title-Slug>/1234567/` — the numeric requisition id.
_JOB_ID_RE = re.compile(r"/job/[^/]*/(\d{3,})", re.IGNORECASE)
#: The RCM portal's requisition parameter, in either of its two spellings
#: (`career_job_req_id` is escaped as `career%5fjob%5freq%5fid` on some sites).
_REQ_ID_RE = re.compile(r"career[_%]?(?:5f)?job[_%]?(?:5f)?req[_%]?(?:5f)?id=(\d+)",
                        re.IGNORECASE)
#: Trailing boilerplate in a career-site `<title>`.
_TITLE_TRIM_RE = re.compile(
    r"\s*[|\-–—:]?\s*"
    r"(jobs?|careers?|job search|search jobs|job board|open (?:roles|positions)|"
    r"openings|home)\s*$", re.IGNORECASE)
_TITLE_PREFIX_RE = re.compile(r"^(?:careers?|jobs?)\s+(?:at|with)\s+",
                              re.IGNORECASE)


def _text_of(node: Any) -> str:
    try:
        return " ".join((node.text_content() or "").split())
    except Exception:
        return ""


def _first(node: Any, *xpaths: str) -> str:
    """First non-empty text among several XPaths, or ""."""
    for path in xpaths:
        try:
            hits = node.xpath(path)
        except Exception:
            continue
        for hit in hits or ():
            value = _text_of(hit) if hasattr(hit, "text_content") else \
                " ".join(str(hit or "").split())
            if value:
                return value
    return ""


class SuccessFactorsProvider(AtsProvider):
    vendor = "successfactors"
    #: White-labelled onto employer domains, so there is no slug to guess and
    #: `slug_probe` is False. Tenants come from fingerprinting the employer's
    #: own site — which is the PRIMARY discovery strategy, not a fallback, so
    #: this is a note about how the vendor is reached and not a reason it
    #: cannot be.
    readiness_note = ("reached by fingerprinting the employer's own site; "
                      "the vendor exposes no guessable tenant slug")
    label = "SAP SuccessFactors"

    termless = True
    #: The listing carries title, location, department and date. The detail
    #: page adds the description, but at one request per posting across a
    #: vendor this large; the description is left to the enrichment path.
    has_detail = False
    date_confidence = DATE_EXACT
    #: `career5.successfactors.eu` is a POD, not an employer, so a guessed
    #: tenant is unfalsifiable here — every wrong `?company=` value renders the
    #: same shell. Tenants come from fingerprinting the employer's own site.
    slug_probe = False

    api_hosts = (
        ".successfactors.eu",
        ".successfactors.com",
        # First-party RMK host named by the official career registry.
        "jobs.sap.com",
    )
    portal_hosts = api_hosts

    policy = CrawlPolicy(
        # Each tenant is its own origin (or its own pod), and there is no
        # IP-scoped wall shared across them, so hostname keying is right.
        pacing_key=None,
        min_interval=0.30,
        jitter=0.10,
        max_concurrency=4,
        timeout=10.0,
        detail_timeout=6.0,
        challenge_statuses=frozenset({429, 403}),
        challenge_markers=("just a moment", "verify you are human",
                           "incapsula incident", "access denied",
                           "request unsuccessful"),
        # A bare status is NOT enough. A vendor-wide cooldown stops every
        # tenant, and dead subdomains — which is what tenant discovery feeds in
        # — answer 403 with an ordinary error page. Requiring the marker keeps
        # the protection and removes the false positive (ADR-004 §3.2).
        challenge_requires_marker=True,
        cooldown_seconds=900.0,
        #: The stock RMK template paginates in 25s.
        page_size=25,
        max_pages=12,
        #: One conclusively-stale page may be a block boundary; two in a row is
        #: an answer. The sort is requested, not verified, so this is not 0.
        stale_page_tolerance=1,
    )

    fingerprints = (
        # Richest match: the RCM portal names both the pod and the tenant.
        fp(r"https?://(?P<host>(?:career|performancemanager)[A-Za-z0-9]*"
           r"\.successfactors\.(?:eu|com|com\.cn))/[^\"'\s]*"
           r"[?&]company=(?P<tenant>[A-Za-z0-9_\-]+)", confidence=95),
        # SAP's own first-party RMK site; the tenant is derived from the host.
        fp(r"https?://(?P<host>jobs\.sap\.com)/", confidence=90),
        # Any other SuccessFactors host. It proves the vendor; whether it also
        # names a tenant is decided in `endpoint_from_match`.
        fp(r"https?://(?P<host>[A-Za-z0-9\-]+\.successfactors\.(?:eu|com|com\.cn))/",
           confidence=70),
    )

    # -- endpoints -----------------------------------------------------------

    @staticmethod
    def _tenant_from_host(host: str) -> str:
        """An employer name out of a FIRST-PARTY host, or "".

        `jobs.sap.com` -> `sap`. `career5.successfactors.eu` -> "" — that
        subdomain is a SuccessFactors pod shared by thousands of customers, and
        minting a tenant from it would file every one of them under `career5`.
        """
        parts = [p for p in str(host or "").lower().split(".") if p]
        if len(parts) < 2:
            return ""
        if "successfactors" in parts:
            return ""
        labels = [p for p in parts[:-1]
                  if re.sub(r"\d+$", "", p) not in _INFRA_LABELS]
        return labels[0] if labels else ""

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        host = (fields.get("host") or "").strip().lower()
        if not host:
            return None
        tenant = (fields.get("tenant") or "").strip()
        if not tenant:
            match = _COMPANY_RE.search(url or "")
            tenant = match.group("company") if match else ""
        if not tenant:
            tenant = self._tenant_from_host(host)
        if not tenant:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant, host=host)

    @staticmethod
    def _host_of(endpoint: Endpoint) -> str:
        host = str(endpoint.host or "").strip().lower()
        if host:
            return host
        # A registry row may carry only the tenant when the tenant IS the host.
        tenant = str(endpoint.tenant or "").strip().lower()
        return tenant if "." in tenant else ""

    # -- listing -------------------------------------------------------------

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        """Never raises for a parse or transport problem; see the docstring."""
        host = self._host_of(endpoint)
        if not host:
            ctx.log("info", "successfactors: %s has no host", endpoint.tenant)
            return
        try:
            yield from self._list(endpoint, host, ctx)
        except ChallengeError:
            # The ONE exception that must travel: an anti-bot wall recorded as
            # an empty board retires live employers.
            raise
        except Exception as exc:
            ctx.log("warning", "successfactors: %s listing failed (%s: %s)",
                    host, type(exc).__name__, exc)
            return

    def _list(self, endpoint: Endpoint, host: str,
              ctx: FetchContext) -> Iterator[RawJob]:
        emitted: set = set()

        rss_path = str((endpoint.extra or {}).get("rss_path") or "").strip()
        if rss_path:
            for job in self._from_rss(host, rss_path, ctx):
                if job.source_job_id in emitted:
                    continue
                emitted.add(job.source_job_id)
                yield job

        produced_html = False
        for job in self._from_search(host, ctx):
            if job.source_job_id in emitted:
                continue
            emitted.add(job.source_job_id)
            produced_html = True
            yield job

        # The RCM portal is only worth a request when the RMK search rendered
        # nothing at all — on a normal tenant it is a second fetch for rows we
        # already have.
        if produced_html or emitted:
            return
        for job in self._from_portal(endpoint, host, ctx):
            if job.source_job_id in emitted:
                continue
            emitted.add(job.source_job_id)
            yield job

    # -- shape 1: the RMK search page ----------------------------------------

    def _from_search(self, host: str, ctx: FetchContext) -> Iterator[RawJob]:
        size = int(self.policy.page_size)
        tolerance = max(0, int(getattr(self.policy, "stale_page_tolerance", 0)))
        keyword = str((ctx.options or {}).get("keyword") or "").strip()
        url = f"https://{host}/search/"
        stale_streak = 0

        for page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            params: Dict[str, Any] = {
                "q": keyword,
                "sortColumn": "referencedate",
                "sortDirection": "desc",
                # A ROW offset, not a page index.
                "startrow": page * size,
            }
            status, body = ctx.get_text(url, params=params,
                                        timeout=self.policy.timeout,
                                        vendor=self.vendor)
            if self.policy.is_challenge(status, body or ""):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not body:
                if page:
                    ctx.listing_truncated = True
                return

            rows = self._parse_rows(host, body)
            if not rows:
                return

            fresh = 0
            conclusively_stale = 0
            for job in rows:
                if ctx.should_stop():
                    ctx.listing_truncated = True
                    return
                if ctx.cutoff_iso and job.posted_at and job.posted_at < ctx.cutoff_iso:
                    conclusively_stale += 1
                    continue
                fresh += 1
                yield job

            if fresh:
                stale_streak = 0
            elif conclusively_stale == len(rows):
                # Every row on this page carried a REAL date and every one was
                # older than the cutoff. An undated page proves nothing and is
                # deliberately not counted here.
                stale_streak += 1
                if stale_streak > tolerance:
                    return
            if len(rows) < size:
                return

        ctx.listing_truncated = True

    def _parse_rows(self, host: str, body: str) -> List[RawJob]:
        """Rows from one RMK search page. Returns [] for anything unexpected."""
        try:
            from lxml import html as lxml_html
        except Exception:  # pragma: no cover - lxml is a hard dependency
            return []
        try:
            doc = lxml_html.fromstring(body)
        except Exception:
            return []

        try:
            nodes = doc.xpath('//tr[contains(@class,"data-row")]')
            if not nodes:
                # Some deployments render list items instead of table rows.
                nodes = doc.xpath('//*[contains(@class,"jobTitle-link")]'
                                  '/ancestor::*[self::li or self::tr][1]')
        except Exception:
            return []

        out: List[RawJob] = []
        seen: set = set()
        for node in nodes:
            job = self._row_to_raw(host, node)
            if job is None or job.source_job_id in seen:
                continue
            seen.add(job.source_job_id)
            out.append(job)
        return out

    def _row_to_raw(self, host: str, node: Any) -> Optional[RawJob]:
        try:
            anchors = node.xpath('.//a[contains(@class,"jobTitle-link")]') or \
                node.xpath('.//a[contains(@href,"/job/")]')
        except Exception:
            return None
        if not anchors:
            return None
        anchor = anchors[0]
        href = str(anchor.get("href") or "").strip()
        title = _text_of(anchor)
        if not href or not title:
            return None

        match = _JOB_ID_RE.search(href) or _REQ_ID_RE.search(href)
        # The href path is stable when no numeric id is exposed, so a themed
        # site still produces a usable identity rather than being dropped.
        rid = match.group(1) if match else slugify(href.split("?")[0], 80)
        if not rid:
            return None

        location = _first(node,
                          './/span[contains(@class,"jobLocation")]',
                          './/td[contains(@class,"colLocation")]')
        department = _first(node,
                            './/span[contains(@class,"jobDepartment")]',
                            './/td[contains(@class,"colDepartment")]')
        facility = _first(node,
                          './/span[contains(@class,"jobFacility")]',
                          './/td[contains(@class,"colFacility")]')
        raw_date = _first(node,
                          './/span[contains(@class,"jobDate")]',
                          './/td[contains(@class,"colDate")]')
        posted = to_iso(raw_date)

        return RawJob(
            source_job_id=str(rid),
            title=title,
            apply_url=urljoin(f"https://{host}/", href),
            location=location,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            raw_posted_text=raw_date,
            department=(department or facility)[:120],
            remote=looks_remote(title, location),
        )

    # -- shape 2: the optional RSS feed --------------------------------------

    def _from_rss(self, host: str, rss_path: str,
                  ctx: FetchContext) -> Iterator[RawJob]:
        """Opt-in feed. Best-effort: an absent or reshaped feed yields nothing."""
        path = rss_path if rss_path.startswith("/") else f"/{rss_path}"
        status, body = ctx.get_text(f"https://{host}{path}",
                                    timeout=self.policy.timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return
        try:
            from lxml import etree
            root = etree.fromstring(body.encode("utf-8", "ignore"))
            items = root.xpath("//item")
        except Exception:
            ctx.log("info", "successfactors: %s rss did not parse", host)
            return

        for item in items:
            link = _first(item, "./link/text()")
            title = _first(item, "./title/text()")
            if not link or not title:
                continue
            match = _JOB_ID_RE.search(link) or _REQ_ID_RE.search(link)
            rid = match.group(1) if match else slugify(link.split("?")[0], 80)
            if not rid:
                continue
            raw_date = _first(item, "./pubDate/text()")
            posted = to_iso(raw_date)
            yield RawJob(
                source_job_id=str(rid),
                title=title,
                apply_url=link,
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                raw_posted_text=raw_date,
                description=html_to_text(_first(item, "./description/text()"))[:15000],
                remote=looks_remote(title),
            )

    # -- shape 3: the RCM candidate portal -----------------------------------

    def _from_portal(self, endpoint: Endpoint, host: str,
                     ctx: FetchContext) -> Iterator[RawJob]:
        """Best-effort. This surface is usually a JS shell, and an empty result
        is the EXPECTED outcome rather than a failure to report."""
        tenant = str(endpoint.tenant or "").strip()
        if not tenant or "." in tenant:
            return
        status, body = ctx.get_text(f"https://{host}/career",
                                    params={"company": tenant},
                                    timeout=self.policy.timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return
        try:
            from lxml import html as lxml_html
            doc = lxml_html.fromstring(body)
            anchors = doc.xpath('//a[contains(@href,"job_req_id")'
                                ' or contains(@href,"job%5freq%5fid")]')
        except Exception:
            return

        seen: set = set()
        for anchor in anchors:
            href = str(anchor.get("href") or "").strip()
            title = _text_of(anchor)
            match = _REQ_ID_RE.search(href)
            if not match or not title:
                continue
            rid = match.group(1)
            if rid in seen:
                continue
            seen.add(rid)
            # The portal publishes no date on this surface. ADR-001: say so.
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=urljoin(f"https://{host}/", href),
                posted_at="",
                date_confidence=DATE_UNKNOWN,
                remote=looks_remote(title),
            )

    # -- identity ------------------------------------------------------------

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        """The employer the career site's `<title>` names, or None.

        SuccessFactors publishes no owner field on any anonymous surface, so
        the page title is the only thing the board itself says about whose it
        is. None means "cannot say", never "does not match".
        """
        host = self._host_of(endpoint)
        if not host:
            return None
        try:
            status, body = ctx.get_text(f"https://{host}/search/",
                                        timeout=self.policy.timeout,
                                        vendor=self.vendor)
        except Exception:
            return None
        if status != 200 or not body:
            return None
        match = re.search(r"<title[^>]*>(.{0,200}?)</title>", body,
                          re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip()
        name = _TITLE_PREFIX_RE.sub("", name).strip()
        previous = None
        while previous != name:
            previous = name
            name = _TITLE_TRIM_RE.sub("", name).strip(" -|:–—")
        low = name.lower()
        if (len(name) < 2 or len(name) > 120 or "not found" in low
                or "404" in low or "error" in low
                or low in {"jobs", "careers", "job search", "successfactors",
                           "sap successfactors"}):
            return None
        return name


register(SuccessFactorsProvider())
