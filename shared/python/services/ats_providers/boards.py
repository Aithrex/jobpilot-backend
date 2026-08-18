"""Board ATSes — the seven vendors whose whole open-roles list arrives in one
or two requests.

Why they share a module
-----------------------
Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, NeoGov,
Jobvite and BambooHR are the same shape: a public JSON endpoint keyed by a
tenant slug that returns the employer's ENTIRE board, unpaged (SmartRecruiters
excepted), with no search term. That shape is what makes them termless under ADR-003, and it is why the
crawler spends one request per company on them rather than tenants x terms.
They differ only in field names, so one module with seven small classes reads
better than seven near-identical files.

This is a PORT, not a rewrite
-----------------------------
Every endpoint, parameter and date field below is copied from code already
verified against the live services (`career_pages_service._fetch_*`, and the
crawler's `job_ingest_worker._list_career`). The comments that record *why* a
field was chosen are copied with them, because each one is a bug someone
already paid for:

* Greenhouse's `first_published` (never `updated_at`) — §Greenhouse below.
* NeoGov's POST + XHR header, and its title living in `Classification` rather
  than in the field literally named `JobTitle`.
* SmartRecruiters' `country=us`, without which a 5,800-posting multinational
  buries its US slice past our page budget.
* Lever's four-field body assembly, without which ~60% of its postings store
  no description at all.

Where `career_pages_service` (the live interactive fan-out) and
`job_ingest_worker` (the background crawl) disagree about a field, the comment
says so and says which one this module follows. Those disagreements are the
drift ADR-004 exists to end, and they should be resolved deliberately by
whoever retires the old paths, not silently here.

Dates
-----
All seven publish a real timestamp, so their rows are ADR-001 CONFIDENCE_EXACT.
A posting whose date field is missing or unparseable yields `posted_at=""` and
`DATE_UNKNOWN`; it is never back-filled with now().
"""
from __future__ import annotations

from dataclasses import replace as _replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from services.ats_providers.base import (DATE_EXACT, DATE_UNKNOWN, AtsProvider,
                                         ChallengeError, CrawlPolicy, Endpoint,
                                         FetchContext, RawJob, fp)
from services.ats_providers.registry import register
from services.ats_providers.util import (from_epoch, html_to_text, looks_remote,
                                         to_iso)

# Board APIs are fast, cheap and untroubled by our traffic; these are the
# crawler's current numbers (`job_ingest_worker.HOST_MIN_INTERVAL` / `_JITTER`)
# so switching a source onto this framework does not change its politeness.
# `pacing_key` stays None throughout: Greenhouse, Lever, Ashby, SmartRecruiters,
# Workable and NeoGov each serve every tenant from ONE hostname, so hostname
# keying already gives all their tenants a single shared bucket. Recruitee is
# the exception (`{tenant}.recruitee.com`) and per-host keying is correct there
# too — each tenant is a separate origin, and unlike iCIMS there is no
# IP-scoped wall to share a budget with.
_BOARD_POLICY = CrawlPolicy(
    pacing_key=None,
    min_interval=0.15,
    jitter=0.10,
    max_concurrency=8,
    timeout=8.0,
    detail_timeout=6.0,
    page_size=100,
    max_pages=1,          # whole board in one call; overridden where it pages
)

# Pay fields, by the names the boards actually use. Deliberately a TOP-LEVEL
# lookup on a known list rather than handing the whole job dict to the parser
# and letting it hunt: that parser accepts generic range keys (`min`/`max`,
# `from`/`to`, `start`/`end`), and a job payload has unrelated fields by those
# names - `start` is a start date, not a floor - so a blind search would invent
# salaries. Absent here simply means the description fallback decides.
_PAY_KEYS: Tuple[str, ...] = (
    "pay_input_ranges", "salaryRange", "salary_range", "compensation",
    "compensationTierSummary", "salary", "payRange", "pay")


def _pay_of(job: Dict[str, Any]) -> Any:
    """The vendor's raw pay object, unformatted. `salary_normalizer` reads
    SHAPES rather than one vendor's field names, so passing the object through
    untouched is what lets a board rename its pay field without breaking us."""
    if not isinstance(job, dict):
        return None
    for key in _PAY_KEYS:
        value = job.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _join(*parts: Any, sep: str = ", ") -> str:
    return sep.join(str(p).strip() for p in parts if p and str(p).strip())


