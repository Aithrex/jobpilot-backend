"""Paycor Recruiting (formerly Newton Software) public career embeds.

What the employer's page actually does (observed live 2026-10-21)
-----------------------------------------------------------------
An employer embeds ONE script:

    <script id="gnewtonjs"
            src="https://recruitingbypaycor.com/career/iframe.action?clientId=<32 hex>">
    <script ... src="https://recruitingbypaycor.com/career/aggregateIframe.action?clientId=<32 hex>">

`newton.newtonsoftware.com/career/iframe.action` answers **308** to the same
path on `recruitingbypaycor.com`. The "script" is jQuery plus a block of
configuration (`clientId`, `clientUrl`, `aggregate`, `careerHomeAction`) and a
`getNewtonIframeSrc()` that inserts an iframe at

    {careerUrl}/{careerHomeAction}.action?clientId=<clientId>&parentUrl=<page>

with `careerHomeAction` = `CareerHome` for a single board and
`AggregateCareerHome` for an aggregate (several business units). That iframe
document is server-rendered HTML holding every posting, and each posting
opens `JobIntroduction.action?clientId=<unit>&id=<32 hex>` - which is exactly
what this provider requests, and nothing else.

    * `parentUrl` is not needed: the same 38 postings came back without it.
    * Spring Arbor's embed carries `test=testing`. The embed script was
      byte-identical with and without it and `getNewtonIframeSrc()` never
      forwards `test`, so it cannot change which postings the board serves.
      It is dropped from identity.
    * Two listing layouts exist on the same vendor: the aggregate renders
      `<a href=".../JobIntroduction.action?...">` rows grouped under a header
      (City, when the column header says so); the single board renders
      collapsed department bars whose rows live in inline JS arrays
      (`jobName["<dept>"][n - 1] = "..."`, `jobId[...]`). Both are read.
      Each department bar states "(N positions)", which is the source-reported
      count we check the parse against.
    * An aggregate lists several BUSINESS UNITS, each its own clientId on the
      posting link (RevSpring: "RevSpring Inc" and "Apex Revenue
      Technologies", the latter folded into RevSpring in 2018-19). The unit is
      kept on every row, and an endpoint may restrict itself to named units
      with `extra["business_units"]` - a shared board is never silently
      attributed wholesale.
    * There is NO posting date anywhere (list or detail), so rows are
      DATE_UNKNOWN. The detail page gives title, location, the employer's
      requisition number ("Job Id"), "# of Openings" and the description.

`recruiting.paycor.com` is a different product and is NOT assumed to speak
this protocol; nothing here fingerprints it.
"""
from __future__ import annotations

