"""AppOne (HR Services, Inc., now Paychex Recruiting) career search.

Observed live 2026-10-22 on Universal Background Screening's board, the exact
URL its own careers page links:

    GET https://www2.appone.com/Search/Search.aspx?ServerVar=<host>.appone.com
        ASP.NET search form titled "<Employer> Jobs". Its footer links
        "Browse All Jobs by: Category | Location | State" - plain GETs to
        recruiting.myapps.paychex.com/appone/Branding/ReqTemplate/
        BrowseAllJobsbyCategory.asp?...&ClientID=18117&B_ID=91&...
        &servervar=<host>.appone.com
        The client and branding ids exist only in those links, so they are
        READ from the page each time, never guessed or stored.

    GET <that BrowseAllJobsbyCategory link>
        every open requisition, grouped under "<Employer> <Category> Jobs" and
        a location row, each `MainInfoReq.asp?R_ID=<id>&B_ID=..` with link
        text "<title> - <location> - Job". 10 links, 10 distinct ids; no
        pagination control.

    GET recruiting.myapps.paychex.com/appone/MainInfoReq.asp?R_ID=<id>&...
        label/value rows (Position, Description, Position Requirements,
        Full-Time/Part-Time, Location) and "This position is currently
        accepting applications." No posting date for the requisition itself
        (dates printed in the "we also recommend" sidebar describe OTHER
        requisitions and are not borrowed), so rows are DATE_UNKNOWN.

The tenant is the exact `ServerVar`. `recruiting.myapps.paychex.com` serves
many employers; nothing is listed except through the browse link the
employer's own search page prints.
"""
from __future__ import annotations

import html as _html
import re
from typing import Dict, Iterator, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from services.ats_providers.base import (DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote

VENDOR = "appone"
_SEARCH_HOST = "www2.appone.com"
_REQ_HOST = "recruiting.myapps.paychex.com"
_SERVERVAR_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}\.appone\.com$")
_BROWSE_RE = re.compile(
    r"""href=['"](https://recruiting\.myapps\.paychex\.com/appone/Branding/ReqTemplate/"""
    r"""BrowseAllJobsbyCategory\.asp\?[^'"<>\s]+)['"]""", re.IGNORECASE)
_REQ_LINK_RE = re.compile(
    r"""<a[^>]+href=["'](?P<href>[^"']*MainInfoReq\.asp\?[^"']*R_ID=(?P<id>\d{3,12})[^"']*)["'][^>]*>"""
    r"""(?P<text>.*?)</a>""", re.IGNORECASE | re.DOTALL)
_BOLD_ROW_RE = re.compile(r"""<td[^>]*id=['"]bold['"][^>]*>(?P<t>[^<]*)</td>""",
                          re.IGNORECASE)
_FIELD_RE = re.compile(
    r"""<td\s+class=['"]smallheader['"][^>]*>\s*(?P<label>[^<]+?)\s*</td>\s*<td[^>]*>"""
    r"""(?P<value>.*?)</td>\s*</tr>""", re.IGNORECASE | re.DOTALL)
_ACCEPTING_RE = re.compile(r"currently accepting applications", re.IGNORECASE)
_NOT_ACCEPTING_RE = re.compile(r"no longer accepting applications|position (?:has been )?filled",
                               re.IGNORECASE)


