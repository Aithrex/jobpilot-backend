"""iCIMS — HTML career portals, fetched with the protocol they actually speak.

Correcting the record
---------------------
`scripts/probe_new_ats.py` records iCIMS as Tier C: "every pattern returns HTML,
no JSON, no embedded job payload ... Do not build." The OBSERVATION was right and
the CONCLUSION was wrong, and it cost roughly 4,000 employers.

"Returns HTML" is a statement about a response, not about feasibility - `lxml`
has been a runtime dependency this whole time. The probe was built to find JSON
APIs, so the widest verdict it could ever reach was "this is not a JSON API".

What iCIMS actually needs (verified live 2026-08-17)
----------------------------------------------------
**1. A session-bound `hashed` token.** A bare
`GET /jobs/search?ss=1&in_iframe=1` returns 200 and ~26 KB of real HTML, but the
listing is empty and the page says "Sorry, no jobs were found that match your
search criteria." That is not an empty board - it is an unauthenticated search.
The shell page carries the token in its own form:

    <form id="searchForm" action=".../jobs/search?in_iframe=1&hashed=-435620505">

So the fetch is two steps: bootstrap the shell (collecting the `JSESSIONID`
cookie and the `hashed` value), then page results with both. Exactly the failure
mode already recorded for NeoGov in that same script - the endpoint was always
there, the first probe just did not speak its protocol.

**2. Vendor-wide pacing, not per-host.** `*.icims.com` is WILDCARD DNS, so every
candidate slug resolves and DNS proves nothing about whether a tenant exists.
More importantly the anti-bot wall is enforced per SOURCE IP across all tenants:
six concurrent requests spread over sixty DIFFERENT `careers-*.icims.com`
hostnames returned `405` with an identical 2,115-byte body titled "Human
Verification" - on all sixty. Serial requests spaced ~3s ran indefinitely at 200.

`HostPolicy` keys spacing on hostname, so it would treat twenty iCIMS tenants as
twenty independent budgets and trip the wall every run. Hence
`pacing_key="icims.com"` and `max_concurrency=1`.

**3. 405 means "challenged", not "gone".** `http_get_json` retires any non-429
4xx as permanent, so without `challenge_statuses` every throttled tenant would be
marked dead and dropped from the catalog on the first burst.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN,
                                         STATUS_BLOCKED, STATUS_DEAD,
                                         STATUS_EMPTY, STATUS_VALIDATED,
                                         AtsProvider, ChallengeError,
                                         CrawlPolicy, Endpoint, FetchContext,
                                         PortalMovedError, RawJob,
                                         ValidationResult, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import (html_to_text, looks_minted_now,
                                         looks_remote, to_iso)

_HOST_SUFFIX = "icims.com"

# The interstitial. Matched on the BODY as well as the status because iCIMS
# serves the challenge with a 405, and a bare 405 is otherwise a legitimate
# "wrong method" answer we should not confuse with a soft block.
_CHALLENGE_MARKERS = ("human verification", "are you a robot",
                      "please verify you are a human")

# Job links look like /jobs/12345/some-title/job — the numeric id is the stable
# requisition key and survives title edits.
_JOB_HREF_RE = re.compile(r"/jobs/(\d+)/", re.IGNORECASE)
_FORM_ACTION_RE = re.compile(r'id="searchForm"[^>]*action="([^"]+)"',
                             re.IGNORECASE)
_HASHED_RE = re.compile(r"hashed=(-?\d+)")
_EMPTY_MARKER = "no jobs were found"

#: A portal that has MIGRATED off iCIMS still answers 200 — with a ~140-byte
#: body whose only content is a top-level JS redirect to the employer's new
#: careers site. Observed live on `us-careers-rivian.icims.com` (2026-08-18),
#: which was the ADR-004 seed and which now bounces to `careers.rivian.com`.
#: Without this the tenant reads as an ordinary empty board and is retained and
#: re-crawled forever, and the employer is lost — their jobs moved to a vendor
#: we do support, so the destination is captured and handed back for
#: re-fingerprinting rather than merely discarded.
_MOVED_RE = re.compile(
    r"""(?:window\.top|window|top|self|document)\.location(?:\.href)?\s*=\s*"""
    r"""['"]((?:https?:)?\\?/\\?/[^'"]+)['"]""",
    re.IGNORECASE)


#: Subdomains on icims.com that are infrastructure, not employer portals.
#: Wildcard DNS means these resolve like any tenant, so they must be excluded by
#: name or every discovery pass would manufacture bogus endpoints from a CDN
#: reference in someone's page source.
_NON_TENANT_LABELS = frozenset({
    "www", "login", "config", "provider", "images", "media", "static",
    "cdn01", "cdn02", "cdn03", "support", "help", "developer", "api",
    "uploads", "opphub", "icims",
})
#: Employee-only boards. These serve real, well-formed job rows -- Rivian's
#: `internal-careers-rivian` returned 50 -- so nothing about the RESPONSE marks
#: them as unusable; only the name does. Ingesting them would show job seekers
#: openings they cannot apply to, which is worse than missing the employer.
_INTERNAL_LABEL_RE = re.compile(
    r"(?:^|-)(?:internal|employee|emp|associate|colleague|redeploy)(?:-|$)",
    re.IGNORECASE)
#: iCIMS posting locations arrive coded as `US-GA-Atlanta` (country-region-city)
#: rather than as anything a job seeker types. Recognised here so `_tidy_location`
#: can turn it into "Atlanta, GA, US" before it reaches `normalize_posting` —
#: shipping the coded form would make every iCIMS row miss location matching.
_CODED_LOC_RE = re.compile(
    r"^([A-Z]{2})-([A-Z]{2}|[A-Z]{3})-(.+)$")

_PAGE_OF_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
_RESULTS_OF_RE = re.compile(
    r"Results?\s+[\d,]+\s*(?:-|–|to)\s*[\d,]+\s+of\s+([\d,]+)", re.IGNORECASE)


class IcimsProvider(AtsProvider):
    vendor = "icims"
    label = "iCIMS"

    termless = True
    has_detail = True
    #: What `list_jobs` produces, which is the only thing this may claim.
    #:
    #: Measured across live portals on 2026-08-18: the job CARD carries job
    #: number, type, division, department, category, location and remote
    #: classification — but a posted date on almost none of them. The date lives
    #: on the detail page's `JobPosting` JSON-LD, so `fetch_detail` is what
    #: upgrades an individual row to `exact`.
    #:
    #: Declaring `DATE_EXACT` here while the listing yields none is not a
    #: cosmetic mismatch: it is a provider promising the inventory something it
    #: never delivers, and `scripts/live_provider_check.py` fails a vendor for
    #: exactly that.
    date_confidence = DATE_UNKNOWN

    api_hosts = (f".{_HOST_SUFFIX}",)
    portal_hosts = (f".{_HOST_SUFFIX}",)

    policy = CrawlPolicy(
        # THE important line in this file. See the module docstring: the wall is
        # per source IP across every tenant hostname, so all tenants must share
        # one bucket.
        pacing_key=_HOST_SUFFIX,
        min_interval=2.5,
        jitter=0.8,
        max_concurrency=1,
        timeout=20.0,
        detail_timeout=15.0,
        challenge_statuses=frozenset({403, 405, 429, 503}),
        challenge_markers=_CHALLENGE_MARKERS,
        # A tripped wall persists; sitting out 30 min beats hammering it and
        # extending the block for every other tenant.
        cooldown_seconds=1800.0,
        page_size=20,          # iCIMS renders 20 rows per page, not tunable
        max_pages=25,
    )

    # One pattern is enough because `endpoint_from_match` reads the host itself:
    # the prefix varies far too much (`careers-`, `us-careers-`,
    # `internal-careers-`, none at all) to be worth enumerating.
    fingerprints = (
        fp(r"https?://(?P<tenant>[A-Za-z0-9][A-Za-z0-9_\-]*)\.icims\.com",
           confidence=90),
    )

    # -- identity ------------------------------------------------------------

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        """The SUBDOMAIN LABEL is the identity, verbatim.

        Deriving a "company slug" by stripping a `careers-` prefix is wrong:
        real portals use `careers-rivian`, `us-careers-rivian`,
        `internal-careers-rivian` and bare `rivian` on the same employer, and
        those are DIFFERENT portals with different job sets (external vs
        internal vs regional). Stripping to a common slug both loses portals and
        synthesises hostnames that do not exist.
        """
        host = (urlparse(url).hostname or "").strip().lower()
        if not host.endswith(_HOST_SUFFIX):
            return None
        label = host[: -(len(_HOST_SUFFIX) + 1)]
        if not label or "." in label:
            return None
        if label in _NON_TENANT_LABELS:
            return None
        if _INTERNAL_LABEL_RE.search(label):
            return None
        return Endpoint(vendor=self.vendor, tenant=label, host=host)

    @staticmethod
    def _host_of(endpoint: Endpoint) -> str:
        # An observed host always wins: it is the one we know answers. Only
        # synthesise when a legacy catalog row carried a bare slug.
        host = (endpoint.host or "").strip().lower()
        if host.endswith(_HOST_SUFFIX):
            return host
        tenant = (endpoint.tenant or "").strip().lower()
        if "careers" in tenant:
            return f"{tenant}.{_HOST_SUFFIX}"
        return f"careers-{tenant}.{_HOST_SUFFIX}"

    # -- fetching ------------------------------------------------------------

    def _get(self, ctx: FetchContext, url: str, params: Dict[str, Any],
             timeout: float) -> str:
        status, body = ctx.get_text(url, params=params, timeout=timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status, "human verification wall")
        if status != 200 or not body:
            return ""
        return body

    def _bootstrap(self, endpoint: Endpoint,
                   ctx: FetchContext) -> Tuple[str, Optional[str]]:
        """Fetch the shell page; return (html, hashed_token).

        The token is what turns "no jobs were found" into an actual result set,
        so a missing one is worth logging rather than silently paging an empty
        board forever.
        """
        host = self._host_of(endpoint)
        body = self._get(ctx, f"https://{host}/jobs/search",
                         {"ss": "1", "in_iframe": "1"}, self.policy.timeout)
        if not body:
            return "", None
        moved = self.moved_to(body)
        if moved:
            ctx.log("info", "icims: %s has migrated to %s", endpoint.tenant,
                    moved)
            raise PortalMovedError(self.vendor, moved)
        m = _FORM_ACTION_RE.search(body)
        hashed = None
        if m:
            action = m.group(1).replace("&amp;", "&")
            hm = _HASHED_RE.search(action)
            hashed = hm.group(1) if hm else None
        if hashed is None:
            ctx.log("info", "icims: %s served no hashed token", endpoint.tenant)
        return body, hashed

    @staticmethod
    def moved_to(body: str) -> str:
        """Destination URL when this portal is now just a redirect stub.

        Only a body that is ESSENTIALLY nothing but the redirect counts. Live
        listing pages also assign `location.href` in their own scripts, and
        reading one of those as a migration would retire a working board — so
        two independent conditions must hold:

          * the page carries NO job link at all. A real board always links its
            requisitions (or says it has none); a redirect stub links nothing.
            This is the load-bearing check.
          * the body is tiny. The observed stub was 143 bytes against ~26 KB for
            a real page, so this is a wide margin, and it is a backstop rather
            than the primary signal precisely because a small employer's board
            could in principle be short.
        """
        if not body or len(body) > 2000:
            return ""
        if _JOB_HREF_RE.search(body):
            return ""
        m = _MOVED_RE.search(body)
        if not m:
            return ""
        dest = m.group(1).replace("\\/", "/")
        if dest.startswith("//"):
            dest = "https:" + dest
        return dest[:400]

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        host = self._host_of(endpoint)
        shell, hashed = self._bootstrap(endpoint, ctx)
        if not shell:
            return

        seen: set = set()
        for page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                return
            params: Dict[str, Any] = {"ss": "1", "in_iframe": "1",
                                      "searchRelation": "keyword_all",
                                      "pr": str(page)}
            if hashed:
                params["hashed"] = hashed
            # Page 0 was already fetched as the shell only when no token was
            # needed; with a token the shell is unauthenticated, so refetch.
            body = self._get(ctx, f"https://{host}/jobs/search", params,
                             self.policy.timeout)
            if not body:
                return

            rows = list(self._parse_rows(endpoint, host, body))
            if not rows:
                # An explicit "no jobs" on page 0 is a real empty board; on a
                # later page it is simply the end of pagination.
                return

            new = 0
            for job in rows:
                if job.source_job_id in seen:
                    continue
                seen.add(job.source_job_id)
                new += 1
                yield job
            # iCIMS clamps `pr` past the last page and re-serves the final page,
            # so "every row was a duplicate" is the real end-of-list signal.
            if new == 0:
                return
            if len(rows) < self.policy.page_size:
                return

    # -- parsing -------------------------------------------------------------

    def _parse_rows(self, endpoint: Endpoint, host: str,
                    body: str) -> Iterator[RawJob]:
        """Read the job cards iCIMS actually renders.

        The first cut of this walked bare `//a[contains(@href,"/jobs/")]` and
        read the nearest ancestor's flattened text. That produced rows, so it
        looked like it worked, but every field was wrong in a way only a live
        check catches:

          * titles came out as "Job Title Program Assistant (NGO) ..." because
            iCIMS puts a `<span class="sr-only field-label">Job Title </span>`
            inside the anchor for screen readers, and `text_content()` happily
            concatenates it;
          * locations were empty on ~80% of rows, because the location is in a
            `<dd>` several nodes away and the regex was fishing for "City, ST"
            in flattened text that also contained the whole JD snippet;
          * dates were never found at all, while the class still declared
            `date_confidence = DATE_EXACT`.

        The markup is in fact well structured and stable across portal themes,
        so parse it as the structure it is:

            <li class="iCIMS_JobCardItem">
              <div class="... title">
                <a class="iCIMS_Anchor" href=".../jobs/172004/slug/job">
                  <span class="sr-only field-label">Job Title </span>
                  <h3>Program Assistant (NGO) - The Carter Center</h3>
              <div class="... description">KEY RESPONSIBILITIES: ...</div>
              <dl class="iCIMS_JobHeaderGroup">
                <dt class="iCIMS_JobHeaderField">Job Type</dt>
                <dd class="iCIMS_JobHeaderData">Temporary Full-Time</dd>
                ... Department / Job Category / Location / Posted Date ...

        Reading the `<dl>` gives location, employment type, department and — on
        portals that publish it — a real posted date, so most rows no longer
        need the 2.5s detail fetch that was previously the only way to learn any
        of it.
        """
        try:
            from lxml import html as lxml_html
        except Exception:  # pragma: no cover - lxml is a hard dependency
            yield from self._parse_rows_regex(endpoint, host, body)
            return

        try:
            doc = lxml_html.fromstring(body)
        except Exception:
            yield from self._parse_rows_regex(endpoint, host, body)
            return

        base = f"https://{host}"
        emitted: set = set()

        cards = doc.xpath('//li[contains(@class,"iCIMS_JobCardItem")]')
        if not cards:
            # Older/behind-the-times portal themes render a table instead of
            # cards. Fall back to the anchor walk rather than reporting an empty
            # board, but keep the same field extraction.
            cards = doc.xpath('//*[contains(@class,"iCIMS_JobsTable")]'
                              '//*[self::li or self::tr]')
        if not cards:
            yield from self._parse_rows_anchors(endpoint, host, doc)
            return

        for card in cards:
            anchors = card.xpath('.//a[contains(@href,"/jobs/")]')
            if not anchors:
                continue
            anchor = anchors[0]
            href = anchor.get("href") or ""
            m = _JOB_HREF_RE.search(href)
            if not m:
                continue
            rid = m.group(1)
            if rid in emitted:
                continue

            title = self._title_of(anchor)
            if not title:
                continue
            emitted.add(rid)

            fields = self._header_fields(card)
            description = self._card_description(card)

            location = self._location_of(fields)
            posted = self._posted_of(fields)
            yield RawJob(
                source_job_id=rid,
                title=title[:200],
                apply_url=urljoin(base, href.split("?")[0]),
                location=location,
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                description=description[:4000],
                employment_type=self._first(fields, ("job type", "type",
                                                     "employment type",
                                                     "position type")),
                department=self._first(fields, ("department", "division",
                                                "job category", "category")),
                remote=self._remote_of(fields, title, location),
                detail_url=urljoin(base, href.split("?")[0]),
            )

    # -- card field extraction ----------------------------------------------

    @staticmethod
    def _title_of(anchor: Any) -> str:
        """Clean title text for one job anchor.

        `.sr-only` spans are screen-reader field labels ("Job Title "), not part
        of the title, and they are the reason a naive `text_content()` prefixes
        every row. Prefer the heading iCIMS wraps the real title in; fall back to
        the anchor text with label spans removed, then to the `title` attribute
        (which carries "<id> - <title>").
        """
        for node in anchor.xpath('.//h2 | .//h3 | .//h4'):
            text = " ".join((node.text_content() or "").split())
            if text:
                return text

        clone = anchor
        try:
            import copy
            clone = copy.deepcopy(anchor)
            for junk in clone.xpath('.//*[contains(@class,"sr-only")]'
                                    ' | .//*[contains(@class,"field-label")]'):
                junk.getparent().remove(junk)
        except Exception:
            pass
        text = " ".join((clone.text_content() or "").split())
        if text:
            return text

        attr = " ".join((anchor.get("title") or "").split())
        # "172004 - Program Assistant" -> "Program Assistant"
        return re.sub(r"^\d+\s*-\s*", "", attr)

    @staticmethod
    def _header_fields(card: Any) -> Dict[str, str]:
        """`{lowercased label: value}` from the card's `<dl>` header group.

        Labels vary per portal ("Campus Location (For Posting) : Location",
        "Location : Name", "Requisition Post Information* : Posted Date"), which
        is why callers match on substrings rather than exact keys.
        """
        out: Dict[str, str] = {}
        for tag in card.xpath('.//div[contains(@class,"iCIMS_JobHeaderTag")]'):
            dts = tag.xpath('.//dt')
            dds = tag.xpath('.//dd')
            if not dts or not dds:
                continue
            label = " ".join((dts[0].text_content() or "").split()).lower()
            value = " ".join((dds[0].text_content() or "").split())
            label = label.strip(" :*")
            if label and value:
                out.setdefault(label, value[:300])
        # Some themes emit a flat <dt>/<dd> sequence with no wrapper div.
        if not out:
            dl = card.xpath('.//dl')
            for node in dl:
                dts = node.xpath('./dt')
                dds = node.xpath('./dd')
                for dt, dd in zip(dts, dds):
                    label = " ".join((dt.text_content() or "").split())
                    value = " ".join((dd.text_content() or "").split())
                    label = label.strip(" :*").lower()
                    if label and value:
                        out.setdefault(label, value[:300])
        return out

    @staticmethod
    def _first(fields: Dict[str, str], needles: Tuple[str, ...]) -> str:
        for needle in needles:
            for label, value in fields.items():
                if needle in label:
                    return value
        return ""

    def _location_of(self, fields: Dict[str, str]) -> str:
        """Best location string from the header fields.

        iCIMS commonly emits BOTH a coded posting location (`US-GA-Atlanta`) and
        a friendly site name (`Carter Center`). The coded one is the one a job
        seeker can filter on, so it wins; the site name is only used when it is
        all there is.
        """
        coded = ""
        friendly = ""
        for label, value in fields.items():
            if "location" not in label and "campus" not in label:
                continue
            if _CODED_LOC_RE.match(value):
                coded = coded or value
            elif "," in value or re.search(r"\b[A-Z]{2}\b", value):
                coded = coded or value
            else:
                friendly = friendly or value
        best = coded or friendly
        if not best:
            return ""
        return self._tidy_location(best)[:160]

    @staticmethod
    def _tidy_location(value: str) -> str:
        """`US-GA-Atlanta` -> `Atlanta, GA, US`.

        The coded form is compact but not what `us_geo`/`normalize_posting`
        expect, and shipping it raw would make every iCIMS row fail location
        matching against a resume that says "Atlanta, GA".
        """
        m = _CODED_LOC_RE.match(value)
        if not m:
            return value
        country, region, city = m.group(1), m.group(2), m.group(3)
        city = city.replace("-", " ").strip()
        parts = [p for p in (city, region, country) if p]
        return ", ".join(parts)

    def _posted_of(self, fields: Dict[str, str]) -> str:
        for label, value in fields.items():
            if "posted" not in label and "date" not in label:
                continue
            iso = to_iso(value)
            if iso:
                return iso
            m = self._DATE_RE.search(value)
            if m:
                iso = to_iso(m.group(1))
                if iso:
                    return iso
        return ""

    @staticmethod
    def _remote_of(fields: Dict[str, str], title: str, location: str
                   ) -> Optional[bool]:
        """iCIMS portals often carry an explicit remote classification.

        An explicit field beats guessing from the title, but "N/A or Other" is
        the portal saying it does not classify this req — which is not the same
        as "not remote", so it must fall through to the heuristic rather than
        being read as False.
        """
        for label, value in fields.items():
            if "remote" not in label and "work classification" not in label:
                continue
            low = value.lower()
            if "n/a" in low or "other" in low or not low.strip():
                continue
            if "fully remote" in low or low.strip() in ("remote", "yes"):
                return True
            if "hybrid" in low or "on-site" in low or "onsite" in low:
                return False
        return looks_remote(title, location)

    @staticmethod
    def _card_description(card: Any) -> str:
        nodes = card.xpath('.//*[contains(@class,"description")]')
        for node in nodes:
            text = " ".join((node.text_content() or "").split())
            if len(text) > 40:
                return text
        return ""

    def _parse_rows_anchors(self, endpoint: Endpoint, host: str,
                            doc: Any) -> Iterator[RawJob]:
        """Last-resort walk when no recognisable card container exists."""
        base = f"https://{host}"
        emitted: set = set()
        for anchor in doc.xpath('//a[contains(@href, "/jobs/")]'):
            href = anchor.get("href") or ""
            m = _JOB_HREF_RE.search(href)
            if not m:
                continue
            rid = m.group(1)
            if rid in emitted:
                continue
            title = self._title_of(anchor)
            if not title:
                continue
            emitted.add(rid)
            yield RawJob(
                source_job_id=rid,
                title=title[:200],
                apply_url=urljoin(base, href.split("?")[0]),
                date_confidence=DATE_UNKNOWN,
                remote=looks_remote(title),
                detail_url=urljoin(base, href.split("?")[0]),
            )

    def _parse_rows_regex(self, endpoint: Endpoint, host: str,
                          body: str) -> Iterator[RawJob]:
        """Fallback when lxml cannot parse the document at all."""
        base = f"https://{host}"
        seen: set = set()
        for m in re.finditer(
                r'<a[^>]+href="([^"]*?/jobs/(\d+)/[^"]*)"[^>]*>(.*?)</a>',
                body, re.IGNORECASE | re.DOTALL):
            href, rid, inner = m.group(1), m.group(2), m.group(3)
            if rid in seen:
                continue
            title = " ".join(html_to_text(inner).split())
            if not title:
                continue
            seen.add(rid)
            yield RawJob(source_job_id=rid, title=title[:200],
                         apply_url=urljoin(base, href.split("?")[0]),
                         date_confidence=DATE_UNKNOWN,
                         detail_url=urljoin(base, href.split("?")[0]))

    #: Dates as portals write them in the header `<dd>`: 8/14/2026, 2026-08-14,
    #: "August 14, 2026". Parsed only out of a field whose LABEL mentions a date,
    #: never out of flattened row text — a JD snippet routinely contains dates
    #: ("start date 9/1"), and reading one of those as the posting date would
    #: re-date a stale req as fresh, which ADR-001 forbids.
    _DATE_RE = re.compile(
        r"\b((?:\d{1,2}/\d{1,2}/\d{4})|(?:\d{4}-\d{2}-\d{2})|"
        r"(?:[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s*\d{4}))\b")

    # -- detail --------------------------------------------------------------

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        """The JD body, plus schema.org JobPosting when the portal emits it.

        Detail pages are the ONLY place most iCIMS portals publish a machine
        readable `datePosted`, so this is where a row's date confidence is
        usually earned. It is also expensive at 2.5s/request, so the caller must
        budget it (`MAX_DETAIL_FETCHES`).
        """
        url = job.detail_url or job.apply_url
        if not url:
            return None
        host = self._host_of(endpoint)
        body = self._get(ctx, url, {"in_iframe": "1"}, self.policy.detail_timeout)
        if not body:
            return None

        ld = self._json_ld(body)
        description = ""
        posted = ""
        location = ""
        emp_type = ""
        comp: Any = None
        if ld:
            description = html_to_text(ld.get("description") or "")
            # Some portals mint `datePosted` from their own clock at render
            # time (see `looks_minted_now`). Such a value is not a posting date
            # and must not be treated as one: it is either dropped by the
            # freshness cutoff, silently zeroing the employer, or — shifted the
            # other way — re-dates a stale req as fresh.
            raw_posted = ld.get("datePosted")
            if looks_minted_now(raw_posted):
                ctx.log("info", "icims: %s publishes render-time datePosted "
                                "(%s); treating as undated",
                        endpoint.tenant, raw_posted)
                posted = ""
            else:
                posted = to_iso(raw_posted)
            emp_type = str(ld.get("employmentType") or "")
            comp = ld.get("baseSalary")
            location = self._ld_location(ld)

        if not description:
            description = self._body_text(body)

        if not description and not posted:
            return None
        return RawJob(
            source_job_id=job.source_job_id,
            title=job.title,
            apply_url=job.apply_url,
            description=description[:40000],
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            location=location,
            employment_type=emp_type,
            compensation=comp,
        )

    @staticmethod
    def _json_ld(body: str) -> Optional[Dict[str, Any]]:
        import json
        for m in re.finditer(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                body, re.IGNORECASE | re.DOTALL):
            try:
                data = json.loads(m.group(1).strip())
            except ValueError:
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if isinstance(item, dict) and \
                        str(item.get("@type") or "").lower() == "jobposting":
                    return item
        return None

    @staticmethod
    def _ld_location(ld: Dict[str, Any]) -> str:
        loc = ld.get("jobLocation")
        entries = loc if isinstance(loc, list) else [loc]
        parts: List[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            addr = entry.get("address")
            if not isinstance(addr, dict):
                continue
            chunk = ", ".join(str(addr.get(k)) for k in
                              ("addressLocality", "addressRegion",
                               "addressCountry") if addr.get(k))
            if chunk:
                parts.append(chunk)
        return "; ".join(parts[:3])[:160]

    @staticmethod
    def _body_text(body: str) -> str:
        try:
            from lxml import html as lxml_html
            doc = lxml_html.fromstring(body)
        except Exception:
            return ""
        for sel in ('//div[contains(@class,"iCIMS_JobContent")]',
                    '//div[contains(@class,"iCIMS_Description")]',
                    '//div[@id="jobDescription"]',
                    '//div[contains(@class,"iCIMS_MainWrapper")]'):
            nodes = doc.xpath(sel)
            if nodes:
                text = html_to_text(
                    __import__("lxml.html", fromlist=["tostring"])
                    .tostring(nodes[0], encoding="unicode"))
                if len(text) > 120:
                    return text
        return ""

    # -- validation ----------------------------------------------------------

    def validate(self, endpoint: Endpoint,
                 ctx: FetchContext) -> ValidationResult:
        """Only a successful CONTENT fetch proves a tenant exists.

        Wildcard DNS on `*.icims.com` means every slug resolves and every
        hostname connects, so reachability is meaningless here - a nonexistent
        tenant returns a perfectly valid 200 page. The tenant is real only if the
        portal renders job rows or an explicit empty-board message.
        """
        try:
            shell, hashed = self._bootstrap(endpoint, ctx)
        except PortalMovedError as exc:
            # Gone from iCIMS for good, but the employer still hires. The
            # destination is recorded so the discovery queue can fingerprint
            # the new host rather than losing the company.
            return ValidationResult(STATUS_DEAD, 0,
                                    detail=f"migrated to {exc.destination}")
        except ChallengeError as exc:
            return ValidationResult(STATUS_BLOCKED, detail=str(exc))
        if not shell:
            # Not "dead": wildcard DNS means a real tenant always serves SOMETHING,
            # so serving nothing is a transport failure, not proof of absence.
            return ValidationResult(STATUS_BLOCKED, detail="no shell page")

        host = self._host_of(endpoint)
        params: Dict[str, Any] = {"ss": "1", "in_iframe": "1",
                                  "searchRelation": "keyword_all", "pr": "0"}
        if hashed:
            params["hashed"] = hashed
        try:
            body = self._get(ctx, f"https://{host}/jobs/search", params,
                             self.policy.timeout)
        except ChallengeError as exc:
            return ValidationResult(STATUS_BLOCKED, detail=str(exc))
        if not body:
            return ValidationResult(STATUS_BLOCKED, detail="no listing page")

        rows = list(self._parse_rows(endpoint, host, body))
        if rows:
            total = 0
            m = _RESULTS_OF_RE.search(" ".join(body.split()))
            if m:
                try:
                    total = int(m.group(1).replace(",", ""))
                except ValueError:
                    total = 0
            return ValidationResult(STATUS_VALIDATED, total or len(rows),
                                    rows[0].title)
        if _EMPTY_MARKER in body.lower():
            # A real portal that currently has nothing open. Keep it: it will
            # have openings again, and rediscovering it costs more than keeping.
            return ValidationResult(STATUS_EMPTY, 0,
                                    detail="portal reports zero results")
        if not hashed:
            # Observed on a live board (us-careers-rivian) that had served 50 jobs
            # minutes earlier: the throttle withholds the session token, so the
            # search renders an empty shell. That is indistinguishable from a dead
            # tenant by content alone -- but we know we never got a session, so the
            # check is inconclusive. Calling it dead here retires real employers.
            return ValidationResult(STATUS_BLOCKED,
                                    detail="no session token; verdict withheld")
        return ValidationResult(STATUS_DEAD, detail="no job rows, no empty marker")


register(IcimsProvider())
