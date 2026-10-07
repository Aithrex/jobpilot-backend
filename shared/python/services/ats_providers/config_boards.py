"""Employer-run boards, described by DATA instead of by a branch.

The defect
----------
A handful of large employers run their own board with a public JSON/XHR API and
no third-party ATS. Historically each one arrived as a company-specific branch:
a `_fetch_apple`, an `if row["key"] == "tesla"` in a loop, a special case in the
normalizer. That shape has three costs this package exists to remove:

  * adding an employer is a code change, a review and a deploy, so the long
    tail never gets added;
  * every branch is a place to be individually wrong, and its blast radius is
    the whole crawl — a `KeyError` in one company's mapper ends the shard;
  * the branches drift from the catalog rows that name them, which is exactly
    the four-tables-must-agree failure `registry.py` was written to end.

So there is no company-specific `if` in this file, and there must never be one.
Everything an employer needs is a dict in
`official_career_registry.CUSTOM_BOARD_ADAPTERS`: the request to make, where
the records live in the response, and which field is which. One provider
instance is built per row and registered under that row's `vendor`, so a new
employer is a data change that a non-author can make and a broken row can only
break itself.

What a row may say
------------------
    request.method       "GET" | "POST"
    request.url          the absolute endpoint
    request.params       query parameters sent on every page
    request.json_body    POST body sent on every page (deep-copied per request)
    request.page_field   key INSIDE json_body that carries the page number
    request.offset_field key inside json_body that carries the row offset
    request.preflight    {"url": ..., "method": "GET"} — one request made ONCE
                         before the first page, for boards whose search API
                         answers 200-with-zero-records until the caller holds a
                         session. Nothing is read from its body; the point is
                         the cookie jar `make_fetchers` already shares across a
                         member's crawl. Apple is the case: its
                         `/api/v1/search` returns `totalRecords: 0` to a
                         cookieless client and the full 6,140 once
                         `/api/v1/CSRFToken` has been called on the same
                         session. A 200-with-zero-rows is the WORST failure
                         shape a source can have, because it reads as "this
                         employer is not hiring".
    pagination.style     "page" | "offset" | "none"
    pagination.page_start first page index (Apple's API is 1-based)
    pagination.page_param / offset_param  query parameter names for GET paging
    pagination.page_size  rows per page; also the "a short page is the last
                          page" signal, so it is OPTIONAL and only used when
                          the row states it
    pagination.max_pages  crawl budget for this employer
    records_path         dotted path to the list of records
    total_path           dotted path to the total count, when published
    fields               canonical RawJob attribute -> path or list of paths
    fields.apply_url_template  formatted from the mapped values when the
                          record carries no absolute apply URL

Paths are resolved by `official_career_registry.dig` / `first_present`, which
return None for a miss rather than raising. That is the contract that makes a
vendor's response reshape a source that returns FEWER rows rather than a source
that ends the crawl: a renamed field is a missing value, a moved records list is
zero rows, and both are logged and survived.

Dates (ADR-001)
---------------
`date_confidence` is per INSTANCE, derived from the row: a row that maps
`posted_at` declares `DATE_EXACT` and a row that does not declares
`DATE_UNKNOWN`. Tesla's board publishes no date, and this provider says so
rather than stamping `now()` on 400 requisitions.

Ordering is not promised by any of these APIs, so the freshness boundary is a
ROW filter, never a paging stop — the same contract as Greenhouse and Phenom.
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import replace as _dc_replace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlparse

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote, to_iso
from services.official_career_registry import (CUSTOM_BOARD_ADAPTERS, dig,
                                               first_present)

logger = logging.getLogger(__name__)

#: These are first-party endpoints belonging to the employer, one tenant per
#: host, so hostname pacing is the right scope. The numbers match the board
#: providers'; nothing here is under anti-bot pressure that we know of.
_CONFIG_POLICY = CrawlPolicy(
    pacing_key=None,
    min_interval=0.25,
    jitter=0.10,
    max_concurrency=4,
    timeout=10.0,
    detail_timeout=6.0,
    page_size=100,
    max_pages=10,
    stale_page_tolerance=0,
)

#: Canonical RawJob attributes a row may map with a path (or list of paths).
#: `source_job_id`, `title`, `posted_at`, `description` and `apply_url` are
#: handled explicitly below because each needs its own treatment.
_PLAIN_FIELDS: Tuple[str, ...] = (
    "location", "department", "employment_type", "country")

_TEMPLATE_FIELD_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")


def _host_of(url: Any) -> str:
    try:
        return (urlparse(str(url or "")).hostname or "").lower()
    except Exception:
        return ""


def _as_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value).strip()


def _as_paths(value: Any) -> Sequence[str]:
    if value in (None, "", [], {}):
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(p) for p in value if p)
    return (str(value),)


def _format_template(template: str, values: Dict[str, str]) -> str:
    """Fill `{placeholders}` from mapped values, or return "".

    Values are percent-encoded: a vendor id is untrusted input, and an id
    carrying `/` or `?` would otherwise silently rewrite the URL's path or
    query. A placeholder with no value returns "" for the whole URL rather than
    a half-built link — `https://.../details/` is not a job, and storing it
    would send a candidate to a 404.
    """
    missing: List[str] = []

    def _sub(match: "re.Match[str]") -> str:
        raw = str(values.get(match.group(1)) or "").strip()
        if not raw:
            missing.append(match.group(1))
            return ""
        return quote(raw, safe="-._~")

    out = _TEMPLATE_FIELD_RE.sub(_sub, str(template or ""))
    return "" if missing else out


def _join_sections(record: Dict[str, Any],
                   sections: Sequence[Tuple[str, str]]) -> str:
    """The named fields of one record as a single body, each under its
    heading - the requirement extractor reads "Qualifications" to tell what is
    required from what is preferred."""
    parts: List[str] = []
    for label, path in sections:
        text = html_to_text(_as_text(dig(record, path))).strip()
        if text:
            parts.append(f"{label}\n{text}" if label else text)
    return "\n\n".join(parts)


class ConfigBoardProvider(AtsProvider):
    """One employer-run board, entirely described by its registry row."""

    #: Discovery cannot guess these: the endpoint is a private XHR that no
    #: careers page necessarily links, and the row IS the seed.
    slug_probe = False
    termless = True
    has_detail = False
    fingerprints: Tuple = ()

    def validate(self, endpoint, ctx):
        """A row the registry marks `blocked` reports BLOCKED, not EMPTY.

        The default implementation reads "no postings returned" as
        `STATUS_EMPTY`, and for these boards that is a false claim about the
        employer. Apple's search API answers 200 with zero records without a
        session token; Tesla's is behind an Akamai wall. Both boards exist and
        both are full of jobs -- we cannot read them, which is a fact about US.
        `empty` would put those employers in the covered column with the
        explanation "they are not hiring".
        """
        from services.ats_providers.base import STATUS_BLOCKED, ValidationResult

        status = str(self.row.get("status") or "unverified").strip().lower()
        if status in ("blocked", "unverified"):
            detail = str(self.row.get("status_detail") or "").strip()
            return ValidationResult(
                STATUS_BLOCKED, 0,
                detail=detail or f"adapter status: {status}, never probed live")
        return super().validate(endpoint, ctx)

    def __init__(self, row: Dict[str, Any]) -> None:
        self.row: Dict[str, Any] = dict(row or {})
        self.key = str(self.row.get("key") or "").strip().lower()
        self.vendor = str(self.row.get("vendor") or "").strip().lower()
        self.label = str(self.row.get("display_name") or self.vendor or "")

        request = dict(self.row.get("request") or {})
        self.method = str(request.get("method") or "GET").strip().upper()
        self.url = str(request.get("url") or "").strip()
        self.base_params: Dict[str, Any] = dict(request.get("params") or {})
        body = request.get("json_body")
        self.base_body: Optional[Dict[str, Any]] = \
            dict(body) if isinstance(body, dict) else None
        # Fixed request headers a board insists on (TikTok: `website-path`).
        self.headers: Dict[str, str] = {
            str(k): str(v) for k, v in dict(request.get("headers") or {}).items()}
        self.page_field = str(request.get("page_field") or "").strip()
        self.offset_field = str(request.get("offset_field") or "").strip()
        preflight = dict(request.get("preflight") or {})
        self.preflight_url = str(preflight.get("url") or "").strip()
        self.preflight_method = str(preflight.get("method") or "GET").strip().upper()

        pagination = dict(self.row.get("pagination") or {})
        self.style = str(pagination.get("style") or "none").strip().lower()
        self.page_start = _int_or(pagination.get("page_start"), 0)
        self.page_param = str(pagination.get("page_param") or "page").strip()
        self.offset_param = str(pagination.get("offset_param") or "offset").strip()
        #: 0 == "the row does not say", which disables both the short-page stop
        #: and the total-count stop rather than inventing a page size.
        self.page_size = max(0, _int_or(pagination.get("page_size"), 0))

        self.records_path = str(self.row.get("records_path") or "").strip()
        self.total_path = str(self.row.get("total_path") or "").strip()
        self.fields: Dict[str, Any] = dict(self.row.get("fields") or {})
        self.apply_template = str(self.fields.get("apply_url_template") or "").strip()

        # An optional second request per posting, for a board whose search
        # API prints a summary and keeps the qualifications on the posting.
        detail = dict(self.row.get("detail") or {})
        self.detail_template = str(detail.get("url_template") or "").strip()
        self.detail_record_path = str(detail.get("record_path") or "").strip()
        self.detail_sections: List[Tuple[str, str]] = [
            (str(label or "").strip(), str(path or "").strip())
            for label, path in (detail.get("sections") or ())
            if str(path or "").strip()]
        self.has_detail = bool(self.detail_template and self.detail_sections)
        # A LISTING that already carries the body, split across fields
        # (Atlassian: overview / responsibilities / qualifications).
        self.description_sections: List[Tuple[str, str]] = [
            (str(label or "").strip(), str(path or "").strip())
            for label, path in (self.fields.get("description_sections") or ())
            if str(path or "").strip()]

        # ADR-001: the row decides, not the crawler.
        self.date_confidence = (DATE_EXACT if _as_paths(self.fields.get("posted_at"))
                                else DATE_UNKNOWN)

        max_pages = 1 if self.style == "none" else \
            max(1, _int_or(pagination.get("max_pages"), _CONFIG_POLICY.max_pages))
        self.policy = _dc_replace(
            _CONFIG_POLICY, max_pages=max_pages,
            page_size=self.page_size or _CONFIG_POLICY.page_size)

        api_host = _host_of(self.url)
        self.api_hosts = (api_host,) if api_host else ()
        # The apply link and the landing page are frequently on a different
        # host from the API (SpaceX's records come from Greenhouse), and the
        # liveness probe needs those, not the API host.
        self.portal_hosts = tuple(sorted({
            h for h in (api_host, _host_of(self.apply_template),
                        _host_of(self.row.get("career_landing_url"))) if h}))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ConfigBoardProvider {self.vendor}>"

    # -- endpoints -----------------------------------------------------------

    def default_endpoint(self) -> Endpoint:
        """The single endpoint this row describes.

        These boards have exactly one tenant — the employer that runs them — so
        the row's `key` is the tenant and the crawler needs nothing else to
        schedule it.
        """
        return Endpoint(vendor=self.vendor, tenant=self.key or self.vendor,
                        display_name=self.label,
                        extra={"config_key": self.key})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        return self.default_endpoint()

    # -- listing -------------------------------------------------------------

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        if not self.url or not self.records_path:
            ctx.log("warning", "config_boards: %s row is incomplete", self.vendor)
            return
        if self.method == "POST" and ctx.post_json is None:
            ctx.log("warning", "config_boards: %s needs post_json", self.vendor)
            return

        self._preflight(ctx)

        size = self.page_size
        for index in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return

            payload = self._fetch_page(ctx, index)
            if payload is None:
                if index:
                    ctx.listing_truncated = True
                return

            rows = self._records(payload)
            if not rows:
                # Either the board is exhausted or the response reshaped. Both
                # are "no more rows here", and neither is an exception.
                if index == 0:
                    ctx.log("info", "config_boards: %s returned no records at %s",
                            self.vendor, self.records_path)
                return

            for record in rows:
                if ctx.should_stop():
                    ctx.listing_truncated = True
                    return
                job = self._to_raw(record)
                if job is None:
                    continue
                # Row filter: none of these APIs promise an ordering.
                if ctx.cutoff_iso and job.posted_at and job.posted_at < ctx.cutoff_iso:
                    continue
                yield job

            if self.style == "none":
                return
            if size and len(rows) < size:
                return
            total = _int_or(dig(payload, self.total_path), 0) if self.total_path else 0
            if total and size and (index + 1) * size >= total:
                return

        if self.style != "none":
            ctx.listing_truncated = True

    def _preflight(self, ctx: FetchContext) -> None:
        """One session-establishing request, when the row asks for one.

        Deliberately fire-and-forget. Whatever it returns is ignored — the
        value is the cookie the response set, which lands in the jar
        `make_fetchers` scopes to this member's crawl. A failure is NOT fatal:
        the board may have stopped needing the handshake, and a preflight that
        404s must not cost us the postings the search call would still return.
        It goes through `ctx`, not through `requests`, so it is inside the same
        SSRF guard and the same per-host pacing as every other listing fetch,
        and it is spent ONCE per listing rather than per page.
        """
        if not self.preflight_url:
            return
        try:
            if self.preflight_method == "POST" and ctx.post_json is not None:
                ctx.post_json(self.preflight_url, json_body={},
                              timeout=self.policy.timeout, vendor=self.vendor)
            else:
                ctx.get_json(self.preflight_url, timeout=self.policy.timeout,
                             vendor=self.vendor)
        except ChallengeError:
            raise
        except Exception as exc:
            ctx.log("info", "config_boards: %s preflight failed (%s)",
                    self.vendor, type(exc).__name__)

    def _fetch_page(self, ctx: FetchContext, index: int) -> Optional[Any]:
        params = dict(self.base_params)
        body = copy.deepcopy(self.base_body) if self.base_body is not None else None

        if self.style == "page":
            value: Any = self.page_start + index
            if body is not None and self.page_field:
                body[self.page_field] = value
            else:
                params[self.page_param] = value
        elif self.style == "offset":
            value = index * (self.page_size or _CONFIG_POLICY.page_size)
            if body is not None and self.offset_field:
                body[self.offset_field] = value
            else:
                params[self.offset_param] = value

        # Passed only when the row sets some, so a row without headers makes
        # exactly the call it always did.
        extra = {"extra_headers": dict(self.headers)} if self.headers else {}
        if self.method == "POST":
            status, payload = ctx.post_json(
                self.url, params=params or None, json_body=body,
                timeout=self.policy.timeout, vendor=self.vendor, **extra)
        else:
            status, payload = ctx.get_json(
                self.url, params=params, timeout=self.policy.timeout,
                vendor=self.vendor, **extra)

        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200:
            return None
        return payload

    def _records(self, payload: Any) -> List[Dict[str, Any]]:
        # "$": the response IS the list.
        records = payload if self.records_path == "$" \
            else dig(payload, self.records_path)
        if not isinstance(records, (list, tuple)):
            return []
        return [r for r in records if isinstance(r, dict)]

    # -- mapping -------------------------------------------------------------

    def _to_raw(self, record: Dict[str, Any]) -> Optional[RawJob]:
        try:
            return self._map(record)
        except Exception as exc:  # one odd record must not end the listing
            logger.debug("config_boards: %s could not map a record (%s)",
                         self.vendor, type(exc).__name__)
            return None

    def _map(self, record: Dict[str, Any]) -> Optional[RawJob]:
        values: Dict[str, str] = {}
        for name in ("source_job_id", "title") + _PLAIN_FIELDS:
            found = first_present(record, _as_paths(self.fields.get(name)))
            if name == "location" and isinstance(found, (list, tuple)):
                # A posting open in several places: keep them all, so a role
                # listed "Bengaluru; Seattle" is not filed as India-only.
                found = "; ".join(str(x).strip() for x in found[:4]
                                  if isinstance(x, str) and x.strip())
            values[name] = _as_text(found)
        if not values["source_job_id"] or not values["title"]:
            return None

        raw_posted = first_present(record, _as_paths(self.fields.get("posted_at")))
        posted = to_iso(raw_posted) if raw_posted is not None else ""

        apply_url = _as_text(
            first_present(record, _as_paths(self.fields.get("apply_url"))))
        if not apply_url and self.apply_template:
            apply_url = _format_template(self.apply_template, values)
        if not apply_url:
            return None

        description = first_present(
            record, _as_paths(self.fields.get("description")))
        if self.description_sections:
            description = _join_sections(record, self.description_sections) \
                or description
        compensation = first_present(
            record, _as_paths(self.fields.get("compensation")))

        return RawJob(
            source_job_id=values["source_job_id"],
            title=values["title"],
            apply_url=apply_url,
            location=values["location"],
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            raw_posted_text=_as_text(raw_posted),
            description=(description if self.description_sections
                         else html_to_text(description))[:15000] if description else "",
            country=values["country"],
            employment_type=values["employment_type"],
            compensation=compensation if compensation not in ("", None) else None,
            department=values["department"][:120],
            remote=looks_remote(values["title"], values["location"]),
            extra={"config_key": self.key},
        )

    # -- detail --------------------------------------------------------------

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        """The posting body, assembled from the sections the row names.

        Each section keeps its heading ("Minimum Qualifications") because the
        requirement extractor reads the heading to tell required from
        preferred. None when the row declares no detail, the request fails, or
        nothing came back - the caller keeps the listing text in that case.
        """
        if not self.has_detail:
            return None
        url = _format_template(self.detail_template,
                               {"source_job_id": job.source_job_id})
        if not url:
            return None
        status, payload = ctx.get_json(url, timeout=self.policy.detail_timeout,
                                       vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200:
            return None
        record = dig(payload, self.detail_record_path) \
            if self.detail_record_path else payload
        if not isinstance(record, dict):
            return None
        body = _join_sections(record, self.detail_sections)
        if not body:
            return None
        return _dc_replace(job, description=body[:15000])

    # -- identity ------------------------------------------------------------

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        """None — deliberately.

        `board_owner` exists to CORROBORATE a guessed tenant with something the
        board itself declares. Returning this row's own `display_name` would
        corroborate our data with our data. These endpoints are not guessed
        (`slug_probe = False`); the row is a hand-verified assertion, so there
        is nothing to check and nothing to fabricate.
        """
        return None


def _int_or(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_adapters(rows: Optional[Sequence[Dict[str, Any]]] = None, *,
                      replace: bool = False) -> List[ConfigBoardProvider]:
    """Register one provider per adapter row.

    Failures are per ROW: a malformed dict is logged and skipped, because the
    point of moving these employers into data is that one bad row cannot cost
    the other employers their crawl.
    """
    made: List[ConfigBoardProvider] = []
    for row in (rows if rows is not None else CUSTOM_BOARD_ADAPTERS):
        try:
            provider = ConfigBoardProvider(row)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("config_boards: unusable adapter row %r (%s)",
                           (row or {}).get("key"), exc)
            continue
        if not provider.vendor or not provider.url:
            logger.warning("config_boards: adapter %r has no vendor/url",
                           provider.key or (row or {}).get("key"))
            continue
        try:
            register(provider, replace=replace)
        except ValueError as exc:
            logger.warning("config_boards: %s not registered (%s)",
                           provider.vendor, exc)
            continue
        made.append(provider)
    return made


register_adapters()