def _txt(value: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", value or ""))) \
        .replace(" ", " ").strip()


def employer_from_title(body: str) -> str:
    m = re.search(r"<title>\s*(.*?)\s*</title>", body or "", re.IGNORECASE | re.DOTALL)
    name = _txt(m.group(1)) if m else ""
    return re.sub(r"\s+Jobs$", "", name).strip()


def browse_link(body: str, server_var: str) -> str:
    """The employer's own "browse all by category" link, only if it names
    this exact ServerVar."""
    for m in _BROWSE_RE.finditer(body or ""):
        url = _html.unescape(m.group(1))
        qs = {k.lower(): v for k, v in parse_qs(urlparse(url).query).items()}
        if (qs.get("servervar") or [""])[0].lower() == server_var.lower():
            return url
    return ""


def parse_browse(body: str, employer: str) -> List[Dict[str, str]]:
    """Requisitions on a browse page, in page order, deduped by R_ID."""
    events = []
    for m in _BOLD_ROW_RE.finditer(body or ""):
        events.append((m.start(), "bold", _txt(m.group("t"))))
    for m in _REQ_LINK_RE.finditer(body or ""):
        events.append((m.start(), "req", m))
    events.sort(key=lambda e: e[0])
    category, location = "", ""
    out: Dict[str, Dict[str, str]] = {}
    prefix = (employer + " ").lower() if employer else ""
    for _pos, kind, value in events:
        if kind == "bold":
            if not value:
                continue
            if value.lower().endswith(" jobs"):
                cat = value[:-5].strip()
                if prefix and cat.lower().startswith(prefix):
                    cat = cat[len(prefix):].strip()
                category, location = cat, ""
            else:
                location = value
            continue
        rid = value.group("id")
        text = _txt(value.group("text"))
        text = re.sub(r"\s+-\s+Job$", "", text)
        if location and text.endswith(f" - {location}"):
            text = text[: -len(f" - {location}")].strip()
        if not text:
            continue
        href = urljoin(f"https://{_REQ_HOST}/appone/", _html.unescape(value.group("href")))
        out.setdefault(rid, {"id": rid, "title": text, "location": location,
                             "department": category, "url": href})
    return list(out.values())


def parse_detail(body: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for m in _FIELD_RE.finditer(body or ""):
        label = _txt(m.group("label")).lower()
        if label in fields:
            continue
        fields[label] = m.group("value")
    out: Dict[str, str] = {}
    sections = []
    for label, heading in (("description", ""), ("position requirements", "Requirements")):
        text = html_to_text(fields.get(label, "")).strip()
        if text:
            sections.append(f"{heading}\n{text}" if heading else text)
    if sections:
        out["description"] = "\n\n".join(sections)
    for label, key in (("location", "location"), ("full-time/part-time", "employment_type"),
                       ("position", "position")):
        value = _txt(fields.get(label, ""))
        if value:
            out[key] = value
    if _ACCEPTING_RE.search(body or ""):
        out["accepting_applications"] = "true"
    elif _NOT_ACCEPTING_RE.search(body or ""):
        out["accepting_applications"] = "false"
    return out


class AppOneProvider(AtsProvider):
    vendor = VENDOR
    label = "AppOne (Paychex Recruiting)"

    termless = True
    has_detail = True
    date_confidence = DATE_UNKNOWN
    slug_probe = False

    api_hosts = (_SEARCH_HOST, _REQ_HOST)
    portal_hosts = (_SEARCH_HOST, _REQ_HOST)

    policy = CrawlPolicy(
        pacing_key=None,
        min_interval=1.0,
        jitter=0.25,
        max_concurrency=1,
        timeout=20.0,
        detail_timeout=15.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=500,
        max_pages=1,
    )

    fingerprints = (
        fp(r"https?://www2?\.appone\.com/Search/Search\.aspx\?"
           r"(?:[^\s\"'<>#]*?(?:&|&amp;|&#0?38;))?ServerVar="
           r"(?P<tenant>[A-Za-z0-9][A-Za-z0-9-]{0,80}\.appone\.com)", confidence=95),
        fp(r"https?://recruiting\.myapps\.paychex\.com/appone/[^\s\"'<>#]*?[?&]"
           r"servervar=(?P<tenant>[A-Za-z0-9][A-Za-z0-9-]{0,80}\.appone\.com)",
           confidence=90),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip().lower()
        if not _SERVERVAR_RE.match(tenant):
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant, host=_SEARCH_HOST)

    def board_url(self, endpoint: Endpoint) -> str:
        tenant = str(endpoint.tenant or "").strip().lower()
        return (f"https://{_SEARCH_HOST}/Search/Search.aspx?ServerVar={tenant}"
                if _SERVERVAR_RE.match(tenant) else "")

    def _get(self, ctx: FetchContext, url: str, params=None, detail: bool = False):
        status, body = ctx.get_text(url, params=params or {},
                                    timeout=(self.policy.detail_timeout if detail
                                             else self.policy.timeout),
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        return status, body

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        tenant = str(endpoint.tenant or "").strip().lower()
        if not _SERVERVAR_RE.match(tenant):
            return
        status, page = self._get(ctx, f"https://{_SEARCH_HOST}/Search/Search.aspx",
                                 {"ServerVar": tenant})
        if status != 200 or not page:
            ctx.listing_truncated = True
            return
        link = browse_link(page, tenant)
        if not link:
            ctx.log("info", "appone: %s search page has no browse link", tenant)
            ctx.listing_truncated = True
            return
        employer = employer_from_title(page)
        status, body = self._get(ctx, link)
        if status != 200 or not body:
            ctx.listing_truncated = True
            return
        rows = parse_browse(body, employer)
        if not rows and "MainInfoReq" not in body and "Back to Search" not in body:
            ctx.listing_truncated = True
            return
        for row in rows:
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            extra = {"application_method": "ats_url"}
            if employer:
                extra["board_employer"] = employer
            yield RawJob(
                source_job_id=row["id"], title=row["title"][:200],
                apply_url=row["url"], detail_url=row["url"],
                location=row["location"][:160], department=row["department"][:120],
                posted_at="", date_confidence=DATE_UNKNOWN,
                remote=looks_remote(row["title"], row["location"]),
                extra=extra,
            )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        url = str(job.detail_url or "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != _REQ_HOST \
                or not parsed.path.lower().endswith("/maininforeq.asp"):
            return None
        rid = (parse_qs(parsed.query).get("R_ID") or [""])[0]
        if rid != str(job.source_job_id):
            return None
        status, body = self._get(ctx, url, detail=True)
        if status != 200 or not body:
            return None
        fields = parse_detail(body)
        if not fields.get("description"):
            return None
        extra = {}
        if "accepting_applications" in fields:
            extra["accepting_applications"] = fields["accepting_applications"] == "true"
        return RawJob(source_job_id=job.source_job_id, title=job.title,
                      apply_url=job.apply_url,
                      description=fields["description"][:15000],
                      location=fields.get("location", ""),
                      employment_type=fields.get("employment_type", ""),
                      extra=extra)

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        tenant = str(endpoint.tenant or "").strip().lower()
        if not _SERVERVAR_RE.match(tenant):
            return None
        status, page = ctx.get_text(f"https://{_SEARCH_HOST}/Search/Search.aspx",
                                    params={"ServerVar": tenant},
                                    timeout=self.policy.timeout, vendor=self.vendor)
        if status != 200 or not page:
            return None
        return employer_from_title(page) or None


register(AppOneProvider())
