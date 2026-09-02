"""USAJOBS — the federal government's own board, through its OFFICIAL API.

Why this vendor is worth a provider
-----------------------------------
Every other module here reverse-engineers a career site's XHR. USAJOBS
publishes a documented, keyed REST API instead
(https://developer.usajobs.gov/api-reference/get-api-search), and it is the
single authoritative source for the entire US federal government — the whole
executive branch, one endpoint. There is no scraping to decay: the contract is
versioned and the fields are named.

The credential is the whole design constraint
---------------------------------------------
`GET https://data.usajobs.gov/api/search` requires two headers:

    Authorization-Key: <key issued to a registered email>
    User-Agent:        <that same email address>

and answers 401/403 without them. That makes USAJOBS the first provider in this
package that can be *correctly configured code with no way to fetch*, which is a
state the crawler already has a name for and must not confuse with any other:

  * NOT `dead`   — nothing is broken; the endpoint is fine and will serve the
                   moment a key exists. Marking it dead retires thousands of
                   federal employers that a single SSM parameter would restore.
  * NOT `empty`  — "reachable, genuinely zero open roles" is a factual claim
                   about the board. We have not looked, so we cannot make it.
  * `blocked`    — "retry later, do NOT delete". That is exactly the semantics
                   of a missing credential, so `validate()` returns
                   `STATUS_BLOCKED` with a detail naming the cause, and never
                   raises. A discovery run over a key-less deployment therefore
                   reports "usajobs credentials not configured" once per
                   endpoint instead of a stack trace per endpoint.

`enabled` is likewise decided at CALL time, not at import time. In Lambda the
values arrive from SSM after this module is already imported, so a class-level
`enabled = bool(key)` computed during import would park the vendor for the life
of the container even after the parameter was set. `_credentials()` re-reads and
re-flags on every call, so the provider recovers by itself.

Tenancy
-------
The crawlable unit is one AGENCY: `Organization=<agency subelement code>`
(`AF09`, `TR93`, …), which is what `Endpoint.tenant` carries. The sentinel
tenant `all` sends no `Organization` filter, i.e. the government-wide board.
The API rate-limits per KEY, not per host, so `pacing_key` pins every agency to
one shared bucket — the same lesson iCIMS taught with its IP-scoped wall
(ADR-004 §3.2); keying on the hostname here would be meaningless anyway, since
all agencies share `data.usajobs.gov`.

Dates (ADR-001)
---------------
`PublicationStartDate` is a real published timestamp — the date the
announcement opened — so its rows are `DATE_EXACT`. A row whose field is absent
or unparseable yields `posted_at=""` / `DATE_UNKNOWN`; it is never back-filled
with `now()`, and it is never dropped by the cutoff either, because we cannot
say where an undated row sits.

Sort order is NOT claimed
-------------------------
`SortField` is deliberately not sent. The documented values are not something
this module has verified live, and an unrecognised value risks a 400 that would
zero the whole source — while the only thing the parameter would buy is the
cheaper "stop at the first stale page" rule. So the freshness boundary is a ROW
filter, the same contract Greenhouse and Phenom use, and paging stops on the
count the API itself reports (`SearchResultCountAll`).

`WhoMayApply` is left at its default, which is the public/citizen-eligible
slice. That is the same rule iCIMS's internal-board filter enforces: a row an
outsider can never apply to is worse than no row.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN,
                                         STATUS_BLOCKED, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, ValidationResult,
                                         fp)
from services.ats_providers.registry import register
from services.ats_providers.util import html_to_text, looks_remote, to_iso

#: The documented Search endpoint. Host is fixed by the vendor.
SEARCH_URL = "https://data.usajobs.gov/api/search"

#: Config keys, resolved through `utils.config` (SSM in prod, .env locally).
KEY_SETTING = "USAJOBS_API_KEY"
AGENT_SETTING = "USAJOBS_USER_AGENT"

#: Tenant that means "no Organization filter" — the government-wide board.
ALL_AGENCIES = "all"


def _config_value(key: str) -> str:
    """One credential, or "" when it is not configured anywhere.

    `utils.config` is imported lazily: it pulls in dotenv and (under
    `USE_SSM_SECRETS`) a boto3 SSM client, and this module is imported by the
    provider registry on every cold start, including in processes that will
    never touch USAJOBS.
    """
    try:
        from utils.config import _get_config_value
        return str(_get_config_value(key, "") or "").strip()
    except Exception:
        return ""


def _first_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return value if isinstance(value, dict) else {}


def _names(value: Any, limit: int = 3) -> str:
    """Comma-joined `Name` fields from one of the API's little code lists."""
    if not isinstance(value, list):
        return ""
    out: List[str] = []
    for item in value[:limit]:
        if isinstance(item, dict):
            name = str(item.get("Name") or "").strip()
            if name:
                out.append(name)
    return ", ".join(out)


