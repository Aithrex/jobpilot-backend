"""Avature career sites - server-rendered SearchJobs / JobDetail pages.

Observed live 2026-10-22 (no browser, no private API):

  * Frequentis - `jobs.frequentis.com/careers/SearchJobs` (linked from
    frequentis.com/en/careers): 20 `JobDetail/<slug>/<id>` links a page, the
    page's own paging links `SearchJobs/?jobOffset=20|40|60`, and a source
    count "64 results". The detail page carries schema.org JobPosting with a
    real `datePosted` (2025-10-10 on an older requisition, so it is not the
    render date) and `identifier.value` = the id; its `description` is the
    literal string "None", so the body is read from the page.
  * Flint Hills Resources - its careers page (now on kochinc.com) links the
    SHARED Koch board filtered to FHR:
    `koch.avature.net/en_US/careers/SearchJobs?732=6297&tags=rm.kcm.web.kcm-004`
    (31 results, 6 a page). The page's own paging links carry the same
    filter re-encoded (`3_85_3=6297&3_85_3_format=1077&jobRecordsPerPage=6
    &jobOffset=N`). So this provider requests the EXACT filtered URL the
    employer linked, and then follows only the page's own paging links -
    never an unfiltered Koch search, never all of Koch attributed to FHR.

Endpoint shape: `tenant` = the host, `site` = the portal path plus the exact
filter query the employer linked (`en_US/careers?732=6297&tags=...`), so two
entities on one shared board are two members.

Hosts: `.avature.net` plus the custom hosts verified above, named one by one
- never an open-ended custom-host allowlist.
"""
from __future__ import annotations

import html as _html
import json
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import (html_to_text, looks_minted_now,
                                         looks_remote, to_iso)

VENDOR = "avature"
#: Custom career hosts verified to be Avature, one by one.
CUSTOM_HOSTS = ("jobs.frequentis.com",)
_HOST_RE = re.compile(r"^(?:[a-z0-9-]+\.avature\.net|" +
                      "|".join(re.escape(h) for h in CUSTOM_HOSTS) + r")$")
_PORTAL_RE = re.compile(r"^(?:[a-z]{2}_[A-Z]{2}/)?[A-Za-z0-9_-]{2,40}$")
_DETAIL_RE = re.compile(r"/JobDetail/(?:[^/?#]+/)?(?P<id>\d{1,12})/?$")
_RESULTS_RE = re.compile(r"(\d{1,6})\s+results?\b", re.IGNORECASE)
_PAGE_CAP = 25
_LD_RE = re.compile(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                    re.IGNORECASE | re.DOTALL)


def _ws(value: Any) -> str:
    return re.sub(r"\s+", " ", _html.unescape(str(value or ""))).replace(" ", " ").strip()


def split_site(site: str) -> Tuple[str, str]:
    """`en_US/careers?732=6297` -> ("en_US/careers", "732=6297")."""
    portal, _, query = str(site or "").partition("?")
    return portal.strip("/"), query


def parse_listing(body: str, page_url: str, host: str) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(body or "")
    except Exception:
        return [], {"reported": None, "offsets": {}}
    rows: Dict[str, Dict[str, str]] = {}
    for a in doc.xpath("//a[@href]"):
        url = urljoin(page_url, str(a.get("href"))).split("#")[0]
        u = urlparse(url)
        m = _DETAIL_RE.search(u.path)
        if not m or (u.hostname or "").lower() != host:
            continue
        title = _ws(a.text_content())
        if not title or re.match(r"(?i)^(apply|view|more|details)\b", title):
            continue
        card = a
        for _ in range(6):
            parent = card.getparent()
            if parent is None or len({x.get("href") for x in parent.xpath(".//a[contains(@href,'/JobDetail/')]")}) > 1:
                break
            card = parent
        text = _ws(" ".join(card.itertext()))
        loc = re.search(r"Location\s*:\s*(.+?)(?=\s+(?:Job Number|Category|Posted|Date)\s*:|$)", text)
        rows.setdefault(m.group("id"), {
            "id": m.group("id"), "title": title, "url": url,
            "location": loc.group(1).strip() if loc else "",
            "subtitle": text.replace(title, "", 1).strip()[:300]})
    offsets: Dict[int, str] = {}
    for a in doc.xpath("//a[contains(@href,'jobOffset=')]"):
        url = urljoin(page_url, _html.unescape(str(a.get("href"))))
        u = urlparse(url)
        if (u.hostname or "").lower() != host or "/SearchJobs" not in u.path:
            continue
        try:
            off = int((parse_qs(u.query).get("jobOffset") or ["-1"])[0])
        except ValueError:
            continue
        if off >= 0:
            offsets.setdefault(off, url)
    rep = _RESULTS_RE.search(html_to_text(body or ""))
    return list(rows.values()), {"reported": int(rep.group(1)) if rep else None,
                                 "offsets": offsets}