class _BoardProvider(AtsProvider):
    """Shared plumbing for the seven board vendors.

    Only network access and cutoff handling live here. Every vendor still
    declares its own hosts, fingerprints and field mapping, because those are
    exactly the things that must not be guessed by a shared default.
    """

    termless = True
    date_confidence = DATE_EXACT
    policy = _BOARD_POLICY

    # Every vendor here answers on ONE shared API host, so a slug guess costs a
    # single definitive request (the board exists or the API 404s). That is what
    # makes these safe as discovery's fallback when a careers page turns out to
    # be a JS shell with no ATS link in its HTML - and it is exactly what is NOT
    # true of Workday (wd1..wd103), iCIMS (wildcard DNS) or Oracle (opaque pod
    # hostnames), which is why those three opt out.
    slug_probe = True

    # -- network -------------------------------------------------------------

    def _get_json(self, ctx: FetchContext, url: str,
                  params: Optional[Dict[str, Any]] = None,
                  *, timeout: Optional[float] = None) -> Optional[Any]:
        """One guarded GET. None means "no usable answer for this URL".

        A challenge is raised rather than returned so a soft block is recorded
        as a retryable vendor-wide condition instead of retiring the employer.
        """
        status, payload = ctx.get_json(
            url, params=params or {},
            timeout=timeout if timeout is not None else self.policy.timeout,
            vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200:
            return None
        return payload

    # -- cutoff --------------------------------------------------------------

    def _too_old(self, ctx: FetchContext, posted_at: str) -> bool:
        """True when a row predates the caller's freshness boundary.

        These boards return the whole list in one unpaged call and promise no
        ordering, so the cutoff is a ROW filter here, not a paging stop (which
        is what it is for Workday and ORC). A row with no date is never
        dropped by the cutoff: we cannot tell where it sits, and guessing is
        the ADR-001 failure this pipeline exists to avoid.
        """
        return bool(ctx.cutoff_iso and posted_at and posted_at < ctx.cutoff_iso)

    # -- rows ----------------------------------------------------------------

    @staticmethod
    def _rows(payload: Any, key: str) -> List[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        return [r for r in (payload.get(key) or []) if isinstance(r, dict)]


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

class GreenhouseProvider(_BoardProvider):
    """Greenhouse job boards.

    The largest single vendor in the existing catalog (686 of 1,309 companies),
    which is why its date handling below is load-bearing rather than a detail.
    """

    vendor = "greenhouse"
    label = "Greenhouse"

    has_detail = True

    # The LIST api and the PUBLIC board are different hosts, and the crawler
    # keeps their allowlists separate for that reason (`_HOST_ALLOW` vs
    # `_PROBE_HOST_ALLOW`). `job-boards.greenhouse.io` is the newer public host;
    # both still serve live employers.
    api_hosts = ("boards-api.greenhouse.io",)
    portal_hosts = ("boards.greenhouse.io", "job-boards.greenhouse.io")

    fingerprints = (
        # The API URL itself, occasionally linked by careers pages that render
        # their board client-side.
        fp(r"https?://boards-api\.greenhouse\.io/v\d+/boards/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=95),
        # The embed form names the tenant in a query param, so it must be
        # matched BEFORE the generic path pattern (which would capture the
        # literal "embed" as the tenant).
        fp(r"https?://(?:job-)?boards\.greenhouse\.io/embed/job_board"
           r"[^\"'\s]*[?&]for=(?P<tenant>[A-Za-z0-9_\-]+)", confidence=92),
        fp(r"https?://(?:job-)?boards\.greenhouse\.io/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=85),
    )

    # Path segments that are Greenhouse's own, not an employer slug.
    _RESERVED = frozenset({"embed", "job_board", "jobs", "assets", "static"})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not tenant or tenant.lower() in self._RESERVED:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant)

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        payload = self._get_json(
            ctx, f"https://boards-api.greenhouse.io/v1/boards/"
                 f"{endpoint.tenant}/jobs",
            # `content=false` keeps the list response small; bodies come from
            # `fetch_detail` for the rows that survive the freshness filter,
            # which is far fewer requests than shipping every body every crawl.
            {"content": "false"})
        for job in self._rows(payload, "jobs"):
            if ctx.should_stop():
                return
            title = str(job.get("title") or "").strip()
            rid = str(job.get("id") or "").strip()
            if not rid or not title:
                continue
            # first_published ONLY. `updated_at` is an EDIT timestamp, not a
            # posting date: falling back to it re-dated months-old reqs as
            # "new", and because Greenhouse is over half the catalog it was the
            # single biggest source of fake freshness in the whole product. No
            # first_published => the date is UNKNOWN and stays unknown.
            posted = to_iso(job.get("first_published"))
            if self._too_old(ctx, posted):
                continue
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=str(job.get("absolute_url") or ""),
                location=str((job.get("location") or {}).get("name") or ""),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                raw_posted_text=str(job.get("first_published") or ""),
                compensation=_pay_of(job),
                remote=looks_remote((job.get("location") or {}).get("name")),
                detail_url=(f"https://boards-api.greenhouse.io/v1/boards/"
                            f"{endpoint.tenant}/jobs/{rid}"),
            )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        url = job.detail_url or (
            f"https://boards-api.greenhouse.io/v1/boards/"
            f"{endpoint.tenant}/jobs/{job.source_job_id}")
        payload = self._get_json(ctx, url, timeout=self.policy.detail_timeout)
        if not isinstance(payload, dict):
            return None
        body = html_to_text(payload.get("content") or "")
        if not body:
            return None
        return RawJob(source_job_id=job.source_job_id, title=job.title,
                      apply_url=job.apply_url, description=body[:40000],
                      compensation=_pay_of(payload))


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------

class LeverProvider(_BoardProvider):
    """Lever postings API. Ships the full body inline, so it needs no detail
    request — but the body is spread across four fields (see `_body`)."""

    vendor = "lever"
    label = "Lever"

    has_detail = False

    api_hosts = ("api.lever.co",)
    portal_hosts = ("jobs.lever.co",)

    fingerprints = (
        fp(r"https?://api\.lever\.co/v\d+/postings/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=95),
        fp(r"https?://jobs\.lever\.co/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=90),
    )

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        payload = self._get_json(
            ctx, f"https://api.lever.co/v0/postings/{endpoint.tenant}",
            {"mode": "json"})
        # Lever answers with a bare LIST, not an object.
        if not isinstance(payload, list):
            return
        for job in payload:
            if not isinstance(job, dict):
                continue
            if ctx.should_stop():
                return
            title = str(job.get("text") or "").strip()
            rid = str(job.get("id") or "").strip()
            if not rid or not title:
                continue
            posted = from_epoch(job.get("createdAt"))
            if self._too_old(ctx, posted):
                continue
            cats = job.get("categories") or {}
            workplace = str(job.get("workplaceType") or "").lower()
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=str(job.get("hostedUrl") or ""),
                location=str(cats.get("location") or ""),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                description=self._body(job)[:20000],
                country=str(job.get("country") or ""),
                employment_type=str(cats.get("commitment") or ""),
                compensation=_pay_of(job),
                department=str(cats.get("team")
                               or cats.get("department") or ""),
                remote=True if workplace == "remote" else looks_remote(
                    cats.get("location")),
            )

    @staticmethod
    def _body(job: Dict[str, Any]) -> str:
        """Assemble a Lever posting's full text from the four fields it spans.

        `descriptionPlain` alone is wrong twice over. It is EMPTY on a large
        share of real postings (7 of 17 on the first board checked), and even
        when present it holds only the intro blurb: the actual requirements live
        in `lists`, the structured "What You'll Do" / "What You'll Bring"
        sections, with `additionalPlain` carrying benefits and EEO text. Reading
        only `descriptionPlain` stored ~60% of Lever postings with no body at
        all, and a body-less row cannot be requirement-parsed, cannot be scored
        beyond its title, and is excluded by the job-evidence gate — it occupies
        the inventory while being unservable.

        (`career_pages_service._fetch_lever` still reads `descriptionPlain`
        alone; this follows `job_ingest_worker._lever_body`, which is the
        measured fix.)

        Section headings are kept deliberately: "What You'll Bring" is a strong
        signal for the required-vs-preferred split in `fit_score`.
        """
        parts: List[str] = []
        for key in ("openingPlain", "descriptionPlain"):
            value = str(job.get(key) or "").strip()
            if value:
                parts.append(value)
        if not parts:
            body = str(job.get("descriptionBodyPlain") or "").strip()
            if body:
                parts.append(body)
        for section in (job.get("lists") or []):
            if not isinstance(section, dict):
                continue
            heading = str(section.get("text") or "").strip()
            content = html_to_text(section.get("content") or "").strip()
            if heading and content:
                parts.append(f"{heading}\n{content}")
            elif content:
                parts.append(content)
        extra = str(job.get("additionalPlain") or "").strip()
        if extra:
            parts.append(extra)
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------

class AshbyProvider(_BoardProvider):
    vendor = "ashby"
    label = "Ashby"

    has_detail = False

    api_hosts = ("api.ashbyhq.com",)
    portal_hosts = ("jobs.ashbyhq.com",)

    fingerprints = (
        fp(r"https?://api\.ashbyhq\.com/posting-api/job-board/"
           r"(?P<tenant>[A-Za-z0-9_\-\.]+)", confidence=95),
        fp(r"https?://jobs\.ashbyhq\.com/(?P<tenant>[A-Za-z0-9_\-\.]+)",
           confidence=90),
    )

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        # GET only. POST to this path returns 401, which reads as "credentials
        # required" and previously got Ashby written off as a private API.
        payload = self._get_json(
            ctx, f"https://api.ashbyhq.com/posting-api/job-board/"
                 f"{endpoint.tenant}")
        for job in self._rows(payload, "jobs"):
            if ctx.should_stop():
                return
            # `isListed: false` is a posting the employer has unpublished but
            # not deleted; absent means listed.
            if job.get("isListed") is False:
                continue
            title = str(job.get("title") or "").strip()
            rid = str(job.get("id") or "").strip()
            if not rid or not title:
                continue
            posted = to_iso(job.get("publishedAt"))
            if self._too_old(ctx, posted):
                continue
            secondary = ", ".join(
                str((s or {}).get("location") or "")
                for s in (job.get("secondaryLocations") or [])
                if isinstance(s, dict))
            location = str(job.get("location") or "")
            if secondary.strip(", "):
                location = f"{location} · {secondary}"
            country = str((((job.get("address") or {}).get("postalAddress")
                            or {}).get("addressCountry")) or "")
            description = str(job.get("descriptionPlain") or "").strip() or \
                html_to_text(job.get("descriptionHtml") or "")
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=str(job.get("jobUrl") or job.get("applyUrl") or ""),
                location=location,
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                description=description[:20000],
                country=country,
                employment_type=str(job.get("employmentType") or ""),
                compensation=_pay_of(job),
                department=str(job.get("department") or job.get("team") or ""),
                remote=job.get("isRemote") if isinstance(
                    job.get("isRemote"), bool) else looks_remote(location),
            )


# ---------------------------------------------------------------------------
# SmartRecruiters
# ---------------------------------------------------------------------------

class SmartRecruitersProvider(_BoardProvider):
    """SmartRecruiters postings API — the one board vendor here that pages."""

    vendor = "smartrecruiters"
    label = "SmartRecruiters"

    has_detail = True

    api_hosts = ("api.smartrecruiters.com",)
    portal_hosts = ("jobs.smartrecruiters.com", "careers.smartrecruiters.com")

    # 100 per page, two pages. 200 newest is plenty for a 30-day window and
    # bounds what a giant multinational costs us.
    policy = _replace(_BOARD_POLICY, page_size=100, max_pages=2)

    fingerprints = (
        fp(r"https?://api\.smartrecruiters\.com/v\d+/companies/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=95),
        fp(r"https?://(?:jobs|careers)\.smartrecruiters\.com/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=90),
    )

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        # SmartRecruiters company identifiers are CASE-SENSITIVE in the API
        # path ("Visa", not "visa"), so the captured slug is stored verbatim.
        # `member_key` slugifies separately, so crawl identity is unaffected.
        tenant = (fields.get("tenant") or "").strip()
        if not tenant:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant)

    def slug_candidates(self, name: str, domain: str = "") -> Sequence[str]:
        """Case matters here, so the guesses carry casing variants.

        The base helper lowercases everything, which would miss every
        SmartRecruiters tenant whose id is PascalCase — and most are.
        """
        base = list(super().slug_candidates(name, domain))
        out: List[str] = []
        for slug in base:
            for variant in (slug, slug.capitalize(),
                            "".join(p.capitalize() for p in slug.split("-"))):
                if variant not in out:
                    out.append(variant)
        return out[:6]

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        page_size = int(self.policy.page_size)
        offset = 0
        for _page in range(int(self.policy.max_pages)):
            if ctx.should_stop():
                return
            payload = self._get_json(
                ctx, f"https://api.smartrecruiters.com/v1/companies/"
                     f"{endpoint.tenant}/postings",
                # country=us is applied SERVER-side: giant multinationals
                # (AccorHotels: 5,800+ postings) would otherwise bury their US
                # slice beyond the two pages we are willing to read.
                {"limit": page_size, "offset": offset, "country": "us"})
            content = self._rows(payload, "content")
            if not content:
                return
            for job in content:
                title = str(job.get("name") or "").strip()
                rid = str(job.get("id") or "").strip()
                if not rid or not title:
                    continue
                posted = to_iso(job.get("releasedDate"))
                if self._too_old(ctx, posted):
                    continue
                loc = job.get("location") or {}
                # SmartRecruiters ships function / department / experience /
                # industry labels inline and NO description, so these labels are
                # the only scoring signal a row carries before a detail fetch:
                # an on-topic but oddly-titled posting ("Clinical Partner",
                # function "Nursing") is recalled on them alone.
                exp = str((job.get("experienceLevel") or {}).get("label") or "")
                yield RawJob(
                    source_job_id=rid,
                    title=title,
                    apply_url=(f"https://jobs.smartrecruiters.com/"
                               f"{endpoint.tenant}/{rid}"),
                    location=_join(loc.get("city"), loc.get("region")),
                    posted_at=posted,
                    date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                    country=str(loc.get("country") or ""),
                    compensation=_pay_of(job),
                    department=_join(
                        (job.get("function") or {}).get("label"),
                        (job.get("department") or {}).get("label"),
                        (job.get("industry") or {}).get("label"), sep=" · "),
                    remote=True if loc.get("remote") else looks_remote(
                        loc.get("city"), loc.get("region")),
                    detail_url=(f"https://api.smartrecruiters.com/v1/companies/"
                                f"{endpoint.tenant}/postings/{rid}"),
                    extra={"experience_level": exp} if exp else {},
                )
            offset += len(content)
            if len(content) < page_size:
                return

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        url = job.detail_url or (
            f"https://api.smartrecruiters.com/v1/companies/"
            f"{endpoint.tenant}/postings/{job.source_job_id}")
        payload = self._get_json(ctx, url, timeout=self.policy.detail_timeout)
        if not isinstance(payload, dict):
            return None
        sections = ((payload.get("jobAd") or {}).get("sections") or {})
        body = " ".join(
            html_to_text((sections.get(k) or {}).get("text") or "")
            for k in ("jobDescription", "qualifications",
                      "additionalInformation")).strip()
        if not body:
            return None
        return RawJob(source_job_id=job.source_job_id, title=job.title,
                      apply_url=job.apply_url, description=body[:40000],
                      compensation=_pay_of(payload))


# ---------------------------------------------------------------------------
# Workable
# ---------------------------------------------------------------------------

class WorkableProvider(_BoardProvider):
    vendor = "workable"
    label = "Workable"

    has_detail = True

    # Workable serves the widget API and the public apply pages from the same
    # host, which is why it appears in both crawler allowlists unchanged.
    api_hosts = ("apply.workable.com",)
    portal_hosts = ("apply.workable.com",)

    fingerprints = (
        fp(r"https?://apply\.workable\.com/api/v\d+/(?:widget/)?accounts/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=95),
        fp(r"https?://apply\.workable\.com/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=90),
        # Legacy per-tenant host, still linked from older careers pages.
        fp(r"https?://(?P<tenant>[A-Za-z0-9_\-]+)\.workable\.com",
           confidence=70),
    )

    # `apply.workable.com/j/<shortcode>` is a single posting, `/api/...` is the
    # API itself; neither names an employer.
    _RESERVED = frozenset({"api", "j", "www", "apply", "accounts", "assets",
                           "static", "backend"})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not tenant or tenant.lower() in self._RESERVED:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant)

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        # The v1 widget endpoint, exactly as `_fetch_workable` calls it. The
        # crawler's `_list_career` appends `?details=true`, which inlines the
        # body and would let this vendor skip `fetch_detail` — a real
        # divergence between the two live paths, left as-is here because it is
        # a behaviour change, not a port, and it belongs to whoever retires the
        # old fetchers.
        payload = self._get_json(
            ctx, f"https://apply.workable.com/api/v1/widget/accounts/"
                 f"{endpoint.tenant}")
        for job in self._rows(payload, "jobs"):
            if ctx.should_stop():
                return
            title = str(job.get("title") or "").strip()
            rid = str(job.get("shortcode") or job.get("code") or "").strip()
            if not rid or not title:
                continue
            posted = to_iso(job.get("published_on"))
            if self._too_old(ctx, posted):
                continue
            # Function / department / experience / industry, inline and free.
            # Same scoring role as the SmartRecruiters labels above.
            exp = str(job.get("experience") or "")
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=str(job.get("url") or ""),
                location=_join(job.get("city"), job.get("state")),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                raw_posted_text=str(job.get("published_on") or ""),
                country=str(job.get("country") or ""),
                employment_type=str(job.get("employment_type") or ""),
                compensation=_pay_of(job),
                department=_join(job.get("function"), job.get("department"),
                                 job.get("industry"), sep=" · "),
                remote=True if job.get("telecommuting") else looks_remote(
                    job.get("city"), job.get("state")),
                detail_url=(f"https://apply.workable.com/api/v2/accounts/"
                            f"{endpoint.tenant}/jobs/{rid}"),
                extra={"experience_level": exp} if exp else {},
            )

    def fetch_detail(self, endpoint: Endpoint, job: RawJob,
                     ctx: FetchContext) -> Optional[RawJob]:
        url = job.detail_url or (
            f"https://apply.workable.com/api/v2/accounts/"
            f"{endpoint.tenant}/jobs/{job.source_job_id}")
        payload = self._get_json(ctx, url, timeout=self.policy.detail_timeout)
        if not isinstance(payload, dict):
            return None
        # Requirements live in their own field; a body without them scores
        # badly because the required-vs-preferred split has nothing to read.
        body = html_to_text(
            f"{payload.get('description') or ''} "
            f"{payload.get('requirements') or ''}")
        if not body.strip():
            return None
        return RawJob(source_job_id=job.source_job_id, title=job.title,
                      apply_url=job.apply_url, description=body[:40000],
                      compensation=_pay_of(payload))


# ---------------------------------------------------------------------------
# Recruitee
# ---------------------------------------------------------------------------

class RecruiteeProvider(_BoardProvider):
    """Recruitee. The only board vendor here with a per-tenant hostname, so its
    pacing keys per host — correct, because each tenant is a separate origin."""

    vendor = "recruitee"
    label = "Recruitee"

    has_detail = False

    api_hosts = (".recruitee.com",)
    portal_hosts = (".recruitee.com",)

    fingerprints = (
        fp(r"https?://(?P<tenant>[A-Za-z0-9_\-]+)\.recruitee\.com/api/offers",
           confidence=95),
        fp(r"https?://(?P<tenant>[A-Za-z0-9_\-]+)\.recruitee\.com",
           confidence=90),
    )

    _RESERVED = frozenset({"www", "api", "app", "cdn", "static", "assets",
                           "help", "blog", "status", "support"})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip().lower()
        if not tenant or tenant in self._RESERVED:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant,
                        host=f"{tenant}.recruitee.com")

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        payload = self._get_json(
            ctx, f"https://{endpoint.tenant}.recruitee.com/api/offers/")
        for job in self._rows(payload, "offers"):
            if ctx.should_stop():
                return
            title = str(job.get("title") or "").strip()
            rid = str(job.get("id") or "").strip()
            if not rid or not title:
                continue
            # `created_at`, per `_fetch_recruitee`. (`_list_career` prefers
            # `published_at` and falls back to this; both are publication
            # timestamps rather than edit timestamps, so neither carries the
            # Greenhouse `updated_at` hazard — but the two live paths do
            # disagree, and this module follows the named fetcher.)
            posted = to_iso(job.get("created_at"))
            if self._too_old(ctx, posted):
                continue
            remote_raw = str(job.get("remote") or "").strip().lower()
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=str(job.get("careers_url")
                              or job.get("careers_apply_url") or ""),
                location=str(job.get("location") or ""),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                description=html_to_text(job.get("description") or "")[:20000],
                country=str(job.get("country_code") or ""),
                employment_type=str(job.get("employment_type_code")
                                    or job.get("employment_type") or ""),
                compensation=_pay_of(job),
                department=str(job.get("department") or ""),
                remote=True if remote_raw == "true" else looks_remote(
                    job.get("location")),
            )


# ---------------------------------------------------------------------------
# NeoGov / GovernmentJobs
# ---------------------------------------------------------------------------

def _neogov_iso(raw: Any) -> str:
    """NeoGov posts dates as MM/DD/YY.

    Its own format, so `util.to_iso` cannot read it (that helper requires a
    four-digit year, and guessing a century from two digits is exactly the kind
    of silent inference it refuses to make). Unparseable -> "" so the date is
    UNKNOWN rather than invented (ADR-001). Measured across 781 live postings
    from two agencies: zero unparseable, so this is a guard, not a common path.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%m/%d/%y").replace(
            tzinfo=timezone.utc).isoformat()
    except ValueError:
        return ""


class NeoGovProvider(_BoardProvider):
    """Government employers (states, counties, cities) on NeoGov/GovernmentJobs.

    POST, not GET, and it needs the XHR header — this is the agency page's own
    job-list call. Every GET variant 404s, which is why this vendor was
    initially written off as unfetchable; `scripts/probe_new_ats.py` carries the
    discovery record. A 404 here reads as "no such agency" when it actually
    means "you asked wrong", so the failure is quiet and expensive.

    One request returns the agency's whole board (706 for State of Washington,
    75 for City of Seattle) — the same whole-board shape as Greenhouse, with no
    paging to walk and no per-job detail endpoint.
    """

    vendor = "neogov"
    label = "GovernmentJobs"

    has_detail = False

    # Opted out of the slug-probe fallback. It would technically work (one
    # shared host), but the fallback runs for every company whose careers page
    # is a JS shell, and virtually none of those are municipal governments — so
    # it would be a wasted request on essentially every probe. Agencies reach
    # the catalog by fingerprint or by explicit seeding.
    slug_probe = False

    api_hosts = ("www.governmentjobs.com",)
    portal_hosts = ("www.governmentjobs.com",)

    # Agency boards are the slowest responders in this set (a 706-posting
    # payload with full descriptions inline), so they get the crawler's
    # HEAVY_REQUEST_TIMEOUT rather than the board default.
    policy = _replace(_BOARD_POLICY, timeout=15.0)

    fingerprints = (
        fp(r"https?://(?:www\.)?governmentjobs\.com/careers/"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=95),
        fp(r"https?://agency\.governmentjobs\.com/(?P<tenant>[A-Za-z0-9_\-]+)",
           confidence=85),
    )

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        if ctx.post_json is None:
            ctx.log("warning", "neogov: context has no post_json; %s skipped",
                    endpoint.tenant)
            return
        slug = endpoint.tenant
        status, payload = ctx.post_json(
            f"https://www.governmentjobs.com/careers/{slug}/home/loadJobsOnMaps",
            # An EMPTY FORM body. The endpoint is the page's own XHR and
            # answers to `application/x-www-form-urlencoded`; the header below
            # is what it actually gates on — without it the response is a 404.
            form_body={},
            extra_headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=self.policy.timeout,
            vendor=self.vendor)
        if self.policy.is_challenge(status):
            raise ChallengeError(self.vendor, status)
        if status != 200:
            return
        for job in self._rows(payload, "jobList"):
            if ctx.should_stop():
                return
            job_id = job.get("ID")
            if not job_id:
                continue
            # `JobTitle` is a URL SLUG ("utility-rate-analyst-senior-economist")
            # despite its name; the human title is `Classification`. Using
            # JobTitle puts hyphenated slugs on every card and wrecks
            # title-based role classification downstream.
            title = str(job.get("Classification") or "").strip()
            if not title:
                continue
            posted = _neogov_iso(job.get("PostingDate"))
            if self._too_old(ctx, posted):
                continue
            remote = str(job.get("RemoteWorkOptionText") or "").strip().lower()
            yield RawJob(
                source_job_id=str(job_id),
                title=title,
                # No URL in the payload; the public permalink is built from the
                # ID and the agency slug.
                apply_url=(f"https://www.governmentjobs.com/careers/"
                           f"{slug}/jobs/{job_id}"),
                location=str(job.get("Location") or "").strip(),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                raw_posted_text=str(job.get("PostingDate") or ""),
                description=str(job.get("FullDescription") or "")[:20000],
                # Every agency on this platform is a US public employer.
                country="us",
                employment_type=str(job.get("JobType") or ""),
                compensation=job.get("SalaryInfo"),
                # Department is real signal on an agency board, where titles
                # are terse and civil-service coded.
                department=str(job.get("DepartmentName") or ""),
                remote=bool(remote and remote not in ("no", "none", "onsite")),
            )


# ---------------------------------------------------------------------------
# Jobvite
# ---------------------------------------------------------------------------

class JobviteProvider(_BoardProvider):
    """Jobvite public board JSON.

    Two URL shapes are in the wild. `jobs.jobvite.com/{slug}/jobs.json` is the
    one that actually returns a job array (verified pattern in
    `scripts/probe_new_ats.py`); the `/api/v1/company/{slug}/jobs` sibling is
    tried second because a minority of tenants 404 the first and 200 the
    second. Either way this is a single-host board API, so slug-probe is safe.
    """

    vendor = "jobvite"
    label = "Jobvite"
    has_detail = False
    api_hosts = ("jobs.jobvite.com",)
    portal_hosts = ("jobs.jobvite.com",)
    fingerprints = (
        fp(r"https?://jobs\.jobvite\.com/(?:api/v\d+/company/)?"
           r"(?P<tenant>[A-Za-z0-9_\-]+)", confidence=90),
        fp(r"https?://jobs\.jobvite\.com/(?P<tenant>[A-Za-z0-9_\-]+)/",
           confidence=85),
    )
    _RESERVED = frozenset({"api", "careers", "jobs", "search", "company"})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not tenant or tenant.lower() in self._RESERVED:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant)

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        slug = endpoint.tenant
        payload = self._get_json(
            ctx, f"https://jobs.jobvite.com/{slug}/jobs.json")
        if payload is None:
            payload = self._get_json(
                ctx, f"https://jobs.jobvite.com/api/v1/company/{slug}/jobs")
        for job in _jobvite_rows(payload):
            if ctx.should_stop():
                return
            title = str(job.get("title") or job.get("jobTitle")
                        or job.get("text") or "").strip()
            rid = str(job.get("id") or job.get("jobId")
                      or job.get("requisitionId") or "").strip()
            if not rid or not title:
                continue
            posted = to_iso(job.get("postedDate") or job.get("datePosted")
                            or job.get("createdDate") or job.get("updatedDate"))
            if self._too_old(ctx, posted):
                continue
            apply_url = str(job.get("applyUrl") or job.get("url")
                            or job.get("hostedUrl") or job.get("jobUrl")
                            or f"https://jobs.jobvite.com/{slug}/job/{rid}")
            loc = job.get("location")
            if isinstance(loc, dict):
                loc = _join(loc.get("city"), loc.get("state"), loc.get("country"))
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=apply_url,
                location=str(loc or "").strip(),
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                description=html_to_text(
                    job.get("jobDescription") or job.get("description")
                    or job.get("descriptionHtml") or "")[:20000],
                employment_type=str(job.get("jobType") or job.get("type") or ""),
                compensation=_pay_of(job),
                remote=looks_remote(title, loc, job.get("workplaceType")),
            )


def _jobvite_rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("positions", "jobs", "requisitions", "data"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        if isinstance(rows, dict):
            inner = rows.get("jobs") or rows.get("items") or []
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
    return []


# ---------------------------------------------------------------------------
# BambooHR
# ---------------------------------------------------------------------------

class BambooHrProvider(_BoardProvider):
    """BambooHR careers list JSON.

    `GET https://{slug}.bamboohr.com/careers/list` returns
    `{result: [{id, jobOpeningName, locationLabel, datePosted, ...}]}`.
    Each tenant is its own hostname, like Recruitee; per-host pacing is
    correct (unlike iCIMS there is no vendor-wide wall).
    """

    vendor = "bamboohr"
    label = "BambooHR"
    has_detail = False
    api_hosts = (".bamboohr.com",)
    portal_hosts = (".bamboohr.com",)
    fingerprints = (
        fp(r"https?://(?P<tenant>[A-Za-z0-9][A-Za-z0-9\-]*?)\.bamboohr\.com"
           r"/(?:careers|jobs)", confidence=92),
        fp(r"https?://(?P<tenant>[A-Za-z0-9][A-Za-z0-9\-]*?)\.bamboohr\.com",
           confidence=70),
    )
    _RESERVED = frozenset({"www", "app", "api", "static", "cdn"})

    def endpoint_from_match(self, fields: Dict[str, str],
                            url: str) -> Optional[Endpoint]:
        tenant = (fields.get("tenant") or "").strip()
        if not tenant or tenant.lower() in self._RESERVED:
            return None
        return Endpoint(vendor=self.vendor, tenant=tenant,
                        host=f"{tenant.lower()}.bamboohr.com")

    def list_jobs(self, endpoint: Endpoint,
                  ctx: FetchContext) -> Iterator[RawJob]:
        slug = endpoint.tenant
        payload = self._get_json(
            ctx, f"https://{slug}.bamboohr.com/careers/list")
        for job in self._rows(payload, "result"):
            if ctx.should_stop():
                return
            title = str(job.get("jobOpeningName") or job.get("jobOpening")
                        or job.get("title") or "").strip()
            rid = str(job.get("id") or "").strip()
            if not rid or not title:
                continue
            posted = to_iso(job.get("datePosted") or job.get("postedDate"))
            if self._too_old(ctx, posted):
                continue
            loc = str(job.get("locationLabel") or job.get("location") or "")
            yield RawJob(
                source_job_id=rid,
                title=title,
                apply_url=f"https://{slug}.bamboohr.com/careers/{rid}",
                location=loc,
                posted_at=posted,
                date_confidence=DATE_EXACT if posted else DATE_UNKNOWN,
                department=str(job.get("departmentLabel") or ""),
                employment_type=str(job.get("employmentStatusLabel") or ""),
                compensation=_pay_of(job),
                remote=looks_remote(title, loc),
            )


register(GreenhouseProvider())
register(LeverProvider())
register(AshbyProvider())
register(SmartRecruitersProvider())
register(WorkableProvider())
register(RecruiteeProvider())
register(NeoGovProvider())
register(JobviteProvider())
register(BambooHrProvider())
