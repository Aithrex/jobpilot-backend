"""BreezyHR public career portals (`<company>.breezy.hr`).

Observed live 2026-10-21/22 on `destro-ai.breezy.hr`, the board Destro AI's
own careers page links (`www.destroai.com/careers` -> `destro-ai.breezy.hr/`):

    GET https://<tenant>.breezy.hr/
        server-rendered HTML; every opening is an `<li class="position">`
        card linking `/p/<12 hex id>-<slug>`, with an `<h2>` title, a
        `li.location` and a `li.type`, grouped under `h2.group-header`
        (department when the header's icon is the building, location when it
        is the map marker). 13 cards, 13 distinct ids; no pagination control.

    GET https://<tenant>.breezy.hr/p/<id>-<slug>
        the posting, with a schema.org `JobPosting` block: `datePosted`
        (a real date, "2026-09-16"), `employmentType`, `jobLocation`,
        the full `description`, and `hiringOrganization.name` ("Destro AI").

Only those two requests are made. Breezy also serves a `/json` feed, but the
portal page never requests it, so this provider does not either: the rule for
this package is to implement what the employer's page actually calls.

A job-detail URL (what the research matrix recorded) is one role, not the
board; a fingerprint on any `<tenant>.breezy.hr` URL yields the whole board.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urljoin, urlparse

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import (html_to_text, looks_minted_now,
                                         looks_remote, to_iso)

VENDOR = "breezy"
_SUFFIX = ".breezy.hr"
_TENANT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_ID_RE = re.compile(r"^/p/(?P<id>[0-9a-f]{8,16})(?:-[^/?#]*)?/?$", re.IGNORECASE)
_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)
#: Breezy renders some labels as client-side i18n placeholders.
_TYPE_LABELS = {
    "%LABEL_POSITION_TYPE_FULL_TIME%": "Full-Time",
    "%LABEL_POSITION_TYPE_PART_TIME%": "Part-Time",
    "%LABEL_POSITION_TYPE_CONTRACT%": "Contract",
    "%LABEL_POSITION_TYPE_TEMPORARY%": "Temporary",
    "%LABEL_POSITION_TYPE_INTERN%": "Internship",
    "%LABEL_POSITION_TYPE_SEASONAL%": "Seasonal",
    "%LABEL_POSITION_TYPE_VOLUNTEER%": "Volunteer",
    "%LABEL_POSITION_TYPE_PER_DIEM%": "Per Diem",
}


def _ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_board(body: str, base_url: str) -> List[Dict[str, str]]:
    """Every position card on a portal page, deduped by id. Never raises."""
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(body or "")
    except Exception:
        return []
    out: Dict[str, Dict[str, str]] = {}
    for card in doc.xpath("//li[contains(concat(' ', normalize-space(@class), ' '),"
                          " ' position ')]"):
        links = [a for a in card.xpath(".//a[@href]")
                 if _ID_RE.match(urlparse(urljoin(base_url, a.get("href"))).path)]
        if not links:
            continue
        href = urljoin(base_url, links[0].get("href")).split("#")[0].split("?")[0]
        rid = _ID_RE.match(urlparse(href).path).group("id").lower()
        titles = card.xpath(".//h2")
        title = _ws(titles[0].text_content()) if titles else ""
        if not title:
            continue
        loc = card.xpath(".//li[contains(@class,'location')]")
        typ = card.xpath(".//li[contains(@class,'type')]")
        etype = _ws(typ[0].text_content()) if typ else ""
        group_kind, group = "", ""
        header = card.xpath("preceding::h2[contains(@class,'group-header')][1]")
        if header:
            group = _ws(header[0].text_content())
            icon = " ".join(header[0].xpath(".//i/@class"))
            group_kind = "location" if "map-marker" in icon else "department"
        out.setdefault(rid, {
            "id": rid, "url": href, "title": title,
            "location": _ws(loc[0].text_content()) if loc else
            (group if group_kind == "location" else ""),
            "employment_type": _TYPE_LABELS.get(etype, "" if etype.startswith("%") else etype),
            "department": group if group_kind == "department" else "",
        })
    return list(out.values())


def job_posting_ld(body: str) -> Dict[str, Any]:
    """The `JobPosting` object on a page, including inside `@graph` or with
    an `@type` list. {} when there is none."""
    for raw in _LD_RE.findall(body or ""):
        try:
            data = json.loads(raw.strip())
        except (TypeError, ValueError):
            continue
        stack = list(data) if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if not isinstance(item, dict):
                continue
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(k).lower() == "jobposting" for k in kinds):
                return item
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
    return {}


def _ld_location(ld: Dict[str, Any]) -> str:
    places = ld.get("jobLocation")
    places = places if isinstance(places, list) else [places]
    out: List[str] = []
    for place in places:
        if not isinstance(place, dict):
            continue
        addr = place.get("address") if isinstance(place.get("address"), dict) else {}
        text = ", ".join(x for x in (_ws(addr.get("addressLocality")),
                                     _ws(addr.get("addressRegion")),
                                     _ws(addr.get("addressCountry"))) if x)
        if text and text not in out:
            out.append(text)
    return "; ".join(out[:4])


class BreezyProvider(AtsProvider):
    vendor = VENDOR
    label = "Breezy HR"

    termless = True
    #: The portal has no date and no body; both are on the posting page.
    has_detail = True
    date_confidence = DATE_UNKNOWN
    #: One subdomain per company and a definitive answer per guess would make
    #: a probe cheap, but a guessed slug proves only that SOME board exists.
    #: Discovery reaches Breezy through the employer's own link.
    slug_probe = False

    api_hosts = (_SUFFIX,)
    portal_hosts = (_SUFFIX,)
    reserved_tenants = ("app", "api", "assets-cdn", "gallery-cdn", "marketplace",
                        "help", "developer", "developers", "status", "blog")

    policy = CrawlPolicy(
        pacing_key="breezy.hr",
        min_interval=1.0,
        jitter=0.25,
        max_concurrency=1,
        timeout=15.0,
        detail_timeout=12.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=500,
        max_pages=1,
    )

    fingerprints = (
        fp(r"https?://(?P<tenant>[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)\.breezy\.hr"
           r"(?:[/?#\"'\s]|$)", confidence=90),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip().lower()
        if not _TENANT_RE.match(tenant) or self.is_reserved_tenant(tenant):
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant, host=f"{tenant}{_SUFFIX}")

    @staticmethod
    def _base(endpoint: Endpoint) -> str:
        tenant = str(endpoint.tenant or "").strip().lower()
        return f"https://{tenant}{_SUFFIX}/" if _TENANT_RE.match(tenant) else ""

    def board_url(self, endpoint: Endpoint) -> str:
        return self._base(endpoint)

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        base = self._base(endpoint)
        if not base or self.is_reserved_tenant(endpoint.tenant):
            return
        status, body = ctx.get_text(base, params={}, timeout=self.policy.timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            ctx.listing_truncated = True
            return
        rows = parse_board(body, base)
        if not rows and "positions" not in body:
            # Neither cards nor the portal's positions container: we did not
            # read a board, so this is not a verified zero.
            ctx.listing_truncated = True
            return
        for row in rows:
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            yield RawJob(
                source_job_id=row["id"],
                title=row["title"][:200],
                apply_url=row["url"],
                detail_url=row["url"],
                location=row["location"][:160],
                employment_type=row["employment_type"],
                department=row["department"][:120],
                posted_at="",
                date_confidence=DATE_UNKNOWN,
                remote=looks_remote(row["title"], row["location"]),
                extra={"application_method": "ats_url"},
            )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        base = self._base(endpoint)
        url = str(job.detail_url or "")
        parsed = urlparse(url)
        if not base or parsed.scheme != "https" or \
                (parsed.hostname or "").lower() != urlparse(base).hostname or \
                not _ID_RE.match(parsed.path):
            return None
        status, body = ctx.get_text(url, params={}, timeout=self.policy.detail_timeout,
                                    vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return None
        ld = job_posting_ld(body)
        if not ld:
            return None
        raw_posted = ld.get("datePosted")
        posted = "" if looks_minted_now(raw_posted) else to_iso(raw_posted)
        description = html_to_text(ld.get("description") or "").strip()
        if not description and not posted:
            return None
        org = ld.get("hiringOrganization") if isinstance(
            ld.get("hiringOrganization"), dict) else {}
        extra = {"hiring_organization": _ws(org.get("name"))} if org.get("name") else {}
        etype = ld.get("employmentType")
        return RawJob(
            source_job_id=job.source_job_id, title=job.title, apply_url=job.apply_url,
            description=description[:15000],
            location=_ld_location(ld),
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            raw_posted_text=_ws(raw_posted),
            employment_type=_ws(", ".join(etype) if isinstance(etype, list) else etype),
            extra=extra,
        )

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        """The name the portal prints in its own title ("Openings at X")."""
        base = self._base(endpoint)
        if not base:
            return None
        status, body = ctx.get_text(base, params={}, timeout=self.policy.timeout,
                                    vendor=self.vendor)
        if status != 200 or not body:
            return None
        m = re.search(r"<title>\s*Openings at\s+([^<]{2,120}?)\s*</title>", body,
                      re.IGNORECASE)
        return _ws(m.group(1)) if m else None


register(BreezyProvider())