class UsaJobsProvider(AtsProvider):
    vendor = "usajobs"
    label = "USAJOBS"

    termless = True
    #: The search payload already carries the summary, duties and salary, so a
    #: per-posting request would spend a rate-limited call on nothing.
    has_detail = False
    date_confidence = DATE_EXACT
    #: There is no slug to guess: agencies are identified by an official code
    #: list, not by a name-derived string.
    slug_probe = False

    api_hosts = ("data.usajobs.gov",)
    portal_hosts = ("www.usajobs.gov", "usajobs.gov")

    policy = CrawlPolicy(
        # One bucket for every agency: the quota is attached to the API KEY, so
        # per-host keying (all agencies share one host anyway) would let a wide
        # fan-out spend the whole allowance in a burst.
        pacing_key="data.usajobs.gov",
        min_interval=0.40,
        jitter=0.15,
        max_concurrency=2,
        timeout=12.0,
        detail_timeout=8.0,
        # 403 is how this API refuses a key it does not like or has throttled.
        # Treating it as a challenge parks the VENDOR for a cooldown instead of
        # retiring the agency as dead — and the credential check below means we
        # only ever get here with a key we believed was good.
        challenge_statuses=frozenset({429, 403}),
        cooldown_seconds=900.0,
        # `ResultsPerPage` accepts up to 500; 250 keeps a page cheap to parse
        # and still reaches 2,000 announcements per agency inside `max_pages`.
        page_size=250,
        max_pages=8,
        stale_page_tolerance=0,
    )

    fingerprints = (
        # The API call itself, occasionally embedded in an agency's own site.
        fp(r"https?://data\.usajobs\.gov/api/search[^\"'\s]*[?&]"
           r"Organization=(?P<tenant>[A-Za-z0-9]+)", confidence=95),
        # A public agency-scoped search: `?a=` carries the agency code.
        fp(r"https?://(?:www\.)?usajobs\.gov/[^\"'\s]*[?&]a=(?P<tenant>[A-Za-z0-9]+)",
           confidence=90),
        # Any other usajobs.gov link proves the vendor but names no agency, so
        # it is a signal for discovery and never an endpoint.
        fp(r"https?://(?:www\.)?usajobs\.gov/", confidence=40,
           identity_only=True),
    )

    # -- credentials ---------------------------------------------------------

    def _credentials(self) -> Optional[Tuple[str, str]]:
        """`(key, user_agent)` when both are configured, else None.

        Side effect by design: `enabled` is re-flagged here rather than at
        import, so a key delivered by SSM after the container warmed up brings
        the vendor back without a redeploy, and a key removed parks it without
        one.
        """
        key = _config_value(KEY_SETTING)
        agent = _config_value(AGENT_SETTING)
        ok = bool(key and agent)
        self.enabled = ok
        return (key, agent) if ok else None

    def _headers(self, key: str, agent: str) -> Dict[str, str]:
        # `Host` is sent explicitly because the vendor's own documentation
        # requires it; `User-Agent` must be the email the key was issued to,
        # which is why it is configuration and not a constant.
        return {"Authorization-Key": key, "User-Agent": agent,
                "Host": "data.usajobs.gov"}

    # -- endpoints -----------------------------------------------------------

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not tenant:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant)

    # -- listing -------------------------------------------------------------

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        creds = self._credentials()
        if creds is None:
            # Not an error and not an empty board — see the module docstring.
            ctx.log("info", "usajobs: credentials not configured; %s skipped",
                    endpoint.tenant or ALL_AGENCIES)
            return
        key, agent = creds
        headers = self._headers(key, agent)

        org = (endpoint.tenant or "").strip()
        keyword = str((ctx.options or {}).get("keyword") or "").strip()
        size = int(self.policy.page_size)

        # `Page` is 1-based. Treating it as an offset silently re-reads page 1.
        for page in range(1, int(self.policy.max_pages) + 1):
            if ctx.should_stop():
                ctx.listing_truncated = True
                return
            params: Dict[str, Any] = {"ResultsPerPage": size, "Page": page}
            if org and org.lower() != ALL_AGENCIES:
                params["Organization"] = org
            if keyword:
                params["Keyword"] = keyword

            status, payload = ctx.get_json(SEARCH_URL, params=params,
                                           timeout=self.policy.timeout,
                                           vendor=self.vendor,
                                           extra_headers=headers)
            if self.policy.is_challenge(status):
                raise ChallengeError(self.vendor, status)
            if status != 200 or not isinstance(payload, dict):
                if page > 1:
                    ctx.listing_truncated = True
                return

            result = payload.get("SearchResult")
            if not isinstance(result, dict):
                return
            items = [i for i in (result.get("SearchResultItems") or [])
                     if isinstance(i, dict)]
            if not items:
                return

            for item in items:
                if ctx.should_stop():
                    ctx.listing_truncated = True
                    return
                job = self._to_raw(item)
                if job is None:
                    continue
                # Row filter, not a paging stop: the ordering is not promised
                # (see the module docstring), and an undated row is kept
                # because we cannot say where it sits.
                if ctx.cutoff_iso and job.posted_at and job.posted_at < ctx.cutoff_iso:
                    continue
                yield job

            if len(items) < size:
                return
            total = self._as_int(result.get("SearchResultCountAll"))
            if total and page * size >= total:
                return

        ctx.listing_truncated = True

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return 0

    # -- mapping -------------------------------------------------------------

    @classmethod
    def _to_raw(cls, item: Dict[str, Any]) -> Optional[RawJob]:
        descriptor = item.get("MatchedObjectDescriptor")
        if not isinstance(descriptor, dict):
            return None
        rid = str(item.get("MatchedObjectId")
                  or descriptor.get("PositionID") or "").strip()
        title = str(descriptor.get("PositionTitle") or "").strip()
        if not rid or not title:
            return None

        position_uri = str(descriptor.get("PositionURI") or "").strip()
        apply_uri = descriptor.get("ApplyURI")
        apply_url = ""
        if isinstance(apply_uri, list):
            for candidate in apply_uri:
                if str(candidate or "").strip():
                    apply_url = str(candidate).strip()
                    break
        elif isinstance(apply_uri, str):
            apply_url = apply_uri.strip()
        # `ApplyURI` is where a candidate actually starts an application;
        # `PositionURI` is the announcement. Prefer the former, fall back to
        # the latter, and only then construct — a constructed URL is a guess.
        apply_url = apply_url or position_uri or \
            f"https://www.usajobs.gov/job/{rid}"

        raw_posted = str(descriptor.get("PublicationStartDate") or "").strip()
        posted = to_iso(raw_posted)

        location = str(descriptor.get("PositionLocationDisplay") or "").strip()
        country = str(_first_dict(descriptor.get("PositionLocation"))
                      .get("CountryCode") or "").strip()

        details = descriptor.get("UserArea")
        details = (details.get("Details")
                   if isinstance(details, dict) else None) or {}
        if not isinstance(details, dict):
            details = {}
        body = " ".join(str(details.get(k) or "") for k in
                        ("JobSummary", "MajorDuties", "Requirements"))
        if not body.strip():
            body = str(descriptor.get("QualificationSummary") or "")

        # `RemoteIndicator` is the employer's own statement. `TeleworkEligible`
        # is deliberately NOT read as remote: "may telework" is not "is a
        # remote job", and conflating them mis-files office roles.
        remote_flag = details.get("RemoteIndicator")
        remote = remote_flag if isinstance(remote_flag, bool) else \
            looks_remote(title, location)

        organization = str(descriptor.get("OrganizationName") or "").strip()
        department = str(descriptor.get("DepartmentName") or "").strip()

        return RawJob(
            source_job_id=rid,
            title=title,
            apply_url=apply_url,
            location=location,
            posted_at=posted,
            date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
            raw_posted_text=raw_posted,
            description=html_to_text(body)[:15000],
            country=country,
            employment_type=_names(descriptor.get("PositionSchedule"), 2),
            # The vendor's raw pay object, unformatted: `salary_normalizer`
            # reads SHAPES, so passing it through is what lets the API rename
            # a field without breaking us.
            compensation=_first_dict(descriptor.get("PositionRemuneration"))
            or None,
            department=_names(descriptor.get("JobCategory"), 3)[:120],
            remote=remote,
            extra={
                "organization": organization,
                "department_name": department,
                "position_uri": position_uri,
                "offering_type": _names(
                    descriptor.get("PositionOfferingType"), 2),
                "close_date": str(
                    descriptor.get("ApplicationCloseDate") or "").strip(),
            },
        )

    # -- identity ------------------------------------------------------------

    def canonical_url(self, endpoint: Endpoint, job: RawJob) -> str:
        """The announcement page, which is stable and human-readable.

        `apply_url` may point into the application wizard, so it is not the
        right thing to show as the posting's canonical address.
        """
        uri = str((job.extra or {}).get("position_uri") or "").strip()
        if uri:
            return uri
        if job.source_job_id:
            return f"https://www.usajobs.gov/job/{job.source_job_id}"
        return job.apply_url

    def board_owner(self, endpoint: Endpoint,
                    ctx: FetchContext) -> Optional[str]:
        """The agency this endpoint's postings DECLARE they belong to.

        Every result carries `OrganizationName`, so an agency code can be
        checked against the name it actually serves rather than assumed. One
        cheap request (`ResultsPerPage=1`). Returns None for "cannot say" —
        never a guess — which is what a missing credential also means.
        """
        creds = self._credentials()
        if creds is None:
            return None
        key, agent = creds
        params: Dict[str, Any] = {"ResultsPerPage": 1, "Page": 1}
        org = (endpoint.tenant or "").strip()
        if org and org.lower() != ALL_AGENCIES:
            params["Organization"] = org
        try:
            status, payload = ctx.get_json(SEARCH_URL, params=params,
                                           timeout=self.policy.timeout,
                                           vendor=self.vendor,
                                           extra_headers=self._headers(key, agent))
        except Exception:
            return None
        if status != 200 or not isinstance(payload, dict):
            return None
        result = payload.get("SearchResult")
        items = (result.get("SearchResultItems")
                 if isinstance(result, dict) else None) or []
        for item in items:
            if not isinstance(item, dict):
                continue
            descriptor = item.get("MatchedObjectDescriptor")
            if not isinstance(descriptor, dict):
                continue
            name = str(descriptor.get("OrganizationName")
                       or descriptor.get("DepartmentName") or "").strip()
            if name:
                return name
        return None

    # -- validation ----------------------------------------------------------

    def validate(self, endpoint: Endpoint,
                 ctx: FetchContext) -> ValidationResult:
        """BLOCKED, not DEAD and not EMPTY, when there is no key.

        Raising here would be the worst of the three: `discover_batch` and the
        endpoint audit both call `validate` per candidate, so an exception per
        endpoint turns a single missing SSM parameter into a run-long stream of
        stack traces with no statement of the cause.
        """
        if self._credentials() is None:
            return ValidationResult(
                STATUS_BLOCKED, 0,
                detail="usajobs credentials not configured")
        return super().validate(endpoint, ctx)


register(UsaJobsProvider())
