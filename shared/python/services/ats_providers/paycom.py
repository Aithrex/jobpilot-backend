"""Paycom applicant-tracking career portals.

Observed live 2026-10-22 on the Lincoln Institute of Land Policy's board, the
exact URL its own careers page links:

    GET https://www.paycomonline.net/v4/ats/web.php/jobs?clientkey=<32 hex>
        -> 302 /v4/ats/web.php/portal/<clientkey>/career-page
        HTML whose inline `var configsFromHost = {...}` carries an anonymous
        `sessionJWT` (2 h lifetime, names the client code) and a `libConfig`
        whose `atsPortalMantleServiceUrl` is the API origin
        (`https://portal-applicant-tracking.us-cent.paycomonline.net/`). The
        page then mounts the career-portal micro-frontend, which makes the two
        calls below with `Authorization: <sessionJWT>` (no scheme), `Locale`
        and `Translation-Highlights` headers - nothing else is requested here.

    POST {service}api/ats/job-posting-previews/search
        {"skip": n, "take": 10, "filtersForQuery": {...all filters empty...}}
        -> {"jobPostingPreviews": [...], "jobPostingPreviewsCount": 7}
        Rows: jobId, jobTitle, positionType, remoteType, locations, a
        150-character description TEASER, and `postedOn` as relative text
        ("2 days ago", or "" when the employer hides it).

    GET  {service}api/ats/job-postings/<jobId>
        -> {"jobPosting": {...}}: description + qualifications HTML, salary
        range, category, and `googleJobJson` - a schema.org JobPosting with a
        real `datePosted` ("2026-10-19") and the public posting URL
        `.../portal/<clientkey>/jobs/<jobId>`.

The clientkey is preserved exactly (it is the tenant). The session token is
read from the page on every listing and never stored or logged. The API origin
comes from the page, and is called only when it is an https host under
`paycomonline.net`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import urlparse

from services.ats_providers.base import (DATE_EXACT, DATE_RELATIVE, DATE_UNKNOWN,
                                         AtsProvider, ChallengeError, CrawlPolicy,
                                         Endpoint, FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import (from_relative, html_to_text,
                                         looks_minted_now, looks_remote, to_iso)

VENDOR = "paycom"
_HOST = "www.paycomonline.net"
_KEY_RE = re.compile(r"^[0-9A-Fa-f]{32}$")
_CONFIG_RE = re.compile(r"var\s+configsFromHost\s*=\s*(\{.*?\})\s*;\s*\n", re.DOTALL)
_PAGE = 10


def _portal(key: str) -> str:
    return f"https://{_HOST}/v4/ats/web.php/portal/{key}"


def session_from_page(body: str) -> Tuple[str, str]:
    """(sessionJWT, API origin) from a career page, or ("", "")."""
    m = _CONFIG_RE.search(body or "")
    if not m:
        return "", ""
    try:
        cfg = json.loads(m.group(1))
        lib = cfg.get("libConfig")
        lib = json.loads(lib) if isinstance(lib, str) else (lib or {})
    except (TypeError, ValueError, AttributeError):
        return "", ""
    token = str(cfg.get("sessionJWT") or "").strip()
    service = str(lib.get("atsPortalMantleServiceUrl") or "").strip()
    parsed = urlparse(service)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host.endswith(".paycomonline.net"):
        return token, ""
    if not re.match(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$", token):
        return "", service
    return token, service.rstrip("/") + "/"


def _session_key(key: str) -> str:
    return f"paycom_session:{key}"


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": token, "Locale": "en-US",
            "Translation-Highlights": "false"}


def _search_body(skip: int) -> Dict[str, Any]:
    return {"skip": skip, "take": _PAGE, "filtersForQuery": {
        "distanceFrom": 0, "workEnvironments": [], "positionTypes": [],
        "educationLevels": [], "categories": [], "travelTypes": [],
        "shiftTypes": [], "otherFilters": [], "keywordSearchText": "",
        "location": "", "sortOption": ""}}


class PaycomProvider(AtsProvider):
    vendor = VENDOR
    label = "Paycom"

    termless = True
    has_detail = True
    #: The listing's `postedOn` is relative text; the detail's JSON-LD date is
    #: exact and replaces it.
    date_confidence = DATE_RELATIVE
    slug_probe = False

    #: The API origin is regional (`portal-applicant-tracking.us-cent...`),
    #: so the suffix is the honest scope; the code still checks it per call.
    api_hosts = (".paycomonline.net",)
    portal_hosts = (_HOST,)

    policy = CrawlPolicy(
        pacing_key="paycomonline.net",
        min_interval=1.0,
        jitter=0.25,
        max_concurrency=1,
        timeout=20.0,
        detail_timeout=15.0,
        challenge_statuses=frozenset({429, 503}),
        page_size=_PAGE,
        max_pages=30,
    )

    fingerprints = (
        fp(r"https?://(?:www\.)?paycomonline\.net/v4/ats/web\.php/jobs\?"
           r"(?:[^\s\"'<>#]*?(?:&|&amp;|&#0?38;))?clientkey=(?P<tenant>[0-9A-Fa-f]{32})",
           confidence=95),
        fp(r"https?://(?:www\.)?paycomonline\.net/v4/ats/web\.php/portal/"
           r"(?P<tenant>[0-9A-Fa-f]{32})(?:[/?#]|$)", confidence=94),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        key = (fields.get("tenant") or "").strip()
        if not _KEY_RE.match(key):
            return None
        return Endpoint(vendor=self.vendor, tenant=key, host=_HOST)

    def board_url(self, endpoint: Endpoint) -> str:
        key = str(endpoint.tenant or "").strip()
        return (f"https://{_HOST}/v4/ats/web.php/jobs?clientkey={key}"
                if _KEY_RE.match(key) else "")

    def _session(self, endpoint: Endpoint, ctx: FetchContext) -> Tuple[str, str, str]:
        """(key, token, service). Raises on a challenge; empty strings when the
        page could not be read."""
        key = str(endpoint.tenant or "").strip()
        if not _KEY_RE.match(key):
            return "", "", ""
        status, body = ctx.get_text(f"https://{_HOST}/v4/ats/web.php/jobs",
                                    params={"clientkey": key},
                                    timeout=self.policy.timeout, vendor=self.vendor)
        if self.policy.is_challenge(status, body or ""):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not body:
            return key, "", ""
        token, service = session_from_page(body)
        return key, token, service

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        key, token, service = self._session(endpoint, ctx)
        if not key:
            return
        if not token or not service or ctx.post_json is None:
            ctx.log("info", "paycom: %s page carried no usable session", key)
            ctx.listing_truncated = True
            return
        # Held on THIS crawl's context only - never on the endpoint, whose
        # `to_doc()` is persisted.
        ctx.options[_session_key(key)] = (token, service)
        seen = set()
        total: Optional[int] = None
        for page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            status, payload = ctx.post_json(
                f"{service}api/ats/job-posting-previews/search",
                json_body=_search_body(page * _PAGE), timeout=self.policy.timeout,
                vendor=self.vendor, extra_headers=_headers(token))
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                ctx.listing_truncated = True
                return
            rows = payload.get("jobPostingPreviews")
            if not isinstance(rows, list):
                ctx.listing_truncated = True
                return
            try:
                total = int(payload.get("jobPostingPreviewsCount"))
            except (TypeError, ValueError):
                total = total
            for row in rows:
                job = self._to_raw(key, row)
                if job is None or job.source_job_id in seen:
                    continue
                seen.add(job.source_job_id)
                if ctx.cutoff_iso and job.posted_at and job.posted_at < ctx.cutoff_iso:
                    continue
                yield job
            if not rows or len(rows) < _PAGE or (total is not None and
                                                 (page + 1) * _PAGE >= total):
                if total is not None and len(seen) < total:
                    ctx.listing_truncated = True
                return
        ctx.listing_truncated = True

    @staticmethod
    def _to_raw(key: str, row: Any) -> Optional[RawJob]:
        if not isinstance(row, dict):
            return None
        rid = str(row.get("jobId") or "").strip()
        title = re.sub(r"\s+", " ", str(row.get("jobTitle") or "")).strip()
        if not rid.isdigit() or not title:
            return None
        rel = str(row.get("postedOn") or "").strip()
        posted = from_relative(rel) if rel else ""
        location = str(row.get("locations") or "").strip()
        remote_type = str(row.get("remoteType") or "").strip()
        url = f"{_portal(key)}/jobs/{rid}"
        return RawJob(
            source_job_id=rid, title=title[:200], apply_url=url, detail_url=url,
            location=location[:300],
            posted_at=posted,
            date_confidence=DATE_RELATIVE if posted else DATE_UNKNOWN,
            raw_posted_text=rel,
            # Teaser only; `fetch_detail` replaces it.
            description=html_to_text(row.get("description") or "")[:400],
            employment_type=str(row.get("positionType") or "").strip(),
            remote=(True if "remote" in remote_type.lower() and
                    "hybrid" not in remote_type.lower()
                    else looks_remote(title, location)),
            extra={"application_method": "ats_url",
                   **({"remote_type": remote_type} if remote_type else {})},
        )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        rid = str(job.source_job_id or "").strip()
        if not rid.isdigit():
            return None
        session = ctx.options.get(_session_key(str(endpoint.tenant or "")))
        if not session:
            key, token, service = self._session(endpoint, ctx)
            if not token or not service:
                return None
            session = (token, service)
            ctx.options[_session_key(key)] = session
        token, service = session
        status, payload = ctx.get_json(f"{service}api/ats/job-postings/{rid}",
                                       params={}, timeout=self.policy.detail_timeout,
                                       vendor=self.vendor, extra_headers=_headers(token))
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200 or not isinstance(payload, dict):
            return None
        post = payload.get("jobPosting")
        if not isinstance(post, dict):
            return None
        parts = []
        for body_key, title_key in (("description", "descriptionTitle"),
                                    ("qualifications", "qualificationsTitle")):
            text = html_to_text(post.get(body_key) or "").strip()
            if text:
                heading = str(post.get(title_key) or "").strip()
                parts.append(f"{heading}\n{text}" if heading and body_key != "description"
                             else text)
        ld: Dict[str, Any] = {}
        try:
            ld = json.loads(post.get("googleJobJson") or "{}")
        except (TypeError, ValueError):
            ld = {}
        raw_posted = ld.get("datePosted") if isinstance(ld, dict) else None
        posted = "" if looks_minted_now(raw_posted) else to_iso(raw_posted)
        if not parts and not posted:
            return None
        locs = [str(post.get("location") or "").strip()] + [
            str(x).strip() for x in (post.get("secondaryLocations") or []) if str(x).strip()]
        extra = {k: str(post.get(k)) for k in ("jobCategory", "level", "salaryRange")
                 if post.get(k)}
        out = RawJob(source_job_id=rid, title=job.title, apply_url=job.apply_url,
                     description="\n\n".join(parts)[:15000],
                     location="; ".join(x for x in locs if x)[:300],
                     department=str(post.get("jobCategory") or "")[:120],
                     compensation=(ld.get("baseSalary") if isinstance(ld, dict) else None)
                     or post.get("salaryRange") or None,
                     extra=extra)
        if posted:
            out.posted_at = posted
            out.date_confidence = DATE_EXACT
            out.raw_posted_text = str(raw_posted)
        return out

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        return None


register(PaycomProvider())