import html as _html
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from services.ats_providers.base import (DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote

VENDOR = "paycor"
_HOST = "recruitingbypaycor.com"
_LEGACY_HOST = "newton.newtonsoftware.com"
_BASE = f"https://{_HOST}/career"
_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
SITE_AGGREGATE = "aggregate"

#: A link to a posting, wherever on the page it is.
_JOB_HREF_RE = re.compile(
    r"https?://(?:recruitingbypaycor\.com|newton\.newtonsoftware\.com)"
    r"/career/JobIntroduction\.action\?[^\"'<>\s]+", re.IGNORECASE)
_JS_NAME_RE = re.compile(
    r'jobName\["(?P<dep>[0-9a-fA-F]{32})"\]\[(?P<n>\d+)\s*-\s*1\]\s*=\s*"(?P<v>(?:[^"\\]|\\.)*)"')
_JS_ID_RE = re.compile(
    r'jobId\["(?P<dep>[0-9a-fA-F]{32})"\]\[(?P<n>\d+)\s*-\s*1\]\s*=\s*"(?P<v>[0-9a-fA-F]{32})"')
_OPTION_RE = re.compile(
    r'<option\s+value="(?P<id>[0-9a-fA-F]{32})"\s*>(?P<name>[^<]{1,160})</option>',
    re.IGNORECASE)
_BAR_COUNT_RE = re.compile(r"\((\d{1,5})\s+positions?\)", re.IGNORECASE)
#: The board's own form: present on every career home, with or without jobs.
_TEMPLATE_RE = re.compile(r'<form\s+name="candidateHome"', re.IGNORECASE)
_POOL_RE = re.compile(r"\b(job pool|general application|talent (?:pool|community)|"
                      r"general (?:staff|adjunct) applications?)\b", re.IGNORECASE)


def _clean(value: Any) -> str:
    text = _html.unescape(str(value or ""))
    text = text.replace("\\'", "'").replace('\\"', '"')
    return re.sub(r"\s+", " ", text).strip()


def _purpose_hint(*parts: str) -> str:
    return "talent_community" if any(_POOL_RE.search(p or "") for p in parts) else ""


def parse_listing(body: str) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """Every posting a career-home document lists, and what the page says.

    Returns `(rows, info)`; `info` carries `recognised` (the board template is
    present), `reported` (sum of "(N positions)" bars, or None) and `units`
    (business-unit clientId -> name, from the aggregate's own filter).
    Never raises: an unrecognised document is zero rows with
    `recognised=False`, which the caller reports as unverified, not empty.
    """
    body = body or ""
    options = {m.group("id"): _clean(m.group("name"))
               for m in _OPTION_RE.finditer(body)}
    rows: Dict[str, Dict[str, str]] = {}

    # Layout 1: real anchors (aggregate rows), with the group header above.
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(body) if body.strip() else None
    except Exception:
        doc = None
    group_is_city = bool(re.search(
        r"gnewtonSortByJob[^>]*>\s*<a>\s*(City|Location)\s*</a>", body, re.IGNORECASE))
    if doc is not None:
        for anchor in doc.xpath("//a[@href]"):
            href = str(anchor.get("href") or "")
            if not _JOB_HREF_RE.match(href):
                continue
            qs = parse_qs(urlparse(href).query)
            job_id = (qs.get("id") or [""])[0]
            unit = (qs.get("clientId") or [""])[0]
            if not _ID_RE.match(job_id) or not _ID_RE.match(unit):
                continue
            title = _clean(anchor.text_content())
            if not title:
                continue
            group = ""
            headers = anchor.xpath(
                "preceding::div[contains(@class,'gnewtonCareerGroupHeaderClass')][1]")
            if headers:
                group = _clean(headers[0].text_content())
            rows.setdefault(job_id, {
                "id": job_id, "unit": unit, "title": title,
                "location": group if group_is_city else "",
                "department": "" if group_is_city else group,
            })

    # Layout 2: collapsed department bars backed by inline JS arrays.
    names = {(m.group("dep"), m.group("n")): m.group("v")
             for m in _JS_NAME_RE.finditer(body)}
    unit_m = re.search(r'var\s+urlFront\s*=\s*"[^"]*clientId=([0-9a-fA-F]{32})', body)
    page_unit = unit_m.group(1) if unit_m else ""
    for m in _JS_ID_RE.finditer(body):
        title = _clean(names.get((m.group("dep"), m.group("n")), ""))
        job_id = m.group("v")
        if not title or not page_unit:
            continue
        rows.setdefault(job_id, {
            "id": job_id, "unit": page_unit, "title": title, "location": "",
            "department": options.get(m.group("dep"), ""),
        })

    counts = [int(c) for c in _BAR_COUNT_RE.findall(body)]
    units: Dict[str, str] = {}
    sel = re.search(r'<select[^>]*id="gnewtonBusinessUnit".*?</select>', body,
                    re.IGNORECASE | re.DOTALL)
    if sel:
        units = {m.group("id"): _clean(m.group("name"))
                 for m in _OPTION_RE.finditer(sel.group(0))}
    info = {"recognised": bool(_TEMPLATE_RE.search(body)),
            "reported": sum(counts) if counts else None,
            "units": units}
    return list(rows.values()), info


def parse_detail(body: str) -> Dict[str, str]:
    """Fields of a `JobIntroduction.action` document; {} when unrecognised."""
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(body or "")
    except Exception:
        return {}

    def cell(element_id: str) -> Any:
        found = doc.xpath(f"//*[@id='{element_id}']")
        return found[0] if found else None

    def text(element_id: str, label: str = "") -> str:
        node = cell(element_id)
        if node is None:
            return ""
        value = _clean(node.text_content())
        if label and value.lower().startswith(label.lower()):
            value = value[len(label):].strip(" : ")
        return value

    out: Dict[str, str] = {}
    title = text("gnewtonJobPosition", "Position")
    if title:
        out["title"] = title
    location = cell("gnewtonJobLocationInfo")
    if location is not None:
        parts = [_clean(t) for t in location.itertext() if _clean(t)]
        out["location"] = "; ".join(parts[:4])
    req = text("gnewtonJobID", "Job Id")
    if req:
        out["requisition_number"] = req
    openings = text("gnewtonJobOpening", "# of Openings")
    if openings.isdigit():
        out["openings"] = openings
    desc = cell("gnewtonJobDescriptionText")
    if desc is not None:
        from lxml import html as lxml_html
        out["description"] = html_to_text(
            lxml_html.tostring(desc, encoding="unicode")).strip()
    return out


class PaycorRecruitingProvider(AtsProvider):
    vendor = VENDOR
    label = "Paycor Recruiting"

    termless = True
    has_detail = True
    date_confidence = DATE_UNKNOWN
    #: The tenant is an opaque 32-hex clientId. Nothing to guess.
    slug_probe = False

    api_hosts = (_HOST,)
    portal_hosts = (_HOST,)

    policy = CrawlPolicy(
        pacing_key=_HOST,
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
        fp(r"https?://(?:recruitingbypaycor\.com|newton\.newtonsoftware\.com)/career/"
           r"(?P<mode>aggregateIframe|AggregateCareerHome)\.action\?"
           r"(?:[^\s\"'<>#]*?(?:&|&amp;|&#0?38;))?clientId=(?P<tenant>[0-9a-fA-F]{32})",
           confidence=95),
        fp(r"https?://(?:recruitingbypaycor\.com|newton\.newtonsoftware\.com)/career/"
           r"(?P<mode>iframe|CareerHome|CareerHomeSearch)\.action\?"
           r"(?:[^\s\"'<>#]*?(?:&|&amp;|&#0?38;))?clientId=(?P<tenant>[0-9a-fA-F]{32})",
           confidence=94),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not _ID_RE.match(tenant):
            return None
        mode = (fields.get("mode") or "").lower()
        site = SITE_AGGREGATE if mode.startswith("aggregate") else ""
        return Endpoint(vendor=self.vendor, tenant=tenant, site=site, host=_HOST)

    def board_url(self, endpoint: Endpoint) -> str:
        tenant = str(endpoint.tenant or "").strip()
        if not _ID_RE.match(tenant):
            return ""
        action = "AggregateCareerHome" if endpoint.site == SITE_AGGREGATE else "CareerHome"
        return f"{_BASE}/{action}.action?clientId={tenant}"

    @staticmethod
    def _detail_url(unit: str, job_id: str, aggregate: bool) -> str:
        agg = "fromAggregate=true&" if aggregate else ""
        return f"{_BASE}/JobIntroduction.action?{agg}clientId={unit}&id={job_id}&lang=en"

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        tenant = str(endpoint.tenant or "").strip()
        if not _ID_RE.match(tenant):
            return
        aggregate = endpoint.site == SITE_AGGREGATE
        action = "AggregateCareerHome" if aggregate else "CareerHome"
        status, body = ctx.get_text(f"{_BASE}/{action}.action",
                                    params={"clientId": tenant},
                                    timeout=self.policy.timeout, vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            # Not "no jobs": we did not read the board.
            ctx.listing_truncated = True
            return
        rows, info = parse_listing(body)
        if not info["recognised"] and not rows:
            ctx.log("info", "paycor: %s served an unrecognised page", tenant)
            ctx.listing_truncated = True
            return
        wanted = {str(u) for u in ((endpoint.extra or {}).get("business_units") or ())}
        reported = info.get("reported")
        if reported is not None and len(rows) < reported:
            # The page says it holds more than we could read.
            ctx.listing_truncated = True
        for row in rows:
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            if wanted and row["unit"] not in wanted:
                continue
            url = self._detail_url(row["unit"], row["id"], aggregate)
            extra = {"business_unit_client_id": row["unit"],
                     "application_method": "ats_url"}
            unit_name = info["units"].get(row["unit"])
            if unit_name:
                extra["business_unit"] = unit_name
            hint = _purpose_hint(row["title"], row["department"])
            if hint:
                extra["posting_purpose_hint"] = hint
            yield RawJob(
                source_job_id=row["id"],
                title=row["title"][:200],
                apply_url=url,
                detail_url=url,
                location=row["location"],
                department=row["department"][:120],
                posted_at="",
                date_confidence=DATE_UNKNOWN,
                remote=looks_remote(row["title"], row["location"]),
                extra=extra,
            )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        url = str(job.detail_url or "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != _HOST:
            return None
        qs = parse_qs(parsed.query)
        if not _ID_RE.match((qs.get("id") or [""])[0]) or \
                not _ID_RE.match((qs.get("clientId") or [""])[0]):
            return None
        status, body = ctx.get_text(url, params={}, timeout=self.policy.detail_timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return None
        fields = parse_detail(body)
        if not fields.get("description"):
            return None
        extra = {k: fields[k] for k in ("requisition_number", "openings") if k in fields}
        return RawJob(source_job_id=job.source_job_id, title=job.title,
                      apply_url=job.apply_url,
                      location=fields.get("location", ""),
                      description=fields["description"][:15000],
                      extra=extra)

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        """The business-unit name an aggregate prints for its own clientId."""
        if endpoint.site != SITE_AGGREGATE:
            return None
        status, body = ctx.get_text(
            f"{_BASE}/AggregateCareerHome.action", params={"clientId": endpoint.tenant},
            timeout=self.policy.timeout, vendor=self.vendor)
        if status != 200 or not body:
            return None
        _rows, info = parse_listing(body)
        return info["units"].get(str(endpoint.tenant)) or None


register(PaycorRecruitingProvider())