def _job_posting(body: str) -> Dict[str, Any]:
    for raw in _LD_RE.findall(body or ""):
        try:
            data = json.loads(raw.strip())
        except (TypeError, ValueError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict):
                kinds = item.get("@type")
                kinds = kinds if isinstance(kinds, list) else [kinds]
                if any(str(k).lower() == "jobposting" for k in kinds):
                    return item
    return {}


class AvatureProvider(AtsProvider):
    vendor = VENDOR
    label = "Avature"

    termless = True
    has_detail = True
    date_confidence = DATE_UNKNOWN        # listing: none; detail: exact
    slug_probe = False

    api_hosts = (".avature.net",) + CUSTOM_HOSTS
    portal_hosts = api_hosts
    reserved_tenants = ("www", "static", "cdn")

    policy = CrawlPolicy(
        pacing_key=None, min_interval=1.0, jitter=0.25, max_concurrency=1,
        timeout=20.0, detail_timeout=15.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=20, max_pages=_PAGE_CAP,
    )

    fingerprints = (
        fp(r"https?://(?P<host>[a-z0-9-]+\.avature\.net|" +
           "|".join(re.escape(h) for h in CUSTOM_HOSTS) +
           r")/(?P<site>(?:[a-z]{2}_[A-Z]{2}/)?[A-Za-z0-9_-]{2,40})/SearchJobs/?"
           r"(?:\?(?P<query>[^\s\"'<>#]*))?", confidence=92),
    )

    def endpoint_from_match(self, fields: Dict[str, str], url: str) -> Optional[Endpoint]:
        host = (fields.get("host") or "").lower()
        portal = fields.get("site") or ""
        if not _HOST_RE.match(host) or not _PORTAL_RE.match(portal):
            return None
        query = _html.unescape(fields.get("query") or "")
        # Keep the employer's filter EXACTLY; drop only paging.
        query = "&".join(p for p in query.split("&")
                         if p and not p.lower().startswith("joboffset="))
        site = f"{portal}?{query}" if query else portal
        return Endpoint(vendor=self.vendor, tenant=host, site=site, host=host)

    @staticmethod
    def _parts(endpoint: Endpoint) -> Tuple[str, str, str]:
        host = str(endpoint.tenant or endpoint.host or "").strip().lower()
        portal, query = split_site(endpoint.site)
        if not _HOST_RE.match(host) or not _PORTAL_RE.match(portal):
            return "", "", ""
        return host, portal, query

    def board_url(self, endpoint: Endpoint) -> str:
        host, portal, query = self._parts(endpoint)
        if not host:
            return ""
        return f"https://{host}/{portal}/SearchJobs" + (f"?{query}" if query else "")

    def list_jobs(self, endpoint: Endpoint, ctx: FetchContext) -> Iterator[RawJob]:
        url = self.board_url(endpoint)
        if not url:
            return
        host = self._parts(endpoint)[0]
        #: On a SHARED board (Koch), each row must also name this entity -
        #: a second guard behind the employer's own filtered link.
        entity = _ws((endpoint.extra or {}).get("entity_name")).lower()
        counts = {"reported": None, "parsed": 0, "filtered": 0}
        try:
            yield from self._walk(url, host, entity, counts, ctx)
        finally:
            if counts["filtered"]:
                ctx.log("info", "avature: %s dropped %d rows naming another entity",
                        host, counts["filtered"])
            try:
                ctx.options["avature_counts"] = dict(counts)
            except Exception:
                pass

    def _walk(self, url: str, host: str, entity: str, counts: Dict[str, Any],
              ctx: FetchContext) -> Iterator[RawJob]:
        seen: Dict[str, bool] = {}
        visited = set()
        offset = 0
        for _page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            visited.add(url)
            status, body = ctx.get_text(url, params={}, timeout=self.policy.timeout,
                                        vendor=self.vendor)
            if self.policy.is_challenge(status, body or ""):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not body:
                ctx.listing_truncated = True
                return
            rows, info = parse_listing(body, url, host)
            if info["reported"] is not None:
                counts["reported"] = info["reported"]
            if not rows and not seen:
                # A template with no rows proves nothing.
                ctx.listing_truncated = True
                return
            for row in rows:
                if row["id"] in seen:
                    continue
                seen[row["id"]] = True
                counts["parsed"] = len(seen)
                if entity and entity not in (row["subtitle"] + " " + row["title"]).lower():
                    # A shared board: this row names no hiring entity we own.
                    counts["filtered"] += 1
                    continue
                yield RawJob(
                    source_job_id=row["id"], title=row["title"][:200],
                    apply_url=row["url"], detail_url=row["url"],
                    location=row["location"][:200], posted_at="",
                    date_confidence=DATE_UNKNOWN,
                    remote=looks_remote(row["title"], row["location"]),
                    extra={"application_method": "ats_url",
                           **({"listing_subtitle": row["subtitle"]} if row["subtitle"] else {})},
                )
            reported = counts["reported"]
            later = sorted(o for o in info["offsets"] if o > offset)
            if not later or (reported is not None and len(seen) >= reported):
                if reported is not None and len(seen) < reported:
                    ctx.listing_truncated = True
                return
            offset = later[0]
            url = info["offsets"][offset]
            if url in visited:
                ctx.listing_truncated = True
                return
        ctx.listing_truncated = True

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        host = self._parts(endpoint)[0]
        url = str(job.detail_url or "")
        u = urlparse(url)
        if not host or u.scheme != "https" or (u.hostname or "").lower() != host \
                or not _DETAIL_RE.search(u.path):
            return None
        status, body = ctx.get_text(url, params={}, timeout=self.policy.detail_timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return None
        ld = _job_posting(body)
        description = html_to_text(ld.get("description") or "") if ld else ""
        if description.strip().lower() in ("none", "null"):
            description = ""
        try:
            from lxml import html as lxml_html
            doc = lxml_html.fromstring(body)
            for bad in doc.xpath("//script|//style|//nav|//header|//footer|//form|"
                                 "//*[contains(@class,'social-share')]"):
                bad.getparent().remove(bad)
            main = (doc.xpath("//*[contains(@class,'main__content')]")
                    or doc.xpath("//main") or [doc])[0]
            page_text = html_to_text(lxml_html.tostring(main, encoding="unicode")).strip()
            more = [_ws(s.text_content()) for s in doc.xpath(
                "//*[contains(concat(' ',normalize-space(@class),' '),' moreDetail ')]/span")]
        except Exception:
            page_text, more = "", []
        if len(description) < 300 and len(page_text) > len(description):
            description = page_text
        if not description:
            return None
        raw_posted = ld.get("datePosted") if ld else None
        posted = "" if (not raw_posted or looks_minted_now(raw_posted)) else to_iso(raw_posted)
        loc = ""
        if more:
            loc = more[0]
        out = RawJob(source_job_id=job.source_job_id, title=job.title, apply_url=job.apply_url,
                     description=description[:15000], location=loc,
                     posted_at=posted, date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                     raw_posted_text=_ws(raw_posted), extra={})
        if len(more) > 1 and more[1]:
            out.extra["legal_entity"] = more[1]
        org = ld.get("hiringOrganization") if ld else None
        if isinstance(org, dict) and org.get("name"):
            out.extra["hiring_organization"] = _ws(org["name"])
        return out

    def board_owner(self, endpoint: Endpoint, ctx: FetchContext) -> Optional[str]:
        return None


register(AvatureProvider())
